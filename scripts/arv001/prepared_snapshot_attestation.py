"""Strict read-only attestation for an existing ARV-001 prepared publication.

This module validates a previously published prepared-state directory before any
carry-forward copy is allowed. It never mutates the source tree and never calls
an LLM/provider endpoint.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.arv001.prepared_verification import (
    PreparedVerificationError,
    PrivatePreparedVerificationDescriptor,
    parse_private_descriptor,
)

_SCHEMA_VERSION = "arv001-prepared-state-v1"
_EXPECTED_TOP_LEVEL = frozenset(
    {
        "prepared.sqlite3",
        "application-data",
        "runtime-profile.json",
        "prepared-verification.json",
        "prepared-state-manifest.json",
        "sanitized-acceptance-result.json",
    }
)


class PreparedSnapshotAttestationError(RuntimeError):
    """Stable fail-closed error for source prepared-publication attestation."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PreparedSnapshotAttestation:
    descriptor: PrivatePreparedVerificationDescriptor
    database_sha256: str
    manifest_sha256: str
    descriptor_sha256: str
    runtime_profile_sha256: str
    application_data_tree_sha256: str
    sanitized_result_sha256: str


def _fail(condition: bool, code: str) -> None:
    if not condition:
        raise PreparedSnapshotAttestationError(code)


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise PreparedSnapshotAttestationError("prepared_snapshot_path_unreadable") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise PreparedSnapshotAttestationError("prepared_snapshot_file_unreadable") from exc
    return digest.hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_children(root: Path) -> list[Path]:
    try:
        return sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise PreparedSnapshotAttestationError("prepared_snapshot_path_unreadable") from exc


def _assert_safe_regular_file(path: Path) -> None:
    info = _lstat(path)
    _fail(not stat.S_ISLNK(info.st_mode), "prepared_snapshot_symlink_detected")
    _fail(stat.S_ISREG(info.st_mode), "prepared_snapshot_file_invalid")
    _fail(stat.S_IMODE(info.st_mode) == 0o600, "prepared_snapshot_mode_invalid")


def _assert_safe_directory(path: Path) -> None:
    info = _lstat(path)
    _fail(not stat.S_ISLNK(info.st_mode), "prepared_snapshot_symlink_detected")
    _fail(stat.S_ISDIR(info.st_mode), "prepared_snapshot_directory_invalid")
    _fail(stat.S_IMODE(info.st_mode) == 0o700, "prepared_snapshot_mode_invalid")


def _tree_hash(root: Path) -> str:
    _assert_safe_directory(root)
    records: list[dict[str, object]] = []
    stack = [root]
    while stack:
        current = stack.pop()
        for child in _safe_children(current):
            info = _lstat(child)
            _fail(not stat.S_ISLNK(info.st_mode), "prepared_snapshot_symlink_detected")
            relative = child.relative_to(root).as_posix()
            if stat.S_ISDIR(info.st_mode):
                _fail(stat.S_IMODE(info.st_mode) == 0o700, "prepared_snapshot_mode_invalid")
                records.append({"path": relative, "type": "directory"})
                stack.append(child)
            elif stat.S_ISREG(info.st_mode):
                _fail(stat.S_IMODE(info.st_mode) == 0o600, "prepared_snapshot_mode_invalid")
                records.append(
                    {
                        "path": relative,
                        "type": "file",
                        "sha256": _sha256_file(child),
                        "size": info.st_size,
                    }
                )
            else:
                raise PreparedSnapshotAttestationError(
                    "prepared_snapshot_unsafe_filesystem_object"
                )
    records.sort(key=lambda item: (str(item["path"]).count("/"), str(item["path"])))
    return hashlib.sha256(_canonical_json_bytes(records)).hexdigest()


def _load_json(path: Path, code: str) -> dict[str, Any]:
    _assert_safe_regular_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedSnapshotAttestationError(code) from exc
    _fail(isinstance(value, dict), code)
    return value


