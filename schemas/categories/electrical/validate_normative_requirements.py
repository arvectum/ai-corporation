#!/usr/bin/env python3
"""Validate ARV-067F clause-level normative requirements and fail-closed fixtures."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
FIXTURE_DIR = REPO_ROOT / "fixtures" / "ontology" / "electrical"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be object")
    return value


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be object")
    return value


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u00ad", "")).strip()


def sha_text(value: str) -> str:
    return hashlib.sha256(normalize(value).encode("utf-8")).hexdigest()


def load_data() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    manifest = load_yaml(HERE / "normative_requirements.v1.yaml")
    documents = load_yaml(HERE / manifest["document_edition_file"])
    requirements: list[dict[str, Any]] = []
    for relative_path in manifest["requirement_files"]:
        requirements.extend(load_yaml(HERE / relative_path)["requirements"])
    fixtures = load_yaml(FIXTURE_DIR / "normative_requirement_cases.yaml")
    return manifest, documents, requirements, fixtures


def load_category_ids() -> set[str]:
    manifest = load_yaml(HERE / "category_tree.v1.yaml")
    return {str(node["category_id"]) for path in manifest["node_files"] for node in load_yaml(HERE / path)["nodes"]}


def load_attribute_ids() -> set[str]:
    registry = load_yaml(HERE / "attribute_registry.v1.yaml")
    return {str(row["id"]) for path in registry["attribute_files"] for row in load_yaml(HERE / path)["attributes"]}


def load_normative_document_ids() -> set[str]:
    registry = load_yaml(HERE / "normative_registry.v1.yaml")
    return {str(row["id"]) for row in registry["documents"]}


def error(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def _constraint_signature(requirement: dict[str, Any]) -> str:
    constraint = requirement["constraint"]
    key = constraint.get("attribute_id") or constraint.get("context_field") or constraint.get("evidence_kind") or constraint.get("documentation_reference")
    conditions = json.dumps(requirement["applies_when"], ensure_ascii=False, sort_keys=True)
    categories = ",".join(sorted(requirement["category_refs"]))
    return f"{categories}|{key}|{conditions}"


def _constraint_value(requirement: dict[str, Any]) -> str:
    return json.dumps(requirement["constraint"], ensure_ascii=False, sort_keys=True)


def validate_dataset(manifest: dict[str, Any], documents_doc: dict[str, Any], requirements: list[dict[str, Any]], *, categories: set[str], attributes: set[str], normative_ids: set[str]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    docs = documents_doc["documents"]
    doc_by_id = {str(row["edition_id"]): row for row in docs}
    if len(doc_by_id) != len(docs):
        errors.append(error("NRF_DUPLICATE_DOCUMENT_EDITION", "duplicate document edition id"))
    req_by_id = {str(row["id"]): row for row in requirements}
    if len(req_by_id) != len(requirements):
        errors.append(error("NRF_DUPLICATE_REQUIREMENT", "duplicate requirement id"))
    priorities = manifest["applicability_priorities"]
    constraint_types = set(manifest["constraint_types"])

    for document in docs:
        did = str(document["edition_id"])
        if document["normative_document_id"] not in normative_ids:
            errors.append(error("NRF_UNKNOWN_NORMATIVE_DOCUMENT", did))
        if document.get("current_status_inferred") is not False:
            errors.append(error("NRF_EDITION_STATUS_INFERENCE_FORBIDDEN", did))
        if document["source"].get("full_text_stored") is not False:
            errors.append(error("NRF_FULL_TEXT_STORAGE_FORBIDDEN", did))
        if not SHA256_RE.fullmatch(str(document["source"].get("file_sha256", ""))):
            errors.append(error("NRF_SOURCE_FILE_HASH_INVALID", did))

    for requirement in requirements:
        rid = str(requirement["id"])
        document = doc_by_id.get(str(requirement.get("document_edition_id")))
        if document is None:
            errors.append(error("NRF_UNKNOWN_DOCUMENT_EDITION", rid))
        page = requirement.get("page")
        if not isinstance(page, int) or page < 1:
            errors.append(error("NRF_SOURCE_PAGE_REQUIRED", rid))
        elif document and page > int(document["source"]["page_count"]):
            errors.append(error("NRF_SOURCE_PAGE_OUT_OF_BOUNDS", rid))
        if not SHA256_RE.fullmatch(str(requirement.get("page_text_sha256", ""))):
            errors.append(error("NRF_PAGE_HASH_INVALID", rid))
        excerpt = str(requirement.get("source_excerpt", ""))
        if not excerpt or len(excerpt) > 220:
            errors.append(error("NRF_SOURCE_EXCERPT_REQUIRED", rid))
        if requirement.get("source_excerpt_sha256") != sha_text(excerpt):
            errors.append(error("NRF_SOURCE_EXCERPT_HASH_MISMATCH", rid))
        if document and requirement["provenance"].get("source_file_sha256") != document["source"]["file_sha256"]:
            errors.append(error("NRF_SOURCE_FILE_HASH_MISMATCH", rid))
        if requirement["provenance"].get("full_text_stored") is not False:
            errors.append(error("NRF_FULL_TEXT_STORAGE_FORBIDDEN", rid))
        for category in requirement.get("category_refs", []):
            if category not in categories:
                errors.append(error("NRF_UNKNOWN_CATEGORY", f"{rid}:{category}"))
        for attribute in requirement.get("attribute_refs", []):
            if attribute not in attributes:
                errors.append(error("NRF_UNKNOWN_ATTRIBUTE", f"{rid}:{attribute}"))
        conditions = requirement.get("applies_when", {}).get("all", [])
        if not conditions:
            errors.append(error("NRF_APPLIES_WHEN_REQUIRED", rid))
        if requirement.get("automatic_compliance_decision") is not False:
            errors.append(error("NRF_AUTOMATIC_COMPLIANCE_FORBIDDEN", rid))
        if requirement.get("human_review_required") is not True:
            errors.append(error("NRF_HUMAN_REVIEW_REQUIRED", rid))
        level = requirement.get("applicability_level")
        if level not in priorities:
            errors.append(error("NRF_UNKNOWN_APPLICABILITY_LEVEL", rid))
        elif requirement.get("priority") != priorities[level]:
            errors.append(error("NRF_PRIORITY_MISMATCH", rid))
        review = requirement.get("review", {})
        if review.get("status") == "expert_reviewed" and not review.get("reviewer_id"):
            errors.append(error("NRF_REVIEWER_REQUIRED", rid))
        constraint = requirement.get("constraint", {})
        ctype = constraint.get("type")
        if ctype not in constraint_types:
            errors.append(error("NRF_UNKNOWN_CONSTRAINT_TYPE", rid))
            continue
        attribute_id = constraint.get("attribute_id")
        if attribute_id and attribute_id not in requirement.get("attribute_refs", []):
            errors.append(error("NRF_CONSTRAINT_ATTRIBUTE_NOT_DECLARED", rid))
        active_fields = {
            key for key in ["values", "minimum", "maximum", "range", "evidence_kind", "marking_pattern", "documentation_reference"]
            if constraint.get(key) not in (None, [], {})
        }
        allowed = {
            "allowed_values": {"values"}, "minimum": {"minimum"}, "maximum": {"maximum"}, "range": {"range"},
            "required_evidence": {"evidence_kind", "documentation_reference"},
            "marking": {"values", "marking_pattern"}, "documentation": {"documentation_reference"},
        }.get(ctype, set())
        required_any = {
            "required_evidence": {"evidence_kind"}, "marking": {"values", "marking_pattern"},
        }.get(ctype, allowed)
        if not active_fields or not active_fields <= allowed or not (active_fields & required_any):
            errors.append(error("NRF_CONSTRAINT_PAYLOAD_INVALID", rid))

    groups: dict[str, list[dict[str, Any]]] = {}
    for requirement in requirements:
        if requirement.get("status") not in {"candidate", "active_snapshot"}:
            continue
        groups.setdefault(_constraint_signature(requirement), []).append(requirement)
    for rows in groups.values():
        by_priority: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            by_priority.setdefault(int(row.get("priority", -1)), []).append(row)
        for priority_rows in by_priority.values():
            values = {_constraint_value(row) for row in priority_rows}
            if len(priority_rows) > 1 and len(values) > 1:
                errors.append(error("NRF_UNRESOLVED_CONFLICT", ",".join(sorted(str(row["id"]) for row in priority_rows))))
    return errors


def apply_mutation(manifest: dict[str, Any], documents: dict[str, Any], requirements: list[dict[str, Any]], case: dict[str, Any]) -> None:
    mutation = case["mutation"]
    target = case.get("target_id")
    if mutation == "none": return
    req = next((row for row in requirements if row["id"] == target), None)
    doc = next((row for row in documents["documents"] if row["edition_id"] == target), None)
    if mutation == "unknown_document": req["document_edition_id"] = "UNKNOWN@0"
    elif mutation == "missing_page": req["page"] = None
    elif mutation == "bad_quote_hash": req["source_excerpt_sha256"] = "0" * 64
    elif mutation == "unknown_category": req["category_refs"] = ["electrical.unknown"]
    elif mutation == "unknown_attribute": req["attribute_refs"] = ["unknown_attribute"]
    elif mutation == "missing_conditions": req["applies_when"]["all"] = []
    elif mutation == "auto_compliance_enabled": req["automatic_compliance_decision"] = True
    elif mutation == "review_gate_disabled": req["human_review_required"] = False
    elif mutation == "priority_mismatch": req["priority"] = 999
    elif mutation == "constraint_payload_invalid": req["constraint"]["minimum"] = 1
    elif mutation == "evidence_kind_missing": req["constraint"]["evidence_kind"] = None
    elif mutation == "attribute_not_declared": req["attribute_refs"] = []
    elif mutation == "page_out_of_bounds": req["page"] = 9999
    elif mutation == "current_status_inferred": doc["current_status_inferred"] = True
    elif mutation == "full_text_stored": doc["source"]["full_text_stored"] = True
    elif mutation == "approved_without_reviewer": req["review"]["status"] = "expert_reviewed"
    elif mutation == "same_priority_conflict":
        clone = copy.deepcopy(req); clone["id"] = req["id"] + "-CONFLICT"; clone["constraint"]["values"] = [999]; requirements.append(clone)
    elif mutation == "unknown_constraint_type": req["constraint"]["type"] = "unsupported"
    elif mutation == "source_file_hash_mismatch": req["provenance"]["source_file_sha256"] = "0" * 64
    else: raise ValueError(mutation)


def validate_schemas() -> None:
    for name in ["normative_requirements.schema.json","normative_document_editions.schema.json","normative_requirement_fragment.schema.json","normative_requirement_cases.schema.json"]:
        schema = load_json(HERE / name)
        if not schema.get("$schema", "").endswith("2020-12/schema") or schema.get("additionalProperties") is not False:
            raise AssertionError(f"{name}: schema must be closed draft 2020-12")


def main() -> int:
    try:
        validate_schemas()
        manifest, documents, requirements, fixtures = load_data()
        categories = load_category_ids(); attributes = load_attribute_ids(); normative_ids = load_normative_document_ids()
        errors = validate_dataset(manifest, documents, requirements, categories=categories, attributes=attributes, normative_ids=normative_ids)
        if errors: raise AssertionError(errors)
        if len(documents["documents"]) != manifest["counts"]["document_editions"]: raise AssertionError("document count")
        if len(requirements) != manifest["counts"]["requirements"]: raise AssertionError("requirement count")
        if len(fixtures["cases"]) != manifest["counts"]["fixture_cases"]: raise AssertionError("fixture count")
        for case in fixtures["cases"]:
            m = copy.deepcopy(manifest); d = copy.deepcopy(documents); r = copy.deepcopy(requirements)
            apply_mutation(m, d, r, case)
            codes = {row["code"] for row in validate_dataset(m, d, r, categories=categories, attributes=attributes, normative_ids=normative_ids)}
            expected = set(case["expected_error_codes"])
            if not expected <= codes: raise AssertionError(f"{case['id']}: expected {sorted(expected)}, got {sorted(codes)}")
            if not expected and codes: raise AssertionError(f"{case['id']}: unexpected {sorted(codes)}")
        print(f"ARV-067F normative requirements: OK (document_editions={len(documents['documents'])}, requirements={len(requirements)}, fixture_cases={len(fixtures['cases'])}, runtime_import=false)")
        return 0
    except Exception as exc:
        print(f"ARV-067F normative requirements: FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
