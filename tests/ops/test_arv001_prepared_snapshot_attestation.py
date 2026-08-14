from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.arv001 import prepared_snapshot_attestation as attestation

HEAD = "5" * 40
CORPUS = "6" * 64


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: bytes) -> None:
    path.write_bytes(value)
    os.chmod(path, 0o600)


def _valid_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "prepared-state"
    root.mkdir(mode=0o700)
    app = root / "application-data"
    app.mkdir(mode=0o700)

    _write(root / "prepared.sqlite3", b"sqlite-bytes")
    _write(root / "prepared-verification.json", b"{}")
    _write(root / "runtime-profile.json", b"{}")
    result = {
        "counters": {
            "provider_generation_calls": 0,
            "controlled_provider_invocations": 0,
        }
    }
    _write(
        root / "sanitized-acceptance-result.json",
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode(),
    )

    descriptor = SimpleNamespace(
        physical_document_count=10,
        logical_document_count=6,
        extracted_document_count=10,
        chunk_count=233,
        controlled_preflight_invocations=1,
        controlled_provider_invocations=0,
        provider_generation_calls=0,
        gate5_ready=True,
        controlled_preflight_verified=True,
        provider_results_absent=True,
        generation_artifacts_absent=True,
    )
    monkeypatch.setattr(attestation, "parse_private_descriptor", lambda *args, **kwargs: descriptor)

    manifest = {
        "schema_version": "arv001-prepared-state-v1",
        "head_sha": HEAD,
        "corpus_sha256": CORPUS,
        "exact_file_set": sorted(attestation._EXPECTED_TOP_LEVEL),
        "physical_document_count": 10,
        "logical_document_count": 6,
        "extracted_document_count": 10,
        "chunk_count": 233,
        "controlled_preflight_invocations": 1,
        "controlled_provider_invocations": 0,
        "provider_generation_calls": 0,
        "snapshot_binding_verified": True,
        "source_graph_binding_verified": True,
        "gate5_ready": True,
        "controlled_preflight_verified": True,
        "database_sha256": _sha(root / "prepared.sqlite3"),
        "prepared_verification_sha256": _sha(root / "prepared-verification.json"),
        "runtime_profile_sha256": _sha(root / "runtime-profile.json"),
        "sanitized_result_sha256": _sha(root / "sanitized-acceptance-result.json"),
        "application_data_tree_sha256": attestation._tree_hash(app),
    }
    _write(
        root / "prepared-state-manifest.json",
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
    )
    return root


def test_valid_publication_is_fully_hash_attested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _valid_root(tmp_path, monkeypatch)
    result = attestation.verify_published_prepared_snapshot(
        root, expected_head=HEAD, expected_corpus_sha=CORPUS
    )
    assert result.database_sha256 == _sha(root / "prepared.sqlite3")
    assert result.manifest_sha256 == _sha(root / "prepared-state-manifest.json")
    assert result.application_data_tree_sha256 == attestation._tree_hash(
        root / "application-data"
    )


def test_tampered_database_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _valid_root(tmp_path, monkeypatch)
    _write(root / "prepared.sqlite3", b"tampered")
    with pytest.raises(attestation.PreparedSnapshotAttestationError) as error:
        attestation.verify_published_prepared_snapshot(
            root, expected_head=HEAD, expected_corpus_sha=CORPUS
        )
    assert error.value.code == "prepared_snapshot_manifest_hash_mismatch"


def test_extra_top_level_file_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _valid_root(tmp_path, monkeypatch)
    _write(root / "extra.txt", b"extra")
    with pytest.raises(attestation.PreparedSnapshotAttestationError) as error:
        attestation.verify_published_prepared_snapshot(
            root, expected_head=HEAD, expected_corpus_sha=CORPUS
        )
    assert error.value.code == "prepared_snapshot_file_set_invalid"


def test_symlink_application_child_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _valid_root(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    _write(outside, b"secret")
    (root / "application-data" / "link").symlink_to(outside)
    with pytest.raises(attestation.PreparedSnapshotAttestationError) as error:
        attestation.verify_published_prepared_snapshot(
            root, expected_head=HEAD, expected_corpus_sha=CORPUS
        )
    assert error.value.code == "prepared_snapshot_symlink_detected"


def test_wrong_original_head_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _valid_root(tmp_path, monkeypatch)
    with pytest.raises(attestation.PreparedSnapshotAttestationError) as error:
        attestation.verify_published_prepared_snapshot(
            root, expected_head="7" * 40, expected_corpus_sha=CORPUS
        )
    assert error.value.code == "prepared_snapshot_head_mismatch"


def test_nonzero_provider_counter_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _valid_root(tmp_path, monkeypatch)
    manifest_path = root / "prepared-state-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["provider_generation_calls"] = 1
    _write(
        manifest_path,
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
    )
    with pytest.raises(attestation.PreparedSnapshotAttestationError) as error:
        attestation.verify_published_prepared_snapshot(
            root, expected_head=HEAD, expected_corpus_sha=CORPUS
        )
    assert error.value.code == "prepared_snapshot_manifest_invariant_invalid"
