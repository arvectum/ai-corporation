from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.arv001 import snapshot_acceptance as acceptance
from scripts.arv001.prepared_snapshot_attestation import _EXPECTED_TOP_LEVEL, _tree_hash
from scripts.arv001.prepared_verification import canonical_document_identity_hashes

HEAD = "1" * 40
ORIGINAL_HEAD = "h" * 40
CURRENT_HEAD = "c" * 40
CORPUS = "a" * 64


class _Fan:
    """Fake persistent tokenizer used by the acceptance proof checks."""

    persistent = True
    identity = "acceptance-test-tokenizer"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _chmod_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


def _fixture(tmp_path: Path) -> Path:
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
    metadata = {
        "arv001_tender_id": "tender-a",
        "arv001_corpus_sha256": CORPUS,
        "arv001_document_identity_hashes": [f"{i + 1:064x}" for i in range(10)],
    }
    document_rows: list[dict[str, object]] = []
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
    metadata = {
        "arv001_tender_id": "tender-a",
        "arv001_corpus_sha256": CORPUS,
        "arv001_document_identity_hashes": source_hashes,
    }
    document_hashes = list(canonical_document_identity_hashes(document_rows))
    connection.execute(
        "INSERT INTO tender_analysis_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("run", "registry", "completed", 0, None, None, "c", "p", "case", json.dumps(metadata)),
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
        "head_sha": ORIGINAL_HEAD,
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
        json.dumps(result, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    os.chmod(root / "sanitized-acceptance-result.json", 0o600)
    _chmod_tree(root / "application-data")
    os.chmod(root / "application-data", 0o700)
    manifest_sha = {
        "schema_version": "arv001-prepared-state-v1",
        "head_sha": ORIGINAL_HEAD,
        "corpus_sha256": CORPUS,
        "exact_file_set": sorted(_EXPECTED_TOP_LEVEL),
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
        "application_data_tree_sha256": _tree_hash(root / "application-data"),
    }
    manifest_path = root / "prepared-state-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest_sha, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(manifest_path, 0o600)
    os.chmod(root, 0o700)
    return root


def _reconstruction(
    *args, plan_hash: str = "b" * 64, request_hash: str | None = None, **kwargs
) -> SimpleNamespace:
    request_hash = request_hash or plan_hash
    plan = SimpleNamespace(
        plan_version="r10-1-batch-v1",
        plan_hash=plan_hash,
        batches=(),
    )
    request = SimpleNamespace(batch_plan_hash=request_hash)
    return SimpleNamespace(
        requests=[request],
        plan=plan,
        target_run_binding_verified=True,
        canonical_evidence_projection_match=True,
        evidence_packet_hash="e" * 64,
        ordered_fragment_ids_hash="f" * 64,
    )


def _proof(exact_tokens: int = 1200) -> dict[str, object]:
    return {
        "tokenizer_identity": _Fan.identity,
        "exact_live_output_tokens": exact_tokens,
        "safety_margin_tokens": 200,
    }


def _base_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path, Path]:
    root = _fixture(tmp_path)
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    isolated = tmp_path / "isolated"
    policy = tmp_path / "approved-policy.json"
    policy.write_text('{"provider":"openai_compatible","budget":{}}')
    os.chmod(policy, 0o600)
    monkeypatch.setattr(
        acceptance,
        "_check_protected_drift",
        lambda repository, snapshot, current: (False, False),
    )
    monkeypatch.setattr(
        acceptance,
        "subprocess",
        SimpleNamespace(
            run=lambda command, cwd, check=False: SimpleNamespace(returncode=0)
        ),
    )
    monkeypatch.setattr(acceptance, "_reconstruct_actual_batch_requests", _reconstruction)
    monkeypatch.setattr(
        acceptance, "verify_exact_live_output_budget", lambda request, tokenizer: _proof()
    )
    monkeypatch.setattr(acceptance, "tokenizer_from_environment", _Fan)
    return repository_root, isolated, policy, root


def test_acceptance_zero_transport_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root, isolated, policy, root = _base_setup(tmp_path, monkeypatch)
    report = acceptance.run_snapshot_acceptance(
        prepared_snapshot_root=root,
        repository_root=repository_root,
        current_head=CURRENT_HEAD,
        prepared_snapshot_original_head=ORIGINAL_HEAD,
        expected_corpus_sha=CORPUS,
        approved_policy=policy,
        isolated_runtime_root=isolated,
        tokenizer=_Fan(),
    )
    assert report["schema_version"] == acceptance.SNAPSHOT_ACCEPTANCE_SCHEMA_VERSION
    assert report["ready_for_transport"] is True
    assert report["authorization_consumed"] is False
    assert report["transport_started"] is False
    assert report["controlled_provider_invocations"] == 0
    assert report["provider_generation_calls"] == 0
    assert report["prepared_snapshot_execution_mode"] is True
    assert report["raw_byte_replay"] is False
    assert report["attested_prepared_snapshot_replay"] is True
    assert report["snapshot_attestation_verified"] is True
    assert report["original_published_snapshot_verified"] is True
    assert report["byte_identical_db_copy_verified"] is True
    assert report["target_run_reused"] is True
    assert report["new_run_created"] is False
    assert report["prepare_documents_called"] is False
    assert report["create_application_data_called"] is False
    assert report["canonical_planner_used"] is True
    assert report["canonical_planner_function"] == "build_r10_1_batch_plan"
    assert report["final_request_body_identity_verified"] is True
    assert report["physical_documents"] == 10
    assert report["logical_documents"] == 6
    assert report["extracted_documents"] == 10
    assert report["chunks"] == 233
    assert report["actual_batch_count"] == 1
    assert report["exact_live_output_token_upper_bound"] == 1200
    assert report["output_safety_margin_tokens"] == 200
    assert report["exact_live_output_budget_proof"] == "PASS"
    assert report["exact_live_output_tokenizer_available"] is True


def test_accepts_fails_closed_on_missing_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root, isolated, policy, _ = _base_setup(tmp_path, monkeypatch)
    with pytest.raises(acceptance.SnapshotAcceptanceError) as error:
        acceptance.run_snapshot_acceptance(
            prepared_snapshot_root=tmp_path / "missing",
            repository_root=repository_root,
            current_head=CURRENT_HEAD,
            prepared_snapshot_original_head=ORIGINAL_HEAD,
            expected_corpus_sha=CORPUS,
            approved_policy=policy,
            isolated_runtime_root=isolated,
            tokenizer=_Fan(),
        )
    assert error.value.code in {"prepared_snapshot_root_invalid"}


def test_accepts_blocks_execute_provider_without_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root, isolated, policy, root = _base_setup(tmp_path, monkeypatch)
    with pytest.raises(acceptance.SnapshotAcceptanceError) as error:
        acceptance.run_snapshot_acceptance(
            prepared_snapshot_root=root,
            repository_root=repository_root,
            current_head=CURRENT_HEAD,
            prepared_snapshot_original_head=ORIGINAL_HEAD,
            expected_corpus_sha=CORPUS,
            approved_policy=policy,
            isolated_runtime_root=isolated,
            tokenizer=_Fan(),
            execute_provider=True,
            authorization_granted=False,
        )
    assert error.value.code == "snapshot_authorization_not_granted"


def test_accepts_blocks_execute_provider_even_with_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root, isolated, policy, root = _base_setup(tmp_path, monkeypatch)
    with pytest.raises(acceptance.SnapshotAcceptanceError) as error:
        acceptance.run_snapshot_acceptance(
            prepared_snapshot_root=root,
            repository_root=repository_root,
            current_head=CURRENT_HEAD,
            prepared_snapshot_original_head=ORIGINAL_HEAD,
            expected_corpus_sha=CORPUS,
            approved_policy=policy,
            isolated_runtime_root=isolated,
            tokenizer=_Fan(),
            execute_provider=True,
            authorization_granted=True,
        )
    assert error.value.code == "snapshot_execute_provider_not_runnable_in_this_mode"


def test_accepts_rejects_request_body_plan_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root, isolated, policy, root = _base_setup(tmp_path, monkeypatch)

    def mismatched_reconstruction(*args, **kwargs):
        return _reconstruction(plan_hash="b" * 64, request_hash="9" * 64)

    monkeypatch.setattr(
        acceptance, "_reconstruct_actual_batch_requests", mismatched_reconstruction
    )
    with pytest.raises(acceptance.SnapshotAcceptanceError) as error:
        acceptance.run_snapshot_acceptance(
            prepared_snapshot_root=root,
            repository_root=repository_root,
            current_head=CURRENT_HEAD,
            prepared_snapshot_original_head=ORIGINAL_HEAD,
            expected_corpus_sha=CORPUS,
            approved_policy=policy,
            isolated_runtime_root=isolated,
            tokenizer=_Fan(),
        )
    assert error.value.code == "snapshot_request_body_plan_mismatch"


def test_main_prints_report_and_fails_closed(tmp_path: Path, monkeypatch) -> None:
    repository_root, isolated, policy, root = _base_setup(tmp_path, monkeypatch)
    code = acceptance.main(
        [
            "--prepared-snapshot-root",
            str(root),
            "--repository-root",
            str(repository_root),
            "--current-head",
            CURRENT_HEAD,
            "--prepared-snapshot-original-head",
            ORIGINAL_HEAD,
            "--expected-corpus-sha",
            CORPUS,
            "--approved-policy",
            str(policy),
            "--isolated-runtime-root",
            str(isolated),
        ]
    )
    assert code == 0