from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "schemas" / "categories" / "electrical" / "validate_catalog.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("arv067_validate_catalog", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_arv067_expanded_catalog_validates() -> None:
    module = _load_validator()
    result = module.main()
    assert result == 0


def test_arv067_catalog_covers_rosseti_and_stays_offline() -> None:
    directory = ROOT / "schemas" / "categories" / "electrical"
    catalog = yaml.safe_load((directory / "nomenclature.v1.yaml").read_text(encoding="utf-8"))
    norms = yaml.safe_load((directory / "normative_registry.v1.yaml").read_text(encoding="utf-8"))
    section_ids = {item["id"] for item in catalog["sections"]}

    assert catalog["runtime_import"] is False
    assert norms["runtime_import"] is False
    assert len(section_ids) == 28
    assert catalog["coverage"] == {
        "primary_sections": 21,
        "secondary_sections": 7,
        "total_sections": 28,
        "detailed_profile_refs": [
            "power_cable_low_voltage",
            "self_supporting_insulated_wire",
            "miniature_circuit_breaker",
            "electromechanical_contactor",
        ],
    }
    assert "switches" in section_ids
    assert "power_transformers" in section_ids
    assert "automated_process_control_systems" in section_ids
    assert "relay_protection_automation" in section_ids
    assert "communications" in section_ids

    document_ids = {item["id"] for item in norms["documents"]}
    assert "PUE-7" in document_ids
    assert "STO-34.01-22-001-2023" in document_ids
    assert "STO-34.01-22-002-2023" in document_ids
    assert "GOST-R-58786-2019" in document_ids
    assert "ROSATOM-STANDARDS-COLLECTION" in document_ids
    assert "STO-RUSHYDRO-05.02.126-2020" in document_ids
    assert norms["decision_policy"]["automatic_compliance_decision"] is False
    assert norms["decision_policy"]["human_applicability_review_required"] is True


def test_arv067_runtime_has_no_catalog_import() -> None:
    src_root = ROOT / "src"
    forbidden = {
        "nomenclature.v1.yaml",
        "normative_registry.v1.yaml",
        "ARV-067-ELECTRICAL-NOMENCLATURE",
        "ARV-067-ELECTRICAL-NORMATIVE-BASE",
    }
    hits: list[str] = []
    for path in src_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                hits.append(f"{path.relative_to(ROOT)}:{token}")
    assert hits == []
