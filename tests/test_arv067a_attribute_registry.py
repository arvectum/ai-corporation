from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "schemas" / "categories" / "electrical"
MODULE_PATH = DIRECTORY / "validate_attributes.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("arv067a_validate_attributes", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_attributes(registry: dict) -> list[dict]:
    return [
        item
        for relative_path in registry["attribute_files"]
        for item in yaml.safe_load(
            (DIRECTORY / relative_path).read_text(encoding="utf-8")
        )["attributes"]
    ]


def test_arv067a_attribute_registry_validates() -> None:
    module = _load_validator()
    assert module.main() == 0


def test_arv067a_registry_covers_detailed_taxonomy_and_wave1_attributes() -> None:
    registry = yaml.safe_load(
        (DIRECTORY / "attribute_registry.v1.yaml").read_text(encoding="utf-8")
    )
    ontology = yaml.safe_load(
        (DIRECTORY / "electrical.v1.yaml").read_text(encoding="utf-8")
    )
    nomenclature = yaml.safe_load(
        (DIRECTORY / "nomenclature.v1.yaml").read_text(encoding="utf-8")
    )
    wave1_manifest = yaml.safe_load(
        (DIRECTORY / "detailed_profiles_wave1.v1.yaml").read_text(encoding="utf-8")
    )

    attributes = _load_attributes(registry)
    registered = {item["id"] for item in attributes}
    verified = {
        item["id"]
        for item in attributes
        if item["maturity"] == "verified_detailed_profile"
    }
    original_detailed = {
        attribute["id"]
        for category in ontology["categories"]
        for attribute in category["attributes"]
    }
    wave1_detailed = {
        rule["id"]
        for relative_path in wave1_manifest["profile_files"]
        for profile in yaml.safe_load(
            (DIRECTORY / relative_path).read_text(encoding="utf-8")
        )["profiles"]
        for rule in profile["attributes"]
    }
    discriminators = {
        attribute_id
        for section in nomenclature["sections"]
        for attribute_id in section["discriminator_attributes"]
    }

    assert original_detailed <= verified
    assert wave1_detailed <= verified
    assert original_detailed | wave1_detailed <= registered
    assert discriminators <= registered
    assert len(registered) == 156
    assert len(verified) == 52
    assert registry["profile_id_aliases"] == {
        "electromechanical_contactor": "electromagnetic_contactor"
    }
    assert registry["runtime_import"] is False
    assert registry["governance"]["provisional_attributes_are_not_match_rules"] is True
    assert (
        registry["governance"]["role_specific_voltage_attributes_must_not_be_auto_merged"]
        is True
    )


def test_arv067a_comparator_contract_is_type_safe() -> None:
    registry = yaml.safe_load(
        (DIRECTORY / "attribute_registry.v1.yaml").read_text(encoding="utf-8")
    )
    comparators = {
        item["id"]: set(item["allowed_value_types"])
        for item in registry["comparators"]
    }
    for attribute in _load_attributes(registry):
        assert attribute["value_type"] in comparators[attribute["default_comparator"]]


def test_arv067a_runtime_is_not_wired() -> None:
    forbidden = {
        "attribute_registry.v1.yaml",
        "ARV-067A-ELECTRICAL-ATTRIBUTE-REGISTRY",
    }
    hits: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                hits.append(f"{path.relative_to(ROOT)}:{token}")
    assert hits == []
