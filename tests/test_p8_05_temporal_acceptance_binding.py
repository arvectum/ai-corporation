from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.arv001.complete_corpus_contract import (
    DEFAULT_CORPUS_SHA256,
    DEFAULT_POLICY_SHA256,
    DEFAULT_REGISTRY_NUMBER,
)
from scripts.p8_05_run_temporal_acceptance import _acceptance_command, _parse_success
from scripts.p8_05_temporal_acceptance_binding import (
    AUTHORIZED_STATUS,
    DRIFT_ACCEPTABLE,
    DRIFT_BLOCKING,
    DRIFT_UNCHANGED,
    P8_05_SCHEMA_VERSION,
    P805AcceptanceBindingBlocked,
    REVALIDATION_BLOCKED,
    REVALIDATION_PASS,
    build_authorization_manifest,
    build_fresh_material_snapshot,
    build_revalidation_manifest,
    canonical_sha256,
    classify_revalidation,
    compare_material_snapshots,
    execute_authorized_once,
    load_and_verify_frozen_baseline,
    profile_corpus_hash,
    validate_authorization_manifest,
    verify_frozen_baseline,
)


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "config/arv001/acceptance_baseline.json"
REQUIRED_GROUPS = [
    "application_requirements",
    "contract_draft",
    "contract_performance_security",
    "notice",
    "price_justification",
    "technical_specification",
]


def _baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rows(*, changed: int | None = None) -> list[dict]:
    result = []
    for index in range(10):
        digest = _digest(f"doc-{index}" + ("-changed" if changed == index else ""))
        result.append(
            {
                "original_name": f"Документ {index:02d}.docx",
                "sha256": digest,
                "size_bytes": 100 + index,
            }
        )
    return result


def _baseline_snapshot() -> dict:
    return {
        "physical": _rows(),
        "physical_file_count": 10,
        "logical_document_count": 6,
        "corpus_sha256": DEFAULT_CORPUS_SHA256,
        "retrieved_at": "2026-08-15T10:00:00+00:00",
        "retrieval_ref_sha256": _digest("ref-old"),
    }


def _fresh_snapshot(*, changed: int | None = None, temporal_drift: bool = True) -> dict:
    return {
        "physical": _rows(changed=changed),
        "physical_file_count": 10,
        "logical_document_count": 6,
        "logical_groups": REQUIRED_GROUPS,
        "document_set_status": "complete",
        "analysis_allowed": True,
        "corpus_sha256": DEFAULT_CORPUS_SHA256,
        "retrieved_at": (
            "2026-08-21T06:00:00+00:00"
            if temporal_drift
            else "2026-08-15T10:00:00+00:00"
        ),
        "retrieval_ref_sha256": (
            _digest("ref-new") if temporal_drift else _digest("ref-old")
        ),
    }


def _fresh_files_fixture(tmp_path: Path) -> tuple[dict, Path]:
    names = [
        "epNotification.xml",
        "Техническое задание.docx",
        "Описание объекта закупки.docx",
        "Обоснование НМЦК1.xlsx",
        "Обоснование НМЦК2.docx",
        "Требования к составу заявки.docx",
        "Проект контракта.docx",
        "Проект договора.docx",
        "Реквизиты обеспечения исполнения контракта.docx",
        "Обеспечение исполнения контракта.docx",
    ]
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    files = []
    for index, name in enumerate(names, start=1):
        payload = f"synthetic-{index}".encode()
        stored_name = f"extracted/{index:02d}{Path(name).suffix}"
        path = input_dir / stored_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        files.append(
            {
                "original_name": name,
                "stored_name": stored_name,
                "size_bytes": len(payload),
            }
        )
    metadata = {
        "created_at": "2026-08-21T06:00:00+00:00",
        "procurement_source": "zakupki_gov_ru_getdocs_ip",
        "procurement_id": DEFAULT_REGISTRY_NUMBER,
        "notice_number": DEFAULT_REGISTRY_NUMBER,
        "reestr_number": DEFAULT_REGISTRY_NUMBER,
        "procurement": {
            "procurement_id": DEFAULT_REGISTRY_NUMBER,
            "procurement_number": DEFAULT_REGISTRY_NUMBER,
        },
        "external_actions": False,
        "no_platform_submission": True,
        "no_email_sending": True,
        "no_digital_signature": True,
        "archive_downloaded": True,
        "archive_extraction_complete": True,
        "getdocs_status": "completed",
        "getdocs_ref_id": "fresh-ref-123",
        "files": files,
    }
    return metadata, input_dir


