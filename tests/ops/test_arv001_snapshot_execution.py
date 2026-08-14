from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from scripts.arv001 import prepared_snapshot_attestation as attestation
from scripts.arv001.snapshot_execution import (
    SnapshotExecutionError,
    execute_prepared_state_root,
    execute_snapshot,
)

HEAD = "h" * 40
CORPUS = "a" * 64


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tamper(path: Path, value: bytes) -> None:
    path.write_bytes(value)
    os.chmod(path, 0o600)


def _chmod_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


def _fixture(tmp_path: Path, *, head: str = HEAD) -> tuple[Path, Path, Path]:
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

    from scripts.arv001.prepared_verification import canonical_document_identity_hashes

    document_hashes = list(canonical_document_identity_hashes(document_rows))
    metadata = {
        "arv001_tender_id": "tender-a",
        "arv001_corpus_sha256": CORPUS,
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
        ("tender-a", "registry", CORPUS),
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
        "head_sha": head,
        "target_run_id": "run",
        "customer_id": "c",
        "project_id": "p",
        "case_id": "case",
        "tender_id": "tender-a",
        "run_status": "completed",
        "registry_identity_sha256": _sha(b"registry"),
        "corpus_sha256": CORPUS,
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
    os.chmod(database, 0o600)
    _chmod_tree(data_dir)
    os.chmod(data_dir, 0o700)
    return database, data_dir, descriptor_path


def _full_root(
    tmp_path: Path,
    database: Path,
    data_dir: Path,
    descriptor_path: Path,
) -> Path:
    root = tmp_path / "prepared-state"
    root.mkdir(mode=0o700)
    shutil.copy2(database, root / "prepared.sqlite3")
    shutil.copytree(data_dir, root / "application-data")
    shutil.copy2(descriptor_path, root / "prepared-verification.json")

    (root / "runtime-profile.json").write_bytes(b"{}")
    os.chmod(root / "runtime-profile.json", 0o600)
    result = {
        "counters": {
            "provider_generation_calls": 0,
            "controlled_provider_invocations": 0,
        }
    }
    (root / "sanitized-acceptance-result.json").write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(root / "sanitized-acceptance-result.json", 0o600)
    _chmod_tree(root / "application-data")
    os.chmod(root / "application-data", 0o700)

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
        "database_sha256": _sha((root / "prepared.sqlite3").read_bytes()),
        "prepared_verification_sha256": _sha(
            (root / "prepared-verification.json").read_bytes()
        ),
        "runtime_profile_sha256": _sha((root / "runtime-profile.json").read_bytes()),
        "sanitized_result_sha256": _sha(
            (root / "sanitized-acceptance-result.json").read_bytes()
        ),
        "application_data_tree_sha256": attestation._tree_hash(root / "application-data"),
    }
    manifest_path = root / "prepared-state-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o600)
    os.chmod(root, 0o700)
    return root


def _snapshot_file(data_dir: Path, name: str) -> Path:
    return (
        data_dir / "customer-pilot" / "c" / "p" / "case" / "run" / "analysis" / name
    )


