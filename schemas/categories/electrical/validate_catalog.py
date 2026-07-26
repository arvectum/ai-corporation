#!/usr/bin/env python3
"""Validate ARV-067 Rosseti nomenclature and normative metadata registries."""
from __future__ import annotations

import json
import re
import sys
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


def load_yaml(name: str, *, fixture: bool = False) -> dict[str, Any]:
    base = FIXTURE_DIR if fixture else HERE
    value = yaml.safe_load((base / name).read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"{name}: root must be an object")
    return value


def load_json(name: str) -> dict[str, Any]:
    value = json.loads((HERE / name).read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"{name}: root must be an object")
    return value


def unique(values: list[str], label: str) -> None:
    require(len(values) == len(set(values)), f"duplicate {label}")


def validate_schema_contract(schema: dict[str, Any], required: set[str], label: str) -> None:
    require(schema.get("$schema", "").endswith("2020-12/schema"), f"{label}: schema draft mismatch")
    require(schema.get("type") == "object", f"{label}: schema root must be object")
    require(schema.get("additionalProperties") is False, f"{label}: schema must be closed")
    require(set(schema.get("required", [])) == required, f"{label}: required keys mismatch")
    require(set(schema.get("properties", {})) == required, f"{label}: properties mismatch")


def validate_catalog(catalog: dict[str, Any], manifest: dict[str, Any]) -> set[str]:
    required = {
        "catalog_id", "version", "status", "locale", "runtime_import", "purpose",
        "source_snapshots", "coverage", "sections", "governance",
    }
    require(set(catalog) == required, "catalog top-level keys mismatch")
    require(catalog["catalog_id"] == "ARV-067-ELECTRICAL-NOMENCLATURE", "catalog id mismatch")
    require(re.fullmatch(r"\d+\.\d+\.\d+", str(catalog["version"])) is not None, "catalog version invalid")
    require(catalog["status"] == "research_asset", "catalog status invalid")
    require(catalog["runtime_import"] is False, "catalog must remain offline")

    snapshots = catalog["source_snapshots"]
    require(isinstance(snapshots, list) and len(snapshots) == 2, "exactly two Rosseti snapshots required")
    snapshot_ids = [str(item["id"]) for item in snapshots]
    unique(snapshot_ids, "snapshot id")
    expected_pages = {"primary": 325, "secondary": 110}
    for snapshot in snapshots:
        registry = snapshot["registry"]
        require(snapshot["pages"] == expected_pages[registry], f"{registry}: page count mismatch")
        require(re.fullmatch(r"[a-f0-9]{64}", snapshot["snapshot_sha256"]) is not None, f"{registry}: bad sha256")
        require(snapshot["official_page_url"].startswith("https://www.rosseti.ru/"), f"{registry}: official URL missing")

    sections = catalog["sections"]
    require(isinstance(sections, list) and len(sections) == 28, "catalog must contain 28 Rosseti sections")
    section_ids = [str(item["id"]) for item in sections]
    unique(section_ids, "section id")
    counts = {"primary": 0, "secondary": 0}
    previous_row = {"primary": 0, "secondary": 0}
    previous_page = {"primary": 0, "secondary": 0}
    detailed_refs: set[str] = set()
    for section in sections:
        registry = section["registry"]
        counts[registry] += 1
        require(section["registry_row_start"] > previous_row[registry], f"{registry}: rows are not ordered")
        require(section["registry_page_start"] >= previous_page[registry], f"{registry}: pages are not ordered")
        previous_row[registry] = section["registry_row_start"]
        previous_page[registry] = section["registry_page_start"]
        require(len(section["aliases"]) >= 3, f"{section['id']}: aliases incomplete")
        require(len(section["subcategories"]) >= 3, f"{section['id']}: subcategories incomplete")
        require(len(section["discriminator_attributes"]) >= 5, f"{section['id']}: discriminator set weak")
        unique([str(v) for v in section["discriminator_attributes"]], f"{section['id']} discriminator")
        if section["detailed_profile_refs"]:
            require(section["profile_status"] == "detailed_and_taxonomy", f"{section['id']}: profile status mismatch")
            detailed_refs.update(section["detailed_profile_refs"])
        else:
            require(section["profile_status"] == "taxonomy_only", f"{section['id']}: taxonomy status mismatch")
    require(counts == {"primary": 21, "secondary": 7}, f"registry section counts mismatch: {counts}")
    require(detailed_refs == {"power_cable_low_voltage", "self_supporting_insulated_wire", "miniature_circuit_breaker", "electromechanical_contactor"}, "v1 detailed profile links mismatch")

    coverage = catalog["coverage"]
    require(coverage["primary_sections"] == 21, "primary coverage mismatch")
    require(coverage["secondary_sections"] == 7, "secondary coverage mismatch")
    require(coverage["total_sections"] == 28, "total coverage mismatch")
    governance = catalog["governance"]
    require(governance["operator_register_is_not_equivalence_proof"] is True, "registry equivalence guard missing")
    require(governance["normative_applicability_requires_human_review"] is True, "human normative review guard missing")
    require(governance["full_text_documents_are_not_stored"] is True, "copyright/full-text guard missing")

    require(manifest["section_ids"] == section_ids, "manifest section ids differ from catalog")
    require(manifest["snapshots"] == snapshots, "manifest snapshots differ from catalog")
    require(manifest["expected"]["total_sections"] == 28, "manifest total mismatch")
    return set(section_ids)


