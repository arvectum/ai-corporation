from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable

from src.modules.production_llm_analysis.schemas import (
    DataHandlingReport,
    EvidenceFragment,
    EvidenceFragmentInput,
    EvidencePacket,
)

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic))\s+[^\s]+"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)\s*[:=]\s*[^\s,;]+"),
)
_PATH_PATTERNS = (
    re.compile(r"(?<![\w:])/(?:Users|home|tmp|var|opt|private|mnt)/[^\s,;]+"),
    re.compile(r"(?i)\b[A-Z]:\\(?:Users|Temp|Windows|Program Files)[^\s,;]*"),
)
_SELECTED_FIELDS = ["document_id", "document_name", "chunk_id", "locator", "text"]


def canonical_json_bytes(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_document_name(value: str) -> tuple[str, int]:
    normalized = value.replace("\\", "/").rstrip("/")
    safe = normalized.rsplit("/", maxsplit=1)[-1].strip()
    if not safe:
        safe = "document"
    return safe, int(safe != value)


def _redact_text(value: str) -> tuple[str, int]:
    result = value
    redaction_count = 0
    for pattern in _SECRET_PATTERNS:
        result, count = pattern.subn(lambda match: f"{match.group(1)} [REDACTED_SECRET]" if match.lastindex else "[REDACTED_SECRET]", result)
        redaction_count += count
    for pattern in _PATH_PATTERNS:
        result, count = pattern.subn("[REDACTED_PATH]", result)
        redaction_count += count
    return result, redaction_count


def _fragment_payload(item: EvidenceFragmentInput) -> tuple[dict[str, Any], int, int, int]:
    safe_name, name_redactions = _safe_document_name(item.document_name)
    sanitized_text, text_redactions = _redact_text(item.text)
    payload = {
        "document_id": item.document_id,
        "document_name": safe_name,
        "chunk_id": item.chunk_id,
        "locator": item.locator,
        "text": sanitized_text,
    }
    return payload, name_redactions + text_redactions, len(item.text) + len(item.document_name), len(sanitized_text) + len(safe_name)


def build_evidence_packet(
    *,
    customer_id: str,
    project_id: str,
    procurement_case_id: str,
    run_id: str,
    registry_number: str,
    fragments: Iterable[EvidenceFragmentInput | dict[str, Any]],
) -> EvidencePacket:
    built: list[EvidenceFragment] = []
    redaction_count = 0
    input_chars_before = 0
    input_chars_after = 0

    for raw_item in fragments:
        item = raw_item if isinstance(raw_item, EvidenceFragmentInput) else EvidenceFragmentInput.model_validate(raw_item)
        payload, item_redactions, chars_before, chars_after = _fragment_payload(item)
        fragment_id = canonical_sha256(payload)
        built.append(
            EvidenceFragment(
                fragment_id=fragment_id,
                text_sha256=text_sha256(payload["text"]),
                **payload,
            )
        )
        redaction_count += item_redactions
        input_chars_before += chars_before
        input_chars_after += chars_after

    if not built:
        raise ValueError("Evidence packet requires at least one fragment")

    def source_order(fragment: EvidenceFragment) -> tuple[int, int, str, str]:
        locator = fragment.locator or {}
        try:
            document_order = int(locator.get("document_order", 2**31 - 1))
        except (TypeError, ValueError):
            document_order = 2**31 - 1
        try:
            chunk_index = int(locator.get("chunk_index", 2**63 - 1))
        except (TypeError, ValueError):
            chunk_index = 2**63 - 1
        return document_order, chunk_index, fragment.document_id, fragment.fragment_id

    built.sort(key=source_order)
    fragment_ids = [fragment.fragment_id for fragment in built]
    if len(fragment_ids) != len(set(fragment_ids)):
        raise ValueError("Evidence packet contains duplicate fragments")

    handling = DataHandlingReport(
        redaction_applied=redaction_count > 0,
        redaction_count=redaction_count,
        input_chars_before=input_chars_before,
        input_chars_after=input_chars_after,
        selected_fields=list(_SELECTED_FIELDS),
    )
    unsigned = {
        "customer_id": customer_id,
        "project_id": project_id,
        "procurement_case_id": procurement_case_id,
        "run_id": run_id,
        "registry_number": registry_number,
        "fragments": [fragment.model_dump(mode="json") for fragment in built],
        "data_handling": handling.model_dump(mode="json"),
    }
    return EvidencePacket(
        **unsigned,
        packet_hash=canonical_sha256(unsigned),
    )
