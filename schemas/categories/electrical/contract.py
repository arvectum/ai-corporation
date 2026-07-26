"""Structural checks for the isolated ARV-067 electrical ontology."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
LABELS = {"EXACT", "LIKELY_ANALOG", "PARTIAL", "UNCERTAIN", "NO_MATCH"}
COMPARATORS = {"contains", "exact", "maximum", "minimum"}
VALUE_TYPES = {"boolean", "enum", "integer", "number", "set", "string"}
TOP_LEVEL = {
    "ontology_id", "version", "status", "locale", "runtime_import", "purpose",
    "normalization", "match_labels", "match_policy", "reason_codes", "value_sets",
    "categories", "benchmark_contract",
}


class ValidationError(RuntimeError):
    """Stable validation failure used by the command and pytest."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValidationError(f"cannot read {path.relative_to(REPO_ROOT)}: {exc}") from exc
    require(isinstance(value, dict), f"{path.relative_to(REPO_ROOT)} must be an object")
    return value


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read {path.relative_to(REPO_ROOT)}: {exc}") from exc
    require(isinstance(value, dict), f"{path.relative_to(REPO_ROOT)} must be an object")
    return value


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).casefold().replace("ё", "е")
    text = text.replace("×", "x").replace("х", "x").replace("*", "x")
    return re.sub(r"\s+", " ", re.sub(r"[:;()\[\]{}]", " ", text)).strip()


def compact(value: str) -> str:
    return re.sub(r"[^0-9a-zа-я]+", "", normalize(value))


def duplicates(values: list[str]) -> set[str]:
    seen: set[str] = set()
    repeated: set[str] = set()
    for value in values:
        if value in seen:
            repeated.add(value)
        seen.add(value)
    return repeated


def validate_contract(schema: dict[str, Any], ontology: dict[str, Any]) -> None:
    require(schema.get("$schema", "").endswith("2020-12/schema"), "JSON Schema draft mismatch")
    require(schema.get("type") == "object", "schema root must be object")
    require(schema.get("additionalProperties") is False, "schema root must be closed")
    require(set(schema.get("required", [])) == TOP_LEVEL, "schema required keys mismatch")
    require(set(schema.get("properties", {})) == TOP_LEVEL, "schema properties mismatch")
    require(schema["properties"]["runtime_import"].get("const") is False, "runtime gate missing")
    require(set(ontology) == TOP_LEVEL, "ontology top-level keys mismatch")
    require(ontology.get("ontology_id") == "ARV-067-ELECTRICAL", "ontology_id mismatch")
    require(re.fullmatch(r"\d+\.\d+\.\d+", str(ontology.get("version"))) is not None, "version invalid")
    require(ontology.get("status") == "research_asset", "status must be research_asset")
    require(ontology.get("runtime_import") is False, "runtime_import must remain false")
    require(set(ontology.get("match_labels", [])) == LABELS, "match labels mismatch")

    reasons = ontology.get("reason_codes")
    require(isinstance(reasons, list) and len(reasons) >= 8, "reason registry incomplete")
    reason_ids = [str(item.get("id")) for item in reasons]
    require(not duplicates(reason_ids), "duplicate reason code")
    require(all(item.get("outcome") in LABELS for item in reasons), "reason outcome invalid")

    value_sets = ontology.get("value_sets")
    units = ontology.get("normalization", {}).get("units")
    categories = ontology.get("categories")
    require(isinstance(value_sets, dict) and value_sets, "value_sets missing")
    require(isinstance(units, dict) and units, "units missing")
    require(isinstance(categories, list) and len(categories) >= 4, "four categories required")
    category_ids = [str(item.get("id")) for item in categories]
    require(not duplicates(category_ids), "duplicate category id")

    category_aliases: dict[str, str] = {}
    mark_aliases: dict[str, tuple[str, str]] = {}
    for category in categories:
        category_id = str(category.get("id"))
        require(re.fullmatch(r"[a-z][a-z0-9_]+", category_id) is not None, f"bad category id: {category_id}")
        aliases = category.get("category_aliases")
        require(isinstance(aliases, list) and len(aliases) >= 3, f"{category_id}: aliases missing")
        for alias in aliases:
            normalized = normalize(alias)
            previous = category_aliases.setdefault(normalized, category_id)
            require(previous == category_id, f"category alias collision: {alias}")
        marks = category.get("canonical_marks")
        require(isinstance(marks, dict), f"{category_id}: canonical_marks invalid")
        for mark, aliases_for_mark in marks.items():
            require(isinstance(aliases_for_mark, list) and aliases_for_mark, f"{mark}: aliases missing")
            for alias in [mark, *aliases_for_mark]:
                normalized = compact(alias)
                owner = (category_id, str(mark))
                previous = mark_aliases.setdefault(normalized, owner)
                require(previous == owner, f"canonical mark alias collision: {alias}")
        sources = category.get("source_basis")
        require(isinstance(sources, list) and sources, f"{category_id}: source basis missing")
        require(all(str(item.get("url", "")).startswith("https://") for item in sources), "source URL invalid")
        attributes = category.get("attributes")
        require(isinstance(attributes, list) and len(attributes) >= 5, f"{category_id}: attributes incomplete")
        attribute_ids = [str(item.get("id")) for item in attributes]
        require(not duplicates(attribute_ids), f"{category_id}: duplicate attribute id")
        require(sum(bool(item.get("required")) for item in attributes) >= 4, f"{category_id}: required fields weak")
        aliases_by_attribute: dict[str, str] = {}
        for attribute in attributes:
            attribute_id = str(attribute.get("id"))
            require(attribute.get("type") in VALUE_TYPES, f"{category_id}/{attribute_id}: type invalid")
            require(attribute.get("comparator") in COMPARATORS, f"{category_id}/{attribute_id}: comparator invalid")
            if attribute.get("unit"):
                require(attribute["unit"] in units, f"{category_id}/{attribute_id}: unit unknown")
            if attribute.get("value_set"):
                require(attribute["value_set"] in value_sets, f"{category_id}/{attribute_id}: value_set unknown")
            aliases = attribute.get("aliases")
            require(isinstance(aliases, list) and len(aliases) >= 2, f"{category_id}/{attribute_id}: aliases missing")
            for alias in aliases:
                normalized = normalize(alias)
                previous = aliases_by_attribute.setdefault(normalized, attribute_id)
                require(previous == attribute_id, f"{category_id}: attribute alias collision: {alias}")

    gates = ontology.get("benchmark_contract", {}).get("release_gates", {})
    require(gates.get("category_recall_min", 0) >= 0.95, "category recall gate too weak")
    require(gates.get("false_match_rate_max", 1) <= 0.02, "false-match gate too weak")
    require(gates.get("fixture_cases_min", 0) >= 12, "fixture gate too small")
