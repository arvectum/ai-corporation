from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

NOTICE_KINDS = {"notice", "eis_notice", "notification"}
TECHNICAL_KINDS = {
    "technical_spec",
    "technical_specification",
    "procurement_object_description",
    "specification",
}
CONTRACT_KINDS = {"contract_draft", "draft_contract"}
PRICE_KINDS = {"price_justification", "nmck_justification"}
APPLICATION_KINDS = {
    "application_requirements",
    "application_content_requirements",
    "participant_requirements",
}
SECURITY_KINDS = {
    "contract_performance_security",
    "performance_security",
    "contract_security",
}


def _normalized_kind(item: dict[str, Any]) -> str:
    explicit_candidates = {
        str(item.get("role_hint") or "").strip().lower(),
        str(item.get("document_kind") or "").strip().lower(),
    }
    if explicit_candidates & NOTICE_KINDS:
        return "notice"
    if explicit_candidates & TECHNICAL_KINDS:
        return "technical_specification"
    if explicit_candidates & CONTRACT_KINDS:
        return "contract_draft"
    if explicit_candidates & PRICE_KINDS:
        return "price_justification"
    if explicit_candidates & APPLICATION_KINDS:
        return "application_requirements"
    if explicit_candidates & SECURITY_KINDS:
        return "contract_performance_security"

    name = str(
        item.get("original_name")
        or item.get("display_name")
        or item.get("stored_name")
        or ""
    ).strip()
    lowered = name.lower()
    if any(
        token in lowered
        for token in (
            "реквизиты обеспечения исполнения контракта",
            "обеспечение исполнения контракта",
            "contract performance security",
        )
    ):
        return "contract_performance_security"
    if any(
        token in lowered
        for token in (
            "требования к составу заявки",
            "требования к заявке",
            "состав заявки",
            "application requirements",
        )
    ):
        return "application_requirements"
    if any(
        token in lowered
        for token in (
            "проект контракта",
            "проект договора",
            "contract_draft",
            "contract-draft",
            "contract",
            "agreement",
        )
    ):
        return "contract_draft"
    if any(
        token in lowered
        for token in (
            "техническое задание",
            "техзадание",
            "описание объекта закупки",
            "technical specification",
            "technical_spec",
            "technical-spec",
            "спецификац",
            "ведомост",
        )
    ):
        return "technical_specification"
    if any(
        token in lowered
        for token in (
            "обоснование нмцк",
            "расчет нмцк",
            "расчёт нмцк",
        )
    ):
        return "price_justification"
    if Path(name).suffix.lower() == ".xml" or any(
        token in lowered for token in ("извещение", "notice", "notification")
    ):
        return "notice"
    return "other_attachment"


def _source_name(item: dict[str, Any]) -> str:
    return str(
        item.get("original_name") or item.get("display_name") or ""
    ).strip()


def _public_name(item: dict[str, Any], fallback: str) -> str:
    source_name = _source_name(item)
    if not source_name:
        return fallback
    stem = Path(source_name).stem.strip()
    return stem or fallback


def _grouped_document(
    *,
    kind: str,
    items: list[dict[str, Any]],
    fallback_name: str,
    public_type: str,
) -> dict[str, Any]:
    public_name = (
        _public_name(items[0], fallback_name)
        if len(items) == 1
        else fallback_name
    )
    return {
        "name": public_name,
        "type": public_type,
        "kind": kind,
        "physical_file_count": len(items),
        "files": [
            _source_name(item) or "Документ"
            for item in items
        ],
    }


def build_document_set_summary(files: list[dict[str, Any]]) -> dict[str, Any]:
    physical_files = [item for item in files if isinstance(item, dict)]
    classified = [(_normalized_kind(item), item) for item in physical_files]
    counts = Counter(kind for kind, _item in classified)

    logical_documents: list[dict[str, Any]] = []
    notice_count = counts.get("notice", 0)
    if notice_count:
        logical_documents.append(
            {
                "name": "Извещение о закупке",
                "type": "извещение",
                "kind": "notice",
                "physical_file_count": notice_count,
            }
        )

    public_labels = {
        "technical_specification": (
            "Техническое задание / описание объекта закупки",
            "техническая документация",
        ),
        "price_justification": (
            "Обоснование НМЦК",
            "ценовое обоснование",
        ),
        "application_requirements": (
            "Требования к составу заявки",
            "требования к заявке",
        ),
        "contract_draft": ("Проект контракта", "проект контракта"),
        "contract_performance_security": (
            "Реквизиты обеспечения исполнения контракта",
            "обеспечение исполнения контракта",
        ),
    }
    for kind in (
        "technical_specification",
        "price_justification",
        "application_requirements",
        "contract_draft",
        "contract_performance_security",
    ):
        kind_items = [
            item for item_kind, item in classified if item_kind == kind
        ]
        if not kind_items:
            continue
        label, public_type = public_labels[kind]
        logical_documents.append(
            _grouped_document(
                kind=kind,
                items=kind_items,
                fallback_name=label,
                public_type=public_type,
            )
        )

    other_items = [
        item for item_kind, item in classified
        if item_kind == "other_attachment"
    ]
    grouped_other: dict[str, list[dict[str, Any]]] = {}
    for item in other_items:
        public_name = _public_name(item, "Прочее приложение")
        grouped_other.setdefault(public_name, []).append(item)
    for public_name, items in grouped_other.items():
        logical_documents.append(
            _grouped_document(
                kind="other_attachment",
                items=items,
                fallback_name=public_name,
                public_type="приложение",
            )
        )

    has_technical = counts.get("technical_specification", 0) > 0
    has_contract = counts.get("contract_draft", 0) > 0
    substantive_count = len(physical_files) - notice_count
    missing_required: list[str] = []
    if not has_technical:
        missing_required.append("technical_specification")
    if not has_contract:
        missing_required.append("contract_draft")

    if not physical_files:
        status = "empty"
    elif substantive_count == 0:
        status = "notice_only"
    elif missing_required:
        status = "incomplete"
    else:
        status = "complete"

    return {
        "status": status,
        "analysis_allowed": status == "complete",
        "physical_file_count": len(physical_files),
        "logical_document_count": len(logical_documents),
        "notice_file_count": notice_count,
        "substantive_file_count": substantive_count,
        "kind_counts": dict(sorted(counts.items())),
        "missing_required_document_kinds": missing_required,
        "logical_documents": logical_documents,
    }


def apply_document_set_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    summary = build_document_set_summary(list(metadata.get("files") or []))
    if metadata.get("archive_extraction_complete") is False:
        summary = dict(summary)
        summary["status"] = "incomplete_archive"
        summary["analysis_allowed"] = False
        summary["archive_extraction_complete"] = False
    else:
        summary["archive_extraction_complete"] = metadata.get(
            "archive_extraction_complete"
        )
    metadata["document_set_summary"] = summary
    metadata["document_set_status"] = summary["status"]
    metadata["document_count"] = summary["physical_file_count"]
    metadata["logical_document_count"] = summary["logical_document_count"]
    return summary
