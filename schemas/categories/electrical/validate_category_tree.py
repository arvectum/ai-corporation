#!/usr/bin/env python3
"""Validate the ARV-067B electrical category tree and inheritance contract."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "ontology" / "electrical" / "category_routing_cases.yaml"


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


def load_tree(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    paths = [str(value) for value in manifest["node_files"]]
    unique(paths, "node file")
    fragments: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    fragment_ids: list[str] = []
    for relative_path in paths:
        require(relative_path.startswith("category_nodes/"), f"invalid node file {relative_path}")
        fragment = load_yaml(HERE / relative_path)
        require(fragment["version"] == manifest["version"], f"{relative_path}: version mismatch")
        require(isinstance(fragment["nodes"], list) and fragment["nodes"], f"{relative_path}: empty nodes")
        fragments.append(fragment)
        nodes.extend(fragment["nodes"])
        fragment_ids.append(str(fragment["fragment_id"]))
    unique(fragment_ids, "fragment id")
    return fragments, nodes


def validate_schemas(
    manifest: dict[str, Any],
    manifest_schema: dict[str, Any],
    fragment_schema: dict[str, Any],
    fragments: list[dict[str, Any]],
) -> None:
    for label, schema in (("manifest", manifest_schema), ("fragment", fragment_schema)):
        require(schema.get("$schema", "").endswith("2020-12/schema"), f"{label}: schema draft")
        require(schema.get("type") == "object", f"{label}: schema root")
        require(schema.get("additionalProperties") is False, f"{label}: schema must be closed")
    validate_closed(manifest, manifest_schema, "manifest")
    node_schema = fragment_schema["properties"]["nodes"]["items"]
    for fragment in fragments:
        validate_closed(fragment, fragment_schema, str(fragment["fragment_id"]))
        for node in fragment["nodes"]:
            node_id = str(node.get("category_id", "node"))
            validate_closed(node, node_schema, node_id)
            validate_closed(node["routing"], node_schema["properties"]["routing"], f"{node_id}.routing")
            validate_closed(node["attributes"], node_schema["properties"]["attributes"], f"{node_id}.attributes")
            validate_closed(node["provenance"], node_schema["properties"]["provenance"], f"{node_id}.provenance")
            override_schema = node_schema["properties"]["attributes"]["properties"]["overrides"]["items"]
            for index, override in enumerate(node["attributes"]["overrides"]):
                validate_closed(override, override_schema, f"{node_id}.override[{index}]")


def load_attribute_contract() -> tuple[set[str], dict[str, str]]:
    registry = load_yaml(HERE / "attribute_registry.v1.yaml")
    ids: list[str] = []
    for relative_path in registry["attribute_files"]:
        fragment = load_yaml(HERE / relative_path)
        ids.extend(str(item["id"]) for item in fragment["attributes"])
    unique(ids, "ARV-067A attribute id")
    aliases = {str(key): str(value) for key, value in registry["profile_id_aliases"].items()}
    return set(ids), aliases


def validate_lifecycle(manifest: dict[str, Any], nodes: list[dict[str, Any]]) -> None:
    expected = [
        "taxonomy_only", "profile_draft", "source_verified", "fixtures_ready",
        "benchmark_passed", "shadow_runtime", "operator_approved", "production_active",
        "deprecated",
    ]
    lifecycle = manifest["lifecycle"]
    require(lifecycle["statuses"] == expected, "lifecycle status order mismatch")
    require(lifecycle["promotion_sequence"] == expected[:-1], "promotion sequence mismatch")
    require(set(lifecycle["allowed_transitions"]) == set(expected), "transition map mismatch")
    for source, targets in lifecycle["allowed_transitions"].items():
        unique([str(target) for target in targets], f"transition target for {source}")
        require(set(targets) <= set(expected), f"{source}: unknown transition target")
    values = {str(node["lifecycle_status"]) for node in nodes}
    require(values <= set(expected), "node lifecycle status mismatch")
    require(not values.intersection({"benchmark_passed", "shadow_runtime", "operator_approved", "production_active"}), "offline tree contains activated status")


def validate_graph(manifest: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    ids = [str(node["category_id"]) for node in nodes]
    unique(ids, "category id")
    by_id = {str(node["category_id"]): node for node in nodes}
    require(len(nodes) == 166, f"node count mismatch: {len(nodes)}")
    require(sum(node["node_kind"] == "root" for node in nodes) == 1, "root count mismatch")
    require(sum(node["node_kind"] == "registry" for node in nodes) == 2, "registry count mismatch")
    require(sum(node["node_kind"] == "family" for node in nodes) == 28, "family count mismatch")
    require(sum(node["node_kind"] == "subcategory" for node in nodes) == 135, "subcategory count mismatch")
    require(manifest["root_category_id"] in by_id, "root missing")

    expected_kind = {0: "root", 1: "registry", 2: "family", 3: "subcategory"}
    for node in nodes:
        node_id = str(node["category_id"])
        level = int(node["level"])
        require(re.fullmatch(r"electrical(?:\.[a-z][a-z0-9_]*){0,3}", node_id) is not None, f"{node_id}: invalid id")
        require(node["node_kind"] == expected_kind[level], f"{node_id}: kind/level mismatch")
        require(len(node["path"]) == level + 1 and node["path"][-1] == node_id, f"{node_id}: path shape")
        unique([str(value) for value in node["path"]], f"path item in {node_id}")
        parent_id = node["parent_id"]
        if level == 0:
            require(parent_id is None and node["path"] == [node_id], f"{node_id}: root contract")
            require(node["registry"] == "none", f"{node_id}: root registry")
            require(node["attributes"]["inherit_parent"] is False, f"{node_id}: root inheritance")
        else:
            require(parent_id in by_id, f"{node_id}: orphan parent {parent_id}")
            parent = by_id[str(parent_id)]
            require(int(parent["level"]) + 1 == level, f"{node_id}: parent level")
            require(node["path"] == [*parent["path"], node_id], f"{node_id}: explicit path")
            require(parent["registry"] == "none" or node["registry"] == parent["registry"], f"{node_id}: registry")
            require(node["attributes"]["inherit_parent"] is True, f"{node_id}: inheritance disabled")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        require(node_id not in visiting, f"cycle at {node_id}")
        if node_id in visited:
            return
        visiting.add(node_id)
        parent_id = by_id[node_id]["parent_id"]
        if parent_id is not None:
            visit(str(parent_id))
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in ids:
        visit(node_id)
    require(len(visited) == len(nodes), "unreachable nodes")
    return by_id


def validate_inheritance(
    nodes: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    registered_attributes: set[str],
) -> dict[str, set[str]]:
    effective: dict[str, set[str]] = {}
    for node in sorted(nodes, key=lambda item: int(item["level"])):
        node_id = str(node["category_id"])
        parent_id = node["parent_id"]
        values = set(effective[str(parent_id)]) if parent_id is not None and node["attributes"]["inherit_parent"] else set()
        local = {str(value) for value in node["attributes"]["local_refs"]}
        require(local <= registered_attributes, f"{node_id}: unknown local attributes {sorted(local - registered_attributes)}")
        values.update(local)
        seen_sources: set[str] = set()
        for override in node["attributes"]["overrides"]:
            source = str(override["source_attribute_id"])
            replacement = str(override["replacement_attribute_id"])
            require(source not in seen_sources, f"{node_id}: duplicate override source {source}")
            seen_sources.add(source)
            require(source in values, f"{node_id}: override source is not inherited/local: {source}")
            require(replacement in registered_attributes, f"{node_id}: unknown override replacement {replacement}")
            require(source != replacement, f"{node_id}: no-op override")
            values.remove(source)
            values.add(replacement)
        effective[node_id] = values
    return effective


def normalize_alias(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я]+", " ", value, flags=re.IGNORECASE)
    return " ".join(value.split())


def is_ancestor(ancestor: str, descendant: str, by_id: dict[str, dict[str, Any]]) -> bool:
    current: str | None = descendant
    while current is not None:
        if current == ancestor:
            return True
        current = by_id[current]["parent_id"]
    return False


def validate_aliases(manifest: dict[str, Any], nodes: list[dict[str, Any]], by_id: dict[str, dict[str, Any]]) -> None:
    owners: dict[str, set[str]] = defaultdict(set)
    minimum = int(manifest["routing_policy"]["minimum_alias_length"])
    for node in nodes:
        node_id = str(node["category_id"])
        aliases = [normalize_alias(str(alias)) for alias in node["routing"]["aliases"]]
        unique(aliases, f"normalized alias in {node_id}")
        for alias in aliases:
            require(len(alias) >= minimum, f"{node_id}: alias too short")
            owners[alias].add(node_id)
        broad = node["node_kind"] in {"root", "registry", "family"}
        require(bool(node["routing"]["review_if_only_match"]) is broad, f"{node_id}: review flag mismatch")
    for alias, node_ids in owners.items():
        if len(node_ids) < 2:
            continue
        ordered = sorted(node_ids, key=lambda node_id: int(by_id[node_id]["level"]))
        require(all(is_ancestor(left, right, by_id) for left, right in zip(ordered, ordered[1:])), f"alias collision outside ancestry: {alias} -> {ordered}")


def validate_source_coverage(
    nomenclature: dict[str, Any],
    nodes: list[dict[str, Any]],
    profile_aliases: dict[str, str],
) -> None:
    families = {str(node["provenance"].get("source_section_id")): node for node in nodes if node["node_kind"] == "family"}
    sections = {str(section["id"]): section for section in nomenclature["sections"]}
    require(set(families) == set(sections), "source section mapping mismatch")
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        if node["parent_id"] is not None:
            children[str(node["parent_id"])].append(node)
    for section_id, section in sections.items():
        family = families[section_id]
        require(family["registry"] == section["registry"], f"{section_id}: registry")
        require(family["title_ru"] == section["title_ru"], f"{section_id}: title")
        source_aliases = {normalize_alias(str(value)) for value in [section["title_ru"], *section["aliases"]]}
        mapped_aliases = {normalize_alias(str(value)) for value in family["routing"]["aliases"]}
        require(source_aliases <= mapped_aliases, f"{section_id}: aliases")
        require(family["attributes"]["local_refs"] == section["discriminator_attributes"], f"{section_id}: discriminators")
        leaves = [node for node in children[str(family["category_id"])] if node["node_kind"] == "subcategory"]
        require(Counter(str(value) for value in section["subcategories"]) == Counter(str(node["provenance"].get("source_label")) for node in leaves), f"{section_id}: subcategories")
        source_profiles = {profile_aliases.get(str(value), str(value)) for value in section["detailed_profile_refs"]}
        mapped_profiles = {profile_aliases.get(str(value), str(value)) for node in leaves for value in node["detailed_profile_refs"]}
        require(source_profiles == mapped_profiles, f"{section_id}: detailed profile refs")
        require(all(node["provenance"]["source_section_id"] == section_id for node in leaves), f"{section_id}: leaf provenance")


def validate_profiles(
    ontology: dict[str, Any],
    nodes: list[dict[str, Any]],
    effective: dict[str, set[str]],
    aliases: dict[str, str],
) -> None:
    profiles = {str(category["id"]): {str(attribute["id"]) for attribute in category["attributes"]} for category in ontology["categories"]}
    mapped: dict[str, str] = {}
    for node in nodes:
        for raw_profile in node["detailed_profile_refs"]:
            profile = aliases.get(str(raw_profile), str(raw_profile))
            require(profile in profiles, f"{node['category_id']}: unknown profile {raw_profile}")
            require(profile not in mapped, f"profile mapped twice: {profile}")
            mapped[profile] = str(node["category_id"])
            require(node["node_kind"] == "subcategory", f"{node['category_id']}: profile on non-leaf")
            require(node["lifecycle_status"] == "fixtures_ready", f"{node['category_id']}: profile lifecycle")
            require(profiles[profile] <= effective[str(node["category_id"])], f"{node['category_id']}: profile attributes")
    require(set(mapped) == set(profiles), "not all detailed profiles mapped")


def load_router_module():
    path = HERE / "category_router.py"
    spec = importlib.util.spec_from_file_location("arv067b_category_router", path)
    require(spec is not None and spec.loader is not None, "router import")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_fixtures(nodes: list[dict[str, Any]], version: str) -> int:
    fixture = load_yaml(FIXTURES)
    require(fixture["tree_version"] == version, "fixture version")
    cases = fixture["cases"]
    require(isinstance(cases, list) and len(cases) >= 24, "fixture count")
    unique([str(case["id"]) for case in cases], "fixture id")
    router = load_router_module()
    for case in cases:
        result = router.route_category(str(case["query"]), nodes)
        require(result["status"] == case["expected_status"], f"{case['id']}: status {result}")
        require(result["category_id"] == case["expected_category_id"], f"{case['id']}: category {result}")
        require(result["candidates"] == case["expected_candidates"], f"{case['id']}: candidates {result}")
        require(result["requires_review"] is case["expected_requires_review"], f"{case['id']}: review {result}")
    return len(cases)


def validate_runtime_boundary() -> None:
    forbidden = {"category_tree.v1.yaml", "ARV-067B-ELECTRICAL-CATEGORY-TREE"}
    hits: list[str] = []
    src = REPO_ROOT / "src"
    if src.exists():
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            hits.extend(f"{path.relative_to(REPO_ROOT)}:{token}" for token in forbidden if token in text)
    require(not hits, f"production runtime imports category tree: {hits}")


def main() -> int:
    try:
        manifest = load_yaml(HERE / "category_tree.v1.yaml")
        manifest_schema = load_json(HERE / "category_tree.schema.json")
        fragment_schema = load_json(HERE / "category_tree_fragment.schema.json")
        nomenclature = load_yaml(HERE / "nomenclature.v1.yaml")
        ontology = load_yaml(HERE / "electrical.v1.yaml")
        require(manifest["tree_id"] == "ARV-067B-ELECTRICAL-CATEGORY-TREE", "tree id")
        require(re.fullmatch(r"\d+\.\d+\.\d+", str(manifest["version"])) is not None, "version")
        require(manifest["runtime_import"] is False, "runtime import")
        require(all(manifest["governance"].values()), "governance")
        fragments, nodes = load_tree(manifest)
        validate_schemas(manifest, manifest_schema, fragment_schema, fragments)
        validate_lifecycle(manifest, nodes)
        by_id = validate_graph(manifest, nodes)
        registered, aliases = load_attribute_contract()
        effective = validate_inheritance(nodes, by_id, registered)
        validate_aliases(manifest, nodes, by_id)
        validate_source_coverage(nomenclature, nodes, aliases)
        validate_profiles(ontology, nodes, effective, aliases)
        fixture_count = validate_fixtures(nodes, str(manifest["version"]))
        validate_runtime_boundary()
    except (OSError, json.JSONDecodeError, yaml.YAMLError, KeyError, TypeError, ValueError, ValidationError) as exc:
        print(f"ARV-067B category tree: FAILED: {exc}", file=sys.stderr)
        return 1
    mapped_profiles = sum(bool(node["detailed_profile_refs"]) for node in nodes)
    print(
        "ARV-067B category tree: OK "
        f"(nodes={len(nodes)}, families=28, subcategories=135, "
        f"detailed_profiles={mapped_profiles}, routing_cases={fixture_count}, runtime_import=false)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
