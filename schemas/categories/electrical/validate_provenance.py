#!/usr/bin/env python3
"""Validate ARV-067G provenance, review history, conflicts and audit reporting."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from generate_provenance_report import build_report
from provenance_contract import (
    REVIEW_STATUSES,
    VALID_TRANSITIONS,
    canonical_hash,
    claim_assertion_payload,
    event_payload,
)

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
FIXTURE_DIR = REPO_ROOT / "fixtures" / "ontology" / "electrical"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


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


def git_blob_sha1(path: Path) -> str:
    data = path.read_bytes()
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def error(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def load_dataset() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    manifest = load_yaml(HERE / "provenance_registry.v1.yaml")
    sources = load_yaml(HERE / manifest["source_file"])["sources"]
    claims = [
        row
        for relative_path in manifest["claim_files"]
        for row in load_yaml(HERE / relative_path)["claims"]
    ]
    events = load_yaml(HERE / manifest["review_event_file"])["events"]
    conflicts = load_yaml(HERE / manifest["conflict_file"])["conflicts"]
    report = load_yaml(HERE / manifest["audit_report_file"])
    return manifest, sources, claims, events, conflicts, report


def source_subject_indexes() -> dict[str, set[str]]:
    switches = load_yaml(HERE / "category_nodes" / "switches.v1.yaml")
    switching_attributes = load_yaml(HERE / "attributes" / "switching_insulation.v1.yaml")
    attributes = load_yaml(HERE / "attribute_registry.v1.yaml")
    relations = load_yaml(HERE / "relation_assertions" / "switching.v1.yaml")
    norms = load_yaml(HERE / "normative_requirement_fragments" / "rossetti_primary.v1.yaml")
    return {
        "categories": {row["category_id"] for row in switches["nodes"]},
        "attributes": {row["id"] for row in switching_attributes["attributes"]},
        "relations": {row["assertion_id"] for row in relations["assertions"]},
        "normative_requirements": {row["id"] for row in norms["requirements"]},
        "value_sets": set(attributes["value_sets"]),
        "allowed_values": {
            f"value_set:{key}:{value}"
            for key, item in attributes["value_sets"].items()
            for value in item["values"]
        },
    }


def validate_schemas() -> None:
    names = [
        "provenance_registry.schema.json",
        "provenance_sources.schema.json",
        "provenance_claim_fragment.schema.json",
        "provenance_review_events.schema.json",
        "provenance_conflicts.schema.json",
        "provenance_cases.schema.json",
    ]
    for name in names:
        schema = load_json(HERE / name)
        if not schema.get("$schema", "").endswith("2020-12/schema"):
            raise AssertionError(f"{name}: draft 2020-12 required")
        if schema.get("additionalProperties") is not False:
            raise AssertionError(f"{name}: root schema must be closed")


def validate_dataset(
    manifest: dict[str, Any],
    sources: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    events: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    report: dict[str, Any],
    *,
    verify_repository_files: bool = True,
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    source_by_revision = {row["revision_id"]: row for row in sources}
    claim_by_id = {row["claim_id"]: row for row in claims}
    event_by_id = {row["event_id"]: row for row in events}
    conflict_by_id = {row["conflict_id"]: row for row in conflicts}
    if len(source_by_revision) != len(sources):
        errors.append(error("PRV_DUPLICATE_SOURCE_REVISION", "source revision ids must be unique"))
    if len(claim_by_id) != len(claims):
        errors.append(error("PRV_DUPLICATE_CLAIM", "claim ids must be unique"))
    if len(event_by_id) != len(events):
        errors.append(error("PRV_DUPLICATE_EVENT", "review event ids must be unique"))
    if len(conflict_by_id) != len(conflicts):
        errors.append(error("PRV_DUPLICATE_CONFLICT", "conflict ids must be unique"))

    source_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for source in sources:
        rid = source["revision_id"]
        source_groups[source["source_id"]].append(source)
        algo = source["hash_algorithm"]
        content_hash = source["content_hash"]
        if algo == "git_blob_sha1" and not HEX40.fullmatch(content_hash):
            errors.append(error("PRV_SOURCE_HASH_INVALID", rid))
        elif algo == "sha256" and not HEX64.fullmatch(content_hash):
            errors.append(error("PRV_SOURCE_HASH_INVALID", rid))
        if source.get("full_text_stored") or source.get("protected_content_stored"):
            errors.append(error("PRV_PROTECTED_FULL_TEXT_FORBIDDEN", rid))
        if source.get("secrets_stored"):
            errors.append(error("PRV_SECRET_STORAGE_FORBIDDEN", rid))
        supersedes = source.get("supersedes_revision_id")
        if supersedes and supersedes not in source_by_revision:
            errors.append(error("PRV_UNKNOWN_SUPERSEDED_SOURCE", rid))
        if verify_repository_files and source["source_kind"] == "repository_asset":
            path = REPO_ROOT / source["location"]
            if not path.exists():
                errors.append(error("PRV_SOURCE_FILE_MISSING", rid))
            elif git_blob_sha1(path) != content_hash:
                errors.append(error("PRV_SOURCE_REVISION_REWRITE", rid))
    for source_id, rows in source_groups.items():
        if sum(bool(row["is_current"]) for row in rows) != 1:
            errors.append(error("PRV_SOURCE_CURRENT_REVISION_INVALID", source_id))

    indexes = source_subject_indexes() if verify_repository_files else None
    allowed_claim_types = set(manifest["claim_types"])
    low_threshold = float(manifest["confidence_policy"]["low_below"])
    production_minimum = float(manifest["confidence_policy"]["production_minimum"])
    allowed_reviewer_roles = set(manifest["production_policy"]["reviewer_roles_required"])
    logical_active: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for claim in claims:
        cid = claim["claim_id"]
        logical_active[claim["logical_claim_id"]].append(claim)
        if claim["claim_type"] not in allowed_claim_types:
            errors.append(error("PRV_UNKNOWN_CLAIM_TYPE", cid))
        if claim["assertion_hash"] != canonical_hash(claim_assertion_payload(claim)):
            errors.append(error("PRV_ASSERTION_HASH_MISMATCH", cid))
        confidence = float(claim["confidence"])
        if not 0 <= confidence <= 1:
            errors.append(error("PRV_CONFIDENCE_OUT_OF_RANGE", cid))
        if not claim["source_links"]:
            errors.append(error("PRV_SOURCE_LINK_REQUIRED", cid))
        for link in claim["source_links"]:
            source = source_by_revision.get(link["source_revision_id"])
            if source is None:
                errors.append(error("PRV_UNKNOWN_SOURCE_REVISION", cid))
                continue
            if link["source_content_hash"] != source["content_hash"]:
                errors.append(error("PRV_SOURCE_LINK_HASH_MISMATCH", cid))
            locator = link["locator"]
            if not any(locator.get(key) is not None for key in ("path", "page", "row", "clause", "json_pointer")):
                errors.append(error("PRV_SOURCE_LOCATOR_REQUIRED", cid))
        review = claim["review"]
        status = review["status"]
        if status not in REVIEW_STATUSES:
            errors.append(error("PRV_UNKNOWN_REVIEW_STATUS", cid))
        if status == "human_verified":
            if not review.get("reviewer_id") or not review.get("reviewed_at") or not review.get("rationale"):
                errors.append(error("PRV_HUMAN_REVIEW_METADATA_REQUIRED", cid))
            if review.get("reviewer_role") not in set(manifest["reviewer_roles"]):
                errors.append(error("PRV_REVIEWER_ROLE_INVALID", cid))
        if confidence < low_threshold and (not claim["review_required"] or claim["production_ready"]):
            errors.append(error("PRV_LOW_CONFIDENCE_REVIEW_GATE", cid))
        if claim.get("sensitive_content_stored") or claim.get("protected_full_text_stored"):
            errors.append(error("PRV_CLAIM_SENSITIVE_CONTENT_FORBIDDEN", cid))
        supersedes = claim.get("supersedes_claim_id")
        if supersedes and supersedes not in claim_by_id:
            errors.append(error("PRV_UNKNOWN_SUPERSEDED_CLAIM", cid))
        if claim["production_ready"]:
            if status != manifest["production_policy"]["status_required"]:
                errors.append(error("PRV_PRODUCTION_REQUIRES_HUMAN_VERIFICATION", cid))
            if confidence < production_minimum:
                errors.append(error("PRV_PRODUCTION_CONFIDENCE_TOO_LOW", cid))
            if review.get("reviewer_role") not in allowed_reviewer_roles:
                errors.append(error("PRV_PRODUCTION_REVIEWER_ROLE_REQUIRED", cid))
            if claim["review_required"]:
                errors.append(error("PRV_PRODUCTION_REVIEW_STILL_REQUIRED", cid))
            if not claim["active"]:
                errors.append(error("PRV_PRODUCTION_CLAIM_INACTIVE", cid))
            for conflict_id in claim["conflict_group_ids"]:
                conflict = conflict_by_id.get(conflict_id)
                if conflict and conflict["status"] == "unresolved":
                    errors.append(error("PRV_PRODUCTION_UNRESOLVED_CONFLICT", cid))
            for link in claim["source_links"]:
                source = source_by_revision.get(link["source_revision_id"])
                if source and (not source["is_current"] or source["source_status"] != "active"):
                    errors.append(error("PRV_PRODUCTION_SOURCE_NOT_CURRENT", cid))
        if indexes is not None:
            subject_id = claim["subject"]["id"]
            if claim["claim_type"] in {"category", "alias"} and subject_id not in indexes["categories"]:
                errors.append(error("PRV_UNKNOWN_CATEGORY_SUBJECT", cid))
            elif claim["claim_type"] == "attribute" and subject_id not in indexes["attributes"]:
                errors.append(error("PRV_UNKNOWN_ATTRIBUTE_SUBJECT", cid))
            elif claim["claim_type"] == "relation" and subject_id not in indexes["relations"]:
                errors.append(error("PRV_UNKNOWN_RELATION_SUBJECT", cid))
            elif claim["claim_type"] == "normative_requirement" and subject_id not in indexes["normative_requirements"]:
                errors.append(error("PRV_UNKNOWN_NORMATIVE_SUBJECT", cid))
            elif claim["claim_type"] == "allowed_value":
                key = f"{subject_id}:{claim['object_value']}"
                if key not in indexes["allowed_values"]:
                    errors.append(error("PRV_UNKNOWN_ALLOWED_VALUE_SUBJECT", cid))

    for logical_id, rows in logical_active.items():
        active_rows = [row for row in rows if row["active"]]
        if len(active_rows) > 1:
            errors.append(error("PRV_MULTIPLE_ACTIVE_CLAIM_VERSIONS", logical_id))
        versions = [int(row["claim_version"]) for row in rows]
        if len(set(versions)) != len(versions):
            errors.append(error("PRV_DUPLICATE_CLAIM_VERSION", logical_id))

    events_by_claim: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        eid = event["event_id"]
        if event["claim_id"] not in claim_by_id:
            errors.append(error("PRV_EVENT_UNKNOWN_CLAIM", eid))
            continue
        events_by_claim[event["claim_id"]].append(event)
        if event["event_hash"] != canonical_hash(event_payload(event)):
            errors.append(error("PRV_EVENT_HASH_MISMATCH", eid))
        if event["to_status"] not in VALID_TRANSITIONS.get(event["from_status"], set()):
            errors.append(error("PRV_INVALID_REVIEW_TRANSITION", eid))
        if event["actor_type"] == "human" and (not event.get("reviewer_id") or not event.get("reviewer_role")):
            errors.append(error("PRV_HUMAN_EVENT_REVIEWER_REQUIRED", eid))
    for claim_id, rows in events_by_claim.items():
        ordered = sorted(rows, key=lambda row: int(row["sequence"]))
        expected_previous = None
        expected_from = None
        for sequence, event in enumerate(ordered, start=1):
            if event["sequence"] != sequence:
                errors.append(error("PRV_EVENT_SEQUENCE_GAP", claim_id))
            if event["previous_event_hash"] != expected_previous:
                errors.append(error("PRV_EVENT_CHAIN_BROKEN", event["event_id"]))
            if event["from_status"] != expected_from:
                errors.append(error("PRV_EVENT_FROM_STATUS_MISMATCH", event["event_id"]))
            expected_previous = event["event_hash"]
            expected_from = event["to_status"]
        claim = claim_by_id[claim_id]
        current = ordered[-1]
        if claim["review"]["current_event_id"] != current["event_id"] or claim["review"]["current_event_hash"] != current["event_hash"]:
            errors.append(error("PRV_CURRENT_EVENT_MISMATCH", claim_id))
        if claim["review"]["status"] != current["to_status"]:
            errors.append(error("PRV_REVIEW_STATUS_EVENT_MISMATCH", claim_id))
    for claim in claims:
        if claim["claim_id"] not in events_by_claim:
            errors.append(error("PRV_REVIEW_EVENT_REQUIRED", claim["claim_id"]))

    for conflict in conflicts:
        conflict_id = conflict["conflict_id"]
        member_ids = conflict["claim_ids"]
        if len(member_ids) < 2:
            errors.append(error("PRV_CONFLICT_MEMBERS_REQUIRED", conflict_id))
        for claim_id in member_ids:
            claim = claim_by_id.get(claim_id)
            if claim is None:
                errors.append(error("PRV_CONFLICT_UNKNOWN_CLAIM", conflict_id))
                continue
            if conflict_id not in claim["conflict_group_ids"]:
                errors.append(error("PRV_CONFLICT_BACKREF_REQUIRED", claim_id))
            if conflict["status"] == "unresolved" and (not claim["review_required"] or claim["production_ready"]):
                errors.append(error("PRV_UNRESOLVED_CONFLICT_REVIEW_GATE", claim_id))
        if conflict["status"] == "resolved" and not conflict.get("resolution_event_id"):
            errors.append(error("PRV_CONFLICT_RESOLUTION_EVENT_REQUIRED", conflict_id))

    expected_report = build_report(manifest, sources, claims, events, conflicts)
    if report != expected_report:
        errors.append(error("PRV_AUDIT_REPORT_STALE", "committed report differs from deterministic output"))
    return errors


def apply_mutation(
    manifest: dict[str, Any],
    sources: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    events: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    report: dict[str, Any],
    case: dict[str, Any],
) -> None:
    mutation = case["mutation"]
    target = case.get("target_id")
    if mutation == "none":
        return
    claim = next((row for row in claims if row["claim_id"] == target), None)
    source = next((row for row in sources if row["revision_id"] == target), None)
    event = next((row for row in events if row["event_id"] == target), None)
    if mutation == "unknown_source":
        claim["source_links"][0]["source_revision_id"] = "SRC-UNKNOWN@1"
    elif mutation == "source_link_hash_mismatch":
        claim["source_links"][0]["source_content_hash"] = "0" * 40
    elif mutation == "bad_assertion_hash":
        claim["assertion_hash"] = "0" * 64
    elif mutation == "unknown_claim_type":
        claim["claim_type"] = "unknown"
    elif mutation == "low_confidence_not_review":
        claim["review_required"] = False
    elif mutation == "low_confidence_production":
        claim["production_ready"] = True
    elif mutation == "human_verified_no_reviewer":
        claim["review"]["status"] = "human_verified"
    elif mutation == "human_verified_no_date":
        claim["review"].update({"status": "human_verified", "reviewer_id": "reviewer-1", "reviewer_role": "electrical_domain_expert"})
    elif mutation == "human_verified_bad_role":
        claim["review"].update({"status": "human_verified", "reviewer_id": "reviewer-1", "reviewer_role": "sales", "reviewed_at": "2026-07-27T12:00:00Z"})
    elif mutation == "production_machine_extracted":
        claim["production_ready"] = True
    elif mutation == "production_review_still_required":
        claim["production_ready"] = True
        claim["review"].update({"status": "human_verified", "reviewer_id": "reviewer-1", "reviewer_role": "electrical_domain_expert", "reviewed_at": "2026-07-27T12:00:00Z"})
    elif mutation == "event_hash_mismatch":
        event["event_hash"] = "0" * 64
    elif mutation == "event_chain_break":
        event["previous_event_hash"] = "1" * 64
    elif mutation == "event_sequence_gap":
        event["sequence"] = 3
    elif mutation == "event_status_mismatch":
        claim["review"]["status"] = "rejected"
    elif mutation == "duplicate_claim":
        claims.append(copy.deepcopy(claim))
    elif mutation == "duplicate_source_revision":
        sources.append(copy.deepcopy(source))
    elif mutation == "source_revision_rewrite":
        source["content_hash"] = "0" * len(source["content_hash"])
    elif mutation == "secret_storage_enabled":
        source["secrets_stored"] = True
    elif mutation == "protected_full_text_enabled":
        source["protected_content_stored"] = True
    elif mutation == "claim_sensitive_content":
        claim["sensitive_content_stored"] = True
    elif mutation == "supersedes_unknown":
        claim["supersedes_claim_id"] = "CLM-UNKNOWN@1"
    elif mutation == "multiple_active_versions":
        clone = copy.deepcopy(claim)
        clone["claim_id"] = claim["logical_claim_id"] + "@2"
        clone["claim_version"] = 2
        claims.append(clone)
    elif mutation == "unresolved_conflict_bypass":
        conflict_id = "CONFLICT-FIXTURE-1"
        conflicts.append({"conflict_id": conflict_id, "claim_ids": [claim["claim_id"], claims[0]["claim_id"]], "status": "unresolved", "rationale": "fixture", "resolution_event_id": None})
        claim["conflict_group_ids"].append(conflict_id)
        claims[0]["conflict_group_ids"].append(conflict_id)
        claim["production_ready"] = True
    elif mutation == "resolved_conflict_without_event":
        conflict_id = "CONFLICT-FIXTURE-2"
        conflicts.append({"conflict_id": conflict_id, "claim_ids": [claim["claim_id"], claims[0]["claim_id"]], "status": "resolved", "rationale": "fixture", "resolution_event_id": None})
        claim["conflict_group_ids"].append(conflict_id)
        claims[0]["conflict_group_ids"].append(conflict_id)
    elif mutation == "stale_report":
        report["counts"]["claims"] = 999
    elif mutation == "source_current_revision_invalid":
        source["is_current"] = False
    else:
        raise ValueError(f"unknown mutation {mutation}")


def main() -> int:
    try:
        validate_schemas()
        manifest, sources, claims, events, conflicts, report = load_dataset()
        errors = validate_dataset(manifest, sources, claims, events, conflicts, report)
        if errors:
            raise AssertionError(errors)
        expected = manifest["counts"]
        actual = {
            "sources": len(sources),
            "claims": len(claims),
            "review_events": len(events),
            "conflict_groups": len(conflicts),
        }
        for key, value in actual.items():
            if expected[key] != value:
                raise AssertionError(f"{key}: expected {expected[key]}, got {value}")
        fixtures = load_yaml(FIXTURE_DIR / "provenance_cases.yaml")
        if len(fixtures["cases"]) != expected["fixture_cases"]:
            raise AssertionError("fixture count mismatch")
        for case in fixtures["cases"]:
            m = copy.deepcopy(manifest)
            s = copy.deepcopy(sources)
            c = copy.deepcopy(claims)
            e = copy.deepcopy(events)
            f = copy.deepcopy(conflicts)
            r = copy.deepcopy(report)
            apply_mutation(m, s, c, e, f, r, case)
            codes = {row["code"] for row in validate_dataset(m, s, c, e, f, r, verify_repository_files=False)}
            expected_codes = set(case["expected_error_codes"])
            if not expected_codes <= codes:
                raise AssertionError(f"{case['id']}: expected {sorted(expected_codes)}, got {sorted(codes)}")
            if not expected_codes and codes:
                raise AssertionError(f"{case['id']}: unexpected {sorted(codes)}")
        print(
            "ARV-067G provenance: OK "
            f"(sources={len(sources)}, claims={len(claims)}, review_events={len(events)}, "
            f"conflicts={len(conflicts)}, fixture_cases={len(fixtures['cases'])}, "
            f"production_ready={report['counts']['production_ready']}, runtime_import=false)"
        )
        return 0
    except Exception as exc:
        print(f"ARV-067G provenance: FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
