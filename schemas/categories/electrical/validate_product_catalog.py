#!/usr/bin/env python3
"""Validate ARV-067E electrical product catalog entity contracts."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
FIXTURE_DIR = REPO_ROOT / "fixtures" / "ontology" / "electrical"


class ValidationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"{path.name}: root must be object")
    return value


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"{path.name}: root must be object")
    return value


def unique(values: list[str], label: str) -> None:
    require(len(values) == len(set(values)), f"duplicate {label}")


def validate_closed(
    value: dict[str, Any],
    schema: dict[str, Any],
    label: str,
) -> None:
    required = set(schema.get("required", []))
    properties = set(schema.get("properties", {}))
    require(required <= set(value), f"{label}: missing {sorted(required - set(value))}")
    require(set(value) <= properties, f"{label}: unknown {sorted(set(value) - properties)}")


def load_contract_module():
    path = HERE / "product_catalog_contract.py"
    spec = importlib.util.spec_from_file_location("arv067e_catalog_contract", path)
    require(spec is not None and spec.loader is not None, "contract module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONTRACT = load_contract_module()
has_cycle = CONTRACT.has_cycle
run_contract_case = CONTRACT.run_contract_case
validate_semantics = CONTRACT.validate_semantics


def load_catalog() -> tuple[
    dict[str, Any],
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
]:
    manifest = load_yaml(HERE / "product_catalog.v1.yaml")
    expected_types = {
        "manufacturers": "Manufacturer",
        "series": "ProductSeries",
        "models": "ProductModel",
        "executions": "ProductExecution",
        "offers": "CatalogOffer",
        "evidence": "Evidence",
    }
    catalog: dict[str, list[dict[str, Any]]] = {}
    fragments: dict[str, dict[str, Any]] = {}
    for key, expected_type in expected_types.items():
        relative_path = str(manifest["entity_files"][key])
        require(relative_path.startswith("product_entities/"), f"{key}: path")
        fragment = load_yaml(HERE / relative_path)
        require(fragment["version"] == manifest["version"], f"{key}: version")
        require(fragment["entity_type"] == expected_type, f"{key}: type")
        catalog[key] = fragment["records"]
        fragments[key] = fragment
        require(
            len(catalog[key]) == int(manifest["entity_counts"][key]),
            f"{key}: count",
        )
    return manifest, catalog, fragments


def load_category_ids() -> tuple[dict[str, Any], set[str]]:
    manifest = load_yaml(HERE / "category_tree.v1.yaml")
    nodes = [
        node
        for relative_path in manifest["node_files"]
        for node in load_yaml(HERE / str(relative_path))["nodes"]
    ]
    ids = [str(node["category_id"]) for node in nodes]
    unique(ids, "category id")
    forbidden = {
        "manufacturer_id",
        "series_id",
        "model_id",
        "execution_id",
        "supplier_id",
    }
    require(
        all(not (set(node) & forbidden) for node in nodes),
        "catalog field leaked into category tree",
    )
    return manifest, set(ids)


def load_attributes() -> tuple[dict[str, dict[str, Any]], set[str]]:
    registry = load_yaml(HERE / "attribute_registry.v1.yaml")
    rows = [
        row
        for relative_path in registry["attribute_files"]
        for row in load_yaml(HERE / str(relative_path))["attributes"]
    ]
    ids = [str(row["id"]) for row in rows]
    unique(ids, "attribute id")
    return (
        {str(row["id"]): row for row in rows},
        {str(unit["id"]) for unit in registry["units"]},
    )


def validate_schema_contracts(
    manifest: dict[str, Any],
    fragments: dict[str, dict[str, Any]],
    cases: dict[str, Any],
) -> None:
    manifest_schema = load_json(HERE / "product_catalog.schema.json")
    fragment_schema = load_json(HERE / "product_entity_fragment.schema.json")
    cases_schema = load_json(HERE / "product_catalog_contract_cases.schema.json")
    schemas = {
        "manifest": manifest_schema,
        "fragment": fragment_schema,
        "cases": cases_schema,
    }
    for label, schema in schemas.items():
        require(
            schema.get("$schema", "").endswith("2020-12/schema"),
            f"{label}: draft",
        )
        require(schema.get("type") == "object", f"{label}: root")
        require(schema.get("additionalProperties") is False, f"{label}: open")
    validate_closed(manifest, manifest_schema, "manifest")
    validate_closed(cases, cases_schema, "cases")
    case_schema = cases_schema["properties"]["cases"]["items"]
    for index, case in enumerate(cases["cases"]):
        validate_closed(case, case_schema, f"case[{index}]")

    record_schemas = {
        "manufacturers": fragment_schema["$defs"]["manufacturer"],
        "series": fragment_schema["$defs"]["series"],
        "models": fragment_schema["$defs"]["model"],
        "executions": fragment_schema["$defs"]["execution"],
        "offers": fragment_schema["$defs"]["offer"],
        "evidence": fragment_schema["$defs"]["evidence"],
    }
    for key, fragment in fragments.items():
        validate_closed(fragment, fragment_schema, key)
        schema = record_schemas[key]
        for index, row in enumerate(fragment["records"]):
            validate_closed(row, schema, f"{key}[{index}]")
            validate_closed(
                row["provenance"],
                schema["properties"]["provenance"],
                f"{key}[{index}].provenance",
            )
            if key == "series":
                item_schema = schema["properties"]["capability_ranges"]["items"]
                for item_index, item in enumerate(row["capability_ranges"]):
                    validate_closed(
                        item,
                        item_schema,
                        f"{key}[{index}].range[{item_index}]",
                    )
            if key == "executions":
                item_schema = schema["properties"]["attribute_values"]["items"]
                for item_index, item in enumerate(row["attribute_values"]):
                    validate_closed(
                        item,
                        item_schema,
                        f"{key}[{index}].value[{item_index}]",
                    )


def validate_identifiers(catalog: dict[str, list[dict[str, Any]]]) -> None:
    prefixes = {
        "manufacturers": "manufacturer.fixture.",
        "series": "series.fixture.",
        "models": "model.fixture.",
        "executions": "execution.fixture.",
        "offers": "offer.fixture.",
        "evidence": "evidence.fixture.",
    }
    for key, rows in catalog.items():
        ids = [str(row["id"]) for row in rows]
        unique(ids, f"{key} id")
        for row in rows:
            require(str(row["id"]).startswith(prefixes[key]), f"{row['id']}: id")
            require(row["data_status"] == "synthetic_fixture", f"{row['id']}: data")
            aliases = [str(value) for value in row.get("aliases", [])]
            unique(aliases, f"{row['id']} alias")
    for row in catalog["models"]:
        require(bool(row["factory_designation"]), f"{row['id']}: designation")
        require(bool(row["manufacturer_article"]), f"{row['id']}: article")
    for row in catalog["executions"]:
        require(bool(row["execution_code"]), f"{row['id']}: code")
        require(bool(row["manufacturer_article"]), f"{row['id']}: article")
        require(bool(row["designation_suffixes"]), f"{row['id']}: suffix")


def main() -> int:
    try:
        manifest, catalog, fragments = load_catalog()
        cases = load_yaml(FIXTURE_DIR / "product_catalog_contract_cases.yaml")
        validate_schema_contracts(manifest, fragments, cases)
        validate_identifiers(catalog)
        tree, category_ids = load_category_ids()
        attributes, units = load_attributes()
        require(manifest["runtime_import"] is False, "runtime import")
        require(all(manifest["governance"].values()), "governance")
        require(tree["runtime_import"] is False, "tree runtime")

        errors = validate_semantics(catalog, category_ids, attributes, units)
        require(not errors, f"baseline semantic errors: {errors}")
        unique([str(case["id"]) for case in cases["cases"]], "case id")
        for case in cases["cases"]:
            actual = run_contract_case(
                catalog,
                case,
                category_ids,
                attributes,
                units,
            )
            expected = sorted(str(code) for code in case["expected_error_codes"])
            require(actual == expected, f"{case['id']}: {actual} != {expected}")

        print(
            "ARV-067E product catalog: OK "
            f"(manufacturers={len(catalog['manufacturers'])}, "
            f"series={len(catalog['series'])}, "
            f"models={len(catalog['models'])}, "
            f"executions={len(catalog['executions'])}, "
            f"offers={len(catalog['offers'])}, "
            f"evidence={len(catalog['evidence'])}, "
            f"contract_cases={len(cases['cases'])}, runtime_import=false)"
        )
        return 0
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        print(f"ARV-067E product catalog: FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
