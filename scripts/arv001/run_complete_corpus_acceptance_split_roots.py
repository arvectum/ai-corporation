#!/usr/bin/env python3
"""Run ARV-001 acceptance when summaries and intake metadata live separately."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from scripts.arv001 import application_workflow
from scripts.arv001 import run_complete_corpus_acceptance as runner
from scripts.arv001.complete_corpus_contract import (
    DEFAULT_CORPUS_SHA256,
    AcceptanceBlocked,
)
from scripts.arv001.corpus_hash_resolver import BoundCorpusHashResolver

_CANDIDATE_ARTIFACTS = (
    "physical-files.json",
    "logical-documents.json",
    "document-set-summary.json",
    "deterministic-parse-summary.json",
    "intake-summary.json",
)
_METADATA_ARTIFACT = "metadata.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _argument_value(argv: Sequence[str], flag: str) -> str | None:
    matches = [index for index, value in enumerate(argv) if value == flag]
    if len(matches) > 1:
        raise AcceptanceBlocked(f"duplicate_argument:{flag.lstrip('-')}")
    if not matches:
        return None
    index = matches[0]
    if index + 1 >= len(argv) or argv[index + 1].startswith("--"):
        raise AcceptanceBlocked(f"argument_value_missing:{flag.lstrip('-')}")
    return argv[index + 1]


def _argument_path(
    argv: Sequence[str], flag: str, *, default: Path | None = None
) -> Path:
    value = _argument_value(argv, flag)
    if value is None:
        if default is None:
            raise AcceptanceBlocked(f"required_argument_missing:{flag.lstrip('-')}")
        return default
    return Path(value).expanduser().resolve()


def _replace_argument(argv: Sequence[str], flag: str, value: Path) -> list[str]:
    result = list(argv)
    matches = [index for index, item in enumerate(result) if item == flag]
    if len(matches) != 1:
        raise AcceptanceBlocked(f"required_argument_missing:{flag.lstrip('-')}")
    result[matches[0] + 1] = str(value)
    return result


def _regular_source(path: Path, code: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise AcceptanceBlocked(code)


def _copy_verified(source: Path, destination: Path, code: str) -> tuple[int, str]:
    _regular_source(source, code)
    before_size = source.stat().st_size
    before_hash = _sha256(source)
    shutil.copyfile(source, destination)
    if destination.is_symlink() or not destination.is_file():
        raise AcceptanceBlocked("ephemeral_artifact_copy_invalid")
    if destination.stat().st_size != before_size or _sha256(destination) != before_hash:
        raise AcceptanceBlocked("ephemeral_artifact_copy_mismatch")
    if source.stat().st_size != before_size or _sha256(source) != before_hash:
        raise AcceptanceBlocked("source_artifact_changed_during_view_build")
    return before_size, before_hash


def build_ephemeral_candidate_view(
    *, candidate_root: Path, intake_root: Path, view_root: Path
) -> dict[str, object]:
    """Build a temporary byte-identical view without mutating either source root."""

    candidate_root = candidate_root.expanduser().resolve()
    intake_root = intake_root.expanduser().resolve()
    view_root = view_root.expanduser().resolve()
    if not candidate_root.is_dir() or not intake_root.is_dir():
        raise AcceptanceBlocked("candidate_or_intake_root_missing")
    if view_root.exists():
        raise AcceptanceBlocked("ephemeral_candidate_view_already_exists")
    view_root.mkdir(mode=0o750)

    copied: dict[str, dict[str, object]] = {}
    for name in _CANDIDATE_ARTIFACTS:
        source = candidate_root / name
        size, digest = _copy_verified(
            source,
            view_root / name,
            f"required_candidate_artifact_missing_or_unsafe:{name}",
        )
        copied[name] = {"size_bytes": size, "sha256": digest}

    metadata_source = intake_root / _METADATA_ARTIFACT
    _regular_source(
        metadata_source,
        "required_intake_artifact_missing_or_unsafe:metadata.json",
    )
    candidate_metadata = candidate_root / _METADATA_ARTIFACT
    if candidate_metadata.exists() or candidate_metadata.is_symlink():
        _regular_source(candidate_metadata, "candidate_metadata_artifact_unsafe")
        if (
            candidate_metadata.stat().st_size != metadata_source.stat().st_size
            or _sha256(candidate_metadata) != _sha256(metadata_source)
        ):
            raise AcceptanceBlocked("metadata_artifact_conflict")

    size, digest = _copy_verified(
        metadata_source,
        view_root / _METADATA_ARTIFACT,
        "required_intake_artifact_missing_or_unsafe:metadata.json",
    )
    copied[_METADATA_ARTIFACT] = {"size_bytes": size, "sha256": digest}

    return {
        "artifact_count": len(copied),
        "source_mutations": 0,
        "ephemeral_view": True,
        "artifacts": copied,
    }


def _expected_corpus_sha(argv: Sequence[str], view_root: Path) -> str:
    expected = _argument_value(argv, "--expected-corpus-sha") or DEFAULT_CORPUS_SHA256
    try:
        summary = json.loads(
            (view_root / "intake-summary.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceBlocked("intake_summary_invalid") from exc
    recorded = summary.get("corpus_sha256") if isinstance(summary, dict) else None
    if recorded != expected:
        raise AcceptanceBlocked("intake_summary_corpus_sha_mismatch")
    return expected


def _delegate_with_bound_hash(
    delegated_argv: list[str], expected_sha: str
) -> tuple[int, BoundCorpusHashResolver]:
    resolver = BoundCorpusHashResolver(expected_sha)
    previous_argv = sys.argv
    previous_runner_hash = runner._corpus_hash
    previous_workflow_hash = application_workflow.corpus_hash
    sys.argv = delegated_argv
    runner._corpus_hash = resolver
    application_workflow.corpus_hash = resolver
    try:
        return runner.main(), resolver
    finally:
        application_workflow.corpus_hash = previous_workflow_hash
        runner._corpus_hash = previous_runner_hash
        sys.argv = previous_argv


def main(argv: Sequence[str] | None = None) -> int:
    original_argv = list(sys.argv if argv is None else argv)
    try:
        candidate_root = _argument_path(original_argv, "--candidate-root")
        intake_root = _argument_path(
            original_argv, "--intake-root", default=candidate_root
        )
        with tempfile.TemporaryDirectory(prefix="arv001-candidate-view-") as directory:
            view_root = Path(directory) / "candidate"
            build_ephemeral_candidate_view(
                candidate_root=candidate_root,
                intake_root=intake_root,
                view_root=view_root,
            )
            expected_sha = _expected_corpus_sha(original_argv, view_root)
            delegated_argv = _replace_argument(
                original_argv, "--candidate-root", view_root
            )
            result, resolver = _delegate_with_bound_hash(delegated_argv, expected_sha)
            if result == 0 and resolver.profile is not None:
                profile = resolver.profile.sanitized()
                print(
                    "corpus_hash_profile="
                    + json.dumps(profile, ensure_ascii=True, sort_keys=True),
                    file=sys.stderr,
                )
            return result
    except AcceptanceBlocked as exc:
        value = str(exc)
        safe = (
            value
            if value.isascii() and len(value) <= 300
            else "arv001_split_root_acceptance_blocked"
        )
        print(safe, file=sys.stderr)
        return 2
    except Exception:
        print("arv001_split_root_acceptance_failed", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
