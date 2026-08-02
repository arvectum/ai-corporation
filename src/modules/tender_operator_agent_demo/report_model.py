"""Canonical procurement report facade with a separate customer projection.

The implementation module preserves the historical canonical contract.  This
facade repairs the canonical evidence shape and exposes a sanitized projection
for the R10.1 customer renderer without mutating the canonical model.
"""

from __future__ import annotations

import re
from typing import Any

from src.modules.tender_operator_agent_demo import report_model_legacy as _legacy

for _name, _value in vars(_legacy).items():
    if _name not in {"__name__", "__package__", "__loader__", "__spec__"}:
        globals().setdefault(_name, _value)

UNKNOWN = _legacy.UNKNOWN
CUSTOMER_NOT_EXTRACTED = _legacy.CUSTOMER_NOT_EXTRACTED
_HASH_NAME = _legacy._HASH_NAME
_ORIGINAL_BUILD_PROCUREMENT_REPORT_MODEL = _legacy.build_procurement_report_model


def _format_source_location(value: Any) -> str:
    if isinstance(value, int) or (
        isinstance(value, str) and value.strip().isdigit()
    ):
        return f"позиция {str(value).strip()}"
    raw = str(value or "").strip()
    row_match = re.search(r"(?:^|:)row:(\d+)(?:$|:)", raw, flags=re.IGNORECASE)
    if row_match:
        return f"позиция {row_match.group(1)}"
    text = re.sub(
        r"[0-9a-f]{64}(?:\.[a-z0-9]+)?",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip(": ")
    if text.isdigit():
        return f"позиция {text}"
    return text if text and not _HASH_NAME.fullmatch(text) else "раздел документа"


def _russian_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if "T" in text:
        parsed = _legacy._parse_timestamp(text)
        if parsed:
            offset = parsed.utcoffset()
            suffix = (
                " (UTC)"
                if offset is not None and offset.total_seconds() == 0
                else ""
            )
            return parsed.strftime("%d.%m.%Y %H:%M") + suffix
    match = re.match(
        r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}:\d{2})"
        r"(?::\d{2}(?:\.\d+)?)?\s*([+-]\d{2}:\d{2})?",
        text,
    )
    if not match:
        return text or UNKNOWN
    zone = {
        "+12:00": " (UTC+12)",
        "+03:00": " (UTC+3)",
        "+00:00": " (UTC)",
    }.get(match.group(5), "")
    return (
        f"{match.group(1)}.{match.group(2)}.{match.group(3)} "
        f"{match.group(4)}{zone}"
    )


_legacy._format_source_location = _format_source_location
_legacy._russian_datetime = _russian_datetime


def _canonical_evidence_map(model: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for row in model.get("line_items", []):
        if not isinstance(row, dict):
            continue
        for evidence_id in row.get("evidence_ids", []):
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "document": row.get("source_document_id"),
                    "row": row.get("source_row"),
                    "short_excerpt": row.get("original_name"),
                    "related_items": [row.get("stable_item_id")],
                }
            )
    for risk_index, risk in enumerate(model.get("risks", []), start=1):
        if not isinstance(risk, dict):
            continue
        for locator_index, locator in enumerate(
            risk.get("evidence_locators", []), start=1
        ):
            if (
                not isinstance(locator, dict)
                or not locator.get("document")
                or not locator.get("locator")
            ):
                continue
            evidence.append(
                {
                    "evidence_id": f"risk:{risk_index}:locator:{locator_index}",
                    "document": locator["document"],
                    "row": locator["locator"],
                    "short_excerpt": risk.get("risk")
                    or risk.get("description")
                    or "Подтверждённый риск",
                    "related_items": [],
                }
            )
    return evidence


def build_procurement_report_model(
    metadata: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
    *,
    repository_sha: str = "unknown",
) -> dict[str, Any]:
    model = _ORIGINAL_BUILD_PROCUREMENT_REPORT_MODEL(
        metadata,
        outputs,
        repository_sha=repository_sha,
    )
    analysis_as_of = (
        metadata.get("analysis_completed_at")
        or metadata.get("prepared_at")
        or metadata.get("created_at")
    )
    parsed_as_of = _legacy._parse_timestamp(analysis_as_of)
    model["analysis_as_of"] = _russian_datetime(analysis_as_of)
    model["analysis_as_of_iso"] = (
        parsed_as_of.isoformat() if parsed_as_of else None
    )
    model["publication_datetime_display"] = _russian_datetime(
        model.get("publication_datetime")
    )
    model["application_deadline_display"] = _russian_datetime(
        model.get("application_deadline")
    )
    model["evidence_map"] = _canonical_evidence_map(model)
    return model


