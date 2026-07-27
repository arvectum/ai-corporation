from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "schemas" / "categories" / "electrical"
FIXTURES = ROOT / "fixtures" / "ontology" / "electrical"
MODULE_PATH = DIRECTORY / "validate_product_catalog.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "arv067e_validate_product_catalog",
        MODULE_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_manifest_and_catalog() -> tuple[dict, dict[str, list[dict]]]:
    manifest = yaml.safe_load(
        (DIRECTORY / "product_catalog.v1.yaml").read_text(encoding="utf-8")
    )
    catalog = {
        key: yaml.safe_load(
            (DIRECTORY / relative_path).read_text(encoding="utf-8")
        )["records"]
        for key, relative_path in manifest["entity_files"].items()
    }
    return manifest, catalog


def test_arv067e_product_catalog_validates() -> None:
    validator = _load_validator()
    assert validator.main() == 0


def test_arv067e_entity_layers_and_counts_are_explicit() -> None:
    manifest, catalog = _load_manifest_and_catalog()
    assert manifest["entity_counts"] == {
        "manufacturers": 3,
        "series": 4,
        "models": 5,
        "executions": 6,
        "offers": 5,
        "evidence": 8,
    }
    prefixes = {
        "manufacturers": "manufacturer.fixture.",
        "series": "series.fixture.",
        "models": "model.fixture.",
        "executions": "execution.fixture.",
        "offers": "offer.fixture.",
        "evidence": "evidence.fixture.",
    }
    for key, records in catalog.items():
        assert len(records) == manifest["entity_counts"][key]
        assert all(record["id"].startswith(prefixes[key]) for record in records)
        assert all(record["data_status"] == "synthetic_fixture" for record in records)


def test_arv067e_categories_do_not_depend_on_manufacturers_or_models() -> None:
    _, catalog = _load_manifest_and_catalog()
    series = {record["id"]: record for record in catalog["series"]}
    models = {record["id"]: record for record in catalog["models"]}
    executions = {record["id"]: record for record in catalog["executions"]}

    for model in models.values():
        assert "attribute_values" not in model
        assert "capability_ranges" not in model
        assert model["category_id"] == series[model["series_id"]]["category_id"]

    for execution in executions.values():
        assert execution["category_id"] == models[execution["model_id"]]["category_id"]
        assert execution["attribute_values"]

    for offer in catalog["offers"]:
        assert "attribute_values" not in offer
        assert "capability_ranges" not in offer


def test_arv067e_replacement_history_is_versioned_and_acyclic() -> None:
    validator = _load_validator()
    _, catalog = _load_manifest_and_catalog()
    model_edges = {
        record["id"]: record["replacement_model_id"]
        for record in catalog["models"]
        if record.get("replacement_model_id")
    }
    execution_edges = {
        record["id"]: record["replacement_execution_id"]
        for record in catalog["executions"]
        if record.get("replacement_execution_id")
    }
    assert model_edges == {
        "model.fixture.arv_vector.lbs_old": "model.fixture.arv_vector.lbs_next"
    }
    assert execution_edges == {
        "execution.fixture.arv_vector.lbs_old_630": (
            "execution.fixture.arv_vector.lbs_next_630"
        )
    }
    assert validator.has_cycle(model_edges) is False
    assert validator.has_cycle(execution_edges) is False


def test_arv067e_rossetti_is_operator_evidence_not_category_or_equivalence() -> None:
    manifest, catalog = _load_manifest_and_catalog()
    approvals = [
        record
        for record in catalog["evidence"]
        if record["source_document_id"].startswith("rossetti_")
    ]
    assert len(approvals) == 2
    assert all(record["evidence_type"] == "operator_approval" for record in approvals)
    assert all(record["target_type"] == "ProductExecution" for record in approvals)
    assert all(record["operator_id"] == "rossetti" for record in approvals)
    assert all(record["asserts_real_approval"] is False for record in approvals)
    assert manifest["governance"]["operator_approval_is_not_universal_equivalence"]
    assert manifest["governance"]["rossetti_rows_map_to_operator_approval"]


def test_arv067e_contract_cases_fail_closed_with_expected_reason_codes() -> None:
    validator = _load_validator()
    _, catalog = _load_manifest_and_catalog()
    _, category_ids = validator.load_category_ids()
    attributes, units = validator.load_attributes()
    cases = yaml.safe_load(
        (FIXTURES / "product_catalog_contract_cases.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert len(cases["cases"]) == 15
    for case in cases["cases"]:
        actual = validator.run_contract_case(
            catalog,
            case,
            category_ids,
            attributes,
            units,
        )
        assert actual == sorted(case["expected_error_codes"])


def test_arv067e_production_runtime_is_not_wired() -> None:
    forbidden = {
        "product_catalog.v1.yaml",
        "ARV-067E-ELECTRICAL-PRODUCT-CATALOG",
        "validate_product_catalog",
    }
    hits: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                hits.append(f"{path.relative_to(ROOT)}:{token}")
    assert hits == []
