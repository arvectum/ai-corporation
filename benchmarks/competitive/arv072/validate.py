#!/usr/bin/env python3
"""Dependency-free consistency validation for the ARV-072 benchmark package."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
CASES_PATH = REPO_ROOT / "fixtures" / "competitive" / "arv072" / "cases.json"


class ValidationError(RuntimeError):
    pass


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"missing file: {path.relative_to(REPO_ROOT)}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(
            f"invalid JSON: {path.relative_to(REPO_ROOT)}:{exc.lineno}:{exc.colno}"
        ) from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def validate_rubric(data: dict[str, Any]) -> set[str]:
    require(data.get("benchmark_id") == "ARV-072", "rubric benchmark_id mismatch")
    dimensions = data.get("dimensions")
    require(isinstance(dimensions, list) and dimensions, "rubric dimensions missing")
    ids = [item.get("id") for item in dimensions]
    required = {"extraction", "evidence", "decision", "time", "cost", "documents"}
    require(set(ids) == required, f"rubric dimensions must be {sorted(required)}")
    require(len(ids) == len(set(ids)), "duplicate rubric dimension id")
    require(sum(item.get("weight", 0) for item in dimensions) == 100, "rubric weights must sum to 100")
    for dimension in dimensions:
        items = dimension.get("items")
        require(isinstance(items, list) and items, f"{dimension['id']}: items missing")
        require(
            sum(item.get("weight", 0) for item in items) == dimension["weight"],
            f"{dimension['id']}: item weights do not match dimension weight",
        )
        item_ids = [item.get("id") for item in items]
        require(len(item_ids) == len(set(item_ids)), f"{dimension['id']}: duplicate item id")
    rules = data.get("automatic_fail_rules")
    require(isinstance(rules, list) and len(rules) >= 5, "automatic fail rules incomplete")
    return required


def validate_benchmark(data: dict[str, Any]) -> set[str]:
    require(data.get("benchmark_id") == "ARV-072", "benchmark_id mismatch")
    products = data.get("products")
    require(isinstance(products, list) and len(products) >= 6, "at least six products are required")
    ids = [product.get("id") for product in products]
    require(len(ids) == len(set(ids)), "duplicate product id")
    require("arvectum" in ids, "Arvectum is missing from product registry")
    require(len([item for item in ids if item != "arvectum"]) >= 5, "five external products are required")
    for product in products:
        require(str(product.get("official_url", "")).startswith("https://"), f"{product.get('id')}: official_url missing")
        require(product.get("live_gate"), f"{product.get('id')}: live_gate missing")
    protocol = data.get("live_protocol", {})
    require(protocol.get("minimum_comparable_products") >= 5, "minimum comparable products is too low")
    require(protocol.get("cases") == 5, "protocol must use exactly five cases")
    require(protocol.get("runs_per_product_case") == 2, "protocol must require two runs")
    require("scores from public claims" in protocol.get("forbidden", []), "public claims scoring guard missing")
    status = data.get("status", {})
    require(status.get("preparation") == "complete", "preparation status must be complete")
    require(status.get("live_execution") in {"blocked", "in_progress", "complete"}, "invalid live status")
    if status.get("live_execution") == "blocked":
        require(status.get("blockers"), "blocked live status needs explicit blockers")
    return set(ids)


def validate_cases(data: dict[str, Any]) -> None:
    require(data.get("benchmark_id") == "ARV-072", "cases benchmark_id mismatch")
    cases = data.get("cases")
    require(isinstance(cases, list) and len(cases) == 5, "exactly five cases are required")
    ids = [case.get("case_id") for case in cases]
    numbers = [case.get("procurement_number") for case in cases]
    require(len(ids) == len(set(ids)), "duplicate case id")
    require(len(numbers) == len(set(numbers)), "duplicate procurement number")
    require(
        all(isinstance(number, str) and len(number) == 19 and number.isdigit() for number in numbers),
        "invalid procurement number",
    )
    for case in cases:
        require(case.get("truth_status"), f"{case.get('case_id')}: truth_status missing")
        require(isinstance(case.get("live_ready"), bool), f"{case.get('case_id')}: live_ready must be boolean")
        if case["live_ready"]:
            for field in ("source_bundle_sha256", "truth_pack_sha256"):
                value = case.get(field)
                require(isinstance(value, str) and len(value) == 64, f"{case['case_id']}: ready case lacks {field}")
        else:
            require(case.get("blocking_actions"), f"{case['case_id']}: non-ready case lacks blockers")


def validate_schema(data: dict[str, Any], dimensions: set[str]) -> None:
    require(data.get("$schema", "").endswith("2020-12/schema"), "result schema draft mismatch")
    properties = data.get("properties", {})
    schema_dimensions = properties.get("dimensions", {}).get("properties", {})
    require(set(schema_dimensions) == dimensions, "result schema dimensions differ from rubric")
    require("overall_score" not in properties, "raw result schema must not accept overall_score")


def main() -> int:
    try:
        rubric = load_json(HERE / "rubric.json")
        benchmark = load_json(HERE / "benchmark.json")
        cases = load_json(CASES_PATH)
        schema = load_json(HERE / "live_result.schema.json")
        dimensions = validate_rubric(rubric)
        validate_benchmark(benchmark)
        validate_cases(cases)
        validate_schema(schema, dimensions)
        report = (HERE / "report_template.md").read_text(encoding="utf-8")
        require("DRAFT_UNTIL_LIVE_EVIDENCE_COMPLETE" in report, "report release gate missing")
        require("Extraction /25" in report and "Evidence /25" in report, "report table does not match rubric")
    except (ValidationError, OSError) as exc:
        print(f"ARV-072 benchmark package: FAILED: {exc}", file=sys.stderr)
        return 1
    print("ARV-072 benchmark package: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