def _customer_document_label(value: Any) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text or _HASH_NAME.fullmatch(text):
        return "Извещение о закупке", "извещение"
    lowered = text.lower()
    role = "notice" if "notice" in lowered or "извещ" in lowered else ""
    return _legacy._customer_document_label(
        {"display_name": text, "role_hint": role}
    )


def _customer_evidence_location(value: Any) -> str:
    location = _format_source_location(value)
    if location.startswith("раздел "):
        return location
    if location.startswith("позиция "):
        return f"раздел «Объект закупки», {location}"
    return location


def build_customer_report_projection(model: dict[str, Any]) -> dict[str, Any]:
    """Return a sanitized customer model without mutating canonical data."""
    metadata = (
        model.get("metadata")
        if isinstance(model.get("metadata"), dict)
        else {}
    )
    document_summary = (
        metadata.get("document_set_summary")
        if isinstance(metadata.get("document_set_summary"), dict)
        else {}
    )
    logical_documents = document_summary.get("logical_documents")
    source_documents = (
        logical_documents
        if isinstance(logical_documents, list) and logical_documents
        else model.get("customer_documents", [])
    )
    documents = [
        {
            "name": str(item.get("name") or "Документ закупки"),
            "type": str(item.get("type") or "документ"),
        }
        for item in source_documents
        if isinstance(item, dict)
    ]

    line_items: list[dict[str, Any]] = []
    for row in model.get("line_items", []):
        if not isinstance(row, dict):
            continue
        location = _format_source_location(row.get("source_row"))
        if location.startswith("позиция "):
            source_display = (
                "Извещение о закупке — раздел «Объект закупки», " + location
            )
        elif location.startswith("раздел "):
            source_display = "Извещение о закупке — " + location
        else:
            source_display = "Извещение о закупке — " + location
        line_items.append(
            {
                "sequence": row.get("sequence"),
                "original_name": row.get("original_name")
                or row.get("display_name")
                or UNKNOWN,
                "quantity_display": row.get("quantity_display") or UNKNOWN,
                "unit_original": row.get("unit_original") or UNKNOWN,
                "okpd2": row.get("okpd2"),
                "source_display": source_display,
            }
        )

    customer_evidence: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in model.get("evidence_map", []):
        if not isinstance(item, dict):
            continue
        document_label, document_type = _customer_document_label(
            item.get("document")
        )
        location = _customer_evidence_location(item.get("row"))
        key = (document_label, document_type, location)
        if key in seen:
            continue
        seen.add(key)
        customer_evidence.append(
            {
                "document_label": document_label,
                "document_type": document_type,
                "location": location,
            }
        )

    return {
        "procurement_number": model.get("procurement_number"),
        "procurement_title": model.get("procurement_title"),
        "customer_name": model.get("customer_name"),
        "publication_datetime_display": model.get(
            "publication_datetime_display"
        )
        or _russian_datetime(model.get("publication_datetime")),
        "application_deadline_display": model.get(
            "application_deadline_display"
        )
        or _russian_datetime(model.get("application_deadline")),
        "analysis_as_of": model.get("analysis_as_of"),
        "analysis_as_of_iso": model.get("analysis_as_of_iso"),
        "deadline_status": model.get("deadline_status"),
        "nmck": model.get("nmck"),
        "delivery_place": model.get("delivery_place"),
        "documents_count": int(
            document_summary.get("physical_file_count")
            or metadata.get("document_count")
            or len(documents)
        ),
        "customer_documents": documents,
        "customer_decision": dict(model.get("customer_decision") or {}),
        "line_items": line_items,
        "evidence_map": customer_evidence,
        "unit_economics": (
            dict(model.get("unit_economics") or {})
            if model.get("unit_economics")
            else None
        ),
        "corpus_limitations": list(model.get("corpus_limitations") or []),
        "customer_questions": list(model.get("customer_questions") or []),
        "risks": [
            dict(item)
            for item in model.get("risks", [])
            if isinstance(item, dict)
        ],
    }