def verify_published_prepared_snapshot(
    root: Path,
    *,
    expected_head: str,
    expected_corpus_sha: str,
) -> PreparedSnapshotAttestation:
    """Verify an immutable prepared-state publication before carry-forward.

    The source publication is accepted only when its exact file set, filesystem
    safety, manifest identity, all manifest-recorded hashes, descriptor and
    zero-generation invariants are independently re-derived from source bytes.
    """

    _assert_safe_directory(root)
    actual_names = {item.name for item in _safe_children(root)}
    _fail(actual_names == _EXPECTED_TOP_LEVEL, "prepared_snapshot_file_set_invalid")

    for name in _EXPECTED_TOP_LEVEL - {"application-data"}:
        _assert_safe_regular_file(root / name)
    _assert_safe_directory(root / "application-data")

    _fail(
        not any(
            item.is_file() and (item.name.endswith("-wal") or item.name.endswith("-shm"))
            for item in _safe_children(root)
        ),
        "prepared_snapshot_sqlite_sidecar_present",
    )

    manifest_path = root / "prepared-state-manifest.json"
    descriptor_path = root / "prepared-verification.json"
    runtime_profile_path = root / "runtime-profile.json"
    result_path = root / "sanitized-acceptance-result.json"
    database_path = root / "prepared.sqlite3"
    application_data = root / "application-data"

    manifest = _load_json(manifest_path, "prepared_snapshot_manifest_invalid")
    _load_json(runtime_profile_path, "prepared_snapshot_runtime_profile_invalid")
    result = _load_json(result_path, "prepared_snapshot_result_invalid")

    _fail(manifest.get("schema_version") == _SCHEMA_VERSION, "prepared_snapshot_manifest_version_invalid")
    _fail(manifest.get("head_sha") == expected_head, "prepared_snapshot_head_mismatch")
    _fail(
        manifest.get("corpus_sha256") == expected_corpus_sha,
        "prepared_snapshot_corpus_mismatch",
    )
    _fail(
        set(manifest.get("exact_file_set") or ()) == _EXPECTED_TOP_LEVEL,
        "prepared_snapshot_manifest_file_set_invalid",
    )

    expected_counts = {
        "physical_document_count": 10,
        "logical_document_count": 6,
        "extracted_document_count": 10,
        "chunk_count": 233,
        "controlled_preflight_invocations": 1,
        "controlled_provider_invocations": 0,
        "provider_generation_calls": 0,
    }
    for key, expected in expected_counts.items():
        _fail(manifest.get(key) == expected, "prepared_snapshot_manifest_invariant_invalid")
    for key in ("snapshot_binding_verified", "source_graph_binding_verified", "gate5_ready", "controlled_preflight_verified"):
        _fail(manifest.get(key) is True, "prepared_snapshot_manifest_invariant_invalid")

    database_sha = _sha256_file(database_path)
    descriptor_sha = _sha256_file(descriptor_path)
    runtime_profile_sha = _sha256_file(runtime_profile_path)
    result_sha = _sha256_file(result_path)
    application_tree_sha = _tree_hash(application_data)

    expected_hashes = {
        "database_sha256": database_sha,
        "prepared_verification_sha256": descriptor_sha,
        "runtime_profile_sha256": runtime_profile_sha,
        "sanitized_result_sha256": result_sha,
        "application_data_tree_sha256": application_tree_sha,
    }
    for key, actual in expected_hashes.items():
        _fail(manifest.get(key) == actual, "prepared_snapshot_manifest_hash_mismatch")

    try:
        descriptor = parse_private_descriptor(
            descriptor_path,
            expected_head=expected_head,
            expected_corpus_sha=expected_corpus_sha,
        )
    except PreparedVerificationError as exc:
        raise PreparedSnapshotAttestationError(exc.code) from exc

    _fail(descriptor.physical_document_count == 10, "prepared_snapshot_descriptor_invariant_invalid")
    _fail(descriptor.logical_document_count == 6, "prepared_snapshot_descriptor_invariant_invalid")
    _fail(descriptor.extracted_document_count == 10, "prepared_snapshot_descriptor_invariant_invalid")
    _fail(descriptor.chunk_count == 233, "prepared_snapshot_descriptor_invariant_invalid")
    _fail(descriptor.controlled_preflight_invocations == 1, "prepared_snapshot_descriptor_invariant_invalid")
    _fail(descriptor.controlled_provider_invocations == 0, "prepared_snapshot_descriptor_invariant_invalid")
    _fail(descriptor.provider_generation_calls == 0, "prepared_snapshot_descriptor_invariant_invalid")
    _fail(descriptor.gate5_ready is True, "prepared_snapshot_descriptor_invariant_invalid")
    _fail(descriptor.controlled_preflight_verified is True, "prepared_snapshot_descriptor_invariant_invalid")
    _fail(descriptor.provider_results_absent is True, "prepared_snapshot_descriptor_invariant_invalid")
    _fail(descriptor.generation_artifacts_absent is True, "prepared_snapshot_descriptor_invariant_invalid")

    # Result is not trusted as an authority, but it must remain a zero-generation
    # publication if those counters are present in the sanitized payload.
    counters = result.get("counters")
    if isinstance(counters, dict):
        _fail(counters.get("provider_generation_calls", 0) == 0, "prepared_snapshot_result_invariant_invalid")
        _fail(counters.get("controlled_provider_invocations", 0) == 0, "prepared_snapshot_result_invariant_invalid")

    return PreparedSnapshotAttestation(
        descriptor=descriptor,
        database_sha256=database_sha,
        manifest_sha256=_sha256_file(manifest_path),
        descriptor_sha256=descriptor_sha,
        runtime_profile_sha256=runtime_profile_sha,
        application_data_tree_sha256=application_tree_sha,
        sanitized_result_sha256=result_sha,
    )
