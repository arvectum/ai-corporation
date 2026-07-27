"""Semantic contract and negative-case evaluator for ARV-067E."""
from __future__ import annotations

import copy
from typing import Any

ERROR_UNKNOWN_CATEGORY = "CATALOG_UNKNOWN_CATEGORY"
ERROR_SERIES_VALUES = "CATALOG_SERIES_EXECUTION_VALUES_FORBIDDEN"
ERROR_MODEL_RANGES = "CATALOG_MODEL_CAPABILITY_RANGE_FORBIDDEN"
ERROR_UNKNOWN_MANUFACTURER = "CATALOG_UNKNOWN_MANUFACTURER"
ERROR_MODEL_CYCLE = "CATALOG_MODEL_REPLACEMENT_CYCLE"
ERROR_EXECUTION_CYCLE = "CATALOG_EXECUTION_REPLACEMENT_CYCLE"
ERROR_OUTSIDE_RANGE = "CATALOG_EXECUTION_OUTSIDE_SERIES_RANGE"
ERROR_UNKNOWN_ATTRIBUTE = "CATALOG_UNKNOWN_ATTRIBUTE"
ERROR_EVIDENCE_MISSING = "CATALOG_EVIDENCE_REFERENCE_MISSING"
ERROR_OFFER_EXECUTION = "CATALOG_OFFER_EXECUTION_UNKNOWN"
ERROR_OFFER_VALUES = "CATALOG_OFFER_TECHNICAL_VALUES_FORBIDDEN"
ERROR_APPROVAL_TARGET = "CATALOG_OPERATOR_APPROVAL_CATEGORY_TARGET_FORBIDDEN"
ERROR_ROSSETTI_TYPE = "CATALOG_ROSSETTI_REQUIRES_OPERATOR_APPROVAL"
ERROR_DUPLICATE = "CATALOG_DUPLICATE_ENTITY_ID"
ERROR_REPLACEMENT_STATE = "CATALOG_REPLACEMENT_SOURCE_NOT_RETIRED"


def by_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row["id"]): row for row in records}


def has_cycle(edges: dict[str, str]) -> bool:
    for start in edges:
        seen: set[str] = set()
        current = start
        while current in edges:
            if current in seen:
                return True
            seen.add(current)
            current = edges[current]
    return False


def _inside(value: Any, lower: float, upper: float) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return lower <= float(value) <= upper
    if isinstance(value, dict) and {"min", "max"} <= set(value):
        return lower <= float(value["min"]) <= float(value["max"]) <= upper
    return True


def validate_semantics(
    catalog: dict[str, list[dict[str, Any]]],
    category_ids: set[str],
    attributes: dict[str, dict[str, Any]],
    units: set[str],
) -> list[str]:
    errors: set[str] = set()
    all_ids = [str(row["id"]) for rows in catalog.values() for row in rows]
    if len(all_ids) != len(set(all_ids)):
        errors.add(ERROR_DUPLICATE)

    manufacturers = by_id(catalog["manufacturers"])
    series = by_id(catalog["series"])
    models = by_id(catalog["models"])
    executions = by_id(catalog["executions"])
    evidence = by_id(catalog["evidence"])

    for row in catalog["series"]:
        if row.get("manufacturer_id") not in manufacturers:
            errors.add(ERROR_UNKNOWN_MANUFACTURER)
        if row.get("category_id") not in category_ids:
            errors.add(ERROR_UNKNOWN_CATEGORY)
        if "attribute_values" in row:
            errors.add(ERROR_SERIES_VALUES)
        for item in row.get("capability_ranges", []):
            attribute = attributes.get(str(item.get("attribute_id")))
            if attribute is None:
                errors.add(ERROR_UNKNOWN_ATTRIBUTE)
                continue
            if float(item["min"]) > float(item["max"]):
                errors.add(ERROR_OUTSIDE_RANGE)
            unit = str(item.get("unit"))
            if unit not in units or unit != attribute.get("canonical_unit"):
                errors.add(ERROR_UNKNOWN_ATTRIBUTE)

    for row in catalog["models"]:
        parent = series.get(str(row.get("series_id")))
        if parent is None:
            errors.add(ERROR_UNKNOWN_MANUFACTURER)
        elif row.get("category_id") != parent.get("category_id"):
            errors.add(ERROR_UNKNOWN_CATEGORY)
        if row.get("category_id") not in category_ids:
            errors.add(ERROR_UNKNOWN_CATEGORY)
        if "capability_ranges" in row or "attribute_values" in row:
            errors.add(ERROR_MODEL_RANGES)
        if row.get("replacement_model_id") and row.get("lifecycle_status") not in {
            "legacy",
            "discontinued",
        }:
            errors.add(ERROR_REPLACEMENT_STATE)

    model_edges = {
        str(row["id"]): str(row["replacement_model_id"])
        for row in catalog["models"]
        if row.get("replacement_model_id") in models
    }
    if has_cycle(model_edges):
        errors.add(ERROR_MODEL_CYCLE)

    for row in catalog["executions"]:
        model = models.get(str(row.get("model_id")))
        if model is None:
            errors.add(ERROR_UNKNOWN_CATEGORY)
            continue
        if (
            row.get("category_id") not in category_ids
            or row.get("category_id") != model.get("category_id")
        ):
            errors.add(ERROR_UNKNOWN_CATEGORY)
        if (
            row.get("replacement_execution_id")
            and row.get("lifecycle_status") != "discontinued"
        ):
            errors.add(ERROR_REPLACEMENT_STATE)

        parent = series.get(str(model.get("series_id")))
        ranges = {
            str(item["attribute_id"]): item
            for item in (parent or {}).get("capability_ranges", [])
        }
        for item in row.get("attribute_values", []):
            attribute_id = str(item.get("attribute_id"))
            attribute = attributes.get(attribute_id)
            if attribute is None:
                errors.add(ERROR_UNKNOWN_ATTRIBUTE)
                continue
            canonical_unit = attribute.get("canonical_unit")
            if canonical_unit is not None:
                if item.get("unit") != canonical_unit or canonical_unit not in units:
                    errors.add(ERROR_UNKNOWN_ATTRIBUTE)
            elif item.get("unit") is not None:
                errors.add(ERROR_UNKNOWN_ATTRIBUTE)
            for evidence_ref in item.get("evidence_refs", []):
                linked = evidence.get(str(evidence_ref))
                if linked is None or linked.get("target_id") != row.get("id"):
                    errors.add(ERROR_EVIDENCE_MISSING)
            declared = ranges.get(attribute_id)
            if declared and not _inside(
                item.get("value"),
                float(declared["min"]),
                float(declared["max"]),
            ):
                errors.add(ERROR_OUTSIDE_RANGE)

    execution_edges = {
        str(row["id"]): str(row["replacement_execution_id"])
        for row in catalog["executions"]
        if row.get("replacement_execution_id") in executions
    }
    if has_cycle(execution_edges):
        errors.add(ERROR_EXECUTION_CYCLE)

    for row in catalog["offers"]:
        if row.get("execution_id") not in executions:
            errors.add(ERROR_OFFER_EXECUTION)
        if "attribute_values" in row or "capability_ranges" in row:
            errors.add(ERROR_OFFER_VALUES)

    targets = {
        "ProductSeries": series,
        "ProductModel": models,
        "ProductExecution": executions,
    }
    for row in catalog["evidence"]:
        evidence_type = str(row.get("evidence_type"))
        target_type = str(row.get("target_type"))
        target = targets.get(target_type)
        if target is None or row.get("target_id") not in target:
            errors.add(
                ERROR_APPROVAL_TARGET
                if evidence_type == "operator_approval"
                else ERROR_EVIDENCE_MISSING
            )
        if evidence_type == "operator_approval" and target_type not in {
            "ProductModel",
            "ProductExecution",
        }:
            errors.add(ERROR_APPROVAL_TARGET)
        if str(row.get("source_document_id")).startswith("rossetti_"):
            if evidence_type != "operator_approval" or row.get("operator_id") != (
                "rossetti"
            ):
                errors.add(ERROR_ROSSETTI_TYPE)
        if evidence_type == "certificate" and not row.get("certificate_number"):
            errors.add(ERROR_EVIDENCE_MISSING)
        if evidence_type == "operator_approval" and not row.get("operator_id"):
            errors.add(ERROR_ROSSETTI_TYPE)
        if any(
            attribute_id not in attributes
            for attribute_id in row.get("supported_attribute_ids", [])
        ):
            errors.add(ERROR_UNKNOWN_ATTRIBUTE)
        if row.get("asserts_real_approval") is not False:
            errors.add(ERROR_ROSSETTI_TYPE)
    return sorted(errors)


