from __future__ import annotations

import copy
import importlib.util
import json
import shutil
from pathlib import Path

import yaml

from src.modules.electrical_ontology_shadow.assets import load_shadow_snapshot
from src.modules.electrical_ontology_shadow.audit import ShadowAuditStore
from src.modules.electrical_ontology_shadow.service import run_shadow_payload_safely
from src.shared.config.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
ELECTRICAL = ROOT / "schemas" / "categories" / "electrical"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _settings(**overrides) -> Settings:
    values = {
        "electrical_ontology_shadow_enabled": False,
        "electrical_ontology_shadow_kill_switch": True,
        "electrical_ontology_shadow_allowed_profiles": "",
        "electrical_ontology_shadow_approval_id": None,
        "electrical_ontology_shadow_max_source_chars": 12_000,
        "electrical_ontology_shadow_max_items": 64,
        "electrical_ontology_shadow_max_audit_bytes": 262_144,
        "electrical_ontology_shadow_timeout_ms": 5_000,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _make_release_eligible_copy(tmp_path: Path) -> Path:
    repository_root = tmp_path / "repo"
    target = repository_root / "schemas" / "categories" / "electrical"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ELECTRICAL, target)

    release_path = target / "truth_pack_release_report.v1.yaml"
    release = yaml.safe_load(release_path.read_text(encoding="utf-8"))
    release["status"] = "RELEASE_ELIGIBLE"
    release["gates"]["independent_acceptance_passed"] = True
    release["gates"]["release_gate_passed"] = True
    release["acceptance"]["accepted_profiles"] = 15
    release["acceptance"]["pending_profiles"] = 0
    release["acceptance"]["independent_acceptance_complete"] = True
    release_path.write_text(
        yaml.safe_dump(release, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    acceptance_path = target / "truth_pack_acceptance.v1.yaml"
    acceptance = yaml.safe_load(acceptance_path.read_text(encoding="utf-8"))
    acceptance["status"] = "accepted"
    for index, row in enumerate(acceptance["profiles"], start=1):
        row["acceptance_status"] = "accepted"
        row["primary_annotator_id"] = f"expert.primary.{index}"
        row["acceptance_annotator_id"] = f"expert.acceptance.{index}"
        row["accepted_at"] = "2026-07-27T12:00:00+00:00"
        row["acceptance_hash"] = f"{index:064x}"[-64:]
        row["audited_item_count"] = 32
        row["disagreement_rate"] = 0.0
        row["rationale"] = "Independent acceptance fixture for runtime contract test."
    acceptance["summary"].update(
        {
            "accepted_profiles": 15,
            "pending_profiles": 0,
            "rejected_profiles": 0,
            "independent_acceptance_complete": True,
            "release_gate_passed": True,
        }
    )
    acceptance_path.write_text(
        yaml.safe_dump(acceptance, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return repository_root


def test_arv067i_policy_validator_passes() -> None:
    validator = _load_module(
        ELECTRICAL / "validate_shadow_runtime.py",
        "arv067i_policy_validator",
    )
    assert validator.main() == 0


def test_arv067i_defaults_are_disabled_and_killed() -> None:
    settings = Settings(_env_file=None)
    assert settings.electrical_ontology_shadow_enabled is False
    assert settings.electrical_ontology_shadow_kill_switch is True
    assert settings.electrical_ontology_shadow_allowed_profiles == ""
    assert settings.electrical_ontology_shadow_approval_id is None


def test_arv067i_canonical_snapshot_enforces_arv067h_block() -> None:
    snapshot = load_shadow_snapshot(repository_root=ROOT)
    assert snapshot.registry_id == "ARV-067D-ELECTRICAL-DETAILED-PROFILES-WAVE1"
    assert snapshot.registry_version == "1.0.0"
    assert snapshot.benchmark_id == "ARV-067H-ELECTRICAL-TRUTH-PACK-BENCHMARK"
    assert snapshot.benchmark_version == "1.0.0"
    assert len(snapshot.profiles) == 15
    assert snapshot.accepted_profile_ids == frozenset()
    assert snapshot.independent_acceptance_complete is False
    assert snapshot.release_gate_passed is False
    assert len(snapshot.snapshot_root_hash) == 64


def test_arv067i_disabled_path_has_no_audit_side_effect(tmp_path: Path) -> None:
    primary = {"recommendation": "manual_review"}
    before = copy.deepcopy(primary)
    summary = run_shadow_payload_safely(
        run_id="toa-run-disabled",
        tenant_id="tenant-a",
        source_text="кабельная муфта",
        primary_result=primary,
        settings=_settings(),
        repository_root=ROOT,
        audit_root=tmp_path / "audit",
    )
    assert summary["status"] == "DISABLED"
    assert summary["reason_codes"] == ["SHADOW_FEATURE_FLAG_DISABLED"]
    assert summary["production_effect"] is False
    assert primary == before
    assert not (tmp_path / "audit").exists()


def test_arv067i_kill_switch_blocks_and_persists_audit(tmp_path: Path) -> None:
    settings = _settings(
        electrical_ontology_shadow_enabled=True,
        electrical_ontology_shadow_kill_switch=True,
        electrical_ontology_shadow_audit_root=str(tmp_path / "audit"),
    )
    summary = run_shadow_payload_safely(
        run_id="toa-run-killed",
        tenant_id="tenant-a",
        source_text="кабельная муфта",
        primary_result={"recommendation": "go"},
        settings=settings,
        repository_root=ROOT,
    )
    assert summary["status"] == "BLOCKED"
    assert summary["reason_codes"] == ["SHADOW_KILL_SWITCH_ACTIVE"]
    assert summary["candidate_count"] == 0
    store = ShadowAuditStore(
        tmp_path / "audit",
        max_payload_bytes=settings.electrical_ontology_shadow_max_audit_bytes,
    )
    audit = store.load(tenant_id="tenant-a", run_id="toa-run-killed")
    assert audit["safety"]["primary_result_mutated"] is False
    assert audit["safety"]["external_actions"] is False
    assert audit["safety"]["production_promotion_allowed"] is False


def test_arv067i_canonical_gate_blocks_even_when_flag_is_enabled(tmp_path: Path) -> None:
    settings = _settings(
        electrical_ontology_shadow_enabled=True,
        electrical_ontology_shadow_kill_switch=False,
        electrical_ontology_shadow_approval_id="approval-ARV-067I-001",
        electrical_ontology_shadow_allowed_profiles="cable_joint",
        electrical_ontology_shadow_audit_root=str(tmp_path / "audit"),
    )
    summary = run_shadow_payload_safely(
        run_id="toa-run-gated",
        tenant_id="tenant-a",
        source_text="кабельная муфта",
        primary_result={"recommendation": "go"},
        settings=settings,
        repository_root=ROOT,
    )
    assert summary["status"] == "BLOCKED"
    assert "ARV067H_RELEASE_GATE_NOT_PASSED" in summary["reason_codes"]
    assert "ARV067H_INDEPENDENT_ACCEPTANCE_INCOMPLETE" in summary["reason_codes"]
    assert "SHADOW_NO_ELIGIBLE_PROFILES" in summary["reason_codes"]
    assert summary["production_effect"] is False


def test_arv067i_eligible_fixture_runs_shadow_without_primary_effect(tmp_path: Path) -> None:
    repository_root = _make_release_eligible_copy(tmp_path)
    audit_root = tmp_path / "audit"
    settings = _settings(
        electrical_ontology_shadow_enabled=True,
        electrical_ontology_shadow_kill_switch=False,
        electrical_ontology_shadow_approval_id="approval-ARV-067I-001",
        electrical_ontology_shadow_allowed_profiles="cable_joint",
        electrical_ontology_shadow_audit_root=str(audit_root),
        electrical_ontology_shadow_max_source_chars=1_000,
    )
    primary = {
        "canonical_category_id": "electrical.primary.cables_fittings_pipes.cable_joint",
        "recommendation": "manual_review",
    }
    before = copy.deepcopy(primary)
    structured = {
        "items": [
            {
                "item_id": "line-1",
                "name": "Кабельная муфта концевая",
                "requested_attributes": {
                    "joint_type": "end",
                    "rated_voltage_kv": 10,
                },
                "candidate_attributes": {
                    "joint_type": "end",
                    "rated_voltage_kv": 10,
                },
                "evidence_confirmed": True,
            }
        ]
    }
    source = (
        "Кабельная муфта. Контакт test@example.com, +7 (999) 123-45-67, "
        "ИНН 1234567890. " + "данные " * 500
    )
    summary = run_shadow_payload_safely(
        run_id="toa-run-active",
        tenant_id="tenant-a",
        source_text=source,
        primary_result=primary,
        structured_payload=structured,
        settings=settings,
        repository_root=repository_root,
    )
    assert summary["status"] == "SHADOW_COMPLETED"
    assert summary["candidate_count"] == 1
    assert summary["disagreement_count"] == 0
    assert summary["production_effect"] is False
    assert summary["external_actions"] is False
    assert summary["requires_review"] is True
    assert primary == before

    store = ShadowAuditStore(
        audit_root,
        max_payload_bytes=settings.electrical_ontology_shadow_max_audit_bytes,
    )
    audit = store.load(tenant_id="tenant-a", run_id="toa-run-active")
    assert audit["results"][0]["status"] == "EXACT"
    assert audit["results"][0]["requires_review"] is True
    assert audit["input"]["source_truncated"] is True
    assert audit["input"]["raw_source_stored"] is False
    serialized = json.dumps(audit, ensure_ascii=False)
    assert "test@example.com" not in serialized
    assert "+7 (999) 123-45-67" not in serialized
    assert "1234567890" not in serialized
    assert len(audit["versions"]["snapshot_root_hash"]) == 64


def test_arv067i_disagreement_is_review_only(tmp_path: Path) -> None:
    repository_root = _make_release_eligible_copy(tmp_path)
    settings = _settings(
        electrical_ontology_shadow_enabled=True,
        electrical_ontology_shadow_kill_switch=False,
        electrical_ontology_shadow_approval_id="approval-ARV-067I-002",
        electrical_ontology_shadow_allowed_profiles="cable_joint",
        electrical_ontology_shadow_audit_root=str(tmp_path / "audit"),
    )
    summary = run_shadow_payload_safely(
        run_id="toa-run-disagreement",
        tenant_id="tenant-a",
        source_text="Кабельная муфта",
        primary_result={"canonical_category_id": "electrical.primary.switching.fuse"},
        structured_payload={
            "items": [
                {
                    "item_id": "line-1",
                    "name": "Кабельная муфта",
                    "attributes": {"joint_type": "end"},
                }
            ]
        },
        settings=settings,
        repository_root=repository_root,
    )
    assert summary["status"] == "SHADOW_COMPLETED"
    assert summary["disagreement_count"] == 1
    assert summary["uncertain_count"] == 1
    assert summary["requires_review"] is True
    assert summary["production_effect"] is False


def test_arv067i_version_mismatch_fails_safe(tmp_path: Path) -> None:
    repository_root = _make_release_eligible_copy(tmp_path)
    policy_path = (
        repository_root
        / "schemas"
        / "categories"
        / "electrical"
        / "shadow_runtime_policy.v1.yaml"
    )
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    policy["pins"]["ontology_version"] = "9.9.9"
    policy_path.write_text(
        yaml.safe_dump(policy, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    settings = _settings(
        electrical_ontology_shadow_enabled=True,
        electrical_ontology_shadow_kill_switch=False,
        electrical_ontology_shadow_approval_id="approval-ARV-067I-003",
        electrical_ontology_shadow_allowed_profiles="cable_joint",
        electrical_ontology_shadow_audit_root=str(tmp_path / "audit"),
    )
    summary = run_shadow_payload_safely(
        run_id="toa-run-version-mismatch",
        tenant_id="tenant-a",
        source_text="Кабельная муфта",
        primary_result={"recommendation": "go"},
        settings=settings,
        repository_root=repository_root,
    )
    assert summary["status"] == "SAFE_FAILURE"
    assert summary["reason_codes"] == ["SHADOW_ASSET_COMPATIBILITY_ERROR"]
    assert summary["production_effect"] is False


def test_arv067i_audit_store_partitions_tenants(tmp_path: Path) -> None:
    store = ShadowAuditStore(tmp_path / "audit", max_payload_bytes=32_768)
    payload = {"status": "BLOCKED"}
    first = store.save(tenant_id="tenant-a", run_id="run-1", payload=payload)
    second = store.save(tenant_id="tenant-b", run_id="run-1", payload=payload)
    assert first != second
    assert first.parent.parent.name != second.parent.parent.name
    assert "tenant-a" not in str(first)
    assert "tenant-b" not in str(second)
