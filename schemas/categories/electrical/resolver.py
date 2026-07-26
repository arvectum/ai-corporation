"""Deterministic synonym resolver used only by ARV-067 fixtures."""

from __future__ import annotations

import re
from typing import Any

try:
    from .contract import compact, normalize, require
except ImportError:  # Direct execution through validate.py.
    from contract import compact, normalize, require


def indexes(ontology: dict[str, Any]):
    categories = {item["id"]: item for item in ontology["categories"]}
    marks: dict[str, tuple[str, str]] = {}
    aliases: dict[str, str] = {}
    for category in ontology["categories"]:
        aliases.update({normalize(alias): category["id"] for alias in category["category_aliases"]})
        for mark, mark_aliases in category["canonical_marks"].items():
            for alias in [mark, *mark_aliases]:
                marks[compact(alias)] = (category["id"], mark)
    return categories, marks, aliases


def resolve(text: str, ontology: dict[str, Any]) -> dict[str, Any]:
    normalized, compacted = normalize(text), compact(text)
    categories, marks, aliases = indexes(ontology)
    hits = [(len(alias), owner) for alias, owner in marks.items() if alias in compacted]
    mark = None
    if hits:
        longest = max(length for length, _ in hits)
        owners = {owner for length, owner in hits if length == longest}
        require(len(owners) == 1, f"ambiguous canonical mark: {text}")
        category, mark = next(iter(owners))
    else:
        category_hits = [(len(alias), owner) for alias, owner in aliases.items() if alias in normalized]
        require(category_hits, f"category not resolved: {text}")
        category = max(category_hits)[1]
    require(category in categories, f"unknown category resolved: {category}")
    attrs: dict[str, Any] = {}
    dimension = re.search(r"(?<!\d)(\d+)\s*x\s*(\d+(?:[\.,]\d+)?)", normalized)
    if dimension:
        attrs["conductor_count"] = int(dimension.group(1))
        attrs["cross_section_mm2"] = float(dimension.group(2).replace(",", "."))
    voltage_kv = re.search(
        r"(\d+(?:[\.,]\d+)?(?:\s*/\s*\d+(?:[\.,]\d+)?)?)\s*(?:кв|kv)\b",
        normalized,
    )
    if voltage_kv:
        values = re.split(r"\s*/\s*", voltage_kv.group(1))
        attrs["rated_voltage_kv"] = max(float(value.replace(",", ".")) for value in values)
    if category in {"miniature_circuit_breaker", "electromagnetic_contactor"}:
        pole = re.search(r"(?<!\d)([1-4])\s*(?:p\b|полюс(?:а|ов)?\b)", normalized)
        current = re.search(r"(?<![\wкk])(\d+(?:[\.,]\d+)?)\s*(?:а|a)\b", normalized)
        voltage = re.search(r"(\d+(?:[\.,]\d+)?)\s*(?:в|v)\b", normalized)
        if pole:
            attrs["poles"] = int(pole.group(1))
        if category == "miniature_circuit_breaker":
            if current:
                attrs["rated_current_a"] = float(current.group(1).replace(",", "."))
            breaking = re.search(r"(\d+(?:[\.,]\d+)?)\s*(?:ка|ka)\b", normalized)
            curve = re.search(r"(?:характеристика|кривая)?\s*\b([bcd])\b", normalized)
            if breaking:
                attrs["breaking_capacity_ka"] = float(breaking.group(1).replace(",", "."))
            if voltage:
                attrs["rated_voltage_v"] = float(voltage.group(1).replace(",", "."))
            if curve:
                attrs["trip_curve"] = curve.group(1).upper()
        else:
            if current:
                attrs["rated_operational_current_a"] = float(current.group(1).replace(",", "."))
            if voltage:
                attrs["coil_voltage_v"] = float(voltage.group(1).replace(",", "."))
            if re.search(r"\bdc\b|постоянн", normalized):
                attrs["coil_current_type"] = "DC"
            elif re.search(r"\bac\b|переменн", normalized):
                attrs["coil_current_type"] = "AC"
            use = re.search(r"\b(?:ac|ас)\s*-?\s*([134])\b", normalized)
            if use:
                attrs["utilization_category"] = f"AC-{use.group(1)}"
    return {"category": category, "canonical_mark": mark, "attributes": attrs}
