"""Assertion and graph validation helpers for ARV-067C."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from relation_validation_contract import ValidationError, require, unique


def endpoint_key(endpoint: dict[str, Any]) -> str:
    return f"{endpoint['kind']}:{endpoint['id']}"


def canonical_pair(source: dict[str, Any], target: dict[str, Any]) -> tuple[str, str]:
    left, right = endpoint_key(source), endpoint_key(target)
    return (left, right) if left <= right else (right, left)


def resolve_endpoint(
    endpoint: dict[str, Any],
    categories: dict[str, dict[str, Any]],
    components: dict[str, dict[str, Any]],
    normative_ids: set[str],
) -> None:
    kind = str(endpoint["kind"])
    endpoint_id = str(endpoint["id"])
    if kind == "category":
        require(endpoint_id in categories, f"unknown category endpoint {endpoint_id}")
    elif kind == "component_role":
        require(endpoint_id in components, f"unknown component endpoint {endpoint_id}")
    elif kind == "normative_document":
        require(endpoint_id in normative_ids, f"unknown normative endpoint {endpoint_id}")
    elif kind == "catalog_entity":
        raise ValidationError(
            f"catalog entity endpoint {endpoint_id} requires ARV-067E registry"
        )
    else:
        raise ValidationError(f"unknown endpoint kind {kind}")


def validate_assertions(
    manifest: dict[str, Any],
    assertions: list[dict[str, Any]],
    relation_types: dict[str, dict[str, Any]],
    categories: dict[str, dict[str, Any]],
    components: dict[str, dict[str, Any]],
    normative_ids: set[str],
    attributes: set[str],
) -> None:
    assertion_ids = [str(item["assertion_id"]) for item in assertions]
    unique(assertion_ids, "assertion id")
    require(len(assertions) >= 20, "assertion count")
    reason_codes = set(str(value) for value in manifest["reason_codes"])
    evidence_ids: list[str] = []
    duplicate_keys: list[tuple[str, tuple[str, str], str]] = []
    compatibility: defaultdict[tuple[tuple[str, str], str], set[str]] = defaultdict(set)

    for item in assertions:
        assertion_id = str(item["assertion_id"])
        type_id = str(item["relation_type"])
        require(type_id in relation_types, f"{assertion_id}: unknown relation type")
        relation_type = relation_types[type_id]
        resolve_endpoint(item["source"], categories, components, normative_ids)
        resolve_endpoint(item["target"], categories, components, normative_ids)
        require(item["source"]["kind"] in relation_type["source_kinds"], f"{assertion_id}: source kind")
        require(item["target"]["kind"] in relation_type["target_kinds"], f"{assertion_id}: target kind")
        require(endpoint_key(item["source"]) != endpoint_key(item["target"]), f"{assertion_id}: self relation")
        if relation_type["symmetric"]:
            require(
                endpoint_key(item["source"]) <= endpoint_key(item["target"]),
                f"{assertion_id}: symmetric relation must use canonical endpoint order",
            )
        require(set(str(code) for code in item["reason_codes"]) <= reason_codes, f"{assertion_id}: reason codes")
        require(item["status"] != "deprecated" and item["active"] is True, f"{assertion_id}: inactive/deprecated")
        require(bool(item["evidence"]), f"{assertion_id}: evidence")
        evidence_ids.extend(str(value["evidence_id"]) for value in item["evidence"])

        condition_ids = [str(value["id"]) for value in item["conditions"]]
        unique(condition_ids, f"{assertion_id} condition id")
        for condition in item["conditions"]:
            condition_type = str(condition["condition_type"])
            if condition_type == "attribute_match":
                required = {"source_attribute_id", "target_attribute_id", "comparator"}
                require(required <= set(condition), f"{assertion_id}:{condition['id']}: attribute fields")
                refs = {
                    str(condition["source_attribute_id"]),
                    str(condition["target_attribute_id"]),
                }
                require(refs <= attributes, f"{assertion_id}:{condition['id']}: unknown attributes {sorted(refs - attributes)}")
            else:
                forbidden = {"source_attribute_id", "target_attribute_id", "comparator"}
                require(not (forbidden & set(condition)), f"{assertion_id}:{condition['id']}: non-attribute fields")

        evidence_statuses = {str(value["status"]) for value in item["evidence"]}
        if item["decision_ceiling"] == "SUPPORTED":
            require(
                evidence_statuses <= {"verified_structure", "source_verified"},
                f"{assertion_id}: supported ceiling without verified evidence",
            )
        if "required_before_use" in evidence_statuses:
            require("RELATION_EVIDENCE_MISSING" in item["reason_codes"], f"{assertion_id}: evidence reason")
        if type_id in {"compatible_with", "alternative_to", "accessory_for", "governed_by"}:
            if "category" in {item["source"]["kind"], item["target"]["kind"]}:
                require(item["decision_ceiling"] == "CONDITIONAL", f"{assertion_id}: category ceiling")
        if type_id == "not_compatible_with":
            require(item["strength"] == "hard", f"{assertion_id}: negative strength")
            require(item["failure_outcome"] == "NOT_COMPATIBLE", f"{assertion_id}: negative outcome")
        if type_id == "governed_by":
            require(item["target"]["kind"] == "normative_document", f"{assertion_id}: governed target")
        if type_id == "replaces":
            require(item["source"]["kind"] == item["target"]["kind"] == "catalog_entity", f"{assertion_id}: replacement endpoint")
        if type_id == "approved_for":
            require(item["source"]["kind"] == "catalog_entity" and item["target"]["kind"] == "category", f"{assertion_id}: approval endpoint")

        pair = canonical_pair(item["source"], item["target"]) if relation_type["symmetric"] else (
            endpoint_key(item["source"]),
            endpoint_key(item["target"]),
        )
        duplicate_keys.append((type_id, pair, str(item["scope_id"])))
        if type_id in {"compatible_with", "not_compatible_with"}:
            compatibility[(canonical_pair(item["source"], item["target"]), str(item["scope_id"]))].add(type_id)

    unique(evidence_ids, "evidence id")
    require(len(duplicate_keys) == len(set(duplicate_keys)), "duplicate relation assertion for type/pair/scope")
    conflicts = [key for key, values in compatibility.items() if values == {"compatible_with", "not_compatible_with"}]
    require(not conflicts, f"compatible/not-compatible conflicts: {conflicts}")


def validate_replacement_graph(assertions: list[dict[str, Any]]) -> None:
    graph: defaultdict[str, list[str]] = defaultdict(list)
    for item in assertions:
        if item["active"] and item["relation_type"] == "replaces":
            graph[endpoint_key(item["source"])].append(endpoint_key(item["target"]))
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        require(node not in visiting, f"replacement cycle at {node}")
        if node in visited:
            return
        visiting.add(node)
        for target in graph[node]:
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for node in list(graph):
        visit(node)


def validate_domain_coverage(assertions: list[dict[str, Any]]) -> None:
    ids = {str(item["assertion_id"]) for item in assertions}
    required = {
        "REL-CABLE-JOINT-ACCESSORY-POWER-CABLE",
        "REL-SUSPENSION-HARDWARE-ACCESSORY-SIP",
        "REL-MCB-TRIP-UNIT-PART-OF",
        "REL-PROTECTION-TERMINAL-REQUIRES-CURRENT-INPUT",
        "REL-CURRENT-TRANSFORMER-COMPATIBLE-RZA-CURRENT-INPUT",
        "REL-VOLTAGE-TRANSFORMER-COMPATIBLE-RZA-VOLTAGE-INPUT",
    }
    require(required <= ids, "required domain relation coverage")
    seeded_types = {str(item["relation_type"]) for item in assertions}
    require(
        {"part_of", "accessory_for", "compatible_with", "requires", "alternative_to", "not_compatible_with", "governed_by"} <= seeded_types,
        "seeded relation types",
    )
    require("replaces" not in seeded_types, "replacement assertions must wait for ARV-067E")
    require("approved_for" not in seeded_types, "approval assertions must wait for model registry")
