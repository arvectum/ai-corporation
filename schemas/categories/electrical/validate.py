#!/usr/bin/env python3
"""Validate the isolated ARV-067 electrical ontology contract and fixtures."""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent

try:
    from .contract import LABELS, ValidationError, duplicates, load_json, load_yaml, require, validate_contract
    from .matcher import evaluate
    from .resolver import resolve
except ImportError:  # Support direct or importlib file execution.
    sys.path.insert(0, str(HERE))
    from contract import LABELS, ValidationError, duplicates, load_json, load_yaml, require, validate_contract
    from matcher import evaluate
    from resolver import resolve

REPO_ROOT = HERE.parents[2]
FIXTURES = REPO_ROOT / "fixtures" / "ontology" / "electrical"
ONTOLOGY_PATH = HERE / "electrical.v1.yaml"
SCHEMA_PATH = HERE / "ontology.schema.json"


def validate_fixtures(ontology: dict[str, Any]) -> tuple[int, int, set[str]]:
    synonyms = load_yaml(FIXTURES / "synonym_cases.yaml")
    matches = load_yaml(FIXTURES / "match_cases.yaml")
    for fixture in (synonyms, matches):
        require(fixture.get("ontology_version") == ontology["version"], "fixture version mismatch")
    synonym_cases = synonyms.get("cases")
    match_cases = matches.get("cases")
    require(isinstance(synonym_cases, list) and len(synonym_cases) >= 8, "synonym cases incomplete")
    require(isinstance(match_cases, list) and len(match_cases) >= 12, "match cases incomplete")
    for number, case in enumerate(synonym_cases, 1):
        actual = resolve(case["input"], ontology)
        require(actual["category"] == case["expected_category"], f"synonym {number}: category mismatch")
        if "expected_mark" in case:
            require(actual["canonical_mark"] == case["expected_mark"], f"synonym {number}: mark mismatch")
        for key, expected in case.get("expected_attributes", {}).items():
            value = actual["attributes"].get(key)
            if isinstance(expected, (int, float)) and isinstance(value, (int, float)):
                equal = math.isclose(float(value), float(expected), rel_tol=1e-6, abs_tol=1e-6)
            else:
                equal = value == expected
            require(equal, f"synonym {number}: {key} mismatch")
    ids = [str(case.get("id")) for case in match_cases]
    require(not duplicates(ids), "duplicate match case id")
    seen_labels: set[str] = set()
    for case in match_cases:
        result = evaluate(case["requirement"], case["candidate"], ontology)
        seen_labels.add(result.label)
        require(result.label == case["expected_label"], f"{case['id']}: label mismatch")
        require(list(result.reasons) == case["expected_reasons"], f"{case['id']}: reasons mismatch")
    require(seen_labels == LABELS, "fixtures do not cover all labels")
    manifest = load_yaml(FIXTURES / "benchmark_manifest.yaml")
    counts = manifest.get("expected_counts", {})
    require(manifest.get("status") == "contract_fixture_only", "manifest status invalid")
    require(manifest.get("ontology_version") == ontology["version"], "manifest version mismatch")
    require(counts.get("categories") == len(ontology["categories"]), "manifest category count mismatch")
    require(len(synonym_cases) >= counts.get("synonym_cases_min", 0), "manifest synonym gate failed")
    require(len(match_cases) >= counts.get("match_cases_min", 0), "manifest match gate failed")
    require(set(counts.get("labels_covered", [])) == seen_labels, "manifest label gate failed")
    release_note = str(manifest.get("release_note", "")).lower()
    require("not claim production" in release_note, "accuracy disclaimer missing")
    return len(synonym_cases), len(match_cases), seen_labels


def run_validation() -> dict[str, Any]:
    schema = load_json(SCHEMA_PATH)
    ontology = load_yaml(ONTOLOGY_PATH)
    validate_contract(schema, ontology)
    synonym_count, match_count, labels = validate_fixtures(ontology)
    return {
        "ontology_id": ontology["ontology_id"],
        "version": ontology["version"],
        "categories": len(ontology["categories"]),
        "synonym_cases": synonym_count,
        "match_cases": match_count,
        "labels": sorted(labels),
        "runtime_import": ontology["runtime_import"],
    }


def main() -> int:
    try:
        result = run_validation()
    except ValidationError as exc:
        print(f"ARV-067 electrical ontology: FAILED: {exc}", file=sys.stderr)
        return 1
    print(
        "ARV-067 electrical ontology: OK "
        f"(categories={result['categories']}, synonyms={result['synonym_cases']}, "
        f"matches={result['match_cases']}, runtime_import={str(result['runtime_import']).lower()})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
