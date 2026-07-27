#!/usr/bin/env python3
"""Offline evaluator and semantic contract for ARV-067F normative requirements."""
from __future__ import annotations

import re
from typing import Any


def _version_key(value: Any) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", str(value))
    return tuple(int(number) for number in numbers) or (0,)


def _ip_key(value: Any) -> int:
    match = re.search(r"IP\s*(\d+)", str(value), re.IGNORECASE)
    return int(match.group(1)) if match else -1


def _condition_matches(condition: dict[str, Any], context: dict[str, Any]) -> bool | None:
    field = str(condition["field"])
    if field not in context:
        return None
    actual = context[field]
    operator = condition["operator"]
    if operator == "eq":
        return actual == condition["value"]
    if operator == "in":
        return actual in (condition["values"] or [])
    if operator == "contains":
        return set(condition["values"] or []).issubset(set(actual or []))
    if operator == "range":
        limits = condition["range"] or {}
        return limits.get("min") <= actual <= limits.get("max")
    return None


def evaluate_requirement(requirement: dict[str, Any], candidate: dict[str, Any], context: dict[str, Any], evidence_kinds: set[str] | None = None) -> dict[str, Any]:
    """Return an explainable review result, never an automatic compliance decision."""
    evidence_kinds = evidence_kinds or set()
    condition_results = [_condition_matches(row, context) for row in requirement["applies_when"]["all"]]
    if any(result is False for result in condition_results):
        status = "NOT_APPLICABLE"
        reasons = ["NRF_APPLIES_WHEN_FALSE"]
    elif any(result is None for result in condition_results):
        status = "UNCERTAIN"
        reasons = ["NRF_APPLIES_WHEN_CONTEXT_MISSING"]
    else:
        constraint = requirement["constraint"]
        ctype = constraint["type"]
        key = constraint.get("attribute_id") or constraint.get("context_field")
        actual = candidate.get(key) if key else None
        if ctype == "required_evidence":
            ok = constraint.get("evidence_kind") in evidence_kinds
        elif key is None or key not in candidate:
            return {"status":"UNCERTAIN","reason_codes":["NRF_CANDIDATE_VALUE_MISSING"],"requires_review":True,"automatic_compliance_decision":False}
        elif ctype == "allowed_values":
            ok = actual in (constraint.get("values") or [])
        elif ctype == "minimum":
            expected = constraint.get("minimum")
            if str(expected).upper().startswith("IP"):
                ok = _ip_key(actual) >= _ip_key(expected)
            elif isinstance(expected, str):
                ok = _version_key(actual) >= _version_key(expected)
            else:
                ok = actual >= expected
        elif ctype == "maximum":
            ok = actual <= constraint.get("maximum")
        elif ctype == "range":
            limits = constraint.get("range") or {}
            ok = limits.get("min") <= actual <= limits.get("max")
        elif ctype == "marking":
            values = constraint.get("values")
            pattern = constraint.get("marking_pattern")
            ok = actual in values if values else bool(re.fullmatch(pattern or r"(?!)", str(actual)))
        elif ctype == "documentation":
            expected = constraint.get("documentation_reference") or ""
            ok = expected in str(actual)
        else:
            return {"status":"UNCERTAIN","reason_codes":["NRF_UNKNOWN_CONSTRAINT_TYPE"],"requires_review":True,"automatic_compliance_decision":False}
        status = "SATISFIED" if ok else "VIOLATED"
        reasons = ["NRF_CONSTRAINT_SATISFIED" if ok else "NRF_CONSTRAINT_VIOLATED"]
    return {"status":status,"reason_codes":reasons,"requires_review":True,"automatic_compliance_decision":False}
