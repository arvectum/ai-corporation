#!/usr/bin/env python3
"""Validate the ARV-067C electrical relation graph and safety boundaries."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "ontology" / "electrical" / "relation_cases.yaml"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from relation_validation_contract import (  # noqa: E402
    ValidationError,
    load_assertions,
    load_attribute_ids,
    load_category_contract,
    load_normative_ids,
    load_yaml,
    require,
    unique,
    validate_components,
    validate_manifest,
    validate_schemas,
    validate_types,
)
from relation_validation_assertions import (  # noqa: E402
    validate_assertions,
    validate_domain_coverage,
    validate_replacement_graph,
)


def load_evaluator_module():
    path = HERE / "relation_evaluator.py"
    spec = importlib.util.spec_from_file_location("arv067c_relation_evaluator", path)
    require(spec is not None and spec.loader is not None, "evaluator import")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_fixtures(
    version: str,
    assertions: list[dict[str, Any]],
    relation_types: list[dict[str, Any]],
) -> int:
    fixture = load_yaml(FIXTURES)
    require(fixture["graph_version"] == version, "fixture version")
    cases = fixture["cases"]
    require(isinstance(cases, list) and len(cases) >= 24, "fixture count")
    unique([str(case["id"]) for case in cases], "fixture id")
    evaluator = load_evaluator_module()
    for case in cases:
        result = evaluator.evaluate_relation(
            case["source"],
            case["target"],
            assertions,
            relation_types,
            relation_type=str(case["relation_type"]),
            satisfied_condition_ids=case["satisfied_condition_ids"],
            failed_condition_ids=case["failed_condition_ids"],
            evidence_confirmed=bool(case["evidence_confirmed"]),
        )
        expected = {
            "status": case["expected_status"],
            "relation_type": case["expected_relation_type"],
            "relation_ids": case["expected_relation_ids"],
            "requires_review": case["expected_requires_review"],
            "reason_codes": case["expected_reason_codes"],
        }
        require(result == expected, f"{case['id']}: {result} != {expected}")
    return len(cases)


def validate_runtime_boundary() -> None:
    forbidden = {
        "relation_graph.v1.yaml",
        "ARV-067C-ELECTRICAL-RELATION-GRAPH",
    }
    hits: list[str] = []
    src = REPO_ROOT / "src"
    if src.exists():
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    hits.append(f"{path.relative_to(REPO_ROOT)}:{token}")
    require(not hits, f"production runtime imports relation graph: {hits}")


def main() -> int:
    try:
        manifest = load_yaml(HERE / "relation_graph.v1.yaml")
        type_registry = load_yaml(HERE / str(manifest["type_registry_file"]))
        component_registry = load_yaml(HERE / str(manifest["component_registry_file"]))
        fragments, assertions = load_assertions(manifest)
        validate_schemas(manifest, type_registry, component_registry, fragments)
        validate_manifest(manifest)
        categories = load_category_contract()
        attributes = load_attribute_ids()
        normative_ids = load_normative_ids()
        relation_types = validate_types(manifest, type_registry)
        components = validate_components(
            manifest, component_registry, categories, attributes
        )
        validate_assertions(
            manifest,
            assertions,
            relation_types,
            categories,
            components,
            normative_ids,
            attributes,
        )
        validate_replacement_graph(assertions)
        validate_domain_coverage(assertions)
        fixture_count = validate_fixtures(
            str(manifest["version"]), assertions, type_registry["types"]
        )
        validate_runtime_boundary()
    except (
        OSError,
        json.JSONDecodeError,
        yaml.YAMLError,
        KeyError,
        TypeError,
        ValueError,
        ValidationError,
    ) as exc:
        print(f"ARV-067C relation graph: FAILED: {exc}", file=sys.stderr)
        return 1

    print(
        "ARV-067C relation graph: OK "
        f"(types={len(type_registry['types'])}, components={len(component_registry['components'])}, "
        f"assertions={len(assertions)}, fixtures={fixture_count}, runtime_import=false)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
