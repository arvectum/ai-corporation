#!/usr/bin/env python3
"""Generate a deterministic ARV-067G audit report."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

from provenance_contract import parse_timestamp

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]


def load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be object")
    return value


def load_dataset() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    manifest = load_yaml(HERE / "provenance_registry.v1.yaml")
    sources = load_yaml(HERE / manifest["source_file"])["sources"]
    claims = [
        row
        for relative_path in manifest["claim_files"]
        for row in load_yaml(HERE / relative_path)["claims"]
    ]
    events = load_yaml(HERE / manifest["review_event_file"])["events"]
    conflicts = load_yaml(HERE / manifest["conflict_file"])["conflicts"]
    return manifest, sources, claims, events, conflicts


def build_report(
    manifest: dict[str, Any],
    sources: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    events: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    as_of_text = manifest["report_policy"]["as_of"]
    as_of = parse_timestamp(as_of_text)
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for claim in claims:
        status = claim["review"]["status"]
        by_status[status] = by_status.get(status, 0) + 1
        by_type[claim["claim_type"]] = by_type.get(claim["claim_type"], 0) + 1

    stale_sources: set[str] = set()
    source_recheck_required: list[str] = []
    for source in sources:
        if source["source_status"] in {"superseded", "expired"}:
            stale_sources.add(source["revision_id"])
        due_at = source.get("review_due_at")
        if due_at and parse_timestamp(due_at) < as_of:
            stale_sources.add(source["revision_id"])
        if source["source_status"] == "snapshot_requires_recheck":
            source_recheck_required.append(source["revision_id"])

    unresolved = [row["conflict_id"] for row in conflicts if row["status"] == "unresolved"]
    low_threshold = manifest["confidence_policy"]["low_below"]
    return {
        "report_id": "ARV-067G-AUDIT-REPORT",
        "version": "1.0.0",
        "as_of": as_of_text,
        "counts": {
            "sources": len(sources),
            "claims": len(claims),
            "review_events": len(events),
            "conflict_groups": len(conflicts),
            "production_ready": sum(bool(row["production_ready"]) for row in claims),
            "review_required": sum(bool(row["review_required"]) for row in claims),
        },
        "claims_by_status": dict(sorted(by_status.items())),
        "claims_by_type": dict(sorted(by_type.items())),
        "unverified_claim_ids": [
            row["claim_id"] for row in claims if row["review"]["status"] != "human_verified"
        ],
        "low_confidence_claim_ids": [
            row["claim_id"] for row in claims if float(row["confidence"]) < low_threshold
        ],
        "stale_source_revision_ids": sorted(stale_sources),
        "source_recheck_required_ids": source_recheck_required,
        "unresolved_conflict_ids": unresolved,
        "production_blocked_claim_ids": [
            row["claim_id"] for row in claims if not row["production_ready"]
        ],
        "automatic_activation_allowed": False,
    }


def main() -> int:
    manifest, sources, claims, events, conflicts = load_dataset()
    report = build_report(manifest, sources, claims, events, conflicts)
    yaml.safe_dump(report, sys.stdout, allow_unicode=True, sort_keys=False, width=120)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
