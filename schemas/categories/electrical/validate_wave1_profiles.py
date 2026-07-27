#!/usr/bin/env python3
"""Validate ARV-067D wave-1 detailed electrical profiles and fixture gates."""
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


def validate_closed(value: dict[str, Any], schema: dict[str, Any], label: str) -> None:
    required = set(schema.get("required", []))
    properties = set(schema.get("properties", {}))
    require(required <= set(value), f"{label}: missing keys {sorted(required - set(value))}")
    require(set(value) <= properties, f"{label}: unknown keys {sorted(set(value) - properties)}")


def validate_schema_roots(schemas: dict[str, dict[str, Any]]) -> None:
    for label, schema in schemas.items():
        require(schema.get("$schema", "").endswith("2020-12/schema"), f"{label}: schema draft")
        require(schema.get("type") == "object", f"{label}: schema root")
        require(schema.get("additionalProperties") is False, f"{label}: schema must be closed")


def load_profiles(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fragments: list[dict[str, Any]] = []
    profiles: list[dict[str, Any]] = []
    for relative_path in manifest["profile_files"]:
        require(str(relative_path).startswith("profile_fragments/"), f"invalid profile path {relative_path}")
        fragment = load_yaml(HERE / str(relative_path))
        require(fragment["version"] == manifest["version"], f"{relative_path}: version mismatch")
        fragments.append(fragment)
        profiles.extend(fragment["profiles"])
    unique([str(fragment["fragment_id"]) for fragment in fragments], "profile fragment id")
    unique([str(profile["id"]) for profile in profiles], "profile id")
    return fragments, profiles


def load_category_nodes() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest = load_yaml(HERE / "category_tree.v1.yaml")
    nodes: list[dict[str, Any]] = []
    for relative_path in manifest["node_files"]:
        nodes.extend(load_yaml(HERE / str(relative_path))["nodes"])
    by_id = {str(node["category_id"]): node for node in nodes}
    require(len(by_id) == len(nodes), "duplicate category ids")
    return manifest, by_id


def load_attributes() -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    registry = load_yaml(HERE / "attribute_registry.v1.yaml")
    require(
        "attributes/wave1_profiles.v1.yaml" in registry["attribute_files"],
        "wave1 attribute fragment is not registered",
    )
    rows: list[dict[str, Any]] = []
    for relative_path in registry["attribute_files"]:
        rows.extend(load_yaml(HERE / str(relative_path))["attributes"])
    unique([str(row["id"]) for row in rows], "attribute id")
    by_id = {str(row["id"]): row for row in rows}
    comparator_types = {
        str(item["id"]): set(str(value) for value in item["allowed_value_types"])
        for item in registry["comparators"]
    }
    return by_id, comparator_types


def validate_schemas(
    schemas: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
    fragments: list[dict[str, Any]],
    bindings: dict[str, Any],
    fixtures: dict[str, Any],
    benchmark: dict[str, Any],
) -> None:
    validate_schema_roots(schemas)
    validate_closed(manifest, schemas["manifest"], "profile manifest")
    validate_closed(bindings, schemas["bindings"], "category bindings")
    validate_closed(fixtures, schemas["fixtures"], "fixture document")
    validate_closed(benchmark, schemas["benchmark"], "benchmark manifest")

    fragment_schema = schemas["fragment"]
    profile_schema = fragment_schema["properties"]["profiles"]["items"]
    rule_schema = profile_schema["properties"]["attributes"]["items"]
    basis_schema = profile_schema["properties"]["source_basis"]["items"]
    provenance_schema = profile_schema["properties"]["provenance"]
    for fragment in fragments:
        validate_closed(fragment, fragment_schema, str(fragment["fragment_id"]))
        for profile in fragment["profiles"]:
            profile_id = str(profile["id"])
            validate_closed(profile, profile_schema, profile_id)
            validate_closed(profile["provenance"], provenance_schema, f"{profile_id}.provenance")
            for index, rule in enumerate(profile["attributes"]):
                validate_closed(rule, rule_schema, f"{profile_id}.attributes[{index}]")
            for index, basis in enumerate(profile["source_basis"]):
                validate_closed(basis, basis_schema, f"{profile_id}.source_basis[{index}]")

    binding_item_schema = schemas["bindings"]["properties"]["bindings"]["items"]
    for index, binding in enumerate(bindings["bindings"]):
        validate_closed(binding, binding_item_schema, f"binding[{index}]")


def validate_profiles(
    manifest: dict[str, Any],
    profiles: list[dict[str, Any]],
    categories: dict[str, dict[str, Any]],
    attributes: dict[str, dict[str, Any]],
    comparator_types: dict[str, set[str]],
    normative_ids: set[str],
) -> dict[str, dict[str, Any]]:
    require(len(profiles) == 15, f"profile count mismatch: {len(profiles)}")
    expected_domains = {
        "cable_accessories": 3,
        "protection_switching": 5,
        "lines_insulation": 2,
        "low_voltage_controls": 5,
    }
    require(Counter(str(profile["domain"]) for profile in profiles) == Counter(expected_domains), "domain profile counts")
    require(manifest["runtime_import"] is False, "runtime import must be false")
    require(all(manifest["governance"].values()), "manifest governance")

    by_id = {str(profile["id"]): profile for profile in profiles}
    normalized_alias_owners: dict[str, set[str]] = defaultdict(set)
    for profile in profiles:
        profile_id = str(profile["id"])
        target = str(profile["target_category_id"])
        require(target in categories, f"{profile_id}: unknown category {target}")
        node = categories[target]
        expected_kind = "family" if profile["category_scope"] == "family" else "subcategory"
        require(node["node_kind"] == expected_kind, f"{profile_id}: category scope mismatch")
        require(node["lifecycle_status"] == "taxonomy_only", f"{profile_id}: base category must remain taxonomy_only")
        require(profile["lifecycle_status"] == "fixtures_ready", f"{profile_id}: lifecycle")
        require(profile["human_review_required"] is True, f"{profile_id}: review gate")
        require(profile["production_active"] is False, f"{profile_id}: production activation")
        require(int(profile["fixture_minimum"]) >= 12, f"{profile_id}: fixture minimum")
        require(len(profile["aliases"]) >= 3, f"{profile_id}: aliases")
        require(len(profile["canonical_marks"]) >= 1, f"{profile_id}: marks")
        unique([str(value) for value in profile["aliases"]], f"{profile_id} alias")
        unique([str(value) for value in profile["canonical_marks"]], f"{profile_id} canonical mark")
        for alias in profile["aliases"]:
            normalized = " ".join(str(alias).lower().replace("ё", "е").split())
            normalized_alias_owners[normalized].add(profile_id)

        rules = profile["attributes"]
        require(len(rules) >= 6, f"{profile_id}: too few attributes")
        ids = [str(rule["id"]) for rule in rules]
        unique(ids, f"{profile_id} attribute")
        critical = [rule for rule in rules if rule["critical"]]
        required_noncritical = [
            rule for rule in rules
            if rule["requirement"] == "required" and not rule["critical"]
        ]
        optional = [rule for rule in rules if rule["requirement"] == "optional"]
        require(len(critical) >= 2, f"{profile_id}: critical attributes")
        require(len(required_noncritical) >= 1, f"{profile_id}: noncritical required attributes")
        require(len(optional) >= 2, f"{profile_id}: optional attributes")

        for rule in rules:
            attr_id = str(rule["id"])
            require(attr_id in attributes, f"{profile_id}: unknown attribute {attr_id}")
            canonical = attributes[attr_id]
            comparator = str(rule["comparator"])
            require(
                str(canonical["value_type"]) in comparator_types[comparator],
                f"{profile_id}.{attr_id}: comparator/value type mismatch",
            )
            expected_basis = (
                "canonical_default"
                if comparator == canonical["default_comparator"]
                else "profile_requirement_semantics"
            )
            require(rule["comparator_basis"] == expected_basis, f"{profile_id}.{attr_id}: comparator basis")
            require(
                not rule["critical"] or rule["requirement"] == "required",
                f"{profile_id}.{attr_id}: optional critical attribute",
            )
            require(
                str(rule["mismatch_reason_code"]).startswith(profile_id.upper() + "_"),
                f"{profile_id}.{attr_id}: reason code namespace",
            )

        candidates = {str(value) for value in profile["normative_candidates"]}
        require(candidates <= normative_ids, f"{profile_id}: unknown normative candidates {sorted(candidates - normative_ids)}")
        basis_ids = {str(item["source_id"]) for item in profile["source_basis"]}
        require(
            {"category_tree_v1", "attribute_registry_v1", "normative_registry_v1"} <= basis_ids,
            f"{profile_id}: source basis incomplete",
        )

    for alias, owners in normalized_alias_owners.items():
        require(len(owners) == 1, f"cross-profile alias collision: {alias} -> {sorted(owners)}")
    return by_id


def validate_bindings(
    bindings: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    categories: dict[str, dict[str, Any]],
    attributes: dict[str, dict[str, Any]],
) -> None:
    require(bindings["binding_id"] == "ARV-067D-WAVE1-CATEGORY-BINDINGS", "binding id")
    require(all(bindings["governance"].values()), "binding governance")
    rows = bindings["bindings"]
    require(len(rows) == 11, f"binding count mismatch: {len(rows)}")
    unique([str(row["category_id"]) for row in rows], "binding category")
    mapped_profiles: list[str] = []
    for row in rows:
        category_id = str(row["category_id"])
        require(category_id in categories, f"binding: unknown category {category_id}")
        require(categories[category_id]["lifecycle_status"] == row["base_lifecycle_status"], f"{category_id}: base lifecycle")
        require(row["effective_lifecycle_status"] == "fixtures_ready", f"{category_id}: effective lifecycle")
        require(row["human_review_gate"] is True and row["production_active"] is False, f"{category_id}: activation guard")
        refs = [str(value) for value in row["profile_refs"]]
        mapped_profiles.extend(refs)
        for profile_id in refs:
            require(profile_id in profiles, f"{category_id}: unknown profile {profile_id}")
            require(profiles[profile_id]["target_category_id"] == category_id, f"{profile_id}: binding target")
        expected_attributes = sorted({
            str(rule["id"]) for profile_id in refs for rule in profiles[profile_id]["attributes"]
        })
        require(row["effective_attribute_refs"] == expected_attributes, f"{category_id}: effective attributes")
        require(set(expected_attributes) <= set(attributes), f"{category_id}: unknown effective attribute")
        profile_aliases = {
            str(alias) for profile_id in refs for alias in profiles[profile_id]["aliases"]
        }
        require(profile_aliases <= set(row["additional_routing_aliases"]), f"{category_id}: routing aliases")
        if categories[category_id]["node_kind"] == "family":
            require(len(refs) >= 1, f"{category_id}: family discriminator profile")
    unique(mapped_profiles, "bound profile")
    require(set(mapped_profiles) == set(profiles), "not all profiles are bound exactly once")


def load_matcher():
    path = HERE / "wave1_profile_matcher.py"
    spec = importlib.util.spec_from_file_location("arv067d_wave1_matcher", path)
    require(spec is not None and spec.loader is not None, "matcher import")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _select_rule_ids(profile: dict[str, Any], selector: str | None) -> list[str]:
    if selector is None:
        return []
    if ":" in selector:
        group, raw_index = selector.split(":", 1)
        index = int(raw_index)
    else:
        group, index = selector, None
    if group == "critical":
        values = [str(rule["id"]) for rule in profile["attributes"] if rule["critical"]]
    elif group == "required_noncritical":
        values = [
            str(rule["id"]) for rule in profile["attributes"]
            if rule["requirement"] == "required" and not rule["critical"]
        ]
    elif group == "optional":
        values = [
            str(rule["id"]) for rule in profile["attributes"]
            if rule["requirement"] == "optional"
        ]
    else:
        raise ValidationError(f"unknown fixture selector group: {group}")
    if index is None:
        return values
    require(values, f"empty fixture selector group {group}")
    return [values[index % len(values)]]


def materialize_candidate(
    profile: dict[str, Any],
    block: dict[str, Any],
    template: dict[str, Any],
) -> dict[str, Any]:
    template_name = str(template["candidate_template"])
    templates = block["candidate_templates"]
    require(template_name in templates, f"{profile['id']}: unknown candidate template")
    candidate = {
        str(key): value
        for key, value in templates[template_name].items()
    }
    selected = _select_rule_ids(profile, template["selector"])
    operation = str(template["operation"])
    if operation in {"remove", "remove_all"}:
        for attr_id in selected:
            candidate.pop(attr_id, None)
    elif operation in {"mismatch", "mismatch_all"}:
        for attr_id in selected:
            candidate[attr_id] = block["mismatch_values"][attr_id]
    else:
        require(operation == "none", f"{profile['id']}: unknown fixture operation {operation}")
    return candidate


def validate_fixtures(
    fixtures: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
) -> tuple[int, dict[str, Counter[str]], set[str]]:
    template_rows = fixtures["case_templates"]
    require(len(template_rows) == 12, "fixture template count")
    unique([str(row["id"]) for row in template_rows], "fixture template id")
    templates = {str(row["id"]): row for row in template_rows}
    expected_template_statuses = Counter({
        "EXACT": 2,
        "LIKELY_ANALOG": 3,
        "PARTIAL": 3,
        "UNCERTAIN": 2,
        "NO_MATCH": 2,
    })
    require(
        Counter(str(row["expected_status"]) for row in template_rows)
        == expected_template_statuses,
        "fixture template status distribution",
    )

    blocks = fixtures["profiles"]
    require(len(blocks) == len(profiles), "fixture profile block count")
    unique([str(block["profile_id"]) for block in blocks], "fixture profile block")
    require({str(block["profile_id"]) for block in blocks} == set(profiles), "fixture profile coverage")
    matcher = load_matcher()
    distribution: dict[str, Counter[str]] = defaultdict(Counter)
    fixture_ids: set[str] = set()
    total = 0
    for block in blocks:
        profile_id = str(block["profile_id"])
        profile = profiles[profile_id]
        rule_ids = {str(rule["id"]) for rule in profile["attributes"]}
        required_ids = {
            str(rule["id"]) for rule in profile["attributes"]
            if rule["requirement"] == "required"
        }
        requested = block["requested"]
        require(set(requested) <= rule_ids, f"{profile_id}: unknown requested attribute")
        require(required_ids <= set(requested), f"{profile_id}: request lacks required attribute")
        require(set(block["mismatch_values"]) == rule_ids, f"{profile_id}: mismatch values")
        candidate_templates = block["candidate_templates"]
        require(set(candidate_templates) == {"exact", "capability_surplus"}, f"{profile_id}: candidate templates")
        for name, candidate in candidate_templates.items():
            require(set(candidate) == rule_ids, f"{profile_id}.{name}: candidate attribute coverage")
        cases = block["cases"]
        require(len(cases) == 12, f"{profile_id}: fixture count")
        require({str(case["template_id"]) for case in cases} == set(templates), f"{profile_id}: fixture templates")
        for case in cases:
            case_id = str(case["id"])
            require(case_id not in fixture_ids, f"duplicate fixture id {case_id}")
            fixture_ids.add(case_id)
            template_id = str(case["template_id"])
            template = templates[template_id]
            require(case_id == f"{profile_id}__{template_id}", f"{case_id}: fixture id format")
            candidate = materialize_candidate(profile, block, template)
            actual = matcher.evaluate_profile(
                profile,
                requested,
                candidate,
                evidence_confirmed=bool(template["evidence_confirmed"]),
            )
            require(actual["status"] == template["expected_status"], f"{case_id}: status {actual}")
            require(
                str(template["expected_generic_reason"]) in actual["reason_codes"],
                f"{case_id}: generic reason {actual}",
            )
            require(actual["requires_review"] is True, f"{case_id}: review gate")
            distribution[profile_id][str(actual["status"])] += 1
            total += 1
    require(fixtures["case_count"] == total == len(profiles) * 12 == 180, "fixture total")
    for profile_id, profile in profiles.items():
        count = sum(distribution[profile_id].values())
        require(count >= int(profile["fixture_minimum"]), f"{profile_id}: fixture minimum")
        require(distribution[profile_id]["EXACT"] >= 2, f"{profile_id}: exact cases")
        require(distribution[profile_id]["NO_MATCH"] >= 2, f"{profile_id}: no-match cases")
        require(distribution[profile_id]["UNCERTAIN"] >= 2, f"{profile_id}: uncertain cases")
    return total, distribution, fixture_ids


def validate_benchmark(
    benchmark: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    distribution: dict[str, Counter[str]],
    fixture_ids: set[str],
) -> None:
    require(benchmark["status"] == "fixture_gate_only", "benchmark status")
    require(benchmark["minimum_cases_per_profile"] == 12, "benchmark minimum")
    require(len(benchmark["profiles"]) == len(profiles), "benchmark profile count")
    rows = {str(row["profile_id"]): row for row in benchmark["profiles"]}
    require(set(rows) == set(profiles), "benchmark profile ids")
    for profile_id, row in rows.items():
        require(row["case_count"] == 12, f"{profile_id}: benchmark case count")
        expected_distribution = {
            status: distribution[profile_id][status]
            for status in ["EXACT", "LIKELY_ANALOG", "PARTIAL", "UNCERTAIN", "NO_MATCH"]
        }
        require(row["status_distribution"] == expected_distribution, f"{profile_id}: status distribution")
        require(set(row["fixture_ids"]) <= fixture_ids, f"{profile_id}: unknown fixture id")
        require(row["review_cases"] == 12, f"{profile_id}: review count")
    require(benchmark["release_gates"]["production_activation_allowed"] is False, "production gate")
    require(benchmark["release_gates"]["arv067h_required_before_benchmark_passed"] is True, "ARV-067H gate")
    require(benchmark["release_gates"]["arv067i_required_before_shadow_runtime"] is True, "ARV-067I gate")
    require(all(benchmark["governance"].values()), "benchmark governance")


def validate_base_tree_not_mutated(
    categories: dict[str, dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
) -> None:
    wave1_ids = set(profiles)
    embedded = {
        str(ref)
        for node in categories.values()
        for ref in node["detailed_profile_refs"]
        if str(ref) in wave1_ids
    }
    require(not embedded, f"wave1 profiles embedded in source tree: {sorted(embedded)}")


def validate_runtime_boundary() -> None:
    forbidden = {
        "detailed_profiles_wave1.v1.yaml",
        "ARV-067D-ELECTRICAL-DETAILED-PROFILES-WAVE1",
        "wave1_profile_matcher",
    }
    hits: list[str] = []
    src = REPO_ROOT / "src"
    if src.exists():
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            hits.extend(
                f"{path.relative_to(REPO_ROOT)}:{token}"
                for token in forbidden
                if token in text
            )
    require(not hits, f"production runtime imports wave1 profiles: {hits}")


def main() -> int:
    try:
        manifest = load_yaml(HERE / "detailed_profiles_wave1.v1.yaml")
        fragments, profile_rows = load_profiles(manifest)
        bindings = load_yaml(HERE / manifest["category_binding_file"])
        fixtures = load_yaml(REPO_ROOT / manifest["fixture_file"])
        benchmark = load_yaml(REPO_ROOT / manifest["benchmark_manifest"])
        schemas = {
            "manifest": load_json(HERE / "detailed_profiles_wave1.schema.json"),
            "fragment": load_json(HERE / "detailed_profile_fragment.schema.json"),
            "bindings": load_json(HERE / "wave1_category_bindings.schema.json"),
            "fixtures": load_json(HERE / "wave1_profile_cases.schema.json"),
            "benchmark": load_json(HERE / "wave1_benchmark_manifest.schema.json"),
        }
        validate_schemas(schemas, manifest, fragments, bindings, fixtures, benchmark)
        tree_manifest, categories = load_category_nodes()
        require(tree_manifest["version"] == bindings["base_tree_version"], "base tree version")
        attributes, comparator_types = load_attributes()
        normative = load_yaml(HERE / "normative_registry.v1.yaml")
        normative_ids = {str(item["id"]) for item in normative["documents"]}
        profiles = validate_profiles(
            manifest, profile_rows, categories, attributes, comparator_types, normative_ids
        )
        validate_bindings(bindings, profiles, categories, attributes)
        validate_base_tree_not_mutated(categories, profiles)
        fixture_count, distribution, fixture_ids = validate_fixtures(fixtures, profiles)
        validate_benchmark(benchmark, profiles, distribution, fixture_ids)
        validate_runtime_boundary()
    except (
        OSError, json.JSONDecodeError, yaml.YAMLError, KeyError, TypeError,
        ValueError, ValidationError,
    ) as exc:
        print(f"ARV-067D wave1 profiles: FAILED: {exc}", file=sys.stderr)
        return 1

    print(
        "ARV-067D wave1 profiles: OK "
        f"(profiles={len(profiles)}, bindings={len(bindings['bindings'])}, "
        f"fixtures={fixture_count}, attributes={len({rule['id'] for p in profiles.values() for rule in p['attributes']})}, "
        "runtime_import=false)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
