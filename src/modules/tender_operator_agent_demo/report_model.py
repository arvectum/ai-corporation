"""Canonical procurement report facade with a separate customer projection.

The implementation module preserves the historical canonical contract. This
facade repairs the canonical evidence shape and exposes a sanitized projection
for the R10.1 customer renderer without mutating provider output.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.modules.tender_operator_agent_demo import report_model_legacy as _legacy
from src.modules.tender_operator_agent_demo.document_set_completeness import (
    build_document_set_summary,
)

for _name, _value in vars(_legacy).items():
    if _name not in {"__name__", "__package__", "__loader__", "__spec__"}:
        globals().setdefault(_name, _value)

UNKNOWN = _legacy.UNKNOWN
CUSTOMER_NOT_EXTRACTED = _legacy.CUSTOMER_NOT_EXTRACTED
_HASH_NAME = _legacy._HASH_NAME
_UUID = re.compile(
    r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_ORIGINAL_BUILD_PROCUREMENT_REPORT_MODEL = _legacy.build_procurement_report_model
_STALE_MISSING_DOCUMENT_MARKERS = (
    "проект контракта не найден",
    "проект контракта отсутств",
    "отсутствует проект контракта",
    "получить проект контракта",
    "запросить отсутствующие документы",
    "отдельное тз",
    "техническое задание или описание объекта закупки не найден",
    "техническое задание отсутств",
)


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


def _document_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    current = metadata.get("document_set_summary")
    if isinstance(current, dict) and current.get("logical_documents") is not None:
        return dict(current)
    files = [item for item in metadata.get("files", []) if isinstance(item, dict)]
    return build_document_set_summary(files)


def _is_stale_missing_document_text(value: Any) -> bool:
    lowered = str(value or "").lower()
    return any(marker in lowered for marker in _STALE_MISSING_DOCUMENT_MARKERS)


def _safe_customer_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or _HASH_NAME.fullmatch(text) or _UUID.fullmatch(text):
        return None
    if text.startswith(("/", "file:")) or "/Users/" in text or "/Volumes/" in text:
        return None
    return text


def _clean_complete_document_model(
    model: dict[str, Any], document_summary: dict[str, Any]
) -> None:
    """Remove notice-only conclusions when the canonical set is complete."""

    if document_summary.get("status") != "complete":
        return
    decision = model.get("customer_decision")
    if isinstance(decision, dict):
        reasons = [
            item
            for item in decision.get("reasons", [])
            if not _is_stale_missing_document_text(item)
        ]
        complete_reason = (
            "Техническая документация и проект контракта включены в комплект анализа."
        )
        if complete_reason not in reasons:
            reasons.append(complete_reason)
        decision["reasons"] = reasons
        next_actions = [
            item
            for item in model.get("action_plan", [])
            if item and not _is_stale_missing_document_text(item)
        ]
        decision["next_action"] = (
            next_actions[0]
            if next_actions
            else "Проверить коммерческие предложения и собственную себестоимость до решения об участии."
        )
    for key in ("corpus_limitations", "limitations"):
        if isinstance(model.get(key), list):
            model[key] = [
                item
                for item in model[key]
                if not _is_stale_missing_document_text(item)
            ]
    if isinstance(model.get("missing_data"), list):
        model["missing_data"] = [
            item
            for item in model["missing_data"]
            if not _is_stale_missing_document_text(
                item.get("description") if isinstance(item, dict) else item
            )
        ]
    if isinstance(model.get("customer_questions"), list):
        model["customer_questions"] = [
            item
            for item in model["customer_questions"]
            if not _is_stale_missing_document_text(
                item.get("question") if isinstance(item, dict) else item
            )
        ]
    bid = model.get("bid_decision")
    if isinstance(bid, dict):
        for key in ("blockers", "conditions", "rationale"):
            if isinstance(bid.get(key), list):
                bid[key] = [
                    item
                    for item in bid[key]
                    if not _is_stale_missing_document_text(item)
                ]
    coverage = model.get("document_coverage")
    if isinstance(coverage, dict):
        coverage["missing"] = []
        if coverage.get("impact") == "Договорный анализ ограничен":
            coverage["impact"] = ""
    contract = model.get("contract_conditions")
    if isinstance(contract, dict):
        contract["status"] = "present"
        contract["reason"] = "Проект контракта включён в комплект анализа."


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
    document_summary = _document_summary(metadata)
    model_metadata = dict(model.get("metadata") or {})
    model_metadata.update(
        {
            "document_set_summary": document_summary,
            "document_count": document_summary.get("physical_file_count", 0),
            "logical_document_count": document_summary.get(
                "logical_document_count", 0
            ),
            "document_set_status": document_summary.get("status", "unknown"),
        }
    )
    model["metadata"] = model_metadata
    _clean_complete_document_model(model, document_summary)
    return model


def _customer_document_label(value: Any) -> tuple[str, str]:
    text = _safe_customer_text(value)
    if not text:
        return "Документы закупки", "подтверждающий документ"
    if Path(text).name != text:
        return "Документы закупки", "подтверждающий документ"
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

    raw_line_items = [
        row for row in model.get("line_items", []) if isinstance(row, dict)
    ]
    okpd2_codes = [
        item
        for item in model.get("okpd2_codes", [])
        if isinstance(item, dict) and _safe_customer_text(item.get("code"))
    ]
    single_item_okpd2 = (
        str(okpd2_codes[0]["code"])
        if len(raw_line_items) == 1 and len(okpd2_codes) == 1
        else None
    )
    line_items: list[dict[str, Any]] = []
    for row in raw_line_items:
        location = _customer_evidence_location(row.get("source_row"))
        source_display = "Извещение о закупке — " + location
        characteristics = [
            text
            for value in row.get("characteristics", [])
            if (text := _safe_customer_text(value))
        ]
        line_items.append(
            {
                "sequence": row.get("sequence"),
                "original_name": row.get("original_name")
                or row.get("display_name")
                or UNKNOWN,
                "quantity_display": row.get("quantity_display") or UNKNOWN,
                "unit_original": row.get("unit_original") or UNKNOWN,
                "okpd2": row.get("okpd2") or single_item_okpd2,
                "characteristics": characteristics,
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
        "document_set_status": document_summary.get("status") or "unknown",
        "missing_required_document_kinds": list(
            document_summary.get("missing_required_document_kinds") or []
        ),
        "documents_count": int(
            document_summary.get("logical_document_count") or len(documents)
        ),
        "physical_files_count": int(
            document_summary.get("physical_file_count")
            or metadata.get("document_count")
            or len(documents)
        ),
        "document_set_complete": document_summary.get("status") == "complete",
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