def validate_norms(norms: dict[str, Any], section_ids: set[str]) -> None:
    required = {
        "registry_id", "version", "status", "runtime_import", "checked_at", "purpose",
        "authorities_required", "documents", "precedence_policy", "decision_policy", "coverage",
    }
    require(set(norms) == required, "normative registry top-level keys mismatch")
    require(norms["registry_id"] == "ARV-067-ELECTRICAL-NORMATIVE-BASE", "normative registry id mismatch")
    require(norms["runtime_import"] is False, "normative registry must remain offline")
    documents = norms["documents"]
    require(isinstance(documents, list) and len(documents) >= 40, "normative seed is too small")
    ids = [str(item["id"]) for item in documents]
    unique(ids, "normative document id")
    required_docs = {
        "PUE-7", "STO-34.01-22-001-2023", "STO-34.01-22-002-2023",
        "GOST-R-58786-2019", "ROSATOM-STANDARDS-COLLECTION",
        "RUSHYDRO-TECHNICAL-POLICY", "STO-RUSHYDRO-05.02.126-2020",
    }
    require(required_docs.issubset(ids), "required operator/nuclear/hydropower documents missing")
    authority_text = "\n".join(str(item["authority"]) for item in documents)
    for authority in norms["authorities_required"]:
        require(authority in authority_text or authority in {"Минэнерго России", "ЕЭК"}, f"authority missing: {authority}")

    specifically_covered: set[str] = set()
    for item in documents:
        require(str(item["source_url"]).startswith("https://"), f"{item['id']}: source URL invalid")
        require(item["applies_to"], f"{item['id']}: applies_to empty")
        require(item["use_for"], f"{item['id']}: use_for empty")
        for category in item["applies_to"]:
            require(category == "*" or category in section_ids, f"{item['id']}: unknown category {category}")
            if category != "*":
                specifically_covered.add(category)
    require(section_ids.issubset(specifically_covered), "every section needs at least one specific normative/operator source")

    policy = norms["decision_policy"]
    require(policy["automatic_compliance_decision"] is False, "automatic compliance must remain disabled")
    require(policy["human_applicability_review_required"] is True, "human applicability review missing")
    require(policy["edition_and_amendment_check_required"] is True, "edition verification missing")
    require(policy["source_quote_and_page_required_for_claims"] is True, "evidence citation gate missing")
    require(policy["operator_registry_entry_is_not_general_equivalence_proof"] is True, "operator registry guard missing")
    require(len(norms["precedence_policy"]) == 5, "precedence policy must contain five levels")
    require(norms["coverage"]["document_count"] == len(documents), "normative document count mismatch")


def main() -> int:
    try:
        catalog_schema = load_json("nomenclature.schema.json")
        norms_schema = load_json("normative_registry.schema.json")
        catalog = load_yaml("nomenclature.v1.yaml")
        norms = load_yaml("normative_registry.v1.yaml")
        manifest = load_yaml("rosseti_registry_manifest.yaml", fixture=True)
        validate_schema_contract(catalog_schema, set(catalog), "catalog")
        validate_schema_contract(norms_schema, set(norms), "normative registry")
        section_ids = validate_catalog(catalog, manifest)
        validate_norms(norms, section_ids)
    except (OSError, json.JSONDecodeError, yaml.YAMLError, KeyError, TypeError, ValidationError) as exc:
        print(f"ARV-067 expanded electrical catalog: FAILED: {exc}", file=sys.stderr)
        return 1
    print(
        "ARV-067 expanded electrical catalog: OK "
        f"(sections={len(section_ids)}, normative_documents={len(norms['documents'])}, runtime_import=false)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
