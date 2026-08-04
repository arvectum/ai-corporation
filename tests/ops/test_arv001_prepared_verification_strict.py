from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from scripts.arv001.prepared_verification import (
    PreparedVerificationError,
    parse_private_descriptor,
    verify_prepared_database_strict,
)
from tests.ops.test_arv001_prepared_verification import _fixture


def _descriptor(path: Path):
    return parse_private_descriptor(
        path,
        expected_head="h" * 40,
        expected_corpus_sha="a" * 64,
    )


def _verify(database: Path, data_dir: Path, descriptor_path: Path):
    return verify_prepared_database_strict(
        path=database,
        data_dir=data_dir,
        descriptor=_descriptor(descriptor_path),
    )


def _sql(database: Path, statement: str, parameters: tuple[object, ...] = ()) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("statement", "parameters", "code"),
    [
        ("DELETE FROM tender_analysis_runs", (), "prepared_run_missing"),
        (
            "UPDATE tender_analysis_runs SET customer_id = ?",
            ("wrong",),
            "prepared_run_binding_mismatch",
        ),
        ("DELETE FROM procurement_cases", (), "prepared_case_or_project_missing"),
        (
            "UPDATE procurement_cases SET current_run_id = ?",
            ("stale",),
            "prepared_ownership_mismatch",
        ),
        (
            "UPDATE procurement_tenders SET content_hash = ?",
            ("b" * 64,),
            "prepared_tender_binding_mismatch",
        ),
        (
            "UPDATE procurement_tender_documents SET size_bytes = size_bytes + 1 "
            "WHERE id = 'd0'",
            (),
            "prepared_document_identity_mismatch",
        ),
        (
            "DELETE FROM procurement_document_chunks WHERE id = 'ch0'",
            (),
            "prepared_document_counts_mismatch",
        ),
        ("DELETE FROM pilot_run_results", (), "prepared_snapshot_binding_missing"),
        (
            "UPDATE pilot_run_results SET source_graph_hash = ?",
            ("4" * 64,),
            "prepared_source_graph_mismatch",
        ),
        (
            "UPDATE pilot_run_results SET verification_policy_version = ?",
            ("wrong",),
            "prepared_snapshot_policy_mismatch",
        ),
        (
            "UPDATE tender_analysis_runs SET used_llm = 1",
            (),
            "prepared_provider_state_present",
        ),
    ],
)
def test_strict_verifier_reports_exact_failure(
    tmp_path: Path,
    statement: str,
    parameters: tuple[object, ...],
    code: str,
) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    _sql(database, statement, parameters)

    with pytest.raises(PreparedVerificationError) as error:
        _verify(database, data_dir, descriptor_path)

    assert error.value.code == code
    assert "/" not in error.value.code
    assert "\\" not in error.value.code


def test_strict_verifier_rejects_missing_database(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    database.unlink()

    with pytest.raises(PreparedVerificationError) as error:
        _verify(database, data_dir, descriptor_path)

    assert error.value.code == "prepared_database_path_invalid"


def test_strict_verifier_reports_snapshot_file_hash_mismatch(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    requirements = next(data_dir.rglob("requirements.json"))
    requirements.write_bytes(b"tampered")

    with pytest.raises(PreparedVerificationError) as error:
        _verify(database, data_dir, descriptor_path)

    assert error.value.code == "prepared_snapshot_file_hash_mismatch"


def test_strict_verifier_reports_manifest_invalid(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    manifest = next(data_dir.rglob("canonical-binding.manifest.json"))
    manifest.write_text("{", encoding="utf-8")
    manifest_hash = __import__("hashlib").sha256(manifest.read_bytes()).hexdigest()
    _sql(
        database,
        "UPDATE pilot_run_results SET binding_manifest_file_sha256 = ?",
        (manifest_hash,),
    )
    value = json.loads(descriptor_path.read_text(encoding="utf-8"))
    value["snapshot_hash"] = manifest_hash
    descriptor_path.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(descriptor_path, 0o600)

    with pytest.raises(PreparedVerificationError) as error:
        _verify(database, data_dir, descriptor_path)

    assert error.value.code == "prepared_snapshot_manifest_invalid"


def test_strict_verifier_is_read_only(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    before = database.read_bytes()

    result = _verify(database, data_dir, descriptor_path)

    assert result.snapshot_binding_verified is True
    assert database.read_bytes() == before
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()
