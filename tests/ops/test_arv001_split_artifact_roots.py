from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from scripts.arv001 import run_complete_corpus_acceptance_split_roots as adapter
from scripts.arv001.complete_corpus_contract import AcceptanceBlocked


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_artifacts(candidate: Path, intake: Path) -> dict[Path, tuple[int, str]]:
    candidate.mkdir()
    intake.mkdir()
    payloads = {
        "physical-files.json": [{"original_name": "A.xml", "sha256": "a" * 64}],
        "logical-documents.json": [{"name": "Извещение о закупке"}],
        "document-set-summary.json": {
            "status": "complete",
            "analysis_allowed": True,
        },
        "deterministic-parse-summary.json": {"registry_number": "1"},
        "intake-summary.json": {"corpus_sha256": "b" * 64},
    }
    for name, value in payloads.items():
        (candidate / name).write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8"
        )
    (intake / "metadata.json").write_text(
        json.dumps(
            {"files": [{"original_name": "A.xml", "stored_name": "stored.xml"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        path: (path.stat().st_size, _sha(path))
        for path in [*candidate.iterdir(), intake / "metadata.json"]
    }


def test_builds_byte_identical_ephemeral_view_without_source_mutation(
    tmp_path: Path,
):
    candidate = tmp_path / "candidate"
    intake = tmp_path / "intake"
    before = _write_artifacts(candidate, intake)
    view = tmp_path / "view"

    summary = adapter.build_ephemeral_candidate_view(
        candidate_root=candidate,
        intake_root=intake,
        view_root=view,
    )

    assert summary["artifact_count"] == 6
    assert summary["source_mutations"] == 0
    assert summary["ephemeral_view"] is True
    assert sorted(path.name for path in view.iterdir()) == sorted(
        [*adapter._CANDIDATE_ARTIFACTS, adapter._METADATA_ARTIFACT]
    )
    for source, (size, digest) in before.items():
        assert source.stat().st_size == size
        assert _sha(source) == digest
        assert (view / source.name).read_bytes() == source.read_bytes()


def test_rejects_conflicting_metadata_in_candidate_and_intake_roots(tmp_path: Path):
    candidate = tmp_path / "candidate"
    intake = tmp_path / "intake"
    _write_artifacts(candidate, intake)
    (candidate / "metadata.json").write_text(
        json.dumps({"files": []}), encoding="utf-8"
    )

    with pytest.raises(AcceptanceBlocked, match="metadata_artifact_conflict"):
        adapter.build_ephemeral_candidate_view(
            candidate_root=candidate,
            intake_root=intake,
            view_root=tmp_path / "view",
        )


def test_entrypoint_delegates_with_temporary_complete_candidate_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    candidate = tmp_path / "candidate"
    intake = tmp_path / "intake"
    _write_artifacts(candidate, intake)
    delegated_root: Path | None = None

    def fake_main() -> int:
        nonlocal delegated_root
        args = list(sys.argv)
        index = args.index("--candidate-root")
        delegated_root = Path(args[index + 1])
        assert delegated_root != candidate
        assert delegated_root.joinpath("metadata.json").is_file()
        assert delegated_root.joinpath("physical-files.json").is_file()
        return 0

    monkeypatch.setattr(adapter.runner, "main", fake_main)
    result = adapter.main(
        [
            "adapter",
            "--candidate-root",
            str(candidate),
            "--intake-root",
            str(intake),
            "--expected-corpus-sha",
            "b" * 64,
        ]
    )

    assert result == 0
    assert delegated_root is not None
    assert not delegated_root.exists()
