#!/usr/bin/env python3
"""Shared immutable provenance helpers for ARV-067G."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

REVIEW_STATUSES = {"machine_extracted", "human_verified", "rejected", "superseded"}
VALID_TRANSITIONS = {
    None: {"machine_extracted"},
    "machine_extracted": {"human_verified", "rejected", "superseded"},
    "human_verified": {"superseded"},
    "rejected": {"superseded"},
    "superseded": set(),
}


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def claim_assertion_payload(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "claim_type": claim["claim_type"],
        "subject": claim["subject"],
        "predicate": claim["predicate"],
        "object_value": claim.get("object_value"),
        "object_ref": claim.get("object_ref"),
    }


def event_payload(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "event_hash"}


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
