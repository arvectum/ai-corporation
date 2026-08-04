from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from scripts.arv001.prepared_verification import (
    canonical_document_identity_hashes,
    parse_private_descriptor,
    verify_prepared_database,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    database = tmp_path / "prepared.sqlite3"
    data_dir = tmp_path / "application-data"
    snapshot_dir = data_dir / "customer-pilot" / "c" / "p" / "case" / "run" / "analysis"
    snapshot_dir.mkdir(parents=True)
    requirements = b'{"requirements":[]}\n'
    report = b'{"report":"ok"}\n'
    req_hash = _sha(requirements)
    report_hash = _sha(report)
    source_graph_hash = "5" * 64
    manifest = {
        "customer_id": "c",
        "project_id": "p",
        "procurement_case_id": "case",
        "run_id": "run",
        "source_analysis_run_id": "source-run",
        "source_graph_hash": source_graph_hash,
        "requirements_file_sha256": req_hash,
        "canonical_report_file_sha256": report_hash,
    }
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
    (snapshot_dir / "requirements.json").write_bytes(requirements)
    (snapshot_dir / "canonical_report.json").write_bytes(report)
    (snapshot_dir / "canonical-binding.manifest.json").write_bytes(manifest_bytes)
    manifest_hash = _sha(manifest_bytes)

    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE tender_analysis_runs (
            id TEXT PRIMARY KEY, registry_number TEXT, status TEXT, used_llm INTEGER,
            llm_model TEXT, report_path TEXT, customer_id TEXT, project_id TEXT,
            procurement_case_id TEXT, metadata_json TEXT
        );
        CREATE TABLE procurement_cases (
            id TEXT PRIMARY KEY, customer_id TEXT, project_id TEXT, current_run_id TEXT
        );
        CREATE TABLE pilot_projects (id TEXT PRIMARY KEY, customer_id TEXT);
        CREATE TABLE procurement_tenders (
            id TEXT PRIMARY KEY, registry_number TEXT, content_hash TEXT
        );
        CREATE TABLE procurement_tender_documents (
            id TEXT PRIMARY KEY, tender_id TEXT, file_name TEXT, sha256 TEXT,
            size_bytes INTEGER, raw_meta TEXT, text_extraction_status TEXT
        );
        CREATE TABLE procurement_document_chunks (
            id TEXT PRIMARY KEY, tender_id TEXT
        );
        CREATE TABLE pilot_run_results (
            id TEXT PRIMARY KEY, customer_id TEXT, project_id TEXT,
            procurement_case_id TEXT, run_id TEXT, source_analysis_run_id TEXT,
            requirements_storage_key TEXT, requirements_file_sha256 TEXT,
            canonical_report_storage_key TEXT, canonical_report_file_sha256 TEXT,
            binding_manifest_storage_key TEXT, binding_manifest_file_sha256 TEXT,
            source_graph_hash TEXT, source_graph_hash_algorithm TEXT,
            verification_policy_version TEXT
        );
        CREATE TABLE pilot_artifacts (id TEXT PRIMARY KEY, run_id TEXT);
        """
    )
    document_rows = []
    document_hashes = []
    source_hashes = []
    for index in range(10):
        name = f"doc-{index:02d}.txt"
        sha256 = f"{index + 1:064x}"
        source_hashes.append(sha256)
        descriptor = {
            "original_name": name,
            "sha256": sha256,
            "size_bytes": 100 + index,
        }
        document_rows.append(descriptor)
        connection.execute(
            "INSERT INTO procurement_tender_documents VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                f"d{index}",
                "tender-a",
                name,
                sha256,
                100 + index,
                json.dumps({"corpus_descriptor": descriptor}),
                "extracted",
            ),
        )
    document_hashes = list(canonical_document_identity_hashes(document_rows))
    metadata = {
        "arv001_tender_id": "tender-a",
        "arv001_corpus_sha256": "a" * 64,
        "arv001_document_identity_hashes": source_hashes,
    }
    connection.execute(
        "INSERT INTO tender_analysis_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run",
            "registry",
            "completed",
            0,
            None,
            None,
            "c",
            "p",
            "case",
            json.dumps(metadata),
        ),
    )
    connection.execute(
        "INSERT INTO procurement_cases VALUES (?, ?, ?, ?)", ("case", "c", "p", "run")
    )
    connection.execute("INSERT INTO pilot_projects VALUES (?, ?)", ("p", "c"))
    connection.execute(
        "INSERT INTO procurement_tenders VALUES (?, ?, ?)",
        ("tender-a", "registry", "a" * 64),
    )
    for index in range(233):
        connection.execute(
            "INSERT INTO procurement_document_chunks VALUES (?, ?)",
            (f"ch{index}", "tender-a"),
        )
    connection.execute(
        "INSERT INTO pilot_run_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "snapshot-binding",
            "c",
            "p",
            "case",
            "run",
            "source-run",
            "customer-pilot/c/p/case/run/analysis/requirements.json",
            req_hash,
            "customer-pilot/c/p/case/run/analysis/canonical_report.json",
            report_hash,
            "customer-pilot/c/p/case/run/analysis/canonical-binding.manifest.json",
            manifest_hash,
            source_graph_hash,
            "sha256-json-c14n-v1",
            "r8-frozen-canonical-verifier-v1",
        ),
    )
    connection.commit()
    connection.close()

    descriptor = {
        "schema_version": "arv001-prepared-verification-v1",
        "head_sha": "h" * 40,
        "target_run_id": "run",
        "customer_id": "c",
        "project_id": "p",
        "case_id": "case",
        "tender_id": "tender-a",
        "run_status": "completed",
        "registry_identity_sha256": _sha(b"registry"),
        "corpus_sha256": "a" * 64,
        "ordered_document_identity_hashes": document_hashes,
        "physical_document_count": 10,
        "logical_document_count": 6,
        "extracted_document_count": 10,
        "chunk_count": 233,
        "snapshot_id": "snapshot-binding",
        "snapshot_hash": manifest_hash,
        "source_graph_id": "source-run",
        "source_graph_hash": source_graph_hash,
        "gate5_ready": True,
        "controlled_preflight_verified": True,
        "controlled_preflight_invocations": 1,
        "controlled_provider_invocations": 0,
        "provider_generation_calls": 0,
        "provider_results_absent": True,
        "generation_artifacts_absent": True,
    }
    descriptor_path = tmp_path / "prepared-verification.json"
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    os.chmod(descriptor_path, 0o600)
    return database, data_dir, descriptor_path


def test_descriptor_parser_and_exact_run_database_verification(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    descriptor = parse_private_descriptor(
        descriptor_path, expected_head="h" * 40, expected_corpus_sha="a" * 64
    )
    verified = verify_prepared_database(
        path=database, data_dir=data_dir, descriptor=descriptor
    )
    assert verified is not None
    assert verified.target_run_verified is True
    assert verified.snapshot_binding_verified is True
    assert verified.source_graph_binding_verified is True
    assert verified.physical_document_count == 10
    assert verified.chunk_count == 233


def test_verification_is_bound_to_exact_run_not_latest_registry(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO procurement_tenders VALUES (?, ?, ?)",
        ("tender-b", "registry", "b" * 64),
    )
    connection.commit()
    connection.close()
    descriptor = parse_private_descriptor(
        descriptor_path, expected_head="h" * 40, expected_corpus_sha="a" * 64
    )
    assert (
        verify_prepared_database(
            path=database, data_dir=data_dir, descriptor=descriptor
        )
        is not None
    )


def test_snapshot_hash_and_descriptor_schema_fail_closed(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    value = json.loads(descriptor_path.read_text(encoding="utf-8"))
    value["snapshot_hash"] = "0" * 64
    descriptor_path.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(descriptor_path, 0o600)
    descriptor = parse_private_descriptor(
        descriptor_path, expected_head="h" * 40, expected_corpus_sha="a" * 64
    )
    assert (
        verify_prepared_database(
            path=database, data_dir=data_dir, descriptor=descriptor
        )
        is None
    )

    value["extra"] = True
    descriptor_path.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(descriptor_path, 0o600)
    try:
        parse_private_descriptor(
            descriptor_path,
            expected_head="h" * 40,
            expected_corpus_sha="a" * 64,
        )
    except RuntimeError as exc:
        assert str(exc) == "descriptor_schema_invalid"
    else:
        raise AssertionError("extra descriptor field must fail closed")
