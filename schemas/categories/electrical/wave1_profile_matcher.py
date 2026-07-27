#!/usr/bin/env python3
"""Deterministic offline matcher for ARV-067D wave-1 detailed profiles."""
from __future__ import annotations

from typing import Any


STATUS_REASON = {
    "EXACT": "PROFILE_EXACT",
    "LIKELY_ANALOG": "PROFILE_LIKELY_ANALOG",
    "PARTIAL": "PROFILE_PARTIAL",
    "UNCERTAIN": "PROFILE_UNCERTAIN",
    "NO_MATCH": "PROFILE_NO_MATCH",
}


def _normal(value: Any) -> Any:
    if isinstance(value, str):
        return " ".join(value.strip().lower().replace("ё", "е").split())
    if isinstance(value, list):
        return [_normal(item) for item in value]
    return value


def compare_values(comparator: str, requested: Any, candidate: Any) -> bool:
    if comparator == "exact":
        left, right = _normal(requested), _normal(candidate)
        if isinstance(left, list) and isinstance(right, list):
            return sorted(left, key=str) == sorted(right, key=str)
        return left == right
    if comparator == "minimum":
        return float(candidate) >= float(requested)
    if comparator == "maximum":
        return float(candidate) <= float(requested)
    if comparator == "contains":
        required = {_normal(item) for item in requested}
        offered = {_normal(item) for item in candidate}
        return required.issubset(offered)
    if comparator == "range_overlap":
        req_low, req_high = map(float, requested)
        cand_low, cand_high = map(float, candidate)
        return max(req_low, cand_low) <= min(req_high, cand_high)
    raise ValueError(f"unsupported comparator: {comparator}")


def evaluate_profile(
    profile: dict[str, Any],
    requested: dict[str, Any],
    candidate: dict[str, Any],
    *,
    evidence_confirmed: bool,
) -> dict[str, Any]:
    critical_mismatch: list[str] = []
    critical_missing: list[str] = []
    required_mismatch: list[str] = []
    required_missing: list[str] = []
    optional_mismatch: list[str] = []
    optional_missing: list[str] = []
    reasons: set[str] = {
        "HUMAN_REVIEW_REQUIRED",
        "NORMATIVE_APPLICABILITY_NOT_VERIFIED",
    }

    for rule in profile["attributes"]:
        attr_id = str(rule["id"])
        requirement = str(rule["requirement"])
        critical = bool(rule["critical"])
        if attr_id not in requested:
            continue
        if attr_id not in candidate:
            if critical:
                critical_missing.append(attr_id)
                reasons.add("MISSING_CRITICAL_ATTRIBUTE")
            elif requirement == "required":
                required_missing.append(attr_id)
                reasons.add("MISSING_REQUIRED_ATTRIBUTE")
            else:
                optional_missing.append(attr_id)
                reasons.add("MISSING_OPTIONAL_ATTRIBUTE")
            continue
        if compare_values(str(rule["comparator"]), requested[attr_id], candidate[attr_id]):
            continue
        reasons.add(str(rule["mismatch_reason_code"]))
        if critical:
            critical_mismatch.append(attr_id)
            reasons.add("CRITICAL_ATTRIBUTE_MISMATCH")
        elif requirement == "required":
            required_mismatch.append(attr_id)
            reasons.add("REQUIRED_ATTRIBUTE_MISMATCH")
        else:
            optional_mismatch.append(attr_id)
            reasons.add("OPTIONAL_ATTRIBUTE_MISMATCH")

    if critical_mismatch:
        status = "NO_MATCH"
    elif critical_missing:
        status = "UNCERTAIN"
    elif not evidence_confirmed:
        status = "UNCERTAIN"
        reasons.add("PROFILE_EVIDENCE_MISSING")
    elif required_mismatch or required_missing:
        status = "PARTIAL"
    elif optional_mismatch or optional_missing:
        status = "LIKELY_ANALOG"
    else:
        status = "EXACT"

    reasons.add(STATUS_REASON[status])
    return {
        "profile_id": profile["id"],
        "status": status,
        "reason_codes": sorted(reasons),
        "requires_review": True,
        "critical_mismatch_attributes": sorted(critical_mismatch),
        "critical_missing_attributes": sorted(critical_missing),
        "required_issue_attributes": sorted(required_mismatch + required_missing),
        "optional_issue_attributes": sorted(optional_mismatch + optional_missing),
    }
