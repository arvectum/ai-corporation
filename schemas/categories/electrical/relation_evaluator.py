#!/usr/bin/env python3
"""Deterministic offline evaluator for ARV-067C relation assertions."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

STATUS_ORDER = {
    "SUPPORTED": 0,
    "CONDITIONAL": 1,
    "UNCERTAIN": 2,
    "NOT_COMPATIBLE": 3,
    "CONFLICT": 4,
}


def endpoint_key(endpoint: dict[str, str]) -> str:
    return f"{endpoint['kind']}:{endpoint['id']}"


def canonical_pair(
    source: dict[str, str],
    target: dict[str, str],
) -> tuple[str, str]:
    left = endpoint_key(source)
    right = endpoint_key(target)
    return (left, right) if left <= right else (right, left)


def _matches(
    assertion: dict[str, Any],
    source: dict[str, str],
    target: dict[str, str],
    relation_type: dict[str, Any],
) -> bool:
    if relation_type["symmetric"]:
        return canonical_pair(assertion["source"], assertion["target"]) == canonical_pair(
            source, target
        )
    return (
        endpoint_key(assertion["source"]) == endpoint_key(source)
        and endpoint_key(assertion["target"]) == endpoint_key(target)
    )


def _single_status(
    assertion: dict[str, Any],
    *,
    satisfied_condition_ids: set[str],
    failed_condition_ids: set[str],
    evidence_confirmed: bool,
) -> tuple[str, set[str]]:
    reasons = set(str(code) for code in assertion["reason_codes"])
    required = {
        str(condition["id"])
        for condition in assertion["conditions"]
        if bool(condition["required"])
    }
    failed = required & failed_condition_ids
    if failed:
        reasons.add("RELATION_CONDITION_FAILED")
        if assertion["failure_outcome"] == "NOT_COMPATIBLE":
            reasons.add("RELATION_EXPLICITLY_INCOMPATIBLE")
            return "NOT_COMPATIBLE", reasons
        return "UNCERTAIN", reasons

    evidence_statuses = {str(item["status"]) for item in assertion["evidence"]}
    if "required_before_use" in evidence_statuses and not evidence_confirmed:
        reasons.add("RELATION_EVIDENCE_MISSING")
        return "UNCERTAIN", reasons

    missing = required - satisfied_condition_ids
    if missing:
        reasons.add("RELATION_CONDITION_MISSING")
        return "CONDITIONAL", reasons

    if assertion["relation_type"] == "not_compatible_with":
        reasons.add("RELATION_EXPLICITLY_INCOMPATIBLE")
        return "NOT_COMPATIBLE", reasons

    if assertion["decision_ceiling"] == "CONDITIONAL":
        reasons.add("RELATION_CATEGORY_LEVEL_CEILING")
        return "CONDITIONAL", reasons

    verified = evidence_statuses <= {"verified_structure", "source_verified"}
    if not verified:
        reasons.add("RELATION_EVIDENCE_MISSING")
        return "UNCERTAIN", reasons

    reasons.add("RELATION_SUPPORTED")
    return "SUPPORTED", reasons


def evaluate_relation(
    source: dict[str, str],
    target: dict[str, str],
    assertions: Iterable[dict[str, Any]],
    relation_types: Iterable[dict[str, Any]],
    *,
    relation_type: str | None = None,
    satisfied_condition_ids: Iterable[str] = (),
    failed_condition_ids: Iterable[str] = (),
    evidence_confirmed: bool = False,
) -> dict[str, Any]:
    """Evaluate a relation without guessing missing evidence or context."""
    types = {str(item["id"]): item for item in relation_types}
    requested = [relation_type] if relation_type else sorted(types)
    matches: list[dict[str, Any]] = []
    for assertion in assertions:
        if not assertion.get("active", False):
            continue
        assertion_type = str(assertion["relation_type"])
        if assertion_type not in requested:
            continue
        if _matches(assertion, source, target, types[assertion_type]):
            matches.append(assertion)

    if not matches:
        return {
            "status": "UNCERTAIN",
            "relation_type": relation_type,
            "relation_ids": [],
            "requires_review": True,
            "reason_codes": ["RELATION_NOT_FOUND"],
        }

    present_types = {str(item["relation_type"]) for item in matches}
    if {"compatible_with", "not_compatible_with"} <= present_types:
        return {
            "status": "CONFLICT",
            "relation_type": relation_type,
            "relation_ids": sorted(str(item["assertion_id"]) for item in matches),
            "requires_review": True,
            "reason_codes": ["RELATION_CONFLICT"],
        }

    satisfied = {str(value) for value in satisfied_condition_ids}
    failed = {str(value) for value in failed_condition_ids}
    evaluated = [
        (
            assertion,
            *_single_status(
                assertion,
                satisfied_condition_ids=satisfied,
                failed_condition_ids=failed,
                evidence_confirmed=evidence_confirmed,
            ),
        )
        for assertion in matches
    ]
    status = max((item[1] for item in evaluated), key=STATUS_ORDER.__getitem__)
    reasons = sorted({reason for _, _, codes in evaluated for reason in codes})
    requires_review = any(bool(assertion["requires_review"]) for assertion, _, _ in evaluated)
    requires_review = requires_review or status != "SUPPORTED"
    resolved_type = relation_type
    if resolved_type is None and len(present_types) == 1:
        resolved_type = next(iter(present_types))
    return {
        "status": status,
        "relation_type": resolved_type,
        "relation_ids": sorted(str(item["assertion_id"]) for item in matches),
        "requires_review": requires_review,
        "reason_codes": reasons,
    }
