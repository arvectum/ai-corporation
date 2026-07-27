#!/usr/bin/env python3
"""Validate the ARV-067C electrical relation graph and safety boundaries."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent


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


def validate_closed(value: dict[str, Any], schema: dict[str, Any], label: str) -> None:
    required = set(schema.get("required", []))
    properties = set(schema.get("properties", {}))
    require(required <= set(value), f"{label}: missing keys {sorted(required - set(value))}")
    require(set(value) <= properties, f"{label}: unknown keys {sorted(set(value) - properties)}")


def load_category_contract() -> dict[str, dict[str, Any]]:
    manifest = load_yaml(HERE / "category_tree.v1.yaml")
    nodes: list[dict[str, Any]] = []
    for relative_path in manifest["node_files"]:
        fragment = load_yaml(HERE / str(relative_path))
        nodes.extend(fragment["nodes"])
    ids = [str(node["category_id"]) for node in nodes]
    unique(ids, "category endpoint")
    return {str(node["category_id"]): node for node in nodes}


def load_attribute_ids() -> set[str]:
    registry = load_yaml(HERE / "attribute_registry.v1.yaml")
    ids: list[str] = []
    for relative_path in registry["attribute_files"]:
        fragment = load_yaml(HERE / str(relative_path))
        ids.extend(str(item["id"]) for item in fragment["attributes"])
    unique(ids, "attribute id")
    return set(ids)


def load_normative_ids() -> set[str]:
    registry = load_yaml(HERE / "normative_registry.v1.yaml")
    ids = [str(item["id"]) for item in registry["documents"]]
    unique(ids, "normative document id")
    return set(ids)


def load_assertions(
    manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fragments: list[dict[str, Any]] = []
    assertions: list[dict[str, Any]] = []
    fragment_ids: list[str] = []
    for relative_path in manifest["assertion_files"]:
        fragment = load_yaml(HERE / str(relative_path))
        require(fragment["version"] == manifest["version"], f"{relative_path}: version mismatch")
        require(isinstance(fragment["assertions"], list), f"{relative_path}: assertions")
        fragments.append(fragment)
        assertions.extend(fragment["assertions"])
        fragment_ids.append(str(fragment["fragment_id"]))
    unique(fragment_ids, "relation fragment id")
    return fragments, assertions


def validate_schemas(
    manifest: dict[str, Any],
    type_registry: dict[str, Any],
    component_registry: dict[str, Any],
    fragments: list[dict[str, Any]],
) -> None:
    manifest_schema = load_json(HERE / "relation_graph.schema.json")
    type_schema = load_json(HERE / "relation_type_registry.schema.json")
    component_schema = load_json(HERE / "component_role_registry.schema.json")
    assertion_schema = load_json(HERE / "relation_assertion_fragment.schema.json")
    for label, schema in (
        ("manifest", manifest_schema),
        ("type registry", type_schema),
        ("component registry", component_schema),
        ("assertion fragment", assertion_schema),
    ):
        require(schema.get("$schema", "").endswith("2020-12/schema"), f"{label}: schema draft")
        require(schema.get("type") == "object", f"{label}: schema root")
        require(schema.get("additionalProperties") is False, f"{label}: schema must be closed")

    validate_closed(manifest, manifest_schema, "manifest")
    validate_closed(type_registry, type_schema, "type registry")
    validate_closed(component_registry, component_schema, "component registry")

    type_item_schema = type_schema["properties"]["types"]["items"]
    for relation_type in type_registry["types"]:
        validate_closed(relation_type, type_item_schema, f"type:{relation_type.get('id')}")

    component_item_schema = component_schema["properties"]["components"]["items"]
    provenance_schema = component_item_schema["properties"]["provenance"]
    for component in component_registry["components"]:
        component_id = str(component.get("id", "component"))
        validate_closed(component, component_item_schema, component_id)
        validate_closed(component["provenance"], provenance_schema, f"{component_id}.provenance")

    assertion_item_schema = assertion_schema["properties"]["assertions"]["items"]
    for fragment in fragments:
        validate_closed(fragment, assertion_schema, str(fragment["fragment_id"]))
        for assertion in fragment["assertions"]:
            assertion_id = str(assertion.get("assertion_id", "assertion"))
            validate_closed(assertion, assertion_item_schema, assertion_id)
            validate_closed(assertion["source"], assertion_item_schema["properties"]["source"], f"{assertion_id}.source")
            validate_closed(assertion["target"], assertion_item_schema["properties"]["target"], f"{assertion_id}.target")
            condition_schema = assertion_item_schema["properties"]["conditions"]["items"]
            for condition in assertion["conditions"]:
                validate_closed(condition, condition_schema, f"{assertion_id}.condition:{condition.get('id')}")
            evidence_schema = assertion_item_schema["properties"]["evidence"]["items"]
            for evidence in assertion["evidence"]:
                validate_closed(evidence, evidence_schema, f"{assertion_id}.evidence:{evidence.get('evidence_id')}")
            validate_closed(
                assertion["provenance"],
                assertion_item_schema["properties"]["provenance"],
                f"{assertion_id}.provenance",
            )


def validate_manifest(manifest: dict[str, Any]) -> None:
    require(manifest["graph_id"] == "ARV-067C-ELECTRICAL-RELATION-GRAPH", "graph id")
    require(re.fullmatch(r"\d+\.\d+\.\d+", str(manifest["version"])) is not None, "version")
    require(manifest["runtime_import"] is False, "runtime import")
    require(all(bool(value) for value in manifest["governance"].values()), "governance")
    reason_codes = [str(value) for value in manifest["reason_codes"]]
    unique(reason_codes, "reason code")
    required_codes = {
        "RELATION_SUPPORTED",
        "RELATION_CONDITIONAL",
        "RELATION_EVIDENCE_MISSING",
        "RELATION_EXPLICITLY_INCOMPATIBLE",
        "RELATION_CONFLICT",
        "RELATION_NOT_FOUND",
        "RELATION_REPLACEMENT_CYCLE",
        "RELATION_CONDITION_MISSING",
        "RELATION_CONDITION_FAILED",
        "RELATION_PRODUCT_EVIDENCE_REQUIRED",
    }
    require(required_codes <= set(reason_codes), "required reason codes")


def validate_types(
    manifest: dict[str, Any],
    registry: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    require(registry["version"] == manifest["version"], "type registry version")
    expected = {
        "part_of",
        "accessory_for",
        "compatible_with",
        "requires",
        "replaces",
        "alternative_to",
        "not_compatible_with",
        "approved_for",
        "governed_by",
    }
    ids = [str(item["id"]) for item in registry["types"]]
    unique(ids, "relation type id")
    require(set(ids) == expected, "relation type set")
    by_id = {str(item["id"]): item for item in registry["types"]}
    reason_codes = set(str(value) for value in manifest["reason_codes"])

    symmetric = {"compatible_with", "alternative_to", "not_compatible_with"}
    for type_id, item in by_id.items():
        require(item["reason_code"] in reason_codes, f"{type_id}: reason code")
        require(item["equivalence_semantics"] is False, f"{type_id}: equivalence semantics")
        require(item["absence_outcome"] == "UNCERTAIN", f"{type_id}: absence outcome")
        require(item["evidence_required"] is True, f"{type_id}: evidence requirement")
        unique([str(value) for value in item["source_kinds"]], f"{type_id} source kind")
        unique([str(value) for value in item["target_kinds"]], f"{type_id} target kind")
        if type_id in symmetric:
            require(item["symmetric"] is True and item["directed"] is False, f"{type_id}: symmetry")
            require(item["inverse_type_id"] == type_id, f"{type_id}: inverse")
            require(item["transitivity"] == "none", f"{type_id}: transitivity")
        else:
            require(item["directed"] is True and item["symmetric"] is False, f"{type_id}: direction")
            require(item["inverse_type_id"] is None, f"{type_id}: inverse")
    require(by_id["replaces"]["source_kinds"] == ["catalog_entity"], "replaces source kind")
    require(by_id["replaces"]["target_kinds"] == ["catalog_entity"], "replaces target kind")
    require(by_id["replaces"]["transitivity"] == "acyclic_closure", "replaces closure")
    require(by_id["approved_for"]["source_kinds"] == ["catalog_entity"], "approved source kind")
    require(by_id["approved_for"]["target_kinds"] == ["category"], "approved target kind")
    require(by_id["governed_by"]["target_kinds"] == ["normative_document"], "governed target")
    return by_id


def validate_components(
    manifest: dict[str, Any],
    registry: dict[str, Any],
    categories: dict[str, dict[str, Any]],
    attributes: set[str],
) -> dict[str, dict[str, Any]]:
    require(registry["version"] == manifest["version"], "component registry version")
    ids = [str(item["id"]) for item in registry["components"]]
    unique(ids, "component role id")
    require(len(ids) >= 3, "component role count")
    by_id = {str(item["id"]): item for item in registry["components"]}
    for component_id, item in by_id.items():
        require(re.fullmatch(r"electrical\.component\.[a-z][a-z0-9_]*", component_id) is not None, f"{component_id}: id")
        unique([str(value).lower() for value in item["aliases"]], f"{component_id} alias")
        hosts = {str(value) for value in item["host_category_ids"]}
        refs = {str(value) for value in item["attribute_refs"]}
        require(hosts <= set(categories), f"{component_id}: unknown host categories {sorted(hosts - set(categories))}")
        require(refs <= attributes, f"{component_id}: unknown attributes {sorted(refs - attributes)}")
        require(item["lifecycle_status"] != "deprecated", f"{component_id}: deprecated active component")
    return by_id