def _find(
    catalog: dict[str, list[dict[str, Any]]],
    record_id: str,
) -> dict[str, Any]:
    for rows in catalog.values():
        for row in rows:
            if row.get("id") == record_id:
                return row
    raise KeyError(record_id)


def apply_mutation(
    catalog: dict[str, list[dict[str, Any]]],
    case: dict[str, Any],
) -> None:
    mutation = str(case["mutation"])
    if mutation == "none":
        return
    target = _find(catalog, str(case["target_id"]))
    if mutation == "unknown_category_on_series":
        target["category_id"] = "electrical.fixture.unknown"
    elif mutation == "series_execution_values_forbidden":
        target["attribute_values"] = []
    elif mutation == "model_capability_range_forbidden":
        target["capability_ranges"] = []
    elif mutation == "unknown_manufacturer":
        target["manufacturer_id"] = "manufacturer.fixture.missing"
    elif mutation == "model_replacement_cycle":
        target["replacement_model_id"] = "model.fixture.arv_vector.lbs_old"
    elif mutation == "execution_replacement_cycle":
        target["replacement_execution_id"] = (
            "execution.fixture.arv_vector.lbs_old_630"
        )
    elif mutation == "execution_outside_series_range":
        for item in target["attribute_values"]:
            if item["attribute_id"] == case["attribute_id"]:
                item["value"] = case["value"]
    elif mutation == "unknown_attribute":
        target["attribute_values"][0]["attribute_id"] = case["attribute_id"]
    elif mutation == "missing_evidence_reference":
        target["attribute_values"][0]["evidence_refs"] = [
            "evidence.fixture.missing"
        ]
    elif mutation == "offer_unknown_execution":
        target["execution_id"] = "execution.fixture.missing"
    elif mutation == "offer_technical_values_forbidden":
        target["attribute_values"] = []
    elif mutation == "operator_approval_targets_category":
        target["target_type"] = "Category"
        target["target_id"] = "electrical.primary.insulators"
    elif mutation == "rossetti_wrong_evidence_type":
        target["source_document_id"] = (
            "rossetti_primary_equipment_registry_2026-06-10"
        )
    elif mutation == "duplicate_entity_id":
        catalog["manufacturers"].append(copy.deepcopy(target))
    else:
        raise ValueError(f"unknown mutation: {mutation}")


def run_contract_case(
    base_catalog: dict[str, list[dict[str, Any]]],
    case: dict[str, Any],
    category_ids: set[str],
    attributes: dict[str, dict[str, Any]],
    units: set[str],
) -> list[str]:
    catalog = copy.deepcopy(base_catalog)
    apply_mutation(catalog, case)
    return validate_semantics(catalog, category_ids, attributes, units)
