"""Explainable matching policy used only by ARV-067 fixtures."""

from __future__ import annotations

import math
from typing import Any, NamedTuple

try:
    from .contract import ValidationError, require
    from .resolver import indexes
except ImportError:  # Direct execution through validate.py.
    from contract import ValidationError, require
    from resolver import indexes


class MatchResult(NamedTuple):
    label: str
    reasons: tuple[str, ...]


def compatible(comparator: str, required: Any, candidate: Any) -> bool:
    if comparator == "exact":
        if isinstance(required, (int, float)) and isinstance(candidate, (int, float)):
            return math.isclose(float(required), float(candidate), rel_tol=1e-6, abs_tol=1e-6)
        return required == candidate
    if comparator == "minimum":
        return float(candidate) >= float(required)
    if comparator == "maximum":
        return float(candidate) <= float(required)
    if comparator == "contains":
        return set(candidate) >= set(required)
    raise ValidationError(f"unsupported comparator: {comparator}")


def evaluate(
    requirement: dict[str, Any],
    candidate: dict[str, Any],
    ontology: dict[str, Any],
) -> MatchResult:
    if requirement.get("category") != candidate.get("category"):
        return MatchResult("NO_MATCH", ("CATEGORY_MISMATCH",))
    categories, _, _ = indexes(ontology)
    category = str(requirement.get("category"))
    require(category in categories, f"fixture category unknown: {category}")
    specs = {item["id"]: item for item in categories[category]["attributes"]}
    required_attrs = requirement.get("attributes", {})
    candidate_attrs = candidate.get("attributes", {})
    missing_required = False
    optional_issue = False
    for key, expected in required_attrs.items():
        require(key in specs, f"{category}: fixture attribute unknown: {key}")
        spec = specs[key]
        if key not in candidate_attrs:
            missing_required |= bool(spec["required"])
            optional_issue |= not bool(spec["required"])
            continue
        if not compatible(spec["comparator"], expected, candidate_attrs[key]):
            if spec["required"]:
                if spec["comparator"] == "contains":
                    reason = "SET_NOT_CONTAINED"
                elif spec["comparator"] in {"minimum", "maximum"}:
                    reason = "MINIMUM_CAPABILITY_INSUFFICIENT"
                else:
                    reason = "EXACT_VALUE_MISMATCH"
                return MatchResult("NO_MATCH", (reason,))
            optional_issue = True
    if missing_required:
        return MatchResult("UNCERTAIN", ("REQUIRED_ATTRIBUTE_MISSING",))
    if optional_issue:
        missing = any(
            key not in candidate_attrs and not specs[key]["required"]
            for key in required_attrs
        )
        reason = "OPTIONAL_ATTRIBUTE_MISSING" if missing else "OPTIONAL_VALUE_MISMATCH"
        return MatchResult("PARTIAL", (reason,))
    required_mark = requirement.get("canonical_mark")
    candidate_mark = candidate.get("canonical_mark")
    if required_mark and candidate_mark and required_mark == candidate_mark:
        return MatchResult("EXACT", ("ALL_REQUESTED_ATTRIBUTES_MATCH",))
    if required_mark and candidate_mark:
        return MatchResult("LIKELY_ANALOG", ("CANONICAL_MARK_DIFFERS",))
    return MatchResult("LIKELY_ANALOG", ("CANONICAL_MARK_MISSING",))
