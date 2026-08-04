from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from scripts.arv001 import full_pre_provider_canonical as canonical
from scripts.arv001.prepared_verification import (
    PreparedVerificationError,
    canonical_document_identity_hashes,
)


def test_canonical_doctor_does_not_fold_asset_failures_into_repository(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        canonical,
        "validate_repository",
        lambda **_: (),
    )
    monkeypatch.setattr(
        canonical,
        "validate_python",
        lambda _: (),
    )

    report = canonical._orchestration_doctor(
        private_env=None,
        repository_root=tmp_path,
        head_sha="a" * 40,
        asset_roots=(tmp_path / "missing-assets",),
        gguf_path=tmp_path / "missing.gguf",
        llama_server_path=tmp_path / "missing-llama-server",
    ).sanitized()

    assert report["status"] == "PASS"
    assert report["phases"] == [
        {"phase": "repository", "status": "PASS", "reason_codes": []},
        {"phase": "python_runtime", "status": "PASS", "reason_codes": []},
        {"phase": "static_environment", "status": "PASS", "reason_codes": []},
    ]


def test_canonical_doctor_retains_real_repository_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        canonical,
        "validate_repository",
        lambda **_: ("git_head_mismatch",),
    )
    monkeypatch.setattr(
        canonical,
        "validate_python",
        lambda _: (),
    )

    report = canonical._orchestration_doctor(
        private_env=None,
        repository_root=tmp_path,
        head_sha="a" * 40,
    ).sanitized()

    assert report["status"] == "FAIL_CLOSED"
    assert report["phases"][0] == {
        "phase": "repository",
        "status": "FAIL",
        "reason_codes": ["git_head_mismatch"],
    }


def test_strict_prepared_verification_reason_is_retained(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        canonical,
        "verify_prepared_database_strict",
        lambda **_: (_ for _ in ()).throw(
            PreparedVerificationError("prepared_snapshot_hash_mismatch")
        ),
    )

    result = canonical._verify_prepared_database_with_reason(
        tmp_path / "prepared.sqlite3",
        object(),
        tmp_path / "data",
    )

    assert result is None
    assert canonical._PREPARED_VERIFICATION_REASON == "prepared_snapshot_hash_mismatch"


def test_generic_snapshot_failure_is_replaced_with_strict_reason(monkeypatch) -> None:
    recorder = object()
    captured: dict[str, object] = {}

    def fake_failure(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(canonical, "_ORIGINAL_FAILURE", fake_failure)
    monkeypatch.setattr(
        canonical,
        "_PREPARED_VERIFICATION_REASON",
        "prepared_snapshot_hash_mismatch",
    )

    result = canonical._failure_with_prepared_reason(
        head_sha="a" * 40,
        phase="snapshot_binding",
        code="prepared_database_verification_failed",
        recorder=recorder,
    )

    assert result == {"ok": True}
    assert captured == {
        "head_sha": "a" * 40,
        "phase": "snapshot_binding",
        "code": "prepared_snapshot_hash_mismatch",
        "recorder": recorder,
    }


def test_non_snapshot_failure_is_not_rewritten(monkeypatch) -> None:
    recorder = object()
    captured: dict[str, object] = {}

    def fake_failure(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(canonical, "_ORIGINAL_FAILURE", fake_failure)
    monkeypatch.setattr(
        canonical,
        "_PREPARED_VERIFICATION_REASON",
        "prepared_snapshot_hash_mismatch",
    )

    canonical._failure_with_prepared_reason(
        head_sha="a" * 40,
        phase="runtime_start",
        code="llama_runtime_start_failed",
        recorder=recorder,
    )

    assert captured["code"] == "llama_runtime_start_failed"


def test_document_metadata_uses_persisted_identity_hashes() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE procurement_tender_documents (
            tender_id TEXT,
            file_name TEXT,
            sha256 TEXT,
            document_identity_hash TEXT,
            size_bytes INTEGER,
            raw_meta TEXT,
            text_extraction_status TEXT
        );
        CREATE TABLE procurement_document_chunks (tender_id TEXT);
        """
    )
    rows = [
        {
            "file_name": "a.pdf",
            "sha256": "a" * 64,
            "document_identity_hash": "1" * 64,
            "size_bytes": 10,
        },
        {
            "file_name": "b.pdf",
            "sha256": "b" * 64,
            "document_identity_hash": "2" * 64,
            "size_bytes": 20,
        },
    ]
    for row in rows:
        connection.execute(
            """
            INSERT INTO procurement_tender_documents (
                tender_id, file_name, sha256, document_identity_hash,
                size_bytes, raw_meta, text_extraction_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tender",
                row["file_name"],
                row["sha256"],
                row["document_identity_hash"],
                row["size_bytes"],
                json.dumps(
                    {"corpus_descriptor": {"original_name": row["file_name"]}}
                ),
                "extracted",
            ),
        )
        connection.execute(
            "INSERT INTO procurement_document_chunks (tender_id) VALUES (?)",
            ("tender",),
        )
    identities = canonical_document_identity_hashes(
        {
            "original_name": row["file_name"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
        }
        for row in rows
    )
    descriptor = SimpleNamespace(
        tender_id="tender",
        ordered_document_identity_hashes=identities,
        physical_document_count=2,
        extracted_document_count=2,
        chunk_count=2,
    )
    metadata = {
        "arv001_document_identity_hashes": [
            row["document_identity_hash"] for row in rows
        ]
    }

    documents, extracted, chunks = (
        canonical._verify_documents_with_persisted_identity(
            connection,
            descriptor,
            metadata,
        )
    )

    assert len(documents) == 2
    assert extracted == 2
    assert chunks == 2