def test_frozen_baseline_descriptor_matches_executable_contract() -> None:
    baseline = load_and_verify_frozen_baseline(BASELINE_PATH)
    assert baseline["registry_number"] == DEFAULT_REGISTRY_NUMBER
    assert baseline["corpus"]["sha256"] == DEFAULT_CORPUS_SHA256
    assert baseline["policy"]["sha256"] == DEFAULT_POLICY_SHA256


def test_wrong_tender_cannot_be_bound_to_arv001_acceptance() -> None:
    baseline = _baseline()
    baseline["registry_number"] = "0344100006426000005"
    with pytest.raises(P805AcceptanceBindingBlocked) as caught:
        verify_frozen_baseline(baseline)
    assert caught.value.code == "BLOCKED_FROZEN_BASELINE_CONTRACT_MISMATCH"


def test_wrong_baseline_generation_is_blocked() -> None:
    baseline = _baseline()
    baseline["baseline_generation"] = 3
    with pytest.raises(P805AcceptanceBindingBlocked) as caught:
        verify_frozen_baseline(baseline)
    assert caught.value.code == "BLOCKED_FROZEN_BASELINE_CONTRACT_MISMATCH"


def test_profile_corpus_hash_honors_newline_profile() -> None:
    rows = _rows()
    expected_bytes = (
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    expected = hashlib.sha256(expected_bytes).hexdigest()
    actual = profile_corpus_hash(
        rows,
        fields=["original_name", "sha256", "size_bytes"],
        serialization="canonical_compact_newline",
    )
    assert actual == expected


def test_profile_corpus_hash_blocks_missing_identity_field() -> None:
    rows = _rows()
    rows[0].pop("size_bytes")
    with pytest.raises(P805AcceptanceBindingBlocked) as caught:
        profile_corpus_hash(
            rows,
            fields=["original_name", "sha256", "size_bytes"],
            serialization="canonical_compact_newline",
        )
    assert caught.value.code == "BLOCKED_CORPUS_IDENTITY_FIELD_MISSING"


def test_compare_material_snapshots_all_unchanged() -> None:
    entries = compare_material_snapshots(_baseline_snapshot(), _fresh_snapshot())
    assert len(entries) == 10
    assert {item["classification"] for item in entries} == {"UNCHANGED"}


def test_compare_material_snapshots_detects_changed_bytes() -> None:
    entries = compare_material_snapshots(
        _baseline_snapshot(),
        _fresh_snapshot(changed=4),
    )
    changed = [item for item in entries if item["classification"] == "CHANGED"]
    assert len(changed) == 1
    assert changed[0]["name"] == "Документ 04.docx"


def test_compare_material_snapshots_detects_added_and_removed() -> None:
    baseline = _baseline_snapshot()
    fresh = _fresh_snapshot()
    fresh["physical"] = fresh["physical"][:-1]
    fresh["physical"].append(
        {"original_name": "Новый.docx", "sha256": _digest("new"), "size_bytes": 1}
    )
    entries = compare_material_snapshots(baseline, fresh)
    assert {item["classification"] for item in entries} >= {"ADDED", "REMOVED"}


def test_revalidation_classifies_unchanged_when_retrieval_identity_is_same() -> None:
    baseline = _baseline()
    old = _baseline_snapshot()
    fresh = _fresh_snapshot(temporal_drift=False)
    entries = compare_material_snapshots(old, fresh)
    assert classify_revalidation(baseline, old, fresh, entries) == DRIFT_UNCHANGED


def test_revalidation_classifies_only_temporal_metadata_as_acceptable_drift() -> None:
    baseline = _baseline()
    old = _baseline_snapshot()
    fresh = _fresh_snapshot(temporal_drift=True)
    entries = compare_material_snapshots(old, fresh)
    assert classify_revalidation(baseline, old, fresh, entries) == DRIFT_ACCEPTABLE


def test_revalidation_classifies_changed_document_as_blocking_drift() -> None:
    baseline = _baseline()
    old = _baseline_snapshot()
    fresh = _fresh_snapshot(changed=2)
    entries = compare_material_snapshots(old, fresh)
    assert classify_revalidation(baseline, old, fresh, entries) == DRIFT_BLOCKING


def test_revalidation_classifies_structural_drift_as_blocking() -> None:
    baseline = _baseline()
    old = _baseline_snapshot()
    fresh = _fresh_snapshot()
    fresh["physical_file_count"] = 11
    entries = compare_material_snapshots(old, fresh)
    assert classify_revalidation(baseline, old, fresh, entries) == DRIFT_BLOCKING


def test_revalidation_classifies_missing_logical_group_as_blocking() -> None:
    baseline = _baseline()
    old = _baseline_snapshot()
    fresh = _fresh_snapshot()
    fresh["logical_groups"] = REQUIRED_GROUPS[:-1]
    entries = compare_material_snapshots(old, fresh)
    assert classify_revalidation(baseline, old, fresh, entries) == DRIFT_BLOCKING


def test_revalidation_manifest_passes_on_acceptable_temporal_drift() -> None:
    manifest = build_revalidation_manifest(
        _baseline(),
        _baseline_snapshot(),
        _fresh_snapshot(),
    )
    assert manifest["schema_version"] == P8_05_SCHEMA_VERSION
    assert manifest["status"] == REVALIDATION_PASS
    assert manifest["drift_classification"] == DRIFT_ACCEPTABLE
    assert manifest["authorization_eligible"] is True
    assert manifest["provider_generation_calls_before_authorization"] == 0
    assert manifest["external_actions"] is False
    body = {
        key: value
        for key, value in manifest.items()
        if key not in ("manifest_sha256", "manifest_integrity_ref")
    }
    assert manifest["manifest_sha256"] == canonical_sha256(body)


def test_revalidation_manifest_blocks_material_drift() -> None:
    manifest = build_revalidation_manifest(
        _baseline(),
        _baseline_snapshot(),
        _fresh_snapshot(changed=1),
    )
    assert manifest["status"] == REVALIDATION_BLOCKED
    assert manifest["drift_classification"] == DRIFT_BLOCKING
    assert manifest["authorization_eligible"] is False


def test_authorization_is_bound_to_revalidation_head_corpus_and_policy() -> None:
    baseline = _baseline()
    revalidation = build_revalidation_manifest(
        baseline,
        _baseline_snapshot(),
        _fresh_snapshot(),
    )
    head = "a" * 40
    authorization = build_authorization_manifest(
        baseline,
        revalidation,
        expected_head=head,
    )
    assert authorization["status"] == AUTHORIZED_STATUS
    assert authorization["generation_run_limit"] == 1
    assert authorization["expected_head"] == head
    assert authorization["corpus_sha256"] == DEFAULT_CORPUS_SHA256
    assert authorization["policy_sha256"] == DEFAULT_POLICY_SHA256
    assert authorization["provider_execution_authorized"] is True
    assert authorization["procurement_submission_authorized"] is False
    assert authorization["email_authorized"] is False
    assert authorization["digital_signature_authorized"] is False


def test_blocking_drift_cannot_create_authorization() -> None:
    baseline = _baseline()
    revalidation = build_revalidation_manifest(
        baseline,
        _baseline_snapshot(),
        _fresh_snapshot(changed=3),
    )
    with pytest.raises(P805AcceptanceBindingBlocked) as caught:
        build_authorization_manifest(baseline, revalidation, expected_head="a" * 40)
    assert caught.value.code == "BLOCKED_REVALIDATION_NOT_AUTHORIZABLE"


def test_tampered_revalidation_cannot_create_authorization() -> None:
    baseline = _baseline()
    revalidation = build_revalidation_manifest(
        baseline,
        _baseline_snapshot(),
        _fresh_snapshot(),
    )
    revalidation["fresh_corpus_sha256"] = "0" * 64
    with pytest.raises(P805AcceptanceBindingBlocked) as caught:
        build_authorization_manifest(baseline, revalidation, expected_head="a" * 40)
    assert caught.value.code == "BLOCKED_REVALIDATION_NOT_AUTHORIZABLE"


def test_authorized_execution_starts_exactly_one_subprocess() -> None:
    baseline = _baseline()
    revalidation = build_revalidation_manifest(
        baseline,
        _baseline_snapshot(),
        _fresh_snapshot(),
    )
    head = "a" * 40
    authorization = build_authorization_manifest(baseline, revalidation, expected_head=head)
    calls: list[list[str]] = []

    def fake_runner(command, **kwargs):
        calls.append(list(command))
        assert kwargs["check"] is False
        assert kwargs["capture_output"] is True
        return subprocess.CompletedProcess(command, 0, stdout="{}\n", stderr="")

    execute_authorized_once(
        authorization,
        ["python", "acceptance.py", "--execute-provider"],
        expected_head=head,
        env={},
        runner=fake_runner,
    )
    assert calls == [["python", "acceptance.py", "--execute-provider"]]


def test_invalid_authorization_starts_no_subprocess() -> None:
    baseline = _baseline()
    revalidation = build_revalidation_manifest(
        baseline,
        _baseline_snapshot(),
        _fresh_snapshot(),
    )
    head = "a" * 40
    authorization = build_authorization_manifest(baseline, revalidation, expected_head=head)
    authorization["provider_execution_authorized"] = False
    calls = 0

    def fake_runner(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("runner must not be called")

    with pytest.raises(P805AcceptanceBindingBlocked):
        execute_authorized_once(
            authorization,
            ["python", "acceptance.py"],
            expected_head=head,
            env={},
            runner=fake_runner,
        )
    assert calls == 0


def test_authorization_integrity_tampering_is_blocked() -> None:
    baseline = _baseline()
    revalidation = build_revalidation_manifest(
        baseline,
        _baseline_snapshot(),
        _fresh_snapshot(),
    )
    head = "a" * 40
    authorization = build_authorization_manifest(baseline, revalidation, expected_head=head)
    authorization["generation_run_limit"] = 2
    with pytest.raises(P805AcceptanceBindingBlocked) as caught:
        validate_authorization_manifest(authorization, expected_head=head)
    assert caught.value.code == "BLOCKED_AUTHORIZATION_INVALID"


def test_fresh_snapshot_builds_complete_six_group_evidence(tmp_path: Path) -> None:
    metadata, input_dir = _fresh_files_fixture(tmp_path)
    snapshot = build_fresh_material_snapshot(
        metadata,
        input_dir=input_dir,
        baseline=_baseline(),
    )
    assert snapshot["physical_file_count"] == 10
    assert snapshot["logical_document_count"] == 6
    assert snapshot["logical_groups"] == REQUIRED_GROUPS
    assert snapshot["document_set_status"] == "complete"
    assert snapshot["analysis_allowed"] is True
    assert len(snapshot["corpus_sha256"]) == 64
    assert snapshot["retrieval_ref_sha256"] == _digest("fresh-ref-123")


def test_fresh_snapshot_blocks_unsafe_external_action_context(tmp_path: Path) -> None:
    metadata, input_dir = _fresh_files_fixture(tmp_path)
    metadata["external_actions"] = True
    with pytest.raises(P805AcceptanceBindingBlocked) as caught:
        build_fresh_material_snapshot(metadata, input_dir=input_dir, baseline=_baseline())
    assert caught.value.code == "BLOCKED_FRESH_EIS_CONTEXT_INVALID"


def test_fresh_snapshot_blocks_wrong_registry(tmp_path: Path) -> None:
    metadata, input_dir = _fresh_files_fixture(tmp_path)
    metadata["reestr_number"] = "0344100006426000005"
    with pytest.raises(P805AcceptanceBindingBlocked) as caught:
        build_fresh_material_snapshot(metadata, input_dir=input_dir, baseline=_baseline())
    assert caught.value.code == "BLOCKED_FRESH_REGISTRY_MISMATCH"


def test_fresh_snapshot_blocks_duplicate_document_name(tmp_path: Path) -> None:
    metadata, input_dir = _fresh_files_fixture(tmp_path)
    metadata["files"][1]["original_name"] = metadata["files"][0]["original_name"]
    with pytest.raises(P805AcceptanceBindingBlocked) as caught:
        build_fresh_material_snapshot(metadata, input_dir=input_dir, baseline=_baseline())
    assert caught.value.code == "BLOCKED_DUPLICATE_MATERIAL_NAME"


def test_fresh_snapshot_blocks_declared_size_mismatch(tmp_path: Path) -> None:
    metadata, input_dir = _fresh_files_fixture(tmp_path)
    metadata["files"][0]["size_bytes"] += 1
    with pytest.raises(P805AcceptanceBindingBlocked) as caught:
        build_fresh_material_snapshot(metadata, input_dir=input_dir, baseline=_baseline())
    assert caught.value.code == "BLOCKED_FRESH_FILE_SIZE_MISMATCH"


def test_fresh_snapshot_blocks_path_escape(tmp_path: Path) -> None:
    metadata, input_dir = _fresh_files_fixture(tmp_path)
    metadata["files"][0]["stored_name"] = "../outside.xml"
    with pytest.raises(P805AcceptanceBindingBlocked) as caught:
        build_fresh_material_snapshot(metadata, input_dir=input_dir, baseline=_baseline())
    assert caught.value.code == "BLOCKED_UNSAFE_FRESH_STORED_PATH"


def test_acceptance_command_binds_frozen_identity_and_execute_provider(tmp_path: Path) -> None:
    command = _acceptance_command(
        candidate_root=tmp_path / "candidate",
        intake_root=tmp_path / "intake",
        database_path=tmp_path / "db.sqlite",
        data_dir=tmp_path / "data",
        approved_policy=tmp_path / "policy.json",
        output_root=tmp_path / "out",
        expected_head="a" * 40,
        registry_number=DEFAULT_REGISTRY_NUMBER,
        corpus_sha256=DEFAULT_CORPUS_SHA256,
        policy_sha256=DEFAULT_POLICY_SHA256,
        initialize_database=True,
    )
    assert command.count("--execute-provider") == 1
    assert command.count("--initialize-database") == 1
    assert command[command.index("--registry-number") + 1] == DEFAULT_REGISTRY_NUMBER
    assert command[command.index("--expected-corpus-sha") + 1] == DEFAULT_CORPUS_SHA256
    assert command[command.index("--expected-policy-sha") + 1] == DEFAULT_POLICY_SHA256


def test_parse_success_accepts_only_bound_complete_corpus_result() -> None:
    payload = {
        "status": "complete_corpus_report_ready_for_product_owner_review",
        "marker": "ARV-001_COMPLETE_CORPUS_REPORT_READY_FOR_PRODUCT_OWNER_REVIEW",
        "head_sha": "a" * 40,
        "corpus_sha256": DEFAULT_CORPUS_SHA256,
        "controlled_invocation_count": 1,
        "production_db_mutations": 0,
        "old_arv003_mutations": 0,
        "git_mutations": 0,
        "artifact_hashes": {"canonical-output.json": _digest("artifact")},
    }
    parsed = _parse_success(
        json.dumps(payload),
        expected_head="a" * 40,
        expected_corpus=DEFAULT_CORPUS_SHA256,
    )
    assert parsed == payload


def test_parse_success_rejects_second_controlled_invocation() -> None:
    payload = {
        "status": "complete_corpus_report_ready_for_product_owner_review",
        "marker": "ARV-001_COMPLETE_CORPUS_REPORT_READY_FOR_PRODUCT_OWNER_REVIEW",
        "head_sha": "a" * 40,
        "corpus_sha256": DEFAULT_CORPUS_SHA256,
        "controlled_invocation_count": 2,
        "production_db_mutations": 0,
        "old_arv003_mutations": 0,
        "git_mutations": 0,
        "artifact_hashes": {"canonical-output.json": _digest("artifact")},
    }
    with pytest.raises(P805AcceptanceBindingBlocked) as caught:
        _parse_success(
            json.dumps(payload),
            expected_head="a" * 40,
            expected_corpus=DEFAULT_CORPUS_SHA256,
        )
    assert caught.value.code == "BLOCKED_ACCEPTANCE_SUCCESS_CONTRACT_MISMATCH"
