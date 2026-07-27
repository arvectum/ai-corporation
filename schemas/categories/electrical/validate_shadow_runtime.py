#!/usr/bin/env python3
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[3]
DIRECTORY = ROOT / "schemas" / "categories" / "electrical"
FIXTURE_PATH = ROOT / "fixtures" / "ontology" / "electrical" / "shadow_runtime_contract_cases.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def validate_policy(policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    flags = policy.get("feature_flags") or {}
    gates = policy.get("activation_gates") or {}
    pins = policy.get("pins") or {}
    audit = policy.get("audit") or {}
    safety = policy.get("safety") or {}
    promotion = policy.get("promotion") or {}
    rollback = policy.get("rollback") or {}
    limits = policy.get("limits") or {}
    current = policy.get("current_state") or {}

    if flags.get("enabled_by_default") is not False:
        errors.append("SHADOW_DEFAULT_MUST_BE_DISABLED")
    if flags.get("kill_switch_active_by_default") is not True:
        errors.append("SHADOW_DEFAULT_KILL_SWITCH_REQUIRED")
    if gates.get("arv067h_release_gate_required") is not True:
        errors.append("SHADOW_ARV067H_GATE_REQUIRED")
    if gates.get("independent_acceptance_required") is not True:
        errors.append("SHADOW_INDEPENDENT_ACCEPTANCE_REQUIRED")
    if gates.get("explicit_operator_approval_required") is not True:
        errors.append("SHADOW_OPERATOR_APPROVAL_REQUIRED")
    if flags.get("explicit_profile_allowlist_required") is not True:
        errors.append("SHADOW_PROFILE_ALLOWLIST_REQUIRED")
    if not pins.get("ontology_version") or not pins.get("benchmark_version"):
        errors.append("SHADOW_VERSION_PIN_REQUIRED")
    if audit.get("raw_source_storage_forbidden") is not True:
        errors.append("SHADOW_RAW_SOURCE_STORAGE_FORBIDDEN")
    if audit.get("tenant_partition_required") is not True:
        errors.append("SHADOW_TENANT_PARTITION_REQUIRED")
    if audit.get("redaction_required") is not True:
        errors.append("SHADOW_REDACTION_REQUIRED")
    if safety.get("primary_result_mutation_forbidden") is not True:
        errors.append("SHADOW_PRIMARY_MUTATION_FORBIDDEN")
    if safety.get("external_actions_forbidden") is not True:
        errors.append("SHADOW_EXTERNAL_ACTIONS_FORBIDDEN")
    if promotion.get("production_activation_implemented") is not False:
        errors.append("SHADOW_PRODUCTION_ACTIVATION_FORBIDDEN")
    if promotion.get("automatic_promotion_forbidden") is not True:
        errors.append("SHADOW_AUTOMATIC_PROMOTION_FORBIDDEN")
    if rollback.get("kill_switch_required") is not True:
        errors.append("SHADOW_ROLLBACK_KILL_SWITCH_REQUIRED")

    source_limit = limits.get("max_source_chars_default")
    item_limit = limits.get("max_items_default")
    audit_limit = limits.get("max_audit_bytes_default")
    if not isinstance(source_limit, int) or not 1000 <= source_limit <= 100000:
        errors.append("SHADOW_LIMIT_OUT_OF_RANGE")
    if not isinstance(item_limit, int) or not 1 <= item_limit <= 256:
        errors.append("SHADOW_LIMIT_OUT_OF_RANGE")
    if not isinstance(audit_limit, int) or not 16384 <= audit_limit <= 1048576:
        errors.append("SHADOW_LIMIT_OUT_OF_RANGE")

    if (
        current.get("arv067h_release_gate_passed") is not False
        or current.get("independently_accepted_profiles") != 0
        or current.get("shadow_execution_allowed") is not False
        or current.get("production_effect") is not False
    ):
        errors.append("SHADOW_CURRENT_STATE_MUST_REMAIN_BLOCKED")
    return sorted(set(errors))


def _mutate(policy: dict[str, Any], mutation: str) -> dict[str, Any]:
    value = copy.deepcopy(policy)
    if mutation == "none":
        return value
    if mutation == "enabled_by_default":
        value["feature_flags"]["enabled_by_default"] = True
    elif mutation == "kill_switch_off_by_default":
        value["feature_flags"]["kill_switch_active_by_default"] = False
    elif mutation == "benchmark_gate_removed":
        value["activation_gates"]["arv067h_release_gate_required"] = False
    elif mutation == "independent_acceptance_removed":
        value["activation_gates"]["independent_acceptance_required"] = False
    elif mutation == "operator_approval_removed":
        value["activation_gates"]["explicit_operator_approval_required"] = False
    elif mutation == "allowlist_removed":
        value["feature_flags"]["explicit_profile_allowlist_required"] = False
    elif mutation == "unpinned_ontology_version":
        value["pins"]["ontology_version"] = ""
    elif mutation == "raw_source_storage_enabled":
        value["audit"]["raw_source_storage_forbidden"] = False
    elif mutation == "tenant_partition_disabled":
        value["audit"]["tenant_partition_required"] = False
    elif mutation == "redaction_disabled":
        value["audit"]["redaction_required"] = False
    elif mutation == "primary_mutation_enabled":
        value["safety"]["primary_result_mutation_forbidden"] = False
    elif mutation == "external_actions_enabled":
        value["safety"]["external_actions_forbidden"] = False
    elif mutation == "production_promotion_enabled":
        value["promotion"]["production_activation_implemented"] = True
    elif mutation == "automatic_promotion_enabled":
        value["promotion"]["automatic_promotion_forbidden"] = False
    elif mutation == "no_kill_switch_rollback":
        value["rollback"]["kill_switch_required"] = False
    elif mutation == "invalid_source_limit":
        value["limits"]["max_source_chars_default"] = 9999999
    elif mutation == "current_state_claims_active":
        value["current_state"]["shadow_execution_allowed"] = True
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    return value


def main() -> int:
    policy = _load_yaml(DIRECTORY / "shadow_runtime_policy.v1.yaml")
    schema = __import__("json").loads(
        (DIRECTORY / "shadow_runtime_policy.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(policy, schema)
    assert validate_policy(policy) == []

    fixtures = _load_yaml(FIXTURE_PATH)
    cases = fixtures.get("cases") or []
    assert len(cases) == 18
    for case in cases:
        actual = validate_policy(_mutate(policy, str(case["mutation"])))
        expected = sorted(case.get("expected_error_codes") or [])
        assert actual == expected, (case["id"], actual, expected)

    print(
        "ARV-067I shadow runtime: OK "
        "(policy=1, fixture_cases=18, feature_default=false, kill_switch_default=true, "
        "release_gate=false, accepted_profiles=0, production_effect=false)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