def test_execute_snapshot_is_read_only_bound_to_exact_hash(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    before_db = database.read_bytes()
    before_files = {
        path.name: path.read_bytes() for path in data_dir.rglob("*") if path.is_file()
    }

    result = execute_snapshot(
        database=database,
        data_dir=data_dir,
        descriptor_path=descriptor_path,
        expected_head=HEAD,
        expected_corpus_sha=CORPUS,
    )

    assert result.verified is True
    assert result.snapshot_hash_bound is True
    assert result.recomputed_snapshot_hash == result.snapshot_hash
    assert result.provider_invocations == 0
    assert result.write_count == 0
    assert result.gate_status("database_snapshot") == "snapshot"
    assert result.gate_status("artifact_snapshot") == "snapshot"
    assert result.gate_status("generation_results") == "not-needed"
    assert result.gate_status("prepared_state") == "not-needed"
    assert result.gate_status("run_generation") == "not-needed"

    assert database.read_bytes() == before_db
    after_files = {
        path.name: path.read_bytes() for path in data_dir.rglob("*") if path.is_file()
    }
    assert after_files == before_files


def test_execute_snapshot_rejects_tampered_snapshot_file(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    _tamper(_snapshot_file(data_dir, "canonical_report.json"), b'{"report":"tampered"}\n')

    with pytest.raises(SnapshotExecutionError) as error:
        execute_snapshot(
            database=database,
            data_dir=data_dir,
            descriptor_path=descriptor_path,
            expected_head=HEAD,
            expected_corpus_sha=CORPUS,
        )
    assert error.value.code == "prepared_snapshot_file_hash_mismatch"


def test_execute_snapshot_rejects_tampered_snapshot_manifest(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    _tamper(
        _snapshot_file(data_dir, "canonical-binding.manifest.json"),
        b'{"customer_id":"x"}',
    )

    with pytest.raises(SnapshotExecutionError) as error:
        execute_snapshot(
            database=database,
            data_dir=data_dir,
            descriptor_path=descriptor_path,
            expected_head=HEAD,
            expected_corpus_sha=CORPUS,
        )
    assert error.value.code in {
        "prepared_snapshot_file_hash_mismatch",
        "prepared_snapshot_manifest_mismatch",
    }


def test_execute_snapshot_rejects_wrong_head(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    with pytest.raises(SnapshotExecutionError) as error:
        execute_snapshot(
            database=database,
            data_dir=data_dir,
            descriptor_path=descriptor_path,
            expected_head="7" * 40,
            expected_corpus_sha=CORPUS,
        )
    assert error.value.code == "descriptor_head_mismatch"


def test_execute_snapshot_blocks_provider_generation_state(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE tender_analysis_runs SET used_llm = 1, llm_model = 'x' WHERE id = 'run'"
    )
    connection.commit()
    connection.close()

    with pytest.raises(SnapshotExecutionError) as error:
        execute_snapshot(
            database=database,
            data_dir=data_dir,
            descriptor_path=descriptor_path,
            expected_head=HEAD,
            expected_corpus_sha=CORPUS,
        )
    assert error.value.code == "prepared_provider_state_present"


def test_execute_prepared_state_root_attests_and_executes(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    root = _full_root(tmp_path, database, data_dir, descriptor_path)

    result = execute_prepared_state_root(
        root=root, expected_head=HEAD, expected_corpus_sha=CORPUS
    )
    assert result.verified is True
    assert result.snapshot_hash_bound is True
    assert result.provider_invocations == 0
    assert result.write_count == 0


def test_execute_prepared_state_root_rejects_tampered_database(tmp_path: Path) -> None:
    database, data_dir, descriptor_path = _fixture(tmp_path)
    root = _full_root(tmp_path, database, data_dir, descriptor_path)
    _tamper(root / "prepared.sqlite3", b"tampered")

    with pytest.raises(SnapshotExecutionError) as error:
        execute_prepared_state_root(
            root=root, expected_head=HEAD, expected_corpus_sha=CORPUS
        )
    assert error.value.code == "prepared_snapshot_manifest_hash_mismatch"


def test_cli_root_mode_succeeds_and_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    from scripts.arv001 import run_snapshot_execution

    database, data_dir, descriptor_path = _fixture(tmp_path)
    root = _full_root(tmp_path, database, data_dir, descriptor_path)

    code = run_snapshot_execution.main(
        ["--root", str(root), "--head", HEAD, "--corpus", CORPUS]
    )
    printed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert printed["snapshot_hash_bound"] is True
    assert {g["gate"] for g in printed["gates"]} == {
        "database_snapshot",
        "artifact_snapshot",
        "generation_results",
        "prepared_state",
        "run_generation",
    }

    capsys.readouterr()
    _tamper(root / "prepared-verification.json", b'{"x":1}')
    code = run_snapshot_execution.main(
        ["--root", str(root), "--head", HEAD, "--corpus", CORPUS]
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "snapshot_execution:prepared_snapshot_manifest_hash_mismatch" in err