from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "schemas" / "categories" / "electrical"
VALIDATOR_PATH = DIRECTORY / "validate_relations.py"
EVALUATOR_PATH = DIRECTORY / "relation_evaluator.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_graph() -> tuple[dict, list[dict], list[dict]]:
    manifest = yaml.safe_load(
        (DIRECTORY / "relation_graph.v1.yaml").read_text(encoding="utf-8")
    )
    types = yaml.safe_load(
        (DIRECTORY / manifest["type_registry_file"]).read_text(encoding="utf-8")
    )["types"]
    assertions = [
        assertion
        for relative_path in manifest["assertion_files"]
        for assertion in yaml.safe_load(
            (DIRECTORY / relative_path).read_text(encoding="utf-8")
        )["assertions"]
    ]
    return manifest, types, assertions


def test_arv067c_relation_graph_validates() -> None:
    validator = _load_module(VALIDATOR_PATH, "arv067c_validate_relations")
    assert validator.main() == 0


def test_arv067c_relation_types_do_not_collapse_semantics() -> None:
    manifest, types, assertions = _load_graph()
    by_id = {item["id"]: item for item in types}

    assert set(by_id) == {
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
    assert all(item["equivalence_semantics"] is False for item in types)
    assert by_id["compatible_with"]["symmetric"] is True
    assert by_id["compatible_with"]["transitivity"] == "none"
    assert by_id["accessory_for"]["semantic_class"] == "completeness"
    assert by_id["replaces"]["source_kinds"] == ["catalog_entity"]
    assert by_id["approved_for"]["source_kinds"] == ["catalog_entity"]
    assert not any(item["relation_type"] == "replaces" for item in assertions)
    assert not any(item["relation_type"] == "approved_for" for item in assertions)
    assert manifest["governance"]["compatibility_is_not_equivalence"] is True
    assert manifest["governance"]["completeness_is_not_compatibility"] is True


def test_arv067c_evaluator_supports_structure_and_surfaces_uncertainty() -> None:
    evaluator = _load_module(EVALUATOR_PATH, "arv067c_relation_evaluator")
    _, types, assertions = _load_graph()

    supported = evaluator.evaluate_relation(
        {"kind": "component_role", "id": "electrical.component.mcb_trip_unit"},
        {
            "kind": "category",
            "id": "electrical.primary.other_primary.miniature_circuit_breaker",
        },
        assertions,
        types,
        relation_type="part_of",
    )
    assert supported["status"] == "SUPPORTED"
    assert supported["requires_review"] is False

    conditional = evaluator.evaluate_relation(
        {
            "kind": "category",
            "id": "electrical.primary.line_hardware.connector_hardware",
        },
        {
            "kind": "category",
            "id": (
                "electrical.primary.conductors_ground_wires_sip."
                "self_supporting_insulated_wire"
            ),
        },
        assertions,
        types,
        relation_type="compatible_with",
    )
    assert conditional["status"] == "CONDITIONAL"
    assert "RELATION_CATEGORY_LEVEL_CEILING" in conditional["reason_codes"]

    uncertain = evaluator.evaluate_relation(
        {
            "kind": "category",
            "id": (
                "electrical.secondary.relay_protection_automation."
                "protection_terminal"
            ),
        },
        {
            "kind": "component_role",
            "id": "electrical.component.rza_voltage_measurement_input",
        },
        assertions,
        types,
        relation_type="requires",
    )
    assert uncertain["status"] == "UNCERTAIN"
    assert "RELATION_EVIDENCE_MISSING" in uncertain["reason_codes"]

    absent = evaluator.evaluate_relation(
        {
            "kind": "category",
            "id": "electrical.primary.cables_fittings_pipes.cable_joint",
        },
        {"kind": "category", "id": "electrical.primary.other_primary.contactor"},
        assertions,
        types,
        relation_type="compatible_with",
    )
    assert absent == {
        "status": "UNCERTAIN",
        "relation_type": "compatible_with",
        "relation_ids": [],
        "requires_review": True,
        "reason_codes": ["RELATION_NOT_FOUND"],
    }


def test_arv067c_evaluator_detects_compatibility_conflict() -> None:
    evaluator = _load_module(EVALUATOR_PATH, "arv067c_relation_conflict")
    _, types, assertions = _load_graph()
    positive = next(
        item
        for item in assertions
        if item["assertion_id"] == "REL-MCB-COMPATIBLE-TRIP-UNIT"
    )
    negative = copy.deepcopy(positive)
    negative["assertion_id"] = "REL-SYNTHETIC-CONFLICT"
    negative["relation_type"] = "not_compatible_with"
    negative["reason_codes"] = ["RELATION_NOT_COMPATIBLE_WITH"]
    negative["failure_outcome"] = "NOT_COMPATIBLE"

    result = evaluator.evaluate_relation(
        positive["source"],
        positive["target"],
        [positive, negative],
        types,
    )
    assert result["status"] == "CONFLICT"
    assert result["reason_codes"] == ["RELATION_CONFLICT"]
    assert result["requires_review"] is True


def test_arv067c_replacement_cycle_is_rejected() -> None:
    validator = _load_module(VALIDATOR_PATH, "arv067c_replacement_cycle")
    assertions = [
        {
            "active": True,
            "relation_type": "replaces",
            "source": {"kind": "catalog_entity", "id": "model:a"},
            "target": {"kind": "catalog_entity", "id": "model:b"},
        },
        {
            "active": True,
            "relation_type": "replaces",
            "source": {"kind": "catalog_entity", "id": "model:b"},
            "target": {"kind": "catalog_entity", "id": "model:a"},
        },
    ]
    with pytest.raises(validator.ValidationError, match="replacement cycle"):
        validator.validate_replacement_graph(assertions)


def test_arv067c_production_runtime_is_not_wired() -> None:
    forbidden = {
        "relation_graph.v1.yaml",
        "ARV-067C-ELECTRICAL-RELATION-GRAPH",
    }
    hits: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                hits.append(f"{path.relative_to(ROOT)}:{token}")
    assert hits == []
