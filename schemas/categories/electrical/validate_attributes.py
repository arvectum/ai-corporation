#!/usr/bin/env python3
"""Validate the ARV-067A cross-category electrical attribute registry."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]


class ValidationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"{path.name}: root must be an object")
    return value


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"{path.name}: root must be an object")
    return value


def unique(values: list[str], label: str) -> None:
    require(len(values) == len(set(values)), f"duplicate {label}")


def validate_schema_contract(
    schema: dict[str, Any],
    value: dict[str, Any],
    label: str,
) -> None:
    require(
        schema.get("$schema", "").endswith("2020-12/schema"),
        f"{label}: schema draft mismatch",
    )
    require(schema.get("type") == "object", f"{label}: schema root must be object")
    require(schema.get("additionalProperties") is False, f"{label}: schema root must be closed")
    required = set(schema.get("required", []))
    properties = set(schema.get("properties", {}))
    require(required == set(value), f"{label}: schema required keys differ from data")
    require(properties == set(value), f"{label}: schema properties differ from data")


def load_attribute_fragments(
    registry: dict[str, Any],
    fragment_schema: dict[str, Any],
) -> list[dict[str, Any]]:
    paths = [str(value) for value in registry["attribute_files"]]
    unique(paths, "attribute fragment path")
    rows: list[dict[str, Any]] = []
    fragment_ids: list[str] = []
    for relative_path in paths:
        require(relative_path.startswith("attributes/"), f"invalid fragment path: {relative_path}")
        fragment = load_yaml(HERE / relative_path)
        validate_schema_contract(fragment_schema, fragment, relative_path)
        fragment_ids.append(str(fragment["fragment_id"]))
        require(fragment["version"] == registry["version"], f"{relative_path}: version mismatch")
        rows.extend(fragment["attributes"])
    unique(fragment_ids, "attribute fragment id")
    return rows


def validate_units(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    units = registry["units"]
    require(isinstance(units, list) and len(units) >= 15, "unit registry is too small")
    ids = [str(item["id"]) for item in units]
    unique(ids, "unit id")
    by_id = {str(item["id"]): item for item in units}
    for unit in units:
        require(float(unit["factor_to_base"]) > 0, f"{unit['id']}: conversion factor must be positive")
        require(unit["base_unit"] in by_id, f"{unit['id']}: unknown base unit {unit['base_unit']}")
        base = by_id[unit["base_unit"]]
        require(unit["dimension"] == base["dimension"], f"{unit['id']}: base-unit dimension mismatch")
        require(float(base["factor_to_base"]) == 1, f"{unit['id']}: base unit must have factor 1")
    return by_id


def validate_comparators(registry: dict[str, Any]) -> dict[str, set[str]]:
    expected = {"exact", "minimum", "maximum", "contains", "range_overlap"}
    rows = registry["comparators"]
    ids = [str(item["id"]) for item in rows]
    unique(ids, "comparator id")
    require(set(ids) == expected, "comparator set mismatch")
    return {str(item["id"]): set(item["allowed_value_types"]) for item in rows}


def validate_attributes(
    registry: dict[str, Any],
    units: dict[str, dict[str, Any]],
    comparators: dict[str, set[str]],
) -> dict[str, dict[str, Any]]:
    attributes = registry["_loaded_attributes"]
    require(isinstance(attributes, list) and len(attributes) >= 100, "attribute registry is too small")
    ids = [str(item["id"]) for item in attributes]
    unique(ids, "attribute id")
    by_id = {str(item["id"]): item for item in attributes}
    value_sets = registry["value_sets"]
    unit_optional = {
        "conductor_count",
        "poles",
        "din_modules",
        "circuit_count",
        "optical_fibre_count",
        "pole_count",
        "earthing_switch_count",
        "phase_count",
        "cell_count",
    }

    for item in attributes:
        attr_id = str(item["id"])
        value_type = str(item["value_type"])
        comparator = str(item["default_comparator"])
        require(value_type in comparators[comparator], f"{attr_id}: comparator/value-type mismatch")
        unit = item.get("canonical_unit")
        if unit is not None:
            require(unit in units, f"{attr_id}: unknown unit {unit}")
        if value_type in {"number", "integer", "range"}:
            require(unit is not None or attr_id in unit_optional, f"{attr_id}: numeric/range attribute needs unit")
        if value_type == "boolean":
            require(unit in {None, "boolean"}, f"{attr_id}: boolean unit invalid")
        if "value_set_ref" in item:
            require(item["value_set_ref"] in value_sets, f"{attr_id}: unknown value set")
            require(value_type == "enum", f"{attr_id}: value set requires enum")
        if "allowed_values" in item:
            require(value_type in {"enum", "set"}, f"{attr_id}: allowed values require enum/set")
        if item["maturity"] == "verified_detailed_profile":
            require(
                item["provenance"]["basis"] == "explicit_attribute_definition",
                f"{attr_id}: verified provenance mismatch",
            )
        if item["maturity"] == "provisional_taxonomy":
            require(
                item["provenance"]["basis"] == "discriminator_id_and_engineering_inference",
                f"{attr_id}: provisional provenance mismatch",
            )

    role_specific_voltage = {
        "rated_voltage_kv",
        "rated_voltage_v",
        "rated_operational_voltage_v",
        "coil_voltage_v",
        "highest_voltage_kv",
        "primary_voltage_kv",
        "secondary_voltage_kv",
        "continuous_operating_voltage_kv",
        "nominal_voltage_v",
        "input_voltage_v",
        "output_voltage_v",
    }
    require(role_specific_voltage.issubset(by_id), "role-specific voltage attributes missing")
    require(
        registry["governance"]["role_specific_voltage_attributes_must_not_be_auto_merged"] is True,
        "voltage-role guard missing",
    )
    return by_id


def validate_nomenclature(
    nomenclature: dict[str, Any],
    attributes: dict[str, dict[str, Any]],
) -> set[str]:
    discriminator_ids: set[str] = set()
    for section in nomenclature["sections"]:
        for attr_id in section["discriminator_attributes"]:
            discriminator_ids.add(str(attr_id))
    missing = sorted(discriminator_ids - set(attributes))
    require(not missing, f"nomenclature discriminator attributes missing: {missing}")
    return discriminator_ids


def validate_detailed_profiles(
    ontology: dict[str, Any],
    registry: dict[str, Any],
    attributes: dict[str, dict[str, Any]],
) -> tuple[set[str], set[str]]:
    detailed_ids: set[str] = set()
    profile_ids = {str(category["id"]) for category in ontology["categories"]}
    aliases = registry["profile_id_aliases"]
    for alias, target in aliases.items():
        require(target in profile_ids, f"profile alias {alias}: unknown target {target}")

    for category in ontology["categories"]:
        for source_attr in category["attributes"]:
            attr_id = str(source_attr["id"])
            detailed_ids.add(attr_id)
            require(attr_id in attributes, f"detailed attribute missing: {attr_id}")
            canonical = attributes[attr_id]
            require(
                canonical["maturity"] == "verified_detailed_profile",
                f"{attr_id}: detailed attribute is not verified",
            )
            require(canonical["value_type"] == source_attr["type"], f"{attr_id}: value type differs")
            require(
                canonical["default_comparator"] == source_attr["comparator"],
                f"{attr_id}: comparator differs",
            )
            require(canonical.get("canonical_unit") == source_attr.get("unit"), f"{attr_id}: unit differs")
            require(canonical.get("value_set_ref") == source_attr.get("value_set"), f"{attr_id}: value set differs")
            source_allowed = source_attr.get("allowed_values")
            if source_allowed is not None:
                require(canonical.get("allowed_values") == source_allowed, f"{attr_id}: allowed values differ")
            require(
                set(source_attr.get("aliases", [])).issubset(set(canonical["aliases"])),
                f"{attr_id}: canonical aliases incomplete",
            )
    return detailed_ids, profile_ids


def validate_catalog_profile_refs(
    nomenclature: dict[str, Any],
    registry: dict[str, Any],
    profile_ids: set[str],
) -> None:
    aliases = registry["profile_id_aliases"]
    for section in nomenclature["sections"]:
        for profile_ref in section["detailed_profile_refs"]:
            resolved = aliases.get(profile_ref, profile_ref)
            require(resolved in profile_ids, f"{section['id']}: unresolved detailed profile {profile_ref}")


def validate_runtime_boundary() -> None:
    forbidden = {
        "attribute_registry.v1.yaml",
        "ARV-067A-ELECTRICAL-ATTRIBUTE-REGISTRY",
    }
    hits: list[str] = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                hits.append(f"{path.relative_to(REPO_ROOT)}:{token}")
    require(not hits, f"production runtime imports attribute registry: {hits}")


def main() -> int:
    try:
        registry = load_yaml(HERE / "attribute_registry.v1.yaml")
        schema = load_json(HERE / "attribute_registry.schema.json")
        fragment_schema = load_json(HERE / "attribute_fragment.schema.json")
        ontology = load_yaml(HERE / "electrical.v1.yaml")
        nomenclature = load_yaml(HERE / "nomenclature.v1.yaml")

        required = {
            "registry_id",
            "version",
            "status",
            "locale",
            "runtime_import",
            "purpose",
            "source_assets",
            "profile_id_aliases",
            "attribute_files",
            "units",
            "comparators",
            "value_sets",
            "governance",
        }
        require(set(registry) == required, "registry top-level keys mismatch")
        require(registry["registry_id"] == "ARV-067A-ELECTRICAL-ATTRIBUTE-REGISTRY", "registry id mismatch")
        require(re.fullmatch(r"\d+\.\d+\.\d+", str(registry["version"])) is not None, "version invalid")
        require(registry["runtime_import"] is False, "registry must remain offline")
        validate_schema_contract(schema, registry, "attribute registry")
        registry["_loaded_attributes"] = load_attribute_fragments(registry, fragment_schema)
        units = validate_units(registry)
        comparators = validate_comparators(registry)
        attributes = validate_attributes(registry, units, comparators)
        discriminator_ids = validate_nomenclature(nomenclature, attributes)
        detailed_ids, profile_ids = validate_detailed_profiles(ontology, registry, attributes)
        validate_catalog_profile_refs(nomenclature, registry, profile_ids)
        validate_runtime_boundary()
    except (OSError, json.JSONDecodeError, yaml.YAMLError, KeyError, TypeError, ValueError, ValidationError) as exc:
        print(f"ARV-067A attribute registry: FAILED: {exc}", file=sys.stderr)
        return 1

    loaded = registry["_loaded_attributes"]
    verified = sum(item["maturity"] == "verified_detailed_profile" for item in loaded)
    provisional = sum(item["maturity"] == "provisional_taxonomy" for item in loaded)
    print(
        "ARV-067A attribute registry: OK "
        f"(attributes={len(attributes)}, verified={verified}, provisional={provisional}, "
        f"discriminators={len(discriminator_ids)}, detailed={len(detailed_ids)}, runtime_import=false)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
