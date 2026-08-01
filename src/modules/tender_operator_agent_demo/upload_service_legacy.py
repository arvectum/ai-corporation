from __future__ import annotations

import html
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import urlopen
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from secrets import token_hex
from typing import Any

from fastapi import HTTPException
from fastapi.responses import FileResponse

from src.modules.tender_connectors.text_extraction import extract_text_from_attachment_bytes
from src.modules.procurement_analysis.document_roles import detect_document_role
from src.tender_research.document_text_extractor import (
    EMPTY_STATUS as DOC_EMPTY_STATUS,
    EXTRACTED_STATUS as DOC_EXTRACTED_STATUS,
    UNSUPPORTED_STATUS as DOC_UNSUPPORTED_STATUS,
    extract_text as extract_document_text_from_path,
)
from src.modules.tender_operator_agent_demo.event_log import (
    append_tender_demo_event,
    load_tender_demo_events,
)
from src.modules.tender_operator_agent_demo.procurement_discovery import get_supplier_profile
from src.modules.tender_operator_agent_demo.relevance_scoring import score_procurement_document_text
from src.modules.supplier_search.internet_supplier_search import search_suppliers
from src.modules.supplier_search.yandex_search_client import YandexSearchClient
from src.shared.config.settings import get_settings
from src.modules.tender_operator_agent_demo.quote_normalizer import (
    SpreadsheetSource,
    build_economics_summary,
    build_quote_comparison,
)
from src.modules.tender_operator_agent_demo.schemas import (
    DemoDetailSection,
    DemoFinalRecommendation,
    DemoRecommendationCode,
    DemoStep,
    DemoStepStatus,
    TenderOperatorDemoReportResponse,
    TenderOperatorUploadedFile,
    TenderOperatorUploadedRunAnalyzeResponse,
    TenderOperatorUploadedRunCreateResponse,
    TenderOperatorUploadedRunListResponse,
    TenderOperatorUploadedRunResponse,
    TenderOperatorRunEvent,
    TenderOperatorUploadedRunStatus,
    TenderOperatorUploadedRunStepsResponse,
    TenderOperatorUploadedRunSummary,
)


ALLOWED_EXTENSIONS = {".pdf", ".doc", ".docx", ".xlsx", ".xls", ".txt", ".csv", ".zip", ".xml", ".html", ".htm"}
MAX_FILE_COUNT = 16
MAX_FILE_SIZE_BYTES = 12 * 1024 * 1024
MAX_TOTAL_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_ZIP_ENTRY_COUNT = 32
MAX_ZIP_TOTAL_BYTES = 24 * 1024 * 1024
METADATA_FILE = "metadata.json"
EVENTS_FILE = "events.jsonl"
DEFAULT_TARGET_MARGIN_PERCENT = 15.0
DEFAULT_LOGISTICS_RESERVE_PERCENT = 3.0
DEFAULT_RISK_RESERVE_PERCENT = 5.0
DEFAULT_PAYMENT_DELAY_DAYS = 45

TEXT_TRANSLATIONS = {
    "Compliance with specified technical standards required": "Требуется соответствие указанным техническим стандартам.",
    "Equipment/goods must match stated specifications": "Оборудование и товары должны соответствовать заявленной спецификации.",
    "Acceptance testing per contract terms": "Нужно пройти приёмочные испытания по условиям договора.",
    "Warranty and post-delivery support required": "Требуются гарантия и поддержка после поставки.",
    "Company registration certificate": "Свидетельство о регистрации компании.",
    "Tax clearance certificate": "Справка об отсутствии налоговой задолженности.",
    "Technical proposal with specifications": "Техническое предложение со спецификацией.",
    "Financial guarantee or contract security": "Финансовое обеспечение или обеспечение исполнения договора.",
    "Declaration of conformity": "Декларация о соответствии.",
    "Can you supply the exact item matching the specification? If not, what analog do you propose?": "Можете ли вы поставить точную позицию по спецификации? Если нет, какой аналог предлагаете?",
    "What is your price per unit with VAT and without VAT?": "Укажите цену за единицу с НДС и без НДС.",
    "What is the delivery cost to the specified location?": "Укажите стоимость доставки до указанного объекта.",
    "What is the delivery time from order confirmation?": "Какой срок поставки после подтверждения заказа?",
    "Is the item in stock or made to order? If made to order, what is the manufacturing lead time?": "Позиция в наличии или производится под заказ? Если под заказ, какой срок изготовления?",
    "Do you have the required certificates and declarations of conformity?": "Есть ли необходимые сертификаты и декларации соответствия?",
    "What warranty do you provide?": "Какой гарантийный срок вы предоставляете?",
    "Do you offer an analog that meets the specification? If so, provide details.": "Предлагаете ли аналог, соответствующий спецификации? Если да, укажите детали.",
    "What are your payment terms? Do you require prepayment?": "Какие условия оплаты? Требуется ли предоплата?",
    "How long is your offer valid?": "Какой срок действия вашего предложения?",
    "Is installation/assembly included? If not, what are the additional costs?": "Входит ли монтаж или сборка? Если нет, какие дополнительные затраты?",
    "Is packaging, delivery, and unloading included? If not, what are the additional costs?": "Включены ли упаковка, доставка и разгрузка? Если нет, какие дополнительные затраты?",
    "Penalties for delay": "Штрафы за просрочку.",
    "Post-payment after acceptance": "Оплата после приёмки.",
    "Unilateral termination right": "Право одностороннего расторжения.",
    "Contract security requirement": "Требование обеспечения исполнения договора.",
    "Short delivery timeline": "Сжатый срок поставки.",
    "Required license/SRO/experience": "Обязательная лицензия, СРО или подтверждённый опыт.",
    "Manageable. Include in project planning.": "Риск управляемый. Нужно заложить его в план исполнения.",
    "Standard for public procurement. Requires working capital.": "Типично для закупок. Требует оборотного капитала.",
    "Standard clause. Manageable with proper project management.": "Стандартное условие. Управляется при дисциплине исполнения.",
    "Binds significant working capital. Reduces available margin.": "Замораживает заметный объём оборотных средств и снижает доступную маржу.",
    "Requires supplier with stock or short manufacturing lead time.": "Нужен поставщик со складским остатком или коротким циклом производства.",
    "If operator/supplier cannot meet these, participation is impossible.": "Если оператор или поставщик не соответствуют этому требованию, участвовать нельзя.",
    "Ensure realistic delivery timeline. Include buffer.": "Подтвердить реалистичный срок поставки и заложить буфер.",
    "Factor into cash flow planning. Consider contract security reduction.": "Учесть это в cash-flow и отдельно оценить возможность снижения обеспечения.",
    "Track milestones diligently. Communicate proactively.": "Жёстко контролировать вехи исполнения и заранее эскалировать отклонения.",
    "Include cost of security (bank guarantee fee) in pricing. Negotiate reduction if possible.": "Включить стоимость обеспечения в цену и, если возможно, согласовать снижение.",
    "Verify supplier availability before bidding. Consider partial delivery.": "До участия подтвердить наличие у поставщика и рассмотреть частичную поставку.",
    "Verify requirements early. Check if equivalents are accepted.": "Сразу проверить требования и отдельно уточнить, принимаются ли эквиваленты.",
}


def _translate_user_text(value: str) -> str:
    return TEXT_TRANSLATIONS.get(value, value)


from src.modules.procurement_analysis.frozen_types import AnalyzedDocument


@dataclass
class SupplyItem:
    item_no: str | None
    name: str
    quantity: str | None
    unit: str | None
    characteristics: list[str]
    gost: list[str]
    equivalent_allowed: bool | None
    source_document: str
    source_kind: str
    confidence: str
    raw_fragment: str
    unit_price: str | None = None
    total_price: str | None = None
    source_documents: list[str] = field(default_factory=list)
    item_type: str = "goods"
    quantity_status: str = "specified"
    pricing_basis: str = "unknown"
    source_row_number: int | None = None
    evidence_id: str | None = None
    unit_original: str | None = None
    record_type: str = "line_item"
    unit_code: str | None = None
    ktru: str | None = None
    okpd2: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    name_source_type: str = "unresolved"
    name_source_path: str | None = None
    quantity_source_path: str | None = None
    unit_source_path: str | None = None
    source_record_id: str | None = None
    extraction_strategy: str = "unknown"
    evidence_ids: list[str] = field(default_factory=list)
    official_name: str | None = None
    display_name: str | None = None


def get_demo_runs_root() -> Path:
    configured = os.environ.get("AI_CORP_TENDER_OPERATOR_DEMO_RUNS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / "company_agent_runs" / "tender_operator_demo"


def _ensure_runs_root() -> Path:
    root = get_demo_runs_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_demo_run_dir(run_id: str) -> Path:
    return _ensure_runs_root() / run_id


def get_demo_run_procurement_dir(run_id: str) -> Path:
    return get_demo_run_dir(run_id) / "procurement"


def get_demo_run_input_dir(run_id: str) -> Path:
    return get_demo_run_dir(run_id) / "input"


def get_demo_run_normalized_dir(run_id: str) -> Path:
    return get_demo_run_dir(run_id) / "normalized"


def get_demo_run_output_dir(run_id: str) -> Path:
    return get_demo_run_dir(run_id) / "output"


def _events_path(run_id: str) -> Path:
    return get_demo_run_dir(run_id) / EVENTS_FILE


def _metadata_path(run_id: str) -> Path:
    return get_demo_run_dir(run_id) / METADATA_FILE


def _input_dir(run_id: str) -> Path:
    return get_demo_run_input_dir(run_id)


def _normalized_dir(run_id: str) -> Path:
    return get_demo_run_normalized_dir(run_id)


def _output_dir(run_id: str) -> Path:
    return get_demo_run_output_dir(run_id)


def _safe_datetime() -> str:
    return datetime.now(UTC).isoformat()


def make_demo_run_id() -> str:
    return f"toa-run-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{token_hex(3)}"


def sanitize_demo_filename(name: str, index: int) -> tuple[str, str]:
    original = Path(name or f"file-{index}").name
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext or 'unknown'}")

    stem = Path(original).stem.lower()
    stem = re.sub(r"[^a-z0-9._-]+", "-", stem).strip("._-")
    if not stem:
        stem = f"file-{index}"
    stem = stem[:60]
    stored_name = f"{index:02d}-{stem}{ext}"
    return original, stored_name


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_file_descriptor(
    *,
    file_id: str,
    original_name: str,
    stored_name: str,
    role_hint: str | None,
    size_bytes: int,
    content_type: str,
    source_type: str | None = None,
    source_url: str | None = None,
    document_kind: str | None = None,
    parent_archive: str | None = None,
) -> dict[str, Any]:
    return {
        "file_id": file_id,
        "original_name": original_name,
        "display_name": original_name,
        "stored_name": stored_name,
        "role_hint": role_hint,
        "extension": Path(stored_name).suffix.lower(),
        "size_bytes": size_bytes,
        "content_type": content_type or "application/octet-stream",
        "source": "upload",
        "source_type": source_type or "upload",
        "source_url": source_url,
        "document_kind": document_kind,
        "parent_archive": parent_archive,
        "extracted_text_available": False,
        "text_extraction_status": "pending",
        "warnings": [],
    }


def build_demo_file_descriptor(
    *,
    file_id: str,
    original_name: str,
    stored_name: str,
    role_hint: str | None = None,
    size_bytes: int,
    content_type: str,
    source: str = "upload",
    source_type: str | None = None,
    source_url: str | None = None,
    document_kind: str | None = None,
    parent_archive: str | None = None,
) -> dict[str, Any]:
    descriptor = _build_file_descriptor(
        file_id=file_id,
        original_name=original_name,
        stored_name=stored_name,
        role_hint=role_hint,
        size_bytes=size_bytes,
        content_type=content_type,
        source_type=source_type,
        source_url=source_url,
        document_kind=document_kind,
        parent_archive=parent_archive,
    )
    descriptor["source"] = source
    return descriptor


def ensure_demo_run_structure(run_id: str, *, exist_ok: bool) -> dict[str, Path]:
    run_dir = get_demo_run_dir(run_id)
    input_dir = get_demo_run_input_dir(run_id)
    normalized_dir = get_demo_run_normalized_dir(run_id)
    output_dir = get_demo_run_output_dir(run_id)
    procurement_dir = get_demo_run_procurement_dir(run_id)
    input_dir.mkdir(parents=True, exist_ok=exist_ok)
    normalized_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    procurement_dir.mkdir(parents=True, exist_ok=True)
    return {
        "run_dir": run_dir,
        "input_dir": input_dir,
        "normalized_dir": normalized_dir,
        "output_dir": output_dir,
        "procurement_dir": procurement_dir,
    }


def load_demo_run_metadata(run_id: str) -> dict[str, Any]:
    path = _metadata_path(run_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' was not found")
    return _read_json(path)


def save_demo_run_metadata(run_id: str, metadata: dict[str, Any]) -> None:
    _write_json(_metadata_path(run_id), metadata)


def append_demo_run_event(run_id: str, event_type: str, message: str, details: dict[str, Any] | None = None) -> None:
    append_tender_demo_event(run_id, event_type, message, details or {})


def load_demo_run_events(run_id: str) -> list[TenderOperatorRunEvent]:
    return [TenderOperatorRunEvent.model_validate(item) for item in load_tender_demo_events(run_id)]


def _sanitize_percent(value: float | None, *, default: float, field_name: str) -> float:
    numeric = default if value is None else float(value)
    if numeric < 0 or numeric > 95:
        raise HTTPException(status_code=400, detail=f"{field_name} must be between 0 and 95")
    return round(numeric, 2)


def _sanitize_delay_days(value: int | None, *, default: int) -> int:
    numeric = default if value is None else int(value)
    if numeric < 0 or numeric > 365:
        raise HTTPException(status_code=400, detail="payment_delay_days must be between 0 and 365")
    return numeric


def create_uploaded_demo_run(
    *,
    tender_title: str,
    tender_category: str,
    customer_name: str,
    notes: str | None,
    target_margin_percent: float | None,
    logistics_reserve_percent: float | None,
    risk_reserve_percent: float | None,
    payment_delay_days: int | None,
    uploads: list[tuple[str, str, bytes]],
) -> TenderOperatorUploadedRunCreateResponse:
    if not tender_title.strip():
        raise HTTPException(status_code=400, detail="tender_title is required")
    if not uploads:
        raise HTTPException(status_code=400, detail="At least one file must be uploaded")
    if len(uploads) > MAX_FILE_COUNT:
        raise HTTPException(status_code=400, detail=f"Too many files. Limit: {MAX_FILE_COUNT}")

    total_size = sum(len(content) for _name, _ctype, content in uploads)
    if total_size > MAX_TOTAL_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Total upload size exceeds the allowed limit")

    target_margin_percent = _sanitize_percent(
        target_margin_percent,
        default=DEFAULT_TARGET_MARGIN_PERCENT,
        field_name="target_margin_percent",
    )
    logistics_reserve_percent = _sanitize_percent(
        logistics_reserve_percent,
        default=DEFAULT_LOGISTICS_RESERVE_PERCENT,
        field_name="logistics_reserve_percent",
    )
    risk_reserve_percent = _sanitize_percent(
        risk_reserve_percent,
        default=DEFAULT_RISK_RESERVE_PERCENT,
        field_name="risk_reserve_percent",
    )
    payment_delay_days = _sanitize_delay_days(payment_delay_days, default=DEFAULT_PAYMENT_DELAY_DAYS)

    run_id = make_demo_run_id()
    structure = ensure_demo_run_structure(run_id, exist_ok=False)
    input_dir = structure["input_dir"]

    warnings: list[str] = []
    files: list[dict[str, Any]] = []

    for index, (filename, content_type, content) in enumerate(uploads, start=1):
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(status_code=400, detail=f"File exceeds the allowed size limit: {filename}")

        original_name, stored_name = sanitize_demo_filename(filename, index)
        file_id = f"FILE-{index:02d}"
        target = input_dir / stored_name
        target.write_bytes(content)
        if Path(filename).name != filename or ".." in filename.replace("\\", "/"):
            warnings.append(f"Filename '{filename}' was normalized for safe local storage.")
        files.append(
            build_demo_file_descriptor(
                file_id=file_id,
                original_name=original_name,
                stored_name=stored_name,
                role_hint=_derive_role_hint(stored_name),
                size_bytes=len(content),
                content_type=content_type,
            )
        )

    metadata = {
        "run_id": run_id,
        "created_at": _safe_datetime(),
        "mode": "uploaded_demo",
        "tender_title": tender_title.strip(),
        "tender_category": tender_category.strip() or "Электротехническое оборудование",
        "customer_name": customer_name.strip() or "Промышленный заказчик",
        "notes": notes.strip() if notes and notes.strip() else None,
        "status": TenderOperatorUploadedRunStatus.READY_TO_ANALYZE.value,
        "analysis_mode": "not_started",
        "economics_inputs": {
            "target_margin_percent": target_margin_percent,
            "logistics_reserve_percent": logistics_reserve_percent,
            "risk_reserve_percent": risk_reserve_percent,
            "payment_delay_days": payment_delay_days,
        },
        "files": files,
        "warnings": warnings,
        "limitations": [
            "Только демо- и пилотный режим.",
            "Без внешних действий, без отправки писем, без подачи на площадку, без ЭЦП.",
        ],
        "human_in_the_loop": True,
        "external_actions": False,
        "no_platform_submission": True,
        "no_email_sending": True,
        "no_digital_signature": True,
    }
    save_demo_run_metadata(run_id, metadata)
    append_demo_run_event(
        run_id,
        "run_created",
        "Создан демонстрационный прогон с ручной загрузкой документов.",
        {"mode": "uploaded_demo", "file_count": len(files)},
    )
    return TenderOperatorUploadedRunCreateResponse(
        run_id=run_id,
        status=TenderOperatorUploadedRunStatus.READY_TO_ANALYZE,
        created_at=datetime.fromisoformat(metadata["created_at"]),
        file_count=len(files),
        warnings=warnings,
        limitations=metadata["limitations"],
    )


def append_files_to_demo_run(
    *,
    run_id: str,
    uploads: list[tuple[str, str, bytes]],
) -> TenderOperatorUploadedRunCreateResponse:
    metadata = _load_metadata(run_id)
    if not uploads:
        raise HTTPException(status_code=400, detail="At least one file must be uploaded")

    existing_files = metadata.get("files", [])
    if len(existing_files) + len(uploads) > MAX_FILE_COUNT:
        raise HTTPException(status_code=400, detail=f"Too many files. Limit: {MAX_FILE_COUNT}")

    existing_total = sum(int(item.get("size_bytes", 0)) for item in existing_files)
    new_total = sum(len(content) for _filename, _ctype, content in uploads)
    if existing_total + new_total > MAX_TOTAL_UPLOAD_BYTES:
        raise HTTPException(status_code=400, detail="Total upload size exceeds the allowed limit")

    input_dir = _input_dir(run_id)
    warnings = list(metadata.get("warnings", []))
    start_index = len(existing_files) + 1
    added_files = 0

    for index, (filename, content_type, content) in enumerate(uploads, start=start_index):
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(status_code=400, detail=f"File exceeds the allowed size limit: {filename}")
        original_name, stored_name = sanitize_demo_filename(filename, index)
        file_id = f"FILE-{index:02d}"
        (input_dir / stored_name).write_bytes(content)
        if Path(filename).name != filename or ".." in filename.replace("\\", "/"):
            warnings.append(f"Filename '{filename}' was normalized for safe local storage.")
        existing_files.append(
            build_demo_file_descriptor(
                file_id=file_id,
                original_name=original_name,
                stored_name=stored_name,
                role_hint=_derive_role_hint(stored_name),
                size_bytes=len(content),
                content_type=content_type,
                source="manual_upload",
            )
        )
        append_demo_run_event(
            run_id,
            "attachment_saved",
            f"Документ '{original_name}' добавлен в прогон вручную.",
            {"stored_name": stored_name, "source": "manual_upload"},
        )
        added_files += 1

    metadata["files"] = existing_files
    metadata["warnings"] = sorted(set(warnings))
    if metadata.get("status") == TenderOperatorUploadedRunStatus.DOCS_REQUIRED.value:
        metadata["status"] = TenderOperatorUploadedRunStatus.READY_TO_ANALYZE.value
    if metadata.get("attachments_status") in {"manual_upload_required", "unavailable_in_demo", "source_requires_authorization"}:
        metadata["attachments_status"] = "manual_upload_received"
    metadata["downloaded_files_count"] = len(existing_files)
    metadata["manual_upload_required"] = False
    save_demo_run_metadata(run_id, metadata)
    append_demo_run_event(
        run_id,
        "manual_upload_received",
        "Оператор добавил документы в существующий run.",
        {"added_files": added_files},
    )
    return TenderOperatorUploadedRunCreateResponse(
        run_id=run_id,
        status=TenderOperatorUploadedRunStatus(metadata["status"]),
        created_at=datetime.fromisoformat(metadata["created_at"]),
        file_count=len(existing_files),
        warnings=metadata.get("warnings", []),
        limitations=metadata.get("limitations", []),
    )


def list_uploaded_demo_runs() -> TenderOperatorUploadedRunListResponse:
    root = _ensure_runs_root()
    runs: list[TenderOperatorUploadedRunSummary] = []
    for path in sorted(root.iterdir(), reverse=True):
        metadata_path = path / METADATA_FILE
        if not metadata_path.is_file():
            continue
        metadata = _read_json(metadata_path)
        runs.append(
            TenderOperatorUploadedRunSummary(
                run_id=metadata["run_id"],
                created_at=datetime.fromisoformat(metadata["created_at"]),
                mode=metadata["mode"],
                tender_title=metadata["tender_title"],
                tender_category=metadata["tender_category"],
                customer_name=metadata["customer_name"],
                status=TenderOperatorUploadedRunStatus(metadata["status"]),
                analysis_mode=metadata.get("analysis_mode", "not_started"),
                file_count=len(metadata.get("files", [])),
                warning_count=len(metadata.get("warnings", [])),
                limitations=metadata.get("limitations", []),
                procurement_source=metadata.get("procurement_source"),
                procurement_id=metadata.get("procurement_id"),
                attachments_status=metadata.get("attachments_status"),
            )
        )
    runs.sort(key=lambda item: item.created_at, reverse=True)
    return TenderOperatorUploadedRunListResponse(runs=runs[:12])


def _load_metadata(run_id: str) -> dict[str, Any]:
    return load_demo_run_metadata(run_id)


def _save_metadata(run_id: str, metadata: dict[str, Any]) -> None:
    save_demo_run_metadata(run_id, metadata)


def _decode_text(content: bytes) -> str | None:
    for encoding in ("utf-8", "cp1251", "koi8-r", "latin-1"):
        try:
            text = content.decode(encoding).strip()
            if text:
                return text
        except Exception:
            continue
    return None


def _detect_role(name: str) -> str:
    """Compatibility facade for the storage-neutral frozen-pipeline policy."""
    return detect_document_role(name)


def _derive_role_hint(filename: str) -> str | None:
    lowered = filename.lower()
    if lowered.startswith("technical_spec_"):
        return "technical_spec"
    if lowered.startswith("contract_draft_"):
        return "contract_draft"
    if lowered.startswith("notice_"):
        return "notice"
    if lowered.startswith("tkp_"):
        return "tkp"
    detected = _detect_role(filename)
    return detected if detected != "supporting" else None


def _extract_document_text(file_name: str, content: bytes) -> tuple[str | None, list[str], str]:
    ext = Path(file_name).suffix.lower()
    warnings: list[str] = []
    if ext in {".txt", ".csv", ".xml"}:
        return _decode_text(content), warnings, DOC_EXTRACTED_STATUS
    if ext == ".pdf":
        text = extract_text_from_attachment_bytes(url=file_name, content=content)
        if text:
            return text, warnings, DOC_EXTRACTED_STATUS
    if ext == ".doc":
        text = _extract_text_from_legacy_doc(content)
        if text:
            return text, warnings, DOC_EXTRACTED_STATUS

    try:
        with tempfile.TemporaryDirectory(prefix="toa-extract-") as tmp_dir:
            source_path = Path(tmp_dir) / Path(file_name).name
            source_path.write_bytes(content)
            status, extracted = extract_document_text_from_path(str(source_path))
    except Exception:
        status, extracted = DOC_EMPTY_STATUS, ""

    normalized_text = extracted.strip() or None
    if status == DOC_UNSUPPORTED_STATUS:
        warnings.append(f"Извлечение текста для {ext} пока не поддерживается.")
    elif status != DOC_EXTRACTED_STATUS and not normalized_text:
        warnings.append(f"Не удалось извлечь текст из {Path(file_name).name}.")
    return normalized_text, warnings, status


def _extract_text_from_legacy_doc(content: bytes) -> str | None:
    try:
        with tempfile.TemporaryDirectory(prefix="toa-doc-") as tmp_dir:
            source_path = Path(tmp_dir) / "source.doc"
            output_path = Path(tmp_dir) / "source.txt"
            source_path.write_bytes(content)
            completed = subprocess.run(
                ["textutil", "-convert", "txt", "-output", str(output_path), str(source_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0 or not output_path.is_file():
                return None
            return _decode_text(output_path.read_bytes())
    except Exception:
        return None


def _extract_zip_documents(path: Path, parent_file_id: str) -> list[AnalyzedDocument]:
    documents: list[AnalyzedDocument] = []
    try:
        with zipfile.ZipFile(path) as archive:
            members = [info for info in archive.infolist() if not info.is_dir()]
            if len(members) > MAX_ZIP_ENTRY_COUNT:
                return [
                    AnalyzedDocument(
                        display_name=path.name,
                        extension=".zip",
                        role="supporting",
                        text=None,
                        extracted_text_available=False,
                        warnings=[f"ZIP archive contains too many entries. Limit: {MAX_ZIP_ENTRY_COUNT}."],
                        source="zip",
                        file_id=parent_file_id,
                        raw_content=None,
                    )
                ]
            total_unpacked = sum(info.file_size for info in members)
            if total_unpacked > MAX_ZIP_TOTAL_BYTES:
                return [
                    AnalyzedDocument(
                        display_name=path.name,
                        extension=".zip",
                        role="supporting",
                        text=None,
                        extracted_text_available=False,
                        warnings=["ZIP archive exceeds the safe unpacked size limit."],
                        source="zip",
                        file_id=parent_file_id,
                        raw_content=None,
                    )
                ]

            for idx, info in enumerate(members, start=1):
                entry_path = Path(info.filename)
                if entry_path.is_absolute() or ".." in entry_path.parts:
                    documents.append(
                        AnalyzedDocument(
                            display_name=f"{path.name} :: {info.filename}",
                            extension=entry_path.suffix.lower(),
                            role="supporting",
                            text=None,
                            extracted_text_available=False,
                            warnings=["ZIP entry was rejected because it contains an unsafe path."],
                            source="zip",
                            file_id=f"{parent_file_id}-ZIP-{idx:02d}",
                            raw_content=None,
                        )
                    )
                    continue
                entry_name = entry_path.name
                ext = Path(entry_name).suffix.lower()
                if ext not in ALLOWED_EXTENSIONS or ext == ".zip":
                    continue
                raw = archive.read(info)
                text, warnings, extraction_status = _extract_document_text(entry_name, raw)
                documents.append(
                    AnalyzedDocument(
                        display_name=f"{path.name} :: {entry_name}",
                        extension=ext,
                        role=_detect_role(entry_name),
                        text=text,
                        extracted_text_available=bool(text),
                        warnings=warnings,
                        source="zip",
                        file_id=f"{parent_file_id}-ZIP-{idx:02d}",
                        raw_content=raw,
                    )
                )
    except zipfile.BadZipFile:
        return [
            AnalyzedDocument(
                display_name=path.name,
                extension=".zip",
                role="supporting",
                text=None,
                extracted_text_available=False,
                warnings=["ZIP archive could not be read safely."],
                source="zip",
                file_id=parent_file_id,
                raw_content=None,
            )
        ]
    return documents


def _collect_documents(run_id: str, metadata: dict[str, Any]) -> list[AnalyzedDocument]:
    documents: list[AnalyzedDocument] = []
    normalized_dir = _normalized_dir(run_id)
    normalized_dir.mkdir(parents=True, exist_ok=True)

    for item in metadata.get("files", []):
        stored_path = _input_dir(run_id) / item["stored_name"]
        ext = Path(item["stored_name"]).suffix.lower()
        if ext == ".zip":
            extracted_docs = _extract_zip_documents(stored_path, item["file_id"])
            documents.extend(extracted_docs)
            if extracted_docs:
                item["warnings"] = list(dict.fromkeys(item.get("warnings", []) + ["ZIP archive inspected in safe local mode."]))
            continue

        raw = stored_path.read_bytes()
        text, warnings, extraction_status = _extract_document_text(item["stored_name"], raw)
        document_kind = str(item.get("document_kind") or "").lower()
        role_from_kind = {
            "contract_draft": "contract_draft",
            "technical_specification": "technical_spec",
            "eis_notice": "notice",
        }.get(document_kind)
        role = role_from_kind or item.get("role_hint") or _detect_role(item.get("display_name") or item["stored_name"])
        if text:
            normalized_name = f"{item['file_id'].lower()}-{role}.txt"
            (normalized_dir / normalized_name).write_text(text, encoding="utf-8")
        item["warnings"] = list(dict.fromkeys(item.get("warnings", []) + warnings))
        item["extracted_text_available"] = bool(text)
        item["text_extraction_status"] = extraction_status
        documents.append(
            AnalyzedDocument(
                display_name=item["display_name"],
                extension=ext,
                role=role,
                text=text,
                extracted_text_available=bool(text),
                warnings=warnings,
                source="upload",
                file_id=item["file_id"],
                raw_content=raw,
            )
        )
    return documents


def _collect_role_text(documents: list[AnalyzedDocument], role: str) -> str:
    texts = [doc.text for doc in documents if doc.role == role and doc.text]
    return "\n\n".join(texts).strip()


def _collect_quote_paths(run_id: str, metadata: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for item in metadata.get("files", []):
        if _detect_role(item["stored_name"]) == "tkp":
            paths.append(_input_dir(run_id) / item["stored_name"])
    return paths


def _collect_spreadsheet_sources(documents: list[AnalyzedDocument]) -> list[SpreadsheetSource]:
    return [
        SpreadsheetSource(
            file_id=doc.file_id,
            display_name=doc.display_name,
            source_file=doc.display_name,
            extension=doc.extension,
            raw_content=doc.raw_content or b"",
            source=doc.source,
            role_hint=doc.role,
        )
        for doc in documents
        if doc.extension in {".xlsx", ".xls"} and doc.raw_content
    ]


def _serialize_quote_comparison(quote_comparison) -> dict[str, Any]:
    return quote_comparison.model_dump(mode="json")


def _serialize_economics_summary(economics_summary) -> dict[str, Any]:
    return economics_summary.model_dump(mode="json")


def _maybe_float(value: Any) -> float | None:
    if value in (None, "", "unknown"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_quote_comparison_payload(payload: dict[str, Any]):
    from src.modules.tender_operator_agent_demo.schemas import QuoteComparison

    suppliers = []
    for index, item in enumerate(payload.get("suppliers", []), start=1):
        if isinstance(item, dict) and "supplier_id" in item and "supplier_name" in item:
            suppliers.append(item)
            continue
        suppliers.append(
            {
                "supplier_id": f"SUP-{index:02d}",
                "supplier_name": item.get("supplier_name") or item.get("supplier") or item.get("supplier_label") or f"Supplier {index}",
                "source_file": item.get("source_file") or item.get("supplier_label") or "uploaded quote",
                "source_sheet": item.get("source_sheet"),
                "document_type": item.get("document_type", "legacy_quote_placeholder"),
                "total_amount": _maybe_float(item.get("total_amount") or item.get("price_total")),
                "currency": item.get("currency", "RUB"),
                "items_count": item.get("items_count", 0),
                "delivery_summary": item.get("delivery_summary") or item.get("delivery_time_days"),
                "completeness_score": item.get("completeness_score", 0.0),
                "price_confidence": item.get("price_confidence", 0.0),
                "warnings": item.get("warnings", []),
                "items": item.get("items", []),
            }
        )
    manual_checks = [
        item if isinstance(item, dict) else {"code": "manual_check", "message": str(item)}
        for item in payload.get("manual_checks", [])
    ]
    warnings = [
        item if isinstance(item, dict) else {"code": "warning", "message": str(item)}
        for item in payload.get("warnings", [])
    ]
    return QuoteComparison.model_validate(
            {
                "status": payload.get("status", "blocked"),
                "analysis_mode": payload.get("analysis_mode", "unknown"),
                "supplier_quotes_found": payload.get("supplier_quotes_found", 0),
                "items_extracted": payload.get("items_extracted", 0),
                "suppliers": suppliers,
                "items": payload.get("items", []),
                "comparison_summary": payload.get("comparison_summary", {}),
                "manual_checks": manual_checks,
            "warnings": warnings,
            "limitations": payload.get("limitations", []),
        }
    )


def _coerce_economics_summary_payload(payload: dict[str, Any]):
    from src.modules.tender_operator_agent_demo.schemas import EconomicsSummary

    manual_checks = [
        item if isinstance(item, dict) else {"code": "manual_check", "message": str(item)}
        for item in payload.get("manual_checks", [])
    ]
    warnings = [
        item if isinstance(item, dict) else {"code": "warning", "message": str(item)}
        for item in payload.get("warnings", [])
    ]
    return EconomicsSummary.model_validate(
        {
            "status": payload.get("status", "blocked"),
            "analysis_mode": payload.get("analysis_mode", "unknown"),
            "currency": payload.get("currency"),
            "supplier_cost_min": payload.get("supplier_cost_min"),
            "supplier_cost_selected": payload.get("supplier_cost_selected"),
            "expected_revenue": payload.get("expected_revenue"),
            "preliminary_bid_price": payload.get("preliminary_bid_price"),
            "gross_margin_amount": payload.get("gross_margin_amount"),
            "gross_margin_percent": payload.get("gross_margin_percent"),
            "logistics_reserve": payload.get("logistics_reserve"),
            "risk_reserve": payload.get("risk_reserve"),
            "payment_delay_days": payload.get("payment_delay_days"),
            "cash_gap_estimate": payload.get("cash_gap_estimate"),
            "economics_status": payload.get("economics_status", "insufficient_data"),
            "selected_supplier_name": payload.get("selected_supplier_name"),
            "assumptions": payload.get("assumptions", {}),
            "manual_checks": manual_checks,
            "warnings": warnings,
            "limitations": payload.get("limitations", []),
        }
    )


def _import_runner_module():
    from scripts import run_tender_operator_pilot as pilot_runner

    return pilot_runner


def _try_run_llm_workflow(
    run_id: str,
    notice_text: str | None,
    technical_spec_text: str | None,
    contract_draft_text: str | None,
    quote_paths: list[Path],
    provider_mode: str = "llm",
) -> dict[str, Any] | None:
    try:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from src.modules.controlled_llm_prebid.service import run_controlled_tender_operator_workflow
        from src.shared.db.base import Base

        settings = get_settings()
        if not settings.database_url:
            return None

        engine = create_engine(settings.database_url)
        Base.metadata.create_all(engine)

        context = {
            "deal_id": f"DEMO-{run_id}",
            "operator_id": "tender_operator_demo",
            "operator_profile": {},
            "documents": {
                "notice_text": notice_text or "",
                "technical_spec_text": technical_spec_text or "",
                "contract_draft_text": contract_draft_text or "",
            },
            "workflow_guardrails": {
                "manual_only": True,
                "no_email_send": True,
                "no_platform_submission": True,
                "human_review_required": True,
            },
            "tkp_inputs": [],
        }
        with Session(engine) as session:
            result = run_controlled_tender_operator_workflow(
                session,
                provider_mode=provider_mode,
                context=context,
                include_quote_normalization=False,
                include_bid_decision=False,
                simulate_invalid_output=False,
                provider_name_override=None,
            )
            return {
                "analysis_mode": result.analysis_mode,
                "resolved_provider": result.resolved_provider,
                "requirements": result.requirements,
                "supplier_questions": result.supplier_questions,
                "rfq_draft": result.rfq_draft,
                "contract_risks": result.contract_risks,
                "bid_decision": result.bid_decision,
            }
    except Exception:
        return None


def _runtime_ai_provenance() -> dict[str, Any]:
    """Record observed local runtime facts, never credentials or prompts."""
    settings = get_settings()
    started = time.perf_counter()
    endpoint = settings.local_llm_base_url.rstrip("/") + "/models"
    healthcheck = "unreachable"
    model = settings.llm_model or settings.local_llm_model
    try:
        with urlopen(endpoint, timeout=2) as response:  # local loopback only
            payload = json.loads(response.read().decode("utf-8"))
        models = payload.get("data", [])
        if models:
            model = str(models[0].get("id") or model)
        healthcheck = "ok"
    except Exception:
        healthcheck = "unreachable"
    return {
        "analysis_engine": "deterministic",
        "llm_invoked": False,
        "llm_provider": settings.llm_provider,
        "llm_model": model,
        "llm_endpoint_host": urlparse(settings.local_llm_base_url).hostname,
        "hermes_enabled": settings.hermes_enabled,
        "hermes_healthcheck": "not_configured" if not settings.hermes_enabled else "not_checked",
        "prompt_version": "semantic-matcher-v1",
        "llm_calls_count": 0,
        "llm_latency_ms": round((time.perf_counter() - started) * 1000),
        "fallback_reason": "local_llm_endpoint_unreachable" if healthcheck != "ok" else "local_llm_not_used_by_deterministic_extraction",
        "local_llm_healthcheck": healthcheck,
    }


def _run_supplier_internet_search(
    tender_title: str,
    notice_text: str | None = None,
    technical_spec_text: str | None = None,
) -> SupplierSearchOutcome:
    from src.modules.supplier_search.internet_supplier_search import SupplierSearchOutcome, search_suppliers
    from src.modules.supplier_search.yandex_search_client import YandexSearchClient

    settings = get_settings()
    api_key = settings.yandex_search_api_key
    folder_id = settings.yandex_search_folder_id
    if not api_key or not folder_id:
        return SupplierSearchOutcome(error="Yandex Search API не настроен. Добавьте AI_CORP_YANDEX_SEARCH_API_KEY и AI_CORP_YANDEX_SEARCH_FOLDER_ID.")
    try:
        client = YandexSearchClient(api_key=api_key, folder_id=folder_id, timeout=30)
        return search_suppliers(
            client=client,
            tender_title=tender_title,
            notice_text=notice_text,
            technical_spec_text=technical_spec_text,
            max_results=10,
        )
    except Exception:
        return SupplierSearchOutcome(error="Не удалось выполнить поиск поставщиков через Yandex Search API.")


def _infer_procurement_kind(*texts: str | None) -> str:
    raw_combined = re.sub(r"\s+", " ", " ".join(text or "" for text in texts if text)).strip()
    combined = raw_combined.lower().replace("ё", "е")
    if not combined:
        return "generic"

    if (
        "код активации" in combined
        and "техническ" in combined
        and "поддержк" in combined
        and any(marker in combined for marker in ("средств защиты информации", "программ", "лиценз"))
    ):
        return "software_support"

    software_objects = (
        r"программн(?:ое|ого|ому|ым|ом) обеспечен",
        r"программн(?:ый|ого|ому|ым|ом) (?:продукт|комплекс)",
        r"информационн(?:ая|ой|ую|ые|ых) систем",
        r"(?:^|\W)saas(?:\W|$)",
        r"(?:^|\W)пк\s*[«\"]",
    )
    has_software_object = any(re.search(pattern, combined) for pattern in software_objects) or bool(
        re.search(r"(?:^|\W)ПО(?:\W|$)", raw_combined)
    )
    has_software_change = bool(
        re.search(r"\b(?:внедрен|доработ|модификац|разработ|обновлен|сопровожден)\w*", combined)
    )
    has_software_license = bool(
        re.search(r"(?:неисключительн\w*\s+прав|прав\w*\s+(?:на|использован)|передач\w*\s+прав)", combined)
        or ("лиценз" in combined and has_software_object)
    )
    has_integration = any(
        marker in combined
        for marker in ("интеграц", "смэв", "обмен данн", "api", "межведомствен", "витрин")
    )
    embedded_hardware_software = bool(
        re.search(r"(?:оборудован|компьютер|контроллер|модул)\w*.*(?:встроенн|предустановленн|прошивк)\w*.*(?:по|программ)", combined)
        or re.search(r"(?:встроенн|предустановленн|прошивк)\w*.*(?:по|программ)\w*.*(?:оборудован|компьютер|контроллер|модул)", combined)
    )
    software_semantics = has_software_object and not embedded_hardware_software
    if software_semantics:
        if has_integration and (has_software_change or has_software_license):
            return "mixed"
        if has_software_change:
            return "software_modification"
        if has_software_license:
            return "license"
        if has_integration:
            return "integration"

    if re.search(r"лицензируем\w*\s+(?:вид|деятельност)", combined) and not has_software_object:
        return "generic"

    if "работы электромонтажные" in combined or "выполнение работ" in combined:
        return "works"
    scores = {
        "goods": sum(
            combined.count(marker)
            for marker in (
                "поставка",
                "товар",
                "оборудован",
                "поставк",
                "склад",
                "разгруз",
                "гарантия на товар",
            )
        ),
        "works": sum(
            combined.count(marker)
            for marker in (
                "выполнение работ",
                "работы",
                "результат работ",
                "этап работ",
                "акт сдачи",
                "замена",
                "монтаж",
                "демонтаж",
                "ремонт",
                "пусконалад",
                "смета",
                "кс-2",
                "кс-3",
            )
        ),
        "services": sum(
            combined.count(marker)
            for marker in (
                "оказание услуг",
                "услуг",
                "место оказания услуг",
            )
        ),
        "software_modification": sum(
            combined.count(marker)
            for marker in (
                "программн",
                "пк «",
                "пк \"",
                "программного комплекса",
                "программный комплекс",
                "программного обеспеч",
                "модификац",
                "доработ",
                "модул",
                "сэмд",
                "электронных медицинских документ",
                "исходн",
            )
        ),
        "integration": sum(
            combined.count(marker)
            for marker in (
                "интеграц",
                "смэв",
                "ерн",
                "api",
                "витрин",
                "межведомствен",
                "обмен данн",
            )
        ),
        "license": sum(
            combined.count(marker)
            for marker in (
                "лиценз",
                "права использования",
                "неисключительн",
                "передача прав",
            )
        ),
    }
    if scores["software_modification"] >= 2 and (scores["integration"] >= 1 or scores["license"] >= 1):
        return "mixed"
    if scores["software_modification"] >= 2:
        return "software_modification"
    if scores["integration"] >= 2:
        return "integration"
    if scores["license"] >= 2:
        return "license"
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if ranked and ranked[0][1] > 0:
        return ranked[0][0]
    return "generic"


def _classify_procurement_scope(metadata: dict[str, Any], documents: list[AnalyzedDocument], notice_text: str) -> dict[str, Any]:
    """Classify obligations without using the result to suppress goods extraction."""
    title = str(metadata.get("tender_title") or "").lower()
    text = "\n".join([title, notice_text, *[(doc.text or "") for doc in documents]]).lower()
    items = _collect_supply_items(documents)
    structured_goods = [item for item in items if item.name_source_type == "structured_direct_name"]
    has_goods = bool(items) or "поставка" in title or "поставляем" in text
    structured_codes = (metadata.get("procurement") or {}).get("okpd2_codes", [])
    service_okpd = any(
        str(code.get("code", "")).startswith("62.02")
        for code in structured_codes if isinstance(code, dict)
    )
    okpd_works = any(str(code.get("code", "")).startswith(("41.", "42.", "43.")) for code in structured_codes if isinstance(code, dict))
    strong_works = okpd_works or any(marker in text for marker in ("смета", "кс-2", "кс-3", "ведомость объемов работ"))
    has_services = any(marker in text for marker in ("оказание услуг", "услуг"))
    title_kind = _infer_procurement_kind(title)
    inferred_kind = _infer_procurement_kind(text)
    # A titled supply or a detailed structured product list is authoritative;
    # installation/adjustment in contract boilerplate only makes it mixed.
    software_kinds = {"mixed", "software_modification", "integration", "license", "software_support"}
    support_certificate = (
        "код активации" in text and "техническ" in text and "поддержк" in text
        and any(marker in text for marker in ("средств защиты информации", "программ", "лиценз"))
    )
    if service_okpd or support_certificate:
        primary = "services"
    elif title_kind in software_kinds:
        primary = title_kind
    elif inferred_kind in software_kinds and not has_goods:
        primary = inferred_kind
    elif "оказание услуг" in title:
        primary = "services"
    elif strong_works and not ("поставка" in title or "товар" in title or len(structured_goods) > 1):
        primary = "works"
    elif has_goods and strong_works:
        primary = "mixed"
    elif has_goods:
        primary = "goods"
    elif has_services:
        primary = "services"
    else:
        primary = "unknown"
    software_mixed = primary == "mixed" and (title_kind == "mixed" or not has_goods)
    applicable = primary == "goods" or (primary == "mixed" and not software_mixed) or bool(items)
    return {
        "procurement_primary_scope": primary,
        "contains_goods": has_goods,
        "contains_works": strong_works,
        "contains_services": has_services,
        "software_service_support": service_okpd or support_certificate,
        "activation_support_item": "код активации" in text and "техническ" in text and "поддержк" in text,
        "goods_extraction_applicable": applicable,
        "scope_classification_conflict": has_goods and not applicable,
    }


def _normalize_requirement_title(title: str, procurement_kind: str) -> str | None:
    translated = _translate_user_text(title)
    if procurement_kind != "services":
        return translated
    service_specific = {
        "Требуется соответствие указанным техническим стандартам.": "Услуги должны соответствовать требованиям технического задания и обязательным нормативам.",
        "Оборудование и товары должны соответствовать заявленной спецификации.": "Услуги должны быть оказаны в полном объеме и в соответствии с техническим заданием.",
        "Нужно пройти приёмочные испытания по условиям договора.": "Приемка услуг проводится по условиям контракта.",
        "Требуются гарантия и поддержка после поставки.": "Исполнитель должен обеспечить качественное оказание услуг и выдать предусмотренные итоговые документы.",
        "Техническое предложение со спецификацией.": "Описание программы, графика и состава оказываемых услуг.",
        "Декларация о соответствии.": "Документы, подтверждающие соответствие обязательным требованиям закупки.",
    }
    return service_specific.get(translated, translated)


def _extract_requirement_rows(requirements: dict[str, Any], core_complete: bool, procurement_kind: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for title in requirements.get("technical_requirements", []):
        normalized_title = _normalize_requirement_title(title, procurement_kind)
        if not normalized_title:
            continue
        rows.append(
            {
                "title": normalized_title,
                "detail": "Извлечено детерминированным адаптером из доступных документов.",
                "source": "адаптер раннера" if core_complete else "fallback-адаптер",
            }
        )
    for title in requirements.get("document_requirements", []):
        normalized_title = _normalize_requirement_title(title, procurement_kind)
        if not normalized_title:
            continue
        rows.append(
            {
                "title": normalized_title,
                "detail": "Требование к комплекту документов или подтверждению квалификации.",
                "source": "адаптер раннера" if core_complete else "fallback-адаптер",
            }
        )
    return rows[:10]


def _match_first(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            value = " ".join(group for group in match.groups() if group)
            value = re.sub(r"\s+", " ", value).strip(" .;,\n\t")
            if value:
                return value
    return None


def _collect_matches(text: str, patterns: tuple[str, ...], *, limit: int = 6) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            value = " ".join(group for group in match.groups() if group)
            value = re.sub(r"\s+", " ", value).strip(" .;,\n\t")
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            found.append(value)
            if len(found) >= limit:
                return found
    return found


def _match_first_dotall(text: str, patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if match:
            value = " ".join(group for group in match.groups() if group)
            value = re.sub(r"\s+", " ", value).strip(" .;,\n\t")
            if value:
                return value
    return None


def _normalize_analysis_sentence(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip(" .;,\n\t")
    if not cleaned:
        return None
    cleaned = cleaned[0].upper() + cleaned[1:] if len(cleaned) > 1 else cleaned.upper()
    if cleaned[-1] not in ".!?":
        cleaned += "."
    return cleaned


def _dedupe_text_items(items: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = re.sub(r"\s+", " ", (item or "")).strip(" .").lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(item)
    return unique


def _shorten_payment_terms(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"(\d+)\s*\([^)]+\)\s*рабочих дней", r"\1 рабочих дней", value, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"после подписания Сторонами в единой информационной системе документа о приемке",
        "после подписания документа о приемке",
        cleaned,
        flags=re.IGNORECASE,
    )
    return _normalize_analysis_sentence(cleaned)


def _shorten_acceptance_terms(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"(\d+)\s*\([^)]+\)\s*рабочих дней", r"\1 рабочих дней", value, flags=re.IGNORECASE)
    cleaned = re.sub(
        r",?\s*следующих за днем поступления документа о приемке.*",
        " после поступления документа о приемке",
        cleaned,
        flags=re.IGNORECASE,
    )
    return _normalize_analysis_sentence(cleaned)


def _format_money_value(value: float | str | None) -> str | None:
    if value in (None, ""):
        return None
    try:
        numeric = float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return str(value)
    return f"{numeric:,.2f}".replace(",", " ").replace(".", ",")


def _extract_notice_price(metadata: dict[str, Any], *texts: str) -> str | None:
    procurement = metadata.get("procurement") if isinstance(metadata.get("procurement"), dict) else {}
    for candidate in (
        procurement.get("initial_price"),
        metadata.get("initial_price"),
    ):
        formatted = _format_money_value(candidate)
        if formatted:
            return formatted
    combined = "\n".join(text for text in texts if text)
    if not combined:
        return None
    raw_value = _match_first_dotall(
        combined,
        (
            r"<(?:\w+:)?maxPrice>\s*([\d\s]+(?:[.,]\d+)?)\s*</(?:\w+:)?maxPrice>",
            r"Начальная\s*\(максимальная\)\s*цена\s*(?:контракта|договора)?\D{0,80}([\d\s]+(?:[.,]\d+)?)\s*(?:руб|₽|RUB)",
            r"\bНМЦК\b\D{0,40}([\d\s]+(?:[.,]\d+)?)\s*(?:руб|₽|RUB)",
            r"Цена\s+контракта\D{0,40}([\d\s]+(?:[.,]\d+)?)\s*(?:руб|₽|RUB)",
        ),
    )
    return _format_money_value(raw_value)


def _extract_notice_timeline(text: str, labels: tuple[str, ...], *, stop_markers: tuple[str, ...]) -> str | None:
    if not text:
        return None
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_pattern = "|".join(re.escape(marker) for marker in stop_markers)
    return _match_first_dotall(
        text,
        (
            rf"(?:{label_pattern})[^:\n<]*[:>\-]?\s*(.+?)(?=\s*(?:{stop_pattern})|\Z)",
        ),
    )


def _extract_notice_delivery_deadline(text: str) -> str | None:
    return _extract_notice_timeline(
        text,
        ("Срок поставки", "Сроки поставки", "Срок поставки товара", "Сроки поставки товара", "Период поставки"),
        stop_markers=(
            "Место поставки",
            "Адрес поставки",
            "Условия оплаты",
            "Порядок оплаты",
            "Условия поставки",
            "Порядок поставки",
            "Обеспечение исполнения контракта",
        ),
    ) or _match_first_dotall(
        text,
        (
            r"<(?:\w+:)?deliveryTerm>\s*(.+?)\s*</(?:\w+:)?deliveryTerm>",
            r"поставка[^.\n]{0,80}(в течение[^.\n]+)",
            r"поставка[^.\n]{0,80}(до\s+\d{1,2}\s+[А-Яа-яЁё]+\s+\d{4}\s+года)",
        ),
    )


def _extract_notice_service_deadline(text: str) -> str | None:
    return _extract_notice_timeline(
        text,
        ("Срок оказания услуг", "Сроки оказания услуг", "Период оказания услуг", "Срок выполнения работ"),
        stop_markers=(
            "Место оказания услуг",
            "Место выполнения работ",
            "Условия оплаты",
            "Порядок оплаты",
            "Обеспечение исполнения контракта",
        ),
    ) or _match_first_dotall(
        text,
        (
            r"<(?:\w+:)?deliveryTerm>\s*(.+?)\s*</(?:\w+:)?deliveryTerm>",
            r"оказани[ея]\s+услуг[^.\n]{0,80}(в течение[^.\n]+)",
            r"оказани[ея]\s+услуг[^.\n]{0,80}(до\s+\d{1,2}\s+[А-Яа-яЁё]+\s+\d{4}\s+года)",
        ),
    )


def _rewrite_compliance_highlight(value: str) -> str:
    lowered = value.lower()
    if "федеральной служ" in lowered and "техническому и экспортному контролю" in lowered:
        return "Программа должна быть согласована с ФСТЭК России."
    if "удостоверени" in lowered and "повышени" in lowered:
        return "По итогам обучения нужно выдать удостоверение о повышении квалификации."
    if "аттестаци" in lowered:
        return "Нужно провести итоговую аттестацию слушателей."
    if "учебный план должен содержать" in lowered:
        return "Учебный план должен содержать перечень тем и распределение часов."
    if "раздаточ" in lowered and "материал" in lowered:
        return "Исполнитель должен обеспечить слушателей учебными и раздаточными материалами."
    return _normalize_analysis_sentence(value) or value


def _rewrite_delivery_model_item(value: str, procurement_kind: str) -> str:
    lowered = value.lower()
    if procurement_kind == "services":
        if "очно-заочная" in lowered:
            return "Формат обучения: очно-заочный, с применением дистанционных образовательных технологий."
        if "дистанцион" in lowered:
            return "Часть программы проводится дистанционно на стороне заказчика."
        if "60" in lowered and "%" in lowered:
            return "Около 60% программы проходит в очном формате."
        if "40" in lowered and "%" in lowered:
            return "Около 40% программы проходит дистанционно."
        if "09.00" in value or "18.00" in value:
            return "Режим занятий: с 09:00 до 18:00."
        if "городе хабаровске" in lowered:
            return "Очная часть должна проходить в городе Хабаровске."
    return _normalize_analysis_sentence(value) or value


def _cleanup_tabular_value(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip(" .;,\n\t")
    cleaned = re.sub(r"\s+([,.:;])", r"\1", cleaned)
    cleaned = re.sub(r"([A-Za-zА-Яа-яЁё])\s+(\d)", r"\1 \2", cleaned)
    cleaned = re.sub(r"(\d)\s+([A-Za-zА-Яа-яЁё])", r"\1 \2", cleaned)
    return cleaned or None


def _extract_inline_goods_field(text: str, labels: tuple[str, ...], *, stop_markers: tuple[str, ...]) -> str | None:
    if not text:
        return None
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_pattern = "|".join(re.escape(marker) for marker in stop_markers)
    match = re.search(
        rf"(?:{label_pattern})[^:\n]*[:\-]?\s*(.+?)(?=\s*(?:{stop_pattern})|\Z)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    return _cleanup_tabular_value(match.group(1))


def _cleanup_delivery_address(value: str | None) -> str | None:
    cleaned = _cleanup_tabular_value(value)
    if not cleaned:
        return None
    address_match = re.search(r"по адресу:\s*(.+)$", cleaned, re.IGNORECASE)
    if address_match:
        cleaned = address_match.group(1)
    cleaned = re.sub(r"\s*Расходы по доставке.+$", "", cleaned, flags=re.IGNORECASE)
    return _cleanup_tabular_value(cleaned)


def _extract_goods_characteristics(section_text: str) -> str | None:
    normalized = _cleanup_tabular_value(section_text) or ""
    matches = re.findall(
        r"([А-ЯA-ZЁ][А-ЯA-Zа-яa-zЁё0-9 ,()/%-]{1,60})\s*:\s*([^:]{1,80}?)(?=\s+\d+\s+[А-ЯA-ZЁ]|$)",
        normalized,
    )
    items: list[str] = []
    for name, value in matches:
        left = _cleanup_tabular_value(name)
        right = _cleanup_tabular_value(value)
        if not left or not right:
            continue
        if left.lower().startswith(("параметры для", "инструкция по", "обоснование", "описание объекта закупки")):
            continue
        items.append(f"{left}: {right}")
        if len(items) >= 4:
            break
    return "; ".join(items) if items else None


_SUPPLY_UNIT_PATTERN = re.compile(
    r"^(шт|штука|м|м\.|м2|м3|кг|килограмм|г|грамм|л|миллилитр|компл(?:ект)?|комп|упак(?:овка)?|уп|пара|рул(?:он)?|набор|короб(?:ка)?|пачка|лист|ед\.?|усл\.?\s*ед\.?|пог\.?\s*м\.?)$",
    re.IGNORECASE,
)


def _is_goods_supply_table_present(text: str) -> bool:
    lowered = (text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "перечень запасных частей",
            "перечень товаров",
            "перечень поставки",
            "спецификация",
            "№ п/п",
            "ед.изм",
            "кол-во",
        )
    )


def _normalize_supply_name(value: str | None) -> str:
    cleaned = _cleanup_tabular_value(value) or ""
    cleaned = re.sub(r"\s+или\s+эквивалент\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+,", ",", cleaned)
    return cleaned.strip(" -") or "Позиция"


def _normalize_supply_name_key(value: str | None) -> str:
    cleaned = _normalize_supply_name(value).lower()
    cleaned = cleaned.replace("ё", "е")
    # OCR/DOCX line wrapping can split one word with a hyphen; it is not a
    # different procurement item from the structured XML spelling.
    cleaned = re.sub(r"(?<=[a-zа-я])[-‐‑–]\s*(?=[a-zа-я])", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bгост\b", "", cleaned)
    cleaned = re.sub(r"[^a-zа-я0-9]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _normalize_supply_unit(value: str | None) -> str | None:
    cleaned = (_cleanup_tabular_value(value) or "").lower().replace(" ", "")
    if cleaned in {"", "—", "данныхнедостаточнотребуетсяпроверка", "неуказано"} or "данныхнедостаточно" in cleaned:
        return None
    aliases = {
        "м.": "м",
        "пог.м.": "м",
        "пог.м": "м",
        "ед": "ед.",
        "ед.": "ед.",
        "комп": "компл",
        "штука": "шт",
        "упаковка": "упак",
        "миллилитр": "мл",
        "грамм": "г",
        "килограмм": "кг",
        "коробка": "короб",
    }
    return aliases.get(cleaned, cleaned) or None


def _is_line_item_name(value: str | None) -> bool:
    cleaned = _cleanup_tabular_value(value) or ""
    lowered = cleaned.lower()
    if lowered == "данных недостаточно — требуется проверка":
        return False
    if len(cleaned) < 3 or re.fullmatch(r"(?:гост|ту|ост|санпин)\s*[\d.\-–]+", lowered, re.IGNORECASE):
        return False
    disallowed = (
        "должен соответствовать", "требования к", "технические характеристики", "условия поставки",
        "гарантийн", "упаковк", "примечани", "описание объекта закупки", "наименование товара, услуги",
    )
    if any(marker in lowered for marker in disallowed):
        return False
    if re.fullmatch(r"(?:\d+(?:[.,]\d+)?\s*)?(?:в|кв|вт|квт|мм²|мм2|мм|см)", lowered):
        return False
    return bool(re.search(r"[a-zа-яё]", lowered, re.IGNORECASE))


def _is_ktru_or_okpd(value: str | None) -> bool:
    compact = (_cleanup_tabular_value(value) or "").replace(" ", "")
    return bool(re.fullmatch(r"\d{2,3}(?:\.\d{2,3}){1,4}(?:-\d+(?:-\d+)*)?", compact))


def _normalize_quantity_value(value: str | None) -> str | None:
    cleaned = _cleanup_tabular_value(value)
    if not cleaned:
        return None
    compact = cleaned.replace(" ", "")
    if not re.fullmatch(r"\d+(?:[.,]\d+)?", compact):
        return cleaned
    if "." in compact:
        compact = compact.rstrip("0").rstrip(".")
    if "," in compact:
        compact = compact.rstrip("0").rstrip(",")
    return compact


def _format_decimal_price(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _parse_float(value: str | None) -> float | None:
    if not value:
        return None
    compact = value.replace(" ", "").replace(",", ".")
    try:
        return float(compact)
    except ValueError:
        return None


def _extract_gost_tokens(text: str) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for match in re.finditer(r"ГОСТ\s*[\d.]+(?:[-–]\d{2,4})?(?:-\d{4})?", text or "", re.IGNORECASE):
        raw = _cleanup_tabular_value(match.group(0))
        if not raw:
            continue
        token = raw.upper().replace("–", "-")
        if token in seen:
            continue
        seen.add(token)
        values.append(token)
    return values


def _summarize_supply_characteristics(values: list[str], *, limit: int = 6) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _cleanup_tabular_value(value)
        if not cleaned:
            continue
        if cleaned.lower().startswith(("требования к качеству", "требования к безопасности")):
            continue
        if "__" in cleaned:
            continue
        if any(marker in cleaned.lower() for marker in ("заказчик", "поставщик", "м.п.", "при учете требований")):
            continue
        normalized = cleaned.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _supply_item_to_row(item: SupplyItem) -> dict[str, Any]:
    characteristics = "; ".join(item.characteristics) if item.characteristics else "Требуется сверка характеристик по ТЗ."
    return {
        "№": item.item_no or "—",
        "Наименование": item.display_name or item.name,
        "official_name": item.official_name or item.name,
        "display_name": item.display_name or item.name,
        "Кол-во": item.quantity or "не указано",
        "Ед. изм.": item.unit or "—",
        "Ключевые характеристики": characteristics,
        "ГОСТ / норматив": ", ".join(item.gost) if item.gost else "—",
        "Эквивалент": "да" if item.equivalent_allowed else "нет",
        "Цена за ед., руб.": item.unit_price or "—",
        "Сумма, руб.": item.total_price or "—",
        "Источник": ", ".join(item.source_documents or [item.source_document]),
        "quantity_status": item.quantity_status,
        "evidence_ids": item.evidence_ids or ([item.evidence_id] if item.evidence_id else []),
        "key_characteristics": item.characteristics[:5],
        "name_source_type": item.name_source_type,
        "name_source_path": item.name_source_path,
        "quantity_source_path": item.quantity_source_path,
        "unit_source_path": item.unit_source_path,
        "source_row_number": item.source_row_number,
        "source_record_id": item.source_record_id,
        "extraction_strategy": item.extraction_strategy,
        "okpd2": item.okpd2,
        "ktru": item.ktru,
    }


def _extract_supply_items_from_spec_text(text: str, source_document: str) -> list[SupplyItem]:
    if not text or not _is_goods_supply_table_present(text):
        return []
    lines = [_cleanup_tabular_value(line) for line in text.replace("\f", "\n").splitlines()]
    normalized_lines = [line for line in lines if line]
    if not normalized_lines:
        return []

    start_index = 0
    for index, line in enumerate(normalized_lines):
        lowered = line.lower()
        if "№ п/п" in lowered or "перечень запасных частей" in lowered or "перечень товаров" in lowered:
            start_index = index
            break
    candidate_lines = normalized_lines[start_index:]

    items: list[SupplyItem] = []
    index = 0
    while index < len(candidate_lines):
        line = candidate_lines[index]
        if not re.fullmatch(r"\d{1,3}", line):
            index += 1
            continue
        item_no = line
        if index + 1 >= len(candidate_lines):
            break
        name = _normalize_supply_name(candidate_lines[index + 1])
        if not _is_line_item_name(name):
            index += 1
            continue
        block: list[str] = []
        index += 2
        while index < len(candidate_lines) and not re.fullmatch(r"\d{1,3}", candidate_lines[index]):
            block.append(candidate_lines[index])
            index += 1
        equivalent_allowed = "эквивалент" in (candidate_lines[index - len(block) - 1] if block else name).lower() or "эквивалент" in name.lower()
        unit = None
        quantity = None
        characteristics: list[str] = []
        pos = 0
        while pos < len(block):
            normalized_value = _cleanup_tabular_value(block[pos]) or ""
            next_value = _cleanup_tabular_value(block[pos + 1]) if pos + 1 < len(block) else None
            if not unit and _SUPPLY_UNIT_PATTERN.fullmatch(normalized_value) and next_value:
                if re.fullmatch(r"\d+(?:[.,]\d+)?", next_value.replace(" ", "")):
                    unit = _normalize_supply_unit(normalized_value)
                    quantity = _normalize_quantity_value(next_value)
                    pos += 2
                    continue
            if (
                normalized_value
                and next_value
                and not re.fullmatch(r"\d+(?:[.,]\d+)?", normalized_value.replace(" ", ""))
                and not _SUPPLY_UNIT_PATTERN.fullmatch(normalized_value)
                and not re.fullmatch(r"\d+(?:[.,]\d+)?", next_value.replace(" ", ""))
                and not _SUPPLY_UNIT_PATTERN.fullmatch(next_value)
            ):
                characteristics.append(f"{normalized_value}: {next_value}")
                pos += 2
                continue
            pos += 1
        raw_fragment = "\n".join([item_no, name, *block])
        item = SupplyItem(
            item_no=item_no,
            name=_normalize_supply_name(name),
            quantity=quantity,
            unit=unit,
            characteristics=_summarize_supply_characteristics(characteristics or [_extract_goods_characteristics(raw_fragment) or ""]),
            gost=_extract_gost_tokens(raw_fragment),
            equivalent_allowed=equivalent_allowed,
            source_document=source_document,
            source_kind="technical_spec",
            confidence="high" if quantity and unit else "medium",
            raw_fragment=raw_fragment,
            source_documents=[source_document],
            quantity_status="specified" if quantity is not None and unit else "not_specified",
            source_row_number=len(items) + 1,
            evidence_id=f"ev-{hashlib.sha256(f'{source_document}|spec|{item_no}|{name}|{quantity}|{unit}'.encode('utf-8')).hexdigest()[:16]}",
        )
        if item.name:
            items.append(item)
        if len(items) >= 24:
            break
    return items


def _extract_supply_items_from_xlsx_text(text: str, source_document: str) -> list[SupplyItem]:
    if not text or "\t" not in text:
        return []
    items: list[SupplyItem] = []
    column_map: dict[str, int] = {}
    for line in text.splitlines():
        cells = [_cleanup_tabular_value(cell) or "" for cell in line.split("\t")]
        meaningful = [cell for cell in cells if cell]
        if len(meaningful) < 4:
            continue
        lowered = " ".join(meaningful).lower()
        if "есклп" in lowered and "колич" in lowered:
            # Wrapped XLSX headers for medicinal products are emitted on two
            # lines; their labels are shifted left by one cell after export.
            column_map.update({"name": 1, "identifier": 2, "unit": 3, "quantity": 12})
            continue
        if ("наимен" in lowered or "мнн" in lowered) and ("колич" in lowered or "кол-во" in lowered):
            for index, cell in enumerate(cells):
                cell_lower = cell.lower()
                if "наимен" in cell_lower or "мнн" in cell_lower:
                    column_map["name"] = index
                elif "колич" in cell_lower or "кол-во" in cell_lower:
                    column_map["quantity"] = index
                elif "ед." in cell_lower or "единиц" in cell_lower:
                    column_map["unit"] = index
                elif any(marker in cell_lower for marker in ("ктру", "окпд", "есклп")):
                    column_map["identifier"] = index
            continue
        if any(token in lowered for token in ("используемый метод", "коммерческие предложения", "коэффициент", "лист", "sheet")):
            continue
        if "наименование" in lowered and ("коли" in lowered or "кол-во" in lowered):
            continue
        if not re.fullmatch(r"\d{1,3}", meaningful[0]):
            continue
        if {"name", "unit", "quantity"}.issubset(column_map):
            raw_name = cells[column_map["name"]] if column_map["name"] < len(cells) else ""
            unit_raw = cells[column_map["unit"]] if column_map["unit"] < len(cells) else ""
            quantity_raw = cells[column_map["quantity"]] if column_map["quantity"] < len(cells) else ""
            if not _is_line_item_name(raw_name) or not _SUPPLY_UNIT_PATTERN.fullmatch(unit_raw) or _parse_float(quantity_raw) is None:
                continue
            identifier_index = column_map.get("identifier")
            ktru = cells[identifier_index] if identifier_index is not None and identifier_index < len(cells) else None
            numeric_tail = [_parse_float(value) for value in cells[column_map["quantity"] + 1:] if value]
            numeric_tail = [value for value in numeric_tail if value is not None]
            total_value = numeric_tail[-1] if numeric_tail else None
            quantity = _normalize_quantity_value(quantity_raw)
            item = SupplyItem(
                item_no=meaningful[0], name=_normalize_supply_name(raw_name), quantity=quantity,
                unit=_normalize_supply_unit(unit_raw), characteristics=[], gost=_extract_gost_tokens(" ".join(meaningful)),
                equivalent_allowed="эквивалент" in raw_name.lower(), source_document=source_document,
                source_kind="nmck_xlsx", confidence="high", raw_fragment=line,
                total_price=_format_decimal_price(total_value), source_documents=[source_document],
                quantity_status="specified", source_row_number=len(items) + 1,
                evidence_id=f"ev-{hashlib.sha256(f'{source_document}|xlsx|{len(items)+1}|{raw_name}|{quantity}|{unit_raw}'.encode('utf-8')).hexdigest()[:16]}",
                ktru=ktru if _is_ktru_or_okpd(ktru) else None,
            )
            items.append(item)
            continue
        raw_name = meaningful[1]
        name = _normalize_supply_name(raw_name)
        if not _is_line_item_name(name):
            continue
        value_offset = 2
        ktru = None
        if _is_ktru_or_okpd(meaningful[value_offset]):
            ktru = meaningful[value_offset]
            value_offset += 1
        if len(meaningful) <= value_offset + 1:
            continue
        unit_raw = meaningful[value_offset]
        quantity_raw = meaningful[value_offset + 1]
        if not _SUPPLY_UNIT_PATTERN.fullmatch(unit_raw) or _parse_float(quantity_raw) is None:
            # A table row with shifted/ambiguous columns is review material, not a line item.
            continue
        unit = _normalize_supply_unit(unit_raw)
        quantity = _normalize_quantity_value(quantity_raw)
        numeric_tail = [_parse_float(value) for value in meaningful[value_offset + 2:]]
        numeric_tail = [value for value in numeric_tail if value is not None]
        total_value = numeric_tail[-1] if numeric_tail else None
        quantity_value = _parse_float(quantity)
        derived_unit_price = (total_value / quantity_value) if total_value is not None and quantity_value not in (None, 0) else None
        price_value = derived_unit_price or (numeric_tail[-2] if len(numeric_tail) >= 2 else None)
        item = SupplyItem(
            item_no=meaningful[0],
            name=name,
            quantity=quantity,
            unit=unit,
            characteristics=[],
            gost=_extract_gost_tokens(" ".join(meaningful)),
            equivalent_allowed="эквивалент" in raw_name.lower(),
            source_document=source_document,
            source_kind="nmck_xlsx",
            confidence="high" if total_value is not None else "medium",
            raw_fragment=line,
            unit_price=_format_decimal_price(price_value),
            total_price=_format_decimal_price(total_value),
            source_documents=[source_document],
            quantity_status="specified" if quantity is not None and unit else "not_specified",
            source_row_number=len(items) + 1,
            evidence_id=f"ev-{hashlib.sha256(f'{source_document}|xlsx|{len(items) + 1}|{name}|{quantity}|{unit}'.encode('utf-8')).hexdigest()[:16]}",
            ktru=ktru,
        )
        items.append(item)
        if len(items) >= 24:
            break
    return items


def _extract_service_items_from_nmck_text(text: str, source_document: str) -> list[SupplyItem]:
    """Extract unit-priced services from DOCX/XLSX NMCK table text.

    The document extractor preserves DOCX rows as tab-separated text.  Unlike
    goods specifications, an NMCK service list can have no row number and no
    quantity: its rows are name, unit and comparable unit prices.  This parser
    deliberately does not infer a fixed volume or a contract total.
    """
    if not text:
        return []
    rows: list[SupplyItem] = []
    seen: set[tuple[str, str]] = set()
    pending_name: list[str] = []
    for row_number, raw_line in enumerate(text.splitlines(), start=1):
        cells = [_cleanup_tabular_value(cell) or "" for cell in raw_line.split("\t")]
        meaningful = [cell for cell in cells if cell]
        if len(meaningful) < 3:
            pending_lower = meaningful[0].lower() if meaningful else ""
            if len(meaningful) == 1 and "\t" not in raw_line and meaningful[0] and not any(
                marker in pending_lower for marker in ("обоснование", "используемый метод", "коммерческое предложение", "начальная", "под одной")
            ):
                pending_name.append(meaningful[0])
            continue
        lowered = " ".join(meaningful).lower()
        if any(marker in lowered for marker in (
            "наименование услуг", "коммерческое предложение", "используемый метод",
            "начальная сумма", "итого", "всего", "максимальное значение цены",
            "под одной условной единицей",
        )):
            continue
        unit_index = next((i for i, value in enumerate(meaningful) if re.fullmatch(r"(?:условная\s+единица|нормо[ -]?час(?:а|ов)?)", value, re.IGNORECASE)), None)
        if unit_index is None or unit_index == 0:
            continue
        raw_name = " ".join([*pending_name, *meaningful[:unit_index]]).strip()
        pending_name = []
        prices = [_parse_float(value) for value in meaningful[unit_index + 1:]]
        prices = [value for value in prices if value is not None]
        if not raw_name or not prices:
            continue
        name = _normalize_supply_name(raw_name)
        key = (_normalize_supply_name_key(name), meaningful[unit_index].lower())
        if not key[0] or key in seen:
            continue
        seen.add(key)
        evidence_seed = f"{source_document}|service-table|{row_number}|{name}".encode("utf-8")
        evidence_id = f"ev-{hashlib.sha256(evidence_seed).hexdigest()[:16]}"
        rows.append(SupplyItem(
            item_no=None,
            name=name,
            quantity=None,
            unit=_normalize_supply_unit(meaningful[unit_index]),
            characteristics=[],
            gost=[],
            equivalent_allowed=None,
            source_document=source_document,
            source_kind="nmck_service_table",
            confidence="high",
            raw_fragment=raw_line,
            unit_price=_format_decimal_price(prices[-1]),
            total_price=None,
            source_documents=[source_document],
            item_type="service",
            quantity_status="not_specified",
            pricing_basis="conditional_unit_price" if "условн" in meaningful[unit_index].lower() else "hourly_rate",
            source_row_number=row_number,
            evidence_id=evidence_id,
            unit_original=meaningful[unit_index],
        ))
    return rows


def _extract_supply_items_from_notification_xml(text: str, source_document: str) -> list[SupplyItem]:
    """Extract purchaseObject rows from the notification XML returned by EIS.

    EIS notification XML is source-backed and contains the canonical item name,
    OKEI, price and quantity.  It is commonly classified as a supporting file,
    so it must be parsed independently of the human-readable attachments.
    """
    if not text or "purchaseObject" not in text:
        return []
    rows: list[SupplyItem] = []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    objects = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "purchaseObject"]
    for row_number, element in enumerate(objects, start=1):
        def direct_child(tag: str) -> ET.Element | None:
            return next((node for node in element if node.tag.rsplit("}", 1)[-1] == tag), None)

        def nested_child(parent: ET.Element | None, tag: str) -> ET.Element | None:
            if parent is None:
                return None
            return next((node for node in parent.iter() if node.tag.rsplit("}", 1)[-1] == tag), None)

        def descendants(tag: str) -> list[ET.Element]:
            return [node for node in element.iter() if node.tag.rsplit("}", 1)[-1] == tag]

        def value(tag: str) -> str:
            node = next(iter(descendants(tag)), None)
            return html.unescape(" ".join(node.itertext()).strip()) if node is not None else ""

        # The direct purchaseObject name is the item.  A descendant search can
        # otherwise select the first KTRU characteristic name as the product.
        ktru = next(iter(descendants("KTRU")), None)
        name_node = direct_child("name")
        if name_node is None:
            name_node = next((node for node in (ktru.iter() if ktru is not None else []) if node.tag.rsplit("}", 1)[-1] == "name"), None)
        name = _normalize_supply_name(" ".join(name_node.itertext()).strip() if name_node is not None else "")
        characteristics = []
        for characteristic in descendants("characteristicsUsingTextForm"):
            characteristic_name = next((node for node in characteristic if node.tag.rsplit("}", 1)[-1] == "name"), None)
            if characteristic_name is not None:
                label = " ".join(characteristic_name.itertext()).strip()
                value_node = next((node for node in characteristic.iter() if node.tag.rsplit("}", 1)[-1] in {"concreteValue", "min", "max"}), None)
                unit_node = next((node for node in characteristic.iter() if node.tag.rsplit("}", 1)[-1] == "nationalCode"), None)
                if value_node is not None:
                    comparator = "≥ " if any(node.tag.rsplit("}", 1)[-1] == "minMathNotation" and "greaterOrEqual" in " ".join(node.itertext()) for node in characteristic.iter()) else ""
                    unit_value = " ".join(unit_node.itertext()).strip() if unit_node is not None else ""
                    characteristics.append(f"{label} {comparator}{' '.join(value_node.itertext()).strip()} {unit_value}".strip())
        raw_type = value("type").upper()
        price = _parse_float(value("price"))
        total = _parse_float(value("sum"))
        quantity_node = direct_child("quantity")
        if quantity_node is None:
            drug_customer_info = nested_child(direct_child("drugQuantityCustomersInfo"), "drugQuantityCustomerInfo")
            quantity_node = nested_child(drug_customer_info, "quantity")
        quantity_raw = ""
        if quantity_node is not None:
            quantity_value_node = next(
                (node for node in quantity_node.iter() if node.tag.rsplit("}", 1)[-1] in {"value", "concreteValue"}),
                None,
            )
            quantity_raw = " ".join(quantity_value_node.itertext()).strip() if quantity_value_node is not None else " ".join(quantity_node.itertext()).strip()
        quantity = _normalize_quantity_value(quantity_raw) if re.search(r"\d", quantity_raw) else None
        okei = direct_child("OKEI")
        if okei is None:
            drug_info = nested_child(element, "drugInfo")
            okei = nested_child(drug_info, "manualUserOKEI")
        unit_node = next(
            (node for node in (okei.iter() if okei is not None else []) if node.tag.rsplit("}", 1)[-1] in {"nationalCode", "name"}),
            None,
        )
        unit = _normalize_supply_unit(" ".join(unit_node.itertext()).strip() if unit_node is not None else None)
        if not name or price is None and total is None:
            continue
        item_type = "service" if raw_type in {"SERVICE", "WORK"} else "goods"
        evidence_seed = f"{source_document}|notification-xml|{row_number}|{name}".encode("utf-8")
        rows.append(SupplyItem(
            item_no=None,
            name=name,
            quantity=quantity,
            unit=unit,
            characteristics=characteristics,
            gost=_extract_gost_tokens(" ".join(element.itertext())),
            equivalent_allowed=None,
            source_document=source_document,
            source_kind="notification_xml",
            confidence="high",
            raw_fragment=" ".join(element.itertext()).strip(),
            unit_price=_format_decimal_price(price),
            total_price=_format_decimal_price(total),
            source_documents=[source_document],
            item_type=item_type,
            quantity_status="specified" if quantity is not None and unit else "not_specified",
            pricing_basis="unit_price",
            source_row_number=row_number,
            evidence_id=f"ev-{hashlib.sha256(evidence_seed).hexdigest()[:16]}",
            unit_original=unit,
            name_source_type="structured_direct_name" if direct_child("name") is not None else "structured_item_title",
            name_source_path="purchaseObject/name" if direct_child("name") is not None else "purchaseObject/KTRU/name",
            quantity_source_path="purchaseObject/quantity",
            unit_source_path="purchaseObject/OKEI",
            source_record_id=value("sid") or None,
            extraction_strategy="notification_xml_purchase_object",
        ))
    return rows


def _service_subject_from_sources(metadata: dict[str, Any], notice_text: str, documents: list[AnalyzedDocument]) -> str:
    """Return a source-backed service subject, never a demo/report title."""
    subject = _match_first_dotall(
        notice_text,
        (r"<(?:\w+:)?purchaseObjectInfo>\s*([^<]+?)\s*</(?:\w+:)?purchaseObjectInfo>",),
    )
    if subject:
        return _cleanup_tabular_value(subject) or subject
    for document in documents:
        if document.role != "notice" or not document.text:
            continue
        subject = _match_first_dotall(
            document.text,
            (r"<(?:\w+:)?purchaseObjectInfo>\s*([^<]+?)\s*</(?:\w+:)?purchaseObjectInfo>",),
        )
        if subject:
            return _cleanup_tabular_value(subject) or subject
    return str(metadata.get("tender_title") or "Предмет закупки не извлечён")


def _service_okpd2_from_sources(notice_text: str, documents: list[AnalyzedDocument]) -> str | None:
    source_text = "\n".join([notice_text, *(doc.text or "" for doc in documents if doc.role == "notice")])
    return _match_first_dotall(
        source_text,
        (r"<(?:\w+:)?OKPDCode>\s*([\d.]+)\s*</(?:\w+:)?OKPDCode>",),
    )


def _is_vehicle_maintenance_services(subject: str, okpd2: str | None, service_items: list[SupplyItem]) -> bool:
    corpus = " ".join([subject, *(item.name for item in service_items)]).lower()
    return bool(
        (okpd2 or "").startswith("45.20")
        or ("автотранспорт" in corpus and any(token in corpus for token in ("диагност", "ремонт", "техническ")))
    )


def _service_item_analysis_rows(items: list[SupplyItem]) -> list[dict[str, Any]]:
    """Serialize extracted rows without converting an unspecified volume to zero."""
    return [
        {
            "stable_item_id": item.evidence_id or f"service-{index}",
            "original_name": item.name,
            "normalized_name": item.name,
            "unit": item.unit_original or item.unit,
            "unit_price": item.unit_price,
            "pricing_basis": item.pricing_basis,
            "quantity": item.quantity,
            "quantity_status": item.quantity_status,
            "technical_requirements": item.characteristics,
            "evidence_ids": [item.evidence_id] if item.evidence_id else [],
            "source_document": item.source_document,
            "source_row_number": item.source_row_number,
        }
        for index, item in enumerate(items, start=1)
    ]


def _build_services_preliminary_analysis(
    *,
    metadata: dict[str, Any],
    documents: list[AnalyzedDocument],
    notice_text: str,
    contract_draft_text: str,
) -> dict[str, Any]:
    """Deterministic, domain-safe analysis for a unit-priced service catalogue."""
    service_items = [item for item in _collect_supply_items(documents) if item.item_type == "service"]
    subject = _service_subject_from_sources(metadata, notice_text, documents)
    okpd2 = _service_okpd2_from_sources(notice_text, documents)
    profile = "vehicle_maintenance_services" if _is_vehicle_maintenance_services(subject, okpd2, service_items) else "general_services"
    initial_price = _extract_notice_price(metadata, notice_text, contract_draft_text)
    contract_missing = not bool(contract_draft_text.strip())
    rows = [
        {
            "№": str(index),
            "Наименование услуги": item.name,
            "Объём/количество": "не определён документацией" if item.quantity is None else item.quantity,
            "Единица": item.unit_original or item.unit or "не извлечено — требуется проверка",
            "Цена единицы, руб.": item.unit_price or "не извлечено — требуется проверка",
            "Источник": item.source_document,
            "Evidence": item.evidence_id or "не извлечено — требуется проверка",
        }
        for index, item in enumerate(service_items, start=1)
    ]
    coverage = {
        "extracted_item_count": len(service_items),
        "analyzed_item_count": len(service_items),
        "ignored_item_count": 0,
        "item_evidence_coverage": 1.0 if service_items and all(item.evidence_id for item in service_items) else 0.0,
        "grouping_coverage": 1.0 if service_items else 0.0,
    }
    return {
        "overview": [
            f"Предмет закупки: {subject}",
            f"ОКПД2: {okpd2}." if okpd2 else "ОКПД2 не извлечён — требуется проверка.",
            f"НМЦК: {initial_price} руб." if initial_price else "НМЦК не извлечена — требуется проверка.",
            f"Извлечено и проанализировано позиций услуг: {len(service_items)}.",
            "В документации указаны единичные расценки; фиксированный объём отдельных услуг не определён.",
        ],
        "compliance_highlights": [
            "Необходимо подтвердить наличие специалистов, оборудования и ремонтной базы для перечня услуг до решения об участии."
        ],
        "delivery_model": [
            "Объём услуг определяется потребностью заказчика; единичные расценки не образуют подтверждённую сумму по каждой операции."
        ],
        "contract_highlights": (
            ["Проект контракта отсутствует в доступном наборе документов; оплата, приемка, ответственность и обеспечение не оценены."]
            if contract_missing else ["Проект контракта доступен и требует отдельной документальной проверки."]
        ),
        "next_actions": [
            "Получить проект контракта и проверить оплату, приемку, ответственность, обеспечение и сроки.",
            "Сопоставить единичные расценки с внутренней стоимостью нормо-часа и иными подтверждёнными затратами.",
            "Подтвердить возможность выполнения полного перечня услуг: специалистов, оборудование и ремонтную базу.",
            "Уточнить порядок учета запасных частей и материалов, если он не раскрыт первичными документами.",
        ],
        "extracted_fields": ["предмет закупки", "ОКПД2" if okpd2 else "", "НМЦК" if initial_price else "", "единичные расценки", "объём услуг не определён"],
        "procurement_kind": "services",
        "domain_profile": profile,
        "service_items": _service_item_analysis_rows(service_items),
        "item_coverage": coverage,
        "source_completeness": "partial" if contract_missing else "partial",
        "missing_documents": ["draft_contract"] if contract_missing else [],
        "supply_section_note": "Перечень услуг и единичные расценки собраны из таблицы обоснования НМЦК; фиксированный объём по строкам не заявлен.",
        "spec_table": {
            "columns": ["№", "Наименование услуги", "Объём/количество", "Единица", "Цена единицы, руб.", "Источник", "Evidence"],
            "rows": rows,
        },
    }


def _extract_legacy_goods_spec_rows(technical_spec_text: str) -> list[dict[str, str]]:
    if not technical_spec_text:
        return []
    unit_pattern = r"(шт|м|компл(?:ект)?|упак(?:овка)?|пара|кг|л|рул(?:он)?|набор|ед\.?|усл\.?\s*ед\.?|услуга)"
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    section_pattern = re.compile(
        r"(?:(?P<section_no>\d(?:\s*\d)?)\.\s*)?Описание\s+объекта\s+закупки:",
        re.IGNORECASE,
    )
    matches = list(section_pattern.finditer(technical_spec_text))
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(technical_spec_text)
        section = technical_spec_text[start:end]
        section_number = re.sub(r"\s+", "", match.group("section_no") or "") or str(len(rows) + 1)
        row_match = re.search(
            r"1\s+2\s+3\s+4\s+5\s+6\s+7\s+(?P<row>.+?)(?=Характеристики\s+объекта\s+закупки:|$)",
            section,
            re.IGNORECASE | re.DOTALL,
        )
        if not row_match:
            continue
        row_text = _cleanup_tabular_value(row_match.group("row")) or ""
        parsed_match = re.search(
            rf"(?P<num>\d+)\s+(?P<name>.+?)\s+(?P<unit>{unit_pattern})\s+(?P<tail>.+)$",
            row_text,
            re.IGNORECASE,
        )
        if not parsed_match:
            continue
        tail = _cleanup_tabular_value(parsed_match.group("tail")) or ""
        quantity_match = re.search(r"(\d+(?:[.,]\d+)?)\s*$", tail)
        quantity = _normalize_quantity_value(quantity_match.group(1) if quantity_match else "не указано") or "не указано"
        characteristics = _extract_goods_characteristics(section) or "Требуется сверка характеристик по ТЗ."
        row = {
            "№": section_number or parsed_match.group("num"),
            "Наименование": _cleanup_tabular_value(parsed_match.group("name")) or "Позиция",
            "Кол-во": quantity,
            "Ед. изм.": _cleanup_tabular_value(parsed_match.group("unit")) or "—",
            "Ключевые характеристики": characteristics,
            "ГОСТ / норматив": ", ".join(_extract_gost_tokens(section)) or "—",
            "Эквивалент": "нет",
            "Цена за ед., руб.": "—",
            "Сумма, руб.": "—",
            "Источник": "Техническое задание",
        }
        key = (
            row["Наименование"].lower(),
            row["Ед. изм."].lower(),
            row["Кол-во"].lower(),
            row["Ключевые характеристики"].lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def _merge_supply_items(items: list[SupplyItem]) -> list[SupplyItem]:
    """Create canonical procurement items while retaining every source as evidence.

    A quantity can be absent in a DOCX representation of a purchase object and
    present in XML/XLSX.  It must complement the same item rather than create a
    second row merely because the less complete representation lacks a value.
    """
    merged: list[SupplyItem] = []
    for item in items:
        if item.record_type != "line_item" or not _is_line_item_name(item.name):
            continue
        # Service rows describe operations.  Similar vocabulary does not make
        # diagnostics, repair, replacement and maintenance one canonical row.
        # Preserve each source row; goods-only matching starts below.
        if item.item_type == "service":
            item.evidence_ids = list(dict.fromkeys(item.evidence_ids or ([item.evidence_id] if item.evidence_id else [])))
            item.official_name = item.official_name or item.name
            item.display_name = item.display_name or item.name
            merged.append(item)
            continue
        name_key = _normalize_supply_name_key(item.name)
        if not name_key:
            continue
        item.evidence_ids = list(dict.fromkeys(item.evidence_ids or ([item.evidence_id] if item.evidence_id else [])))
        item.official_name = item.official_name or item.name
        item.display_name = item.display_name or item.name
        item_unit = _normalize_supply_unit(item.unit)
        signature = tuple(sorted(_normalize_supply_name_key(value) for value in item.characteristics if value))
        existing = next((candidate for candidate in merged if (
            (_normalize_supply_name_key(candidate.name) == name_key or bool(
                set(token for token in _normalize_supply_name_key(candidate.name).split() if len(token) >= 5)
                & set(token for token in name_key.split() if len(token) >= 5)
            ))
            and (not _normalize_supply_unit(candidate.unit) or not item_unit or _normalize_supply_unit(candidate.unit) == item_unit)
            and (not candidate.quantity or not item.quantity or candidate.quantity == item.quantity)
            and (not candidate.okpd2 or not item.okpd2 or candidate.okpd2 == item.okpd2)
            and (not candidate.ktru or not item.ktru or candidate.ktru == item.ktru)
            # Characteristics commonly differ in completeness between a DOCX
            # table and structured XML.  They complement an otherwise exact
            # name/unit/quantity match; they must not create a duplicate.
        )), None)
        if existing is None:
            merged.append(item)
            continue
        if not existing.quantity and item.quantity:
            existing.quantity = item.quantity
        if not existing.unit and item.unit:
            existing.unit = item.unit
        if not existing.unit_price and item.unit_price:
            existing.unit_price = item.unit_price
        if not existing.total_price and item.total_price:
            existing.total_price = item.total_price
        if item.gost:
            existing.gost = _summarize_supply_characteristics(existing.gost + item.gost, limit=4)
        if item.characteristics:
            existing.characteristics = _summarize_supply_characteristics(existing.characteristics + item.characteristics, limit=6)
        existing.equivalent_allowed = existing.equivalent_allowed or item.equivalent_allowed
        existing.confidence = "high" if "high" in {existing.confidence, item.confidence} else existing.confidence
        for source_document in item.source_documents or [item.source_document]:
            if source_document not in existing.source_documents:
                existing.source_documents.append(source_document)
        existing.evidence_ids = list(dict.fromkeys(existing.evidence_ids + item.evidence_ids))
        # Structured purchaseObject is preferred for the displayed official
        # name, but its presence never discards quantities/evidence from a
        # complementary specification or NMCK row.
        if item.name_source_type == "structured_direct_name" and existing.name_source_type != "structured_direct_name":
            existing.name = item.name
            existing.official_name = item.name
            existing.display_name = item.name
            existing.name_source_type = item.name_source_type
            existing.name_source_path = item.name_source_path
        if existing.source_document != item.source_document and item.source_kind == "technical_spec":
            existing.source_document = item.source_document
            existing.source_kind = item.source_kind
            existing.raw_fragment = item.raw_fragment or existing.raw_fragment
    def sort_key(item: SupplyItem) -> tuple[int, str]:
        try:
            number = int(item.item_no or "999")
        except ValueError:
            number = 999
        return number, item.name.lower()
    name_counts: dict[str, int] = {}
    for item in merged:
        name_counts[_normalize_supply_name_key(item.official_name or item.name)] = name_counts.get(_normalize_supply_name_key(item.official_name or item.name), 0) + 1
    for item in merged:
        official_name = item.official_name or item.name
        characteristics = _summarize_supply_characteristics(item.characteristics, limit=3)
        # Keep the official wording intact.  Add only compact, source-backed
        # characteristics when a repeated generic name would be ambiguous.
        if name_counts[_normalize_supply_name_key(official_name)] > 1 and characteristics:
            item.display_name = f"{official_name} — {', '.join(characteristics)}"
        else:
            item.display_name = official_name
    return sorted(merged, key=sort_key)


def _collect_supply_items(documents: list[AnalyzedDocument]) -> list[SupplyItem]:
    return _merge_supply_items(_collect_unmerged_source_items(documents))


def _collect_unmerged_source_items(documents: list[AnalyzedDocument]) -> list[SupplyItem]:
    """Return per-document extractor rows before legacy reconciliation.

    The production source graph consumes these rows.  `_collect_supply_items`
    remains a compatibility presentation helper and is never its value source.
    """
    extracted: list[SupplyItem] = []
    for doc in documents:
        text = doc.text or ""
        if not text:
            continue
        lowered_name = doc.display_name.lower()
        if doc.role == "technical_spec" or any(
            token in lowered_name
            for token in ("техническ", "описание объекта", "спецификац", "перечень", "ведомост")
        ):
            extracted.extend(_extract_supply_items_from_spec_text(text, doc.display_name))
        if doc.extension in {".xlsx", ".xls"} or "нмцк" in lowered_name or "обоснование" in lowered_name:
            extracted.extend(_extract_supply_items_from_xlsx_text(text, doc.display_name))
            extracted.extend(_extract_service_items_from_nmck_text(text, doc.display_name))
        if doc.extension == ".xml" or "purchasenotice" in text.lower() or "epnotification" in text.lower() or "purchaseobject" in text.lower():
            extracted.extend(_extract_supply_items_from_notification_xml(text, doc.display_name))
    return extracted


def _extract_goods_spec_table(technical_spec_text: str) -> list[dict[str, str]]:
    rows = [_supply_item_to_row(item) for item in _extract_supply_items_from_spec_text(technical_spec_text, "Техническое задание")]
    return rows or _extract_legacy_goods_spec_rows(technical_spec_text)


def _extract_goods_spec_table_from_tabular_text(text: str) -> list[dict[str, str]]:
    return [_supply_item_to_row(item) for item in _extract_supply_items_from_xlsx_text(text, "Табличное приложение")]


def _build_supply_rows(documents: list[AnalyzedDocument]) -> list[dict[str, str]]:
    return [_supply_item_to_row(item) for item in _collect_supply_items(documents)]


def _document_source_label(doc: AnalyzedDocument) -> str:
    return doc.display_name


def _extract_meaningful_snippets(text: str, keywords: tuple[str, ...], *, max_snippets: int = 8) -> list[str]:
    if not text:
        return []
    snippets: list[str] = []
    seen: set[str] = set()
    normalized_text = re.sub(r"\s+", " ", text)
    for keyword in keywords:
        for match in re.finditer(re.escape(keyword), normalized_text, re.IGNORECASE):
            start = max(0, match.start() - 120)
            end = min(len(normalized_text), match.end() + 220)
            snippet = normalized_text[start:end].strip(" .;,\n\t")
            if len(snippet) < 40:
                continue
            key = snippet.lower()
            if key in seen:
                continue
            seen.add(key)
            snippets.append(snippet)
            if len(snippets) >= max_snippets:
                return snippets
    return snippets


def _build_software_work_rows(documents: list[AnalyzedDocument]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    candidate_docs = [
        doc
        for doc in documents
        if doc.text and (
            doc.role == "technical_spec"
            or "описание объекта" in doc.display_name.lower()
            or "техничес" in doc.display_name.lower()
        )
    ]
    block_definitions = [
        (
            "Новые структурированные электронные медицинские документы",
            ("структурированных электронных медицинских документ", "сэмд"),
            "Разработать и внедрить новые структуры/формы электронных медицинских документов.",
            "ПК «Здравоохранение», медицинская информационная система",
            "Обновленный модуль с поддержкой новых СЭМД",
            "Приемка по реализованным структурам документов и результатам тестирования",
        ),
        (
            "Обработка информации об ИПРА",
            ("ипра", "реабилитации и абилитации"),
            "Реализовать обработку данных индивидуальных программ реабилитации и абилитации.",
            "ПК «Здравоохранение», внешние данные ИПРА",
            "Система обрабатывает и хранит сведения ИПРА в требуемом формате",
            "Приемка по корректной обработке сценариев и данным ИПРА",
        ),
        (
            "Интеграция с ЕРН через СМЭВ",
            ("ерн", "смэв", "межведомственного электронного взаимодействия"),
            "Настроить обмен данными и интеграционный контур с ЕРН через СМЭВ.",
            "ЕРН, СМЭВ, ПК «Здравоохранение»",
            "Рабочая интеграция и обмен данными с внешним регистром",
            "Приемка по интеграционному тестированию и доступности обмена",
        ),
        (
            "Получение данных об участниках СВО",
            ("сво", "витрины данных министерства обороны", "статуса сво"),
            "Реализовать получение и обработку данных об участниках СВО из внешней витрины.",
            "Витрина данных Минобороны, ПК «Здравоохранение»",
            "Обогащение записей пациентов данными по статусу СВО",
            "Приемка по корректному получению, кэшированию и журналированию данных",
        ),
        (
            "Передача лицензии на обновленный модуль",
            ("лиценз", "передаче лицензии", "передача прав"),
            "Передать заказчику лицензию и права использования обновленного модуля.",
            "Документы на лицензирование, обновленный модуль",
            "Заказчик получает правоомерное использование обновленного решения",
            "Приемка по передаче лицензии и комплекту закрывающих документов",
        ),
    ]
    for title, keywords, action, systems, result, acceptance in block_definitions:
        matched_doc = None
        matched_snippet = None
        for doc in candidate_docs:
            snippets = _extract_meaningful_snippets(doc.text or "", keywords, max_snippets=1)
            if snippets:
                matched_doc = doc
                matched_snippet = snippets[0]
                break
        if not matched_doc:
            continue
        rows.append(
            {
                "№": str(len(rows) + 1),
                "Блок работ / результат": title,
                "Что нужно сделать": action,
                "Входные/внешние системы": systems,
                "Результат для заказчика": result,
                "Критерии приёмки": acceptance if not matched_snippet else _cleanup_tabular_value(matched_snippet[:220]) or acceptance,
                "Источник": _document_source_label(matched_doc),
            }
        )
    return rows


def _collect_goods_supply_items_from_documents(documents: list[AnalyzedDocument]) -> list[SupplyItem]:
    return _collect_supply_items(documents)


def _build_goods_requirement_rows(documents: list[AnalyzedDocument]) -> list[dict[str, str]]:
    items = _collect_goods_supply_items_from_documents(documents)
    rows: list[dict[str, str]] = []
    for item in items:
        details = [f"{item.name} — {item.quantity or 'не указано'} {item.unit or ''}".strip()]
        if item.gost:
            details.append(", ".join(item.gost))
        if item.characteristics:
            details.append("; ".join(item.characteristics[:3]))
        rows.append(
            {
                "title": f"{item.name} — {item.quantity or 'не указано'} {item.unit or ''}".strip(),
                "detail": " | ".join(details),
                "source": ", ".join(item.source_documents or [item.source_document]),
                "type": "товарная позиция",
                "priority": "high",
            }
        )
    general_requirements = [
        ("Соответствие ГОСТ / ТУ", "Товар должен соответствовать ГОСТ, ТУ и иной действующей нормативной документации.", "нормативное"),
        ("Сертификаты и паспорт качества", "Нужны сертификаты, декларации или паспорт качества производителя по применимым позициям.", "документы качества"),
        ("Маркировка и безопасность", "Маркировка и безопасность товара должны соответствовать обязательным требованиям.", "качество / безопасность"),
        ("Доставка до заказчика", "Доставка и разгрузка до адреса заказчика должны быть включены и подтверждены поставщиком.", "логистика"),
    ]
    for title, detail, req_type in general_requirements:
        rows.append(
            {
                "title": title,
                "detail": detail,
                "source": "Техническое задание",
                "type": req_type,
                "priority": "medium",
            }
        )
    return rows[:12]


def _build_goods_questions(documents: list[AnalyzedDocument]) -> list[str]:
    items = _collect_goods_supply_items_from_documents(documents)
    questions = [
        f"Подтверждаете поставку {item.name} в объёме {item.quantity or 'не указано'} {item.unit or ''}?".strip()
        for item in items[:6]
    ]
    questions.extend(
        [
            "Укажите производителя, марку, ГОСТ/ТУ и страну происхождения по каждой позиции.",
            "Подтвердите наличие сертификатов, деклараций и паспортов качества по поставляемым товарам.",
            "Подтвердите срок поставки по заявке заказчика и наличие товара на складе.",
            "Включены ли доставка и разгрузка до адреса заказчика в цену предложения?",
            "Есть ли отклонения от характеристик ТЗ или предлагаемые аналоги?",
        ]
    )
    return _dedupe_text_items(questions)


def _build_goods_rfq_payload(metadata: dict[str, Any], documents: list[AnalyzedDocument]) -> dict[str, Any]:
    items = _collect_goods_supply_items_from_documents(documents)
    return {
        "rfq_title": f"RFQ draft / {metadata['tender_title']}",
        "sections": [
            "Позиции поставки и объём",
            "Подтверждение ГОСТ, сертификатов и паспортов качества",
            "Срок поставки, наличие на складе и логистика",
            "Цена за единицу, сумма, НДС и срок действия КП",
        ],
        "items": [
            {
                "№": item.item_no or "—",
                "Позиция": item.name,
                "Кол-во": item.quantity or "не указано",
                "Ед.": item.unit or "—",
                "Обязательные характеристики": "; ".join(item.characteristics) if item.characteristics else "Сверить по ТЗ",
                "ГОСТ / норматив": ", ".join(item.gost) if item.gost else "—",
                "Цена за ед.": item.unit_price or "",
                "Сумма": item.total_price or "",
                "Срок поставки": "",
                "Сертификаты / паспорт": "",
                "Отклонения / аналоги": "Допустим эквивалент" if item.equivalent_allowed else "",
            }
            for item in items
        ],
    }


def _build_goods_economics_payload(
    metadata: dict[str, Any],
    documents: list[AnalyzedDocument],
    analysis_mode: str,
    economics: dict[str, Any] | None,
) -> dict[str, Any]:
    if economics:
        payload = dict(economics)
        payload.setdefault("analysis_mode", analysis_mode)
        payload.setdefault("economics_status", "needs_review")
        payload.setdefault("result", "Экономика требует ручной проверки")
        payload.setdefault("drivers", ["Сопоставление ТКП требует ручного подтверждения."])
        payload["manual_checks"] = [
            item.get("message", item.get("code", "Проверить расчёт по исходным ТКП вручную."))
            if isinstance(item, dict)
            else str(item)
            for item in payload.get("manual_checks", [])
        ] or ["Проверить расчёт по исходным ТКП вручную."]
        payload.setdefault("metrics", [
            {"label": "Минимальная закупочная стоимость", "value": payload.get("supplier_cost_min", "не определена")},
            {"label": "Предварительная цена подачи", "value": payload.get("preliminary_bid_price", "не определена")},
            {"label": "Целевая маржа", "value": payload.get("gross_margin_percent", "не определена")},
        ])
        return payload
    items = _collect_goods_supply_items_from_documents(documents)
    nmck = _extract_notice_price(metadata, _collect_role_text(documents, "technical_spec"), _collect_role_text(documents, "contract_draft"), _collect_role_text(documents, "notice"))
    total_quantity = sum(_parse_float(item.quantity) or 0 for item in items if (item.unit or "") == "м")
    nmck_value = _parse_float(nmck.replace(" ", "") if nmck else None)
    avg_price = (nmck_value / total_quantity) if nmck_value is not None and total_quantity else None
    metrics = [
        {"label": "НМЦК", "value": f"{nmck} руб." if nmck else "не указана"},
        {"label": "Цена закупки", "value": "не определена, требуется КП поставщика"},
        {"label": "Что запросить", "value": "цену за единицу и сумму по каждой позиции"},
        {"label": "Что запросить", "value": "включены ли доставка и разгрузка"},
        {"label": "Что запросить", "value": "НДС, срок действия КП и наличие на складе"},
        {"label": "Общий объём", "value": f"{int(total_quantity) if float(total_quantity).is_integer() else total_quantity} м" if total_quantity else "не рассчитан"},
        {"label": "Ориентир по НМЦК на метр", "value": f"{_format_decimal_price(avg_price)} руб./м" if avg_price is not None else "не рассчитан"},
    ]
    return {
        "analysis_mode": analysis_mode,
        "currency": "RUB",
        "economics_status": "insufficient_data",
        "supplier_cost_min": None,
        "supplier_cost_selected": None,
        "expected_revenue": None,
        "preliminary_bid_price": None,
        "gross_margin_amount": None,
        "gross_margin_percent": None,
        "logistics_reserve": None,
        "risk_reserve": None,
        "payment_delay_days": None,
        "cash_gap_estimate": None,
        "selected_supplier_name": None,
        "result": "Экономика требует запроса КП по товарным позициям",
        "status": "blocked",
        "metrics": metrics,
        "drivers": [
            "Экономика построена по НМЦК и извлечённым позициям поставки без подмены software/integration шаблонами.",
            "Для решения нужны реальные КП по каждой позиции, включая доставку и документы качества.",
        ],
        "manual_checks": [
            "Запросить цену за единицу и сумму по каждой позиции.",
            "Проверить, включены ли доставка, разгрузка, НДС и упаковка.",
            "Сверить наличие товара и срок поставки в течение 15 рабочих дней по заявке.",
        ],
        "warnings": [],
        "limitations": [],
        "assumptions": {"supply_items_count": len(items)},
    }


def _build_document_grounded_requirements(
    documents: list[AnalyzedDocument],
    procurement_kind: str,
) -> list[dict[str, str]]:
    if procurement_kind == "goods":
        rows = _build_goods_requirement_rows(documents)
        if rows:
            return rows
    rows: list[dict[str, str]] = []
    for doc in documents:
        text = doc.text or ""
        if not text:
            continue
        doc_name = doc.display_name
        if procurement_kind in {"mixed", "software_modification", "integration", "license"}:
            mapping = [
                (("модификац", "программного комплекса", "модул"), "Модификация ПК «Здравоохранение»", "функциональное", "high"),
                (("структурированных электронных медицинских документ", "сэмд"), "Разработка новых структурированных электронных медицинских документов", "функциональное", "high"),
                (("ипра", "реабилитации и абилитации"), "Обработка информации об ИПРА", "функциональное", "high"),
                (("ерн", "смэв"), "Интеграция с ЕРН через СМЭВ", "интеграционное", "high"),
                (("сво", "витрины данных министерства обороны"), "Получение данных об участниках СВО из витрины Минобороны", "интеграционное", "high"),
                (("лиценз", "передаче лицензии", "передача прав"), "Передача лицензии и прав на обновленный модуль", "лицензионное", "high"),
                (("приемк", "испытан", "акт"), "Требования к приемке результатов работ", "приёмка", "medium"),
                (("срок", "этап"), "Сроки и этапность выполнения работ", "срок / этап", "medium"),
                (("заявк", "инструкц"), "Требования к заявке участника", "заявка участника", "medium"),
                (("персональн", "медицинск"), "Требования к обработке медицинских и персональных данных", "информационная безопасность / персональные данные", "medium"),
            ]
        else:
            mapping = [
                (("поставка", "товар"), "Требования к предмету поставки", "функциональное", "high"),
                (("приемк", "испытан", "акт"), "Требования к приемке", "приёмка", "medium"),
                (("срок",), "Сроки исполнения", "срок / этап", "medium"),
            ]
        for keywords, title, req_type, priority in mapping:
            snippets = _extract_meaningful_snippets(text, keywords, max_snippets=1)
            if not snippets:
                continue
            if any(item["title"] == title and item["source"] == doc_name for item in rows):
                continue
            rows.append(
                {
                    "title": title,
                    "detail": _cleanup_tabular_value(snippets[0][:260]) or title,
                    "source": doc_name,
                    "type": req_type,
                    "priority": priority,
                }
            )
    return rows[:12]


def _build_document_grounded_questions(procurement_kind: str, documents: list[AnalyzedDocument]) -> list[str]:
    if procurement_kind in {"mixed", "software_modification", "integration", "license"}:
        return [
            "Есть ли у исполнителя опыт доработки медицинских информационных систем и аналогичных госинтеграций?",
            "Есть ли подтвержденный опыт интеграции через СМЭВ и подключения внешних реестров?",
            "Кто предоставляет доступы и тестовые контуры ЕРН, СМЭВ и витрины данных Минобороны?",
            "Доступны ли форматы обмена, спецификации API и правила согласования СЭМД?",
            "Входит ли в объем работ интеграционное тестирование и сопровождение приемки?",
            "Какие результаты передаются заказчику: код, модуль, лицензия, документация, инструкции?",
            "Как оформляется передача лицензии и прав на обновленный модуль?",
            "Какие ограничения и риски есть по персональным данным, медданным и защищенным каналам?",
        ]
    if procurement_kind == "goods":
        return _build_goods_questions(documents)
    service_text = "\n".join(doc.text or "" for doc in documents)
    if procurement_kind == "services" and _is_vehicle_maintenance_services(service_text, _service_okpd2_from_sources(service_text, documents), _collect_supply_items(documents)):
        return [
            "Предоставьте проект контракта для проверки оплаты, приемки, ответственности, обеспечения, сроков и расторжения.",
            "Какова подтверждённая внутренняя стоимость нормо-часа и какие затраты включены в нее?",
            "Какие услуги из перечня выполняются собственными силами, а для каких требуется подрядчик?",
            "Подтверждены ли специалисты, оборудование и ремонтная база для полного перечня операций?",
            "Как учитываются запасные части и материалы, если это не раскрыто документацией?",
            "Можно ли сопоставить каждую релевантную единичную расценку с подтверждённой себестоимостью?",
        ]
    return [
        "Подтверждаете ли вы исполнение требований технического задания в полном объеме?",
        "Какие сроки исполнения и ограничения по поставке/работам вы видите?",
        "Какие документы и подтверждения должны быть подготовлены для заявки?",
    ]


def _build_document_grounded_risks(procurement_kind: str, documents: list[AnalyzedDocument], contract_text: str) -> list[dict[str, str]]:
    if procurement_kind in {"mixed", "software_modification", "integration", "license"}:
        return [
            {"clause": "Неполные требования к интеграциям", "classification": "deal_breaker_candidate", "impact": "Без описания форматов и сценариев обмена объем работ может быть занижен.", "mitigation": "Запросить спецификации API/СМЭВ и перечень обязательных сценариев обмена."},
            {"clause": "Зависимость от доступов к СМЭВ/ЕРН/витрине Минобороны", "classification": "deal_breaker_candidate", "impact": "Без доступов и тестовых контуров сроки и приемка будут сдвигаться.", "mitigation": "Зафиксировать в переписке и контракте, кто и когда предоставляет доступы и тестовые среды."},
            {"clause": "Риск по персональным и медицинским данным", "classification": "deal_breaker_candidate", "impact": "Ошибки в требованиях ИБ и обработке медданных создают юридический и проектный риск.", "mitigation": "Уточнить требования к ИБ, журналированию, ролям доступа и защите каналов."},
            {"clause": "Неочевидный объем доработок", "classification": "market_standard_harsh_term", "impact": "Фактическая трудоемкость модификации модуля может быть выше, чем следует из краткого описания.", "mitigation": "Разбить оценку по функциональным блокам, этапам и интеграциям до запроса КП."},
            {"clause": "Риск приемки по результатам интеграционного тестирования", "classification": "market_standard_harsh_term", "impact": "Приемка зависит от внешних систем и согласований, не полностью контролируемых исполнителем.", "mitigation": "Зафиксировать критерии приемки, тестовые сценарии и роль заказчика во внешних согласованиях."},
            {"clause": "Риск лицензирования и передачи прав", "classification": "market_standard_harsh_term", "impact": "Неясные условия лицензии и передачи прав могут создать спор по результату работ.", "mitigation": "Проверить проект контракта и приложения на режим лицензии, объем прав и пакет передаваемых материалов."},
        ]
    if procurement_kind == "goods":
        return [
            {"clause": "Несоответствие ГОСТ и характеристикам", "classification": "deal_breaker_candidate", "impact": "Поставка аналога с иными характеристиками приведет к отклонению или проблемам на приемке.", "mitigation": "Запросить производителя, ГОСТ/ТУ и паспорт качества по каждой позиции."},
            {"clause": "Риск по сроку поставки", "classification": "market_standard_harsh_term", "impact": "Товар может не уложиться в срок 15 рабочих дней по заявке.", "mitigation": "Подтвердить складской остаток, срок отгрузки и логистику до адреса заказчика."},
            {"clause": "Логистика и разгрузка не включены в цену", "classification": "market_standard_harsh_term", "impact": "Маржа может снизиться, если доставка, барабаны и разгрузка не учтены.", "mitigation": "Уточнить состав цены и включение доставки/разгрузки в КП."},
            {"clause": "Неполный пакет документов качества", "classification": "market_standard_harsh_term", "impact": "Без сертификатов, деклараций или паспорта качества приемка может быть заблокирована.", "mitigation": "Получить комплект документов качества до подачи или до заключения контракта."},
        ]
    service_text = "\n".join(doc.text or "" for doc in documents)
    if procurement_kind == "services" and _is_vehicle_maintenance_services(
        service_text, _service_okpd2_from_sources(service_text, documents), _collect_supply_items(documents)
    ):
        service_items = [item for item in _collect_supply_items(documents) if item.item_type == "service"]
        evidence = ", ".join(item.evidence_id for item in service_items[:3] if item.evidence_id)
        return [
            {"risk_id": "risk-service-volume", "category": "financial/commercial", "clause": "Неопределённый фактический объём услуг", "classification": "deal_breaker_candidate", "impact": "Единичные расценки не позволяют заранее определить выручку, загрузку и полную себестоимость.", "mitigation": "Получить проект контракта и уточнить порядок заявок; считать экономику только после ввода объёмов и себестоимости.", "evidence_ids": evidence},
            {"risk_id": "risk-service-economics", "category": "financial/commercial", "clause": "Недостаточно данных для расчёта экономики", "classification": "deal_breaker_candidate", "impact": "Нет подтверждённых затрат на труд, материалы, логистику, финансирование и фактический объём.", "mitigation": "Собрать внутреннюю стоимость нормо-часа и коммерческие входы по релевантным операциям.", "evidence_ids": evidence},
            {"risk_id": "risk-service-contract", "category": "source_completeness", "clause": "Неполнота договорного анализа", "classification": "deal_breaker_candidate", "impact": "Без проекта контракта нельзя оценить оплату, приемку, штрафы, обеспечение и часть ответственности.", "mitigation": "Получить и проверить проект контракта до безусловного решения.", "evidence_ids": ""},
            {"risk_id": "risk-service-capability", "category": "operational", "clause": "Операционная возможность требует проверки", "classification": "market_standard_harsh_term", "impact": "Доступные документы не подтверждают наличие специалистов, оборудования и ремонтной базы исполнителя.", "mitigation": "Провести due diligence исполнителя по полному перечню операций.", "evidence_ids": evidence},
            {"risk_id": "risk-service-pricing", "category": "financial/commercial", "clause": "Ценовой риск единичных расценок", "classification": "market_standard_harsh_term", "impact": "Без сравнения с внутренней себестоимостью нельзя оценить выгодность единичных расценок.", "mitigation": "Сопоставить цены строк с подтверждённой себестоимостью до формирования цены заявки.", "evidence_ids": evidence},
        ]
    return [
        {"clause": "Недостаточно предметных данных", "classification": "market_standard_harsh_term", "impact": "Часть рисков требует ручной проверки документов.", "mitigation": "Проверить ТЗ и проект контракта вручную."}
    ]


def _build_document_grounded_rfq_sections(procurement_kind: str) -> list[str]:
    if procurement_kind in {"mixed", "software_modification", "integration", "license"}:
        return [
            "Опыт аналогичных доработок медицинских ИС и интеграций",
            "Команда проекта и роли по разработке, интеграции, тестированию и ИБ",
            "Оценка трудоемкости по функциональным блокам и этапам",
            "Подход к интеграции через СМЭВ, ЕРН и витрину Минобороны",
            "Состав передаваемых результатов: модуль, документация, лицензия, права",
            "Стоимость по блокам, тестированию, сопровождению и интеграционным рискам",
        ]
    if procurement_kind == "goods":
        return [
            "Позиции поставки, количество и единицы измерения",
            "Подтверждение характеристик, ГОСТ и допустимости аналогов",
            "Цена за единицу, сумма, НДС и срок действия КП",
            "Срок поставки, наличие на складе, доставка и разгрузка",
            "Сертификаты, декларации и паспорт качества",
        ]
    return [
        "Перечень позиций и объём поставки",
        "Подтверждение сроков, сертификатов и гарантий",
        "Условия оплаты и срок действия КП",
    ]


def _build_goods_preliminary_analysis(
    *,
    metadata: dict[str, Any],
    documents: list[AnalyzedDocument],
    technical_spec_text: str,
    contract_draft_text: str,
    notice_text: str,
) -> dict[str, Any]:
    tz_text = technical_spec_text or ""
    contract_text = contract_draft_text or ""
    notice = notice_text or ""
    initial_price = _extract_notice_price(metadata, notice, contract_text)
    supply_items = _collect_goods_supply_items_from_documents(documents)
    spec_rows = [_supply_item_to_row(item) for item in supply_items] or _extract_goods_spec_table(tz_text)
    delivery_deadline = _extract_inline_goods_field(
        tz_text,
        ("Срок поставки", "Сроки поставки товара"),
        stop_markers=(
            "Требования к качеству",
            "Требования к безопасности",
            "Условия оплаты",
            "Адрес поставки",
            "Место поставки",
            "Условия поставки",
            "Порядок поставки",
        ),
    ) or _extract_notice_delivery_deadline(notice)
    delivery_deadline = _cleanup_tabular_value(delivery_deadline) or delivery_deadline
    delivery_address = _extract_inline_goods_field(
        tz_text,
        ("Место поставки", "Место поставки товаров", "Адрес поставки товара"),
        stop_markers=(
            "Расходы по доставке",
            "Условия поставки",
            "Срок поставки",
            "Сроки поставки товара",
            "Порядок поставки",
            "Условия оплаты",
        ),
    )
    delivery_address = _cleanup_delivery_address(delivery_address) or delivery_address
    payment_terms = _match_first(
        contract_text,
        (
            r"в течение\s+(\d+\s*\([^)]+\)\s*рабочих дней[^.]+документа о приемке)",
            r"в течение\s+(\d+\s*рабочих дней[^.]+документа о приемке)",
            r"Оплата[^.]*?в течение\s+([^.]+)",
        ),
    )
    acceptance_window = _match_first(
        contract_text,
        (
            r"Не позднее\s+(\d+\s*\([^)]+\)\s*рабочих дней[^.]+документа о приемке)",
            r"Не позднее\s+(\d+\s*рабочих дней[^.]+документа о приемке)",
        ),
    )
    execution_security = _match_first(
        contract_text + "\n" + notice,
        (
            r"обеспечени[ея]\s+исполнения\s+контракта[^.]{0,120}",
        ),
    )
    execution_security_percent = _match_first_dotall(
        notice + "\n" + contract_text,
        (
            r"contractGuarantee[\s\S]{0,600}?<(?:\w+:)?part>(\d+(?:[.,]\d+)?)</(?:\w+:)?part>",
            r"обеспечени[ея]\s+исполнения\s+контракта[^%\n]{0,200}?(\d+(?:[.,]\d+)?)\s*%",
        ),
    )

    contract_terms = []
    if payment_terms:
        contract_terms.append(f"Условия оплаты: в течение {payment_terms}.")
    if execution_security:
        security_text = "Обеспечение исполнения контракта: да"
        if execution_security_percent:
            security_text += f", {execution_security_percent.replace('.', ',')}% от НМЦК"
        contract_terms.append(security_text + ".")
    if acceptance_window:
        short_acceptance = _shorten_acceptance_terms(acceptance_window)
        if short_acceptance:
            contract_terms.append(f"Срок приемки: {short_acceptance.rstrip('.')}.")
    if "цена контракта является твердой" in contract_text.lower():
        contract_terms.append("Цена контракта: твердая, без индексации на период исполнения.")
    contract_terms = _dedupe_text_items([_normalize_analysis_sentence(item) or item for item in contract_terms[:6]])

    total_meter_quantity = sum(_parse_float(item.quantity) or 0 for item in supply_items if (item.unit or "") == "м")
    total_positions = len(supply_items)
    top_position = max(supply_items, key=lambda item: _parse_float(item.quantity) or 0, default=None)

    overview = [f"Предмет закупки: {metadata.get('tender_title') or 'поставка товаров'}"]
    if initial_price:
        overview.append(f"НМЦК: {initial_price} руб.")
    if spec_rows:
        overview.append("В ТЗ выделена табличная спецификация по товарам.")
    if total_positions:
        overview.append(f"Количество позиций: {total_positions}.")
    if total_meter_quantity:
        quantity_text = int(total_meter_quantity) if float(total_meter_quantity).is_integer() else total_meter_quantity
        overview.append(f"Общий объём кабеля/провода: {quantity_text} м.")
    if delivery_address:
        overview.append(f"Адрес поставки: {(_normalize_analysis_sentence(delivery_address) or delivery_address).rstrip('.')}.")
    if delivery_deadline:
        overview.append(f"Срок поставки: {(_normalize_analysis_sentence(delivery_deadline) or delivery_deadline).rstrip('.')}.")
    if payment_terms:
        short_payment_terms = _shorten_payment_terms(payment_terms)
        if short_payment_terms:
            overview.append(f"Оплата: {short_payment_terms.rstrip('.')}")
    overview = _dedupe_text_items([_normalize_analysis_sentence(item) or item for item in overview[:7]])

    compliance_highlights = _dedupe_text_items(
        [
            "Нужно подтвердить соответствие каждой позиции ГОСТ, ТУ и характеристикам ТЗ.",
            "Для позиций с обязательной сертификацией нужно получить сертификаты, декларации и паспорта качества.",
            "Аналоги допустимы только при полном соответствии характеристикам и требованиям заказчика.",
            "До участия нужно проверить включение доставки и разгрузки в цену поставщика.",
        ]
    )
    delivery_model = _dedupe_text_items(
        [
            _normalize_analysis_sentence(f"Поставка выполняется по адресу заказчика: {delivery_address}") if delivery_address else None,
            _normalize_analysis_sentence(f"Срок поставки: {delivery_deadline}") if delivery_deadline else None,
            "Поставка выполняется по заявке заказчика; важно подтвердить складской остаток и срок отгрузки.",
            "Доставка и разгрузка должны быть подтверждены поставщиком отдельно.",
        ]
    )
    next_actions = _dedupe_text_items(
        [
            "Сверить извлечённые позиции с ТЗ и НМЦК.xlsx по количеству, единицам и ГОСТ.",
            "Запросить КП по каждой позиции с ценой за единицу, суммой, НДС и сроком поставки.",
            "Подтвердить наличие товара, документы качества и логистику до адреса заказчика.",
        ]
    )
    return {
        "overview": overview,
        "compliance_highlights": compliance_highlights,
        "delivery_model": delivery_model,
        "contract_highlights": contract_terms,
        "next_actions": next_actions,
        "supply_items": [item.__dict__ for item in supply_items],
        "extracted_fields": _dedupe_text_items(
            [
                "НМЦК" if initial_price else "",
                "позиции поставки" if spec_rows else "",
                "срок поставки" if delivery_deadline else "",
                "адрес поставки" if delivery_address else "",
            ]
        ),
        "procurement_kind": "goods",
        "supply_section_note": (
            "Позиции поставки собраны из ТЗ, спецификаций и НМЦК.xlsx."
            if spec_rows
            else "Позиции поставки не извлечены автоматически. Нужна ручная сверка ТЗ и приложений."
        ),
        "spec_table": {
            "columns": ["№", "Наименование", "Кол-во", "Ед. изм.", "Ключевые характеристики", "ГОСТ / норматив", "Эквивалент", "Цена за ед., руб.", "Сумма, руб.", "Источник"],
            "rows": spec_rows,
        },
        "largest_position": top_position.name if top_position else None,
    }


def _build_preliminary_procurement_analysis(
    *,
    metadata: dict[str, Any],
    documents: list[AnalyzedDocument],
    technical_spec_text: str,
    contract_draft_text: str,
    notice_text: str,
) -> dict[str, Any]:
    tz_text = technical_spec_text or ""
    contract_text = contract_draft_text or ""
    notice = notice_text or ""
    combined = "\n".join(part for part in (tz_text, contract_text, notice) if part)
    scope = _classify_procurement_scope(metadata, documents, notice)
    procurement_kind = scope["procurement_primary_scope"]
    extracted_service_items = [item for item in _collect_supply_items(documents) if item.item_type == "service"]
    if extracted_service_items and procurement_kind not in {"works", "mixed"}:
        procurement_kind = "services"
    elif _collect_supply_items(documents) and procurement_kind not in {"works", "services", "mixed"}:
        procurement_kind = "goods"
    if scope["goods_extraction_applicable"] and procurement_kind in {"goods", "mixed"}:
        preliminary = _build_goods_preliminary_analysis(
            metadata=metadata,
            documents=documents,
            technical_spec_text=technical_spec_text,
            contract_draft_text=contract_draft_text,
            notice_text=notice_text,
        )
        preliminary["procurement_kind"] = procurement_kind
        preliminary["scope"] = scope
        return preliminary
    # Keep the established education-services profile for its documented
    # training inputs; all other services must not inherit those assumptions.
    training_markers = ("обучени", "слушател", "учебн", "повышени[яе] квалификац")
    is_training_service = any(re.search(marker, combined, re.IGNORECASE) for marker in training_markers)
    if procurement_kind == "services" and not is_training_service:
        return _build_services_preliminary_analysis(
            metadata=metadata,
            documents=documents,
            notice_text=notice,
            contract_draft_text=contract_text,
        )
    if procurement_kind in {"mixed", "software_modification", "integration", "license", "works"}:
        work_rows = _build_software_work_rows(documents)
        initial_price = _extract_notice_price(metadata, notice, contract_text)
        deadline = metadata.get("deadline") or _extract_notice_service_deadline(notice) or _extract_notice_delivery_deadline(notice)
        delivery_term = metadata.get("procurement", {}).get("delivery_term") if isinstance(metadata.get("procurement"), dict) else None
        tender_title = metadata.get("tender_title") or _cleanup_tabular_value(
            _match_first(combined, (r"Наименование работ:\s*(.+?)(?:\n|$)",))
        ) or "не указан"
        overview = [
            f"Предмет закупки: {tender_title}",
            f"НМЦК: {initial_price} руб." if initial_price else "",
            f"Тип закупки: {procurement_kind}.",
            f"Срок исполнения / подачи: {deadline}." if deadline else "",
            f"Результат для заказчика: модифицированный модуль, интеграции и лицензионный пакет." if work_rows else "",
        ]
        compliance = [
            "Нужно проверить полноту функциональных требований по каждому блоку доработки.",
            "Требования к интеграциям, доступам и форматам обмена должны быть подтверждены документами и перепиской с заказчиком.",
            "Нужно отдельно проверить требования к передаче лицензии, прав и итоговой документации.",
        ]
        contract_terms = []
        if delivery_term:
            contract_terms.append(f"Срок исполнения по документам: {delivery_term}.")
        if "акт" in contract_text.lower() or "приемк" in contract_text.lower():
            contract_terms.append("В проекте контракта есть условия приемки и закрывающих документов.")
        if "лиценз" in (tz_text + "\n" + contract_text).lower():
            contract_terms.append("В составе результата работ фигурирует передача лицензии или прав использования.")
        return {
            "overview": [item for item in overview if item][:6],
            "compliance_highlights": compliance[:6],
            "delivery_model": [
                "Работы зависят от внешних систем, доступов и интеграционного контура заказчика.",
                "Часть требований относится к программной доработке, а не к поставке товара.",
            ],
            "contract_highlights": contract_terms[:6],
            "next_actions": [
                "Разбить объем работ по функциональным блокам и запросить оценку трудозатрат по каждому блоку.",
                "Уточнить порядок предоставления доступов к СМЭВ, ЕРН и витрине Минобороны.",
                "Проверить критерии приемки, тестирования и пакет лицензионных документов.",
            ],
            "extracted_fields": _dedupe_text_items(
                [
                    "НМЦК" if initial_price else "",
                    "функциональные блоки" if work_rows else "",
                    "интеграции" if any("Интеграция" in row.get("Блок работ / результат", "") for row in work_rows) else "",
                    "лицензия" if "лиценз" in (tz_text + contract_text).lower() else "",
                ]
            ),
            "procurement_kind": procurement_kind,
            "scope": scope,
            "supply_section_note": (
                "Состав работ собран по техническим документам и проекту контракта."
                if work_rows
                else "Полный смысловой разбор состава работ не выполнен автоматически. Нужна ручная проверка ТЗ."
            ),
            "spec_table": {
                "columns": ["№", "Блок работ / результат", "Что нужно сделать", "Входные/внешние системы", "Результат для заказчика", "Критерии приёмки", "Источник"],
                "rows": work_rows,
            },
        }

    service_subject = _match_first(
        tz_text,
        (
            r"1\.\s*Наименование и описание услуг:\s*(.+?)(?:\n\d+\.|\Z)",
            r"Объект закупки\s*[:\-]?\s*(.+?)(?:\n|$)",
            r"Описание объекта закупки\s*[:\-]?\s*(.+?)(?:\n|$)",
        ),
    ) or metadata.get("tender_title")
    training_format = _match_first(
        tz_text,
        (
            r"\b(Очно-заочная(?:\s*\([^)]+\))?)\b",
            r"\b(Очная(?:\s*\([^)]+\))?)\b",
            r"\b(Заочная(?:\s*\([^)]+\))?)\b",
            r"Форма обучения\s*\n\s*([^\n]+)",
            r"Форма обучения\s*[:\-]?\s*([^\n]+)",
        ),
    )
    hours = _match_first(
        tz_text,
        (
            r"(\d+\s*час(?:ов|а)?)",
        ),
    )
    listeners = _match_first(
        tz_text,
        (
            r"\b\d+\s*час(?:ов|а)?\s*\n\s*(\d+)\b",
            r"Кол-во слушателей.*?\n.*?\n.*?\n.*?\n.*?\n\s*(\d+)",
            r"(\d+)\s*\(?[а-я]*\)?\s*человек",
            r"слушател[^\n]*?(\d+)",
        ),
    )
    service_deadline = _match_first(
        tz_text,
        (
            r"не позднее\s+(\d{1,2}\s+[А-Яа-яЁё]+\s+\d{4}\s+года)",
            r"не позднее\s+([^.\\n]+)",
            r"Сроки оказания Услуг\s*[–-]\s*([^.\\n]+)",
        ),
    ) or _extract_notice_service_deadline(notice)
    service_deadline = _cleanup_tabular_value(service_deadline) or service_deadline
    location = _match_first(
        tz_text,
        (
            r"3\.\s*Место оказания услуг:\s*(.+?)(?:\n\d+\.|\Z)",
            r"Место оказания Услуг:\s*(.+?)(?:\n\d+\.|\Z)",
        ),
    )
    initial_price = _extract_notice_price(metadata, notice, contract_text)
    payment_terms = _match_first(
        contract_text,
        (
            r"в течение\s+(\d+\s*\([^)]+\)\s*рабочих дней[^.]+документа о приемке)",
            r"в течение\s+(\d+\s*рабочих дней[^.]+документа о приемке)",
            r"Оплата[^.]*?в течение\s+([^.]+)",
        ),
    )
    execution_security = _match_first(
        contract_text + "\n" + notice,
        (
            r"обеспечени[ея]\s+исполнения\s+контракта[^.]{0,120}",
        ),
    )
    if not execution_security and "исполнения контракта" in contract_text.lower() and "обеспеч" in contract_text.lower():
        execution_security = "обеспечение исполнения контракта"
    execution_security_percent = _match_first_dotall(
        notice + "\n" + contract_text,
        (
            r"contractGuarantee[\s\S]{0,600}?<(?:\w+:)?part>(\d+(?:[.,]\d+)?)</(?:\w+:)?part>",
            r"обеспечени[ея]\s+исполнения\s+контракта[^%\n]{0,200}?(\d+(?:[.,]\d+)?)\s*%",
        ),
    )
    execution_security_amount = _match_first_dotall(
        notice + "\n" + contract_text,
        (
            r"contractGuarantee[\s\S]{0,600}?<(?:\w+:)?amount>(\d+(?:[.,]\d+)?)</(?:\w+:)?amount>",
            r"обеспечени[ея]\s+исполнения\s+контракта[^\\d]{0,200}?([\d\s]+(?:[.,]\d+)?)\s*руб",
        ),
    )
    acceptance_window = _match_first(
        contract_text,
        (
            r"Не позднее\s+(\d+\s*\([^)]+\)\s*рабочих дней[^.]+документа о приемке)",
            r"Не позднее\s+(\d+\s*рабочих дней[^.]+документа о приемке)",
        ),
    )
    unilateral_termination = _match_first(
        contract_text,
        (
            r"(Заказчик вправе принять решение об одностороннем отказе[^.]+)",
            r"(одностороннем отказе от исполнения Контракта[^.]+)",
        ),
    )

    compliance_highlights = _collect_matches(
        tz_text,
        (
            r"(согласован[^\n.]*Федеральной службой по техническому и экспортному контролю[^\n.]*)",
            r"(выдать удостоверени[^\n.]*повышении квалификации[^\n.]*)",
            r"(итогов[^\n.]*аттестаци[^\n.]*)",
            r"(учебный план должен содержать[^\n.]*)",
            r"(раздаточн[^\n.]*материал[^\n.]*)",
            r"(ГОСТ\s*\d+(?:-\d+)?[^\n.]*)",
        ),
        limit=6,
    )
    delivery_model = _collect_matches(
        tz_text,
        (
            r"(Очно-заочная[^\n.]*)",
            r"(с применением дистанционных образовательных технологий[^\n.]*)",
            r"(60\s*%\s*времени[^\n.]*)",
            r"(40\s*%\s*времени[^\n.]*)",
            r"(с 09\.00 до 18\.00[^\n.]*)",
            r"(в городе [А-ЯЁA-Z][^;.\n]*)",
        ),
        limit=6,
    )

    extracted_fields = [
        label
        for label, value in (
            ("НМЦК", initial_price),
            ("предмет закупки", service_subject),
            ("формат оказания услуг", training_format),
            ("объём программы", hours),
            ("количество слушателей", listeners),
            ("срок оказания услуг", service_deadline),
            ("место оказания услуг", location),
            ("условия оплаты", payment_terms),
            ("приёмка", acceptance_window),
        )
        if value
    ]

    overview: list[str] = []
    if service_subject:
        overview.append(f"Предмет закупки: {service_subject}")
    if initial_price:
        overview.append(f"НМЦК: {initial_price} руб.")
    if training_format:
        overview.append(f"Формат: {training_format}")
    if hours or listeners:
        overview.append(
            "Объём: "
            + ", ".join(
                part
                for part in (
                    hours,
                    f"{listeners} слушателей" if listeners and listeners.isdigit() else listeners,
                )
                if part
            )
        )
    if service_deadline:
        overview.append(f"Срок оказания услуг: {service_deadline}")
    if location:
        overview.append(f"Место оказания услуг: {location}")
    if payment_terms:
        short_payment_terms = _shorten_payment_terms(payment_terms)
        if short_payment_terms:
            overview.append(f"Оплата: {short_payment_terms.rstrip('.')}")

    overview = _dedupe_text_items([_normalize_analysis_sentence(item) or item for item in overview[:6]])
    compliance_highlights = [
        rewritten
        for rewritten in (_rewrite_compliance_highlight(item) for item in compliance_highlights[:6])
        if rewritten
    ]
    compliance_highlights = _dedupe_text_items(compliance_highlights)
    delivery_model = [
        rewritten
        for rewritten in (_rewrite_delivery_model_item(item, procurement_kind) for item in delivery_model[:6])
        if rewritten
    ]
    delivery_model = _dedupe_text_items(delivery_model)

    next_actions = [
        "Проверить, можем ли мы обеспечить очную часть в Хабаровске и дистанционную часть в требуемом формате."
        if location or training_format
        else "Подтвердить реальный формат оказания услуг и локацию исполнения.",
        "Подтвердить наличие согласованной с ФСТЭК программы и право выдачи удостоверения о повышении квалификации."
        if any("Федеральной службой по техническому и экспортному контролю" in item for item in compliance_highlights)
        else "Проверить обязательные допуски, программу и итоговые документы по обучению.",
        "До запроса ТКП уточнить ресурсы: график, преподаватели, аудитории, оборудование и учебные материалы.",
    ]
    next_actions = _dedupe_text_items([_normalize_analysis_sentence(item) or item for item in next_actions[:4]])

    contract_terms: list[str] = []
    if payment_terms:
        contract_terms.append(f"Условия оплаты: в течение {payment_terms}.")
    if execution_security:
        security_parts: list[str] = ["Обеспечение исполнения контракта: да"]
        if execution_security_percent:
            security_parts.append(f"{execution_security_percent.replace('.', ',')}% от НМЦК")
        if execution_security_amount:
            amount_text = _format_money_value(execution_security_amount)
            if amount_text:
                security_parts.append(f"{amount_text} руб.")
        contract_terms.append(", ".join(security_parts) + ".")
    if acceptance_window:
        short_acceptance = _shorten_acceptance_terms(acceptance_window)
        if short_acceptance:
            contract_terms.append(f"Срок приемки: {short_acceptance.rstrip('.')}.")
    if "цена контракта является твердой" in contract_text.lower():
        contract_terms.append("Цена контракта: твердая, без индексации на период исполнения.")
    if unilateral_termination:
        contract_terms.append("Односторонний отказ от исполнения контракта предусмотрен по основаниям, указанным в договоре.")
    contract_terms = _dedupe_text_items([_normalize_analysis_sentence(item) or item for item in contract_terms[:6]])
    service_spec_rows = _build_supply_rows(documents)

    return {
        "overview": overview[:6],
        "compliance_highlights": compliance_highlights[:6],
        "delivery_model": delivery_model[:6],
        "contract_highlights": contract_terms[:6],
        "next_actions": next_actions[:4],
        "extracted_fields": extracted_fields[:8],
        "procurement_kind": procurement_kind,
        "supply_section_note": (
            "Позиции объекта закупки собраны из технических документов и приложений."
            if service_spec_rows
            else "Структурированные позиции объекта закупки не выделены автоматически. Нужна ручная сверка ТЗ и приложений."
        ),
        "spec_table": {
            "columns": ["№", "Наименование", "Кол-во", "Ед. изм.", "Характеристики", "Источник"],
            "rows": service_spec_rows,
        },
    }


def _normalize_supplier_questions(questions: list[dict[str, Any]], procurement_kind: str) -> list[str]:
    if procurement_kind != "services":
        return [_translate_user_text(item["question"]) for item in questions[:8]]
    service_questions = {
        "spec_match": "Подтверждаете ли вы оказание услуг в полном объеме по техническому заданию?",
        "price": "Какова стоимость услуг с НДС и без НДС?",
        "delivery": "Какие организационные расходы входят в стоимость услуг?",
        "delivery_time": "Подтверждаете ли вы сроки оказания услуг по графику заказчика?",
        "availability": "Есть ли у вас преподаватели, аудитории и ресурсы на требуемые даты?",
        "certificates": "Есть ли документы и согласования, подтверждающие право на оказание этих услуг?",
        "warranty": "Какие итоговые документы и результаты обучения вы обеспечиваете по завершении программы?",
        "analog": "Есть ли отклонения от программы, формата или состава услуг, указанных в ТЗ?",
        "payment": "Какие условия оплаты вы готовы подтвердить со своей стороны?",
        "validity": "Какой срок действия вашего коммерческого предложения?",
        "installation": "Что входит в организацию очной части обучения на вашей площадке?",
        "logistics": "Входят ли в стоимость раздаточные материалы, дистанционная платформа и организационное сопровождение?",
    }
    normalized: list[str] = []
    seen: set[str] = set()
    for item in questions:
        category = str(item.get("category") or "").strip()
        question = service_questions.get(category) or _translate_user_text(item["question"])
        if question in seen:
            continue
        seen.add(question)
        normalized.append(question)
        if len(normalized) >= 8:
            break
    return normalized


def delivery_address_from_preliminary(preliminary_analysis: dict[str, Any]) -> str | None:
    for item in preliminary_analysis.get("overview", []):
        if str(item).startswith("Адрес поставки:"):
            return str(item).split(":", 1)[1].strip() or None
    return None


def _build_output_payloads(
    *,
    metadata: dict[str, Any],
    documents: list[AnalyzedDocument],
    analysis_mode: str,
    requirements: dict[str, Any],
    calibrated_risks: list[dict[str, Any]],
    supplier_questions: list[dict[str, Any]],
    tkp_comparison: dict[str, Any] | None,
    economics: dict[str, Any] | None,
    bid_decision: dict[str, Any] | None,
    core_complete: bool,
    quote_inputs_present: bool,
) -> dict[str, dict[str, Any]]:
    technical_spec_text = _collect_role_text(documents, "technical_spec")
    contract_draft_text = _collect_role_text(documents, "contract_draft")
    contract_documents = [doc for doc in documents if doc.role == "contract_draft"]
    if contract_documents:
        contract_draft_status = "present" if any(doc.text for doc in contract_documents) else "parse_failed"
    elif metadata.get("files"):
        contract_draft_status = "absent"
    else:
        contract_draft_status = "unknown"
    notice_text = _collect_role_text(documents, "notice") or _collect_role_text(documents, "supporting") or metadata["tender_title"]
    scope = _classify_procurement_scope(metadata, documents, notice_text)
    procurement_kind = scope["procurement_primary_scope"]
    if any(item.item_type == "service" for item in _collect_supply_items(documents)) and procurement_kind not in {"works", "mixed"}:
        procurement_kind = "services"
    elif _collect_supply_items(documents) and procurement_kind not in {"works", "services", "mixed"}:
        procurement_kind = "goods"
    grounded_requirement_rows = _build_document_grounded_requirements(documents, procurement_kind)
    requirement_rows = grounded_requirement_rows or _extract_requirement_rows(requirements, core_complete, procurement_kind)
    quote_files_present = quote_inputs_present
    output_warnings = list(metadata.get("warnings", []))
    preliminary_analysis = _build_preliminary_procurement_analysis(
        metadata=metadata,
        documents=documents,
        technical_spec_text=technical_spec_text,
        contract_draft_text=contract_draft_text,
        notice_text=notice_text,
    )
    # Candidate hints retain only legacy sequence/identity while the direct
    # fragments and resolver exclusively supply goods field values.
    from src.modules.procurement_source_graph.model import direct_fragments_to_canonical_model, legacy_rows_to_canonical_model
    from src.modules.procurement_source_graph.serialization import provenance_records, serialize_graph
    from src.modules.procurement_source_graph.structured_fragment_collector import StructuredFragmentCollector
    graph_input_rows = (
        preliminary_analysis.get("service_items") or preliminary_analysis.get("spec_table", {}).get("rows", [])
        if procurement_kind == "services"
        else preliminary_analysis.get("supply_items") or preliminary_analysis.get("service_items") or []
    )
    # Existing XML/DOCX/XLSX extractors populate SupplyItem source metadata.
    # Preserve individual rows: merging them here would reintroduce adapter
    # values before FieldSourceResolver gets to decide each field.
    direct_extracted_items = _collect_unmerged_source_items(documents)
    direct_fragments = StructuredFragmentCollector().collect_supply_items(metadata.get("procurement_id"), direct_extracted_items)
    canonical_graph_model = (
        legacy_rows_to_canonical_model(metadata.get("procurement_id"), procurement_kind, graph_input_rows)
        if procurement_kind == "services" and not any("код активации" in (fragment.name or "").lower() for fragment in direct_fragments)
        else direct_fragments_to_canonical_model(metadata.get("procurement_id"), procurement_kind, direct_fragments, graph_input_rows)
    )
    canonical_run_status = (
        "needs_review" if canonical_graph_model.unresolved_candidates
        else "completed_with_warnings" if canonical_graph_model.quality_issues
        else "completed"
    )
    preliminary_analysis["canonical_procurement_model"] = {
        "procurement_number": canonical_graph_model.procurement_number,
        "procurement_scope": canonical_graph_model.procurement_scope,
        "run_status": canonical_run_status,
        "canonical_items": [
            {"canonical_item_id": item.canonical_item_id, "official_name": item.official_name, "display_name": item.display_name or item.official_name,
             "quantity": item.quantity, "unit": item.unit, "okpd2": next((source.fragment.okpd2 for field, source in item.field_provenance.items() if field == "okpd2"), None),
             "ktru": next((source.fragment.ktru for field, source in item.field_provenance.items() if field == "ktru"), None),
             "characteristics": item.characteristics, "primary_source": item.primary_source.fragment.fragment_key if item.primary_source else None,
             "evidence_ids": [item.primary_source.fragment.fragment_key] if item.primary_source else [],
             "field_provenance": {field: source.fragment.fragment_key for field, source in item.field_provenance.items()},
             "source_document": item.primary_source.fragment.document_instance_id if item.primary_source else None,
             "source_row_number": item.primary_source.fragment.locator if item.primary_source else None,
             "name_source_type": "validated_primary" if item.primary_source else "unresolved", "quality_gate_status": item.status,
             "warnings": item.warnings, "conflicts": item.conflicts, "field_issues": item.field_issues}
            for item in canonical_graph_model.canonical_items
        ],
        "unresolved_candidates": [item.canonical_item_id for item in canonical_graph_model.unresolved_candidates],
        "production_model_hash": canonical_graph_model.production_model_hash,
        "source_fragments": [
            {"fragment_key": fragment.fragment_key, "document_instance_id": fragment.document_instance_id,
             "source_type": fragment.source_type, "locator": fragment.locator, "row_role": fragment.row_role,
             "name": fragment.name, "quantity": fragment.quantity, "unit": fragment.unit,
             "okpd2": fragment.okpd2, "ktru": fragment.ktru, "characteristics": list(fragment.characteristics),
             "provenance_kind": fragment.provenance_kind, "parent_fragment_key": fragment.parent_fragment_key,
             "characteristic_name": fragment.characteristic_name, "characteristic_value": fragment.characteristic_value,
             "characteristic_unit": fragment.characteristic_unit, "source_position": fragment.position_number}
            for fragment in direct_fragments
        ],
        "source_graph": serialize_graph(canonical_graph_model, direct_fragments, "procurement-source-graph-v2"),
        "provenance_records": [record.__dict__ for record in provenance_records(canonical_graph_model)],
    }
    if procurement_kind == "goods" and _is_goods_supply_table_present(technical_spec_text) and not preliminary_analysis.get("spec_table", {}).get("rows"):
        output_warnings.append("Позиции поставки не извлечены из ТЗ/спецификации. Анализ неполный.")

    tender_summary = {
        "run_id": metadata["run_id"],
        "prepared_at": _safe_datetime(),
        "title": metadata["tender_title"],
        "procedure_type": "Поиск закупки + intake" if metadata.get("mode") == "procurement_search_intake" else "Загруженный demo run",
        "customer": metadata["customer_name"],
        "category": metadata["tender_category"],
        "submission_deadline": _safe_datetime(),
        "analysis_status": metadata["status"],
        "procurement_code": metadata.get("procurement_id") or metadata["run_id"].upper(),
        "documents": [
            {
                "name": doc.display_name,
                "role": doc.role,
                "pages": 1,
            }
            for doc in documents
        ],
        "document_signals": [
            f"Режим создания run: {metadata.get('mode', 'uploaded_demo')}.",
            f"Загружено файлов: {len(metadata.get('files', []))}.",
            f"Файлов с извлечённым текстом: {len([doc for doc in documents if doc.text])}.",
            f"Режим анализа: {analysis_mode}.",
        ],
        "preliminary_analysis": preliminary_analysis,
    }

    requirements_payload = {
        "requirements": requirement_rows,
        "preliminary_analysis": preliminary_analysis,
        "analysis_context": {
            "procurement_number": metadata.get("procurement_id"),
            "procurement_subject": (preliminary_analysis.get("overview") or [metadata.get("tender_title")])[0].removeprefix("Предмет закупки: "),
            "customer_name": metadata.get("customer_name") or (metadata.get("procurement") or {}).get("customer_name"),
            "customer_inn": metadata.get("customer_inn") or (metadata.get("procurement") or {}).get("customer_inn"),
            "customer_kpp": metadata.get("customer_kpp") or (metadata.get("procurement") or {}).get("customer_kpp"),
            "delivery_place": metadata.get("delivery_place") or delivery_address_from_preliminary(preliminary_analysis),
            "delivery_address": metadata.get("delivery_address") or delivery_address_from_preliminary(preliminary_analysis),
            "delivery_region": metadata.get("delivery_region"),
            "delivery_status": metadata.get("delivery_status") or ("known" if delivery_address_from_preliminary(preliminary_analysis) else "unknown"),
            "delivery_evidence_ids": [metadata.get("_field_evidence", {}).get("delivery_place")] if metadata.get("_field_evidence", {}).get("delivery_place") else [],
            "contract_draft_status": contract_draft_status,
            "contract_draft_documents": [doc.display_name for doc in contract_documents],
            "contract_draft_evidence_ids": [f"contract:{doc.file_id}" for doc in contract_documents],
            "procurement_scope": scope,
            "goods_extraction_applicable": scope["goods_extraction_applicable"],
            "scope_classification_conflict": scope["scope_classification_conflict"],
            "procurement_category": procurement_kind,
            "domain": preliminary_analysis.get("domain_profile", "unknown"),
            "law": metadata.get("law"),
            "okpd2": _service_okpd2_from_sources(notice_text, documents) if procurement_kind == "services" else None,
            "okpd2_codes": metadata.get("okpd2_codes") or (metadata.get("procurement") or {}).get("okpd2_codes", []),
            "nmck": _extract_notice_price(metadata, notice_text, contract_draft_text),
            "currency": "RUB",
            "service_items": preliminary_analysis.get("service_items", []),
            "document_inventory": [doc.display_name for doc in documents],
            "document_coverage": "partial" if procurement_kind == "services" and not contract_draft_text else "available",
            "missing_documents": [] if contract_draft_status in {"present", "parse_failed"} else preliminary_analysis.get("missing_documents", []),
            "extraction_warnings": output_warnings,
            "known_contract_terms": preliminary_analysis.get("contract_highlights", []) if contract_draft_text else [],
            "unknown_contract_terms": (["payment", "acceptance", "penalties", "security", "liability"] if not contract_draft_text else []),
            "supplier_profile": None,
            "commercial_inputs": economics or {},
            "evidence_map": [item.get("evidence_ids", []) for item in preliminary_analysis.get("service_items", [])],
        },
        "manual_review_points": [
            "Проверить корректность распределения документов по ролям.",
            "Подтвердить ключевые требования по исходным документам перед внешними действиями.",
        ],
    }

    # R10.1 is a claim-bound pipeline.  Its report may only include questions
    # and risks that survived provider grounding; deterministic catalogue
    # templates remain available solely for the legacy/demo adapter.
    claim_bound_mode = analysis_mode == "production_llm_r10_1"
    grounded_questions = _build_document_grounded_questions(procurement_kind, documents)

    supplier_questions_payload = {
        "ambiguities": (
            [
                "Полный LLM-анализ не выполнялся; вопросы собраны детерминированно из документов.",
                "Нужно проверить недостающие первичные документы и коммерческие входы вручную.",
            ]
            if analysis_mode == "fallback_deterministic_adapter"
            else [
                "Параметры оплаты и допустимость аналогов требуют ручной валидации.",
            ]
        ),
        "questions": (
            _normalize_supplier_questions(supplier_questions, procurement_kind)
            if claim_bound_mode
            else grounded_questions if grounded_questions else _normalize_supplier_questions(supplier_questions, procurement_kind)
        ),
        "manual_checks": [
            "Согласовать финальный вопросник с оператором.",
            "Не отправлять вопросы поставщикам автоматически из этого интерфейса.",
        ],
    }

    rfq_payload = (
        _build_goods_rfq_payload(metadata, documents)
        if procurement_kind == "goods"
        else {
            "rfq_title": f"RFQ draft / {metadata['tender_title']}",
            "sections": _build_document_grounded_rfq_sections(procurement_kind),
        }
    )
    rfq_payload["supplier_targets"] = [item.get("supplier_label", "Поставщик") for item in (tkp_comparison or {}).get("suppliers", [])] or [
        "Поставщик 1",
        "Поставщик 2",
        "Поставщик 3",
    ]
    rfq_payload["manual_checks"] = [
        "Проверить RFQ на соответствие исходной закупке.",
        "Отправка RFQ выполняется только человеком вне этого demo UI.",
    ]

    quotes_payload = {
        "status": (
            tkp_comparison.get("status", "blocked")
            if tkp_comparison
            else ("needs_review" if quote_inputs_present else "blocked")
        ),
        "analysis_mode": tkp_comparison.get("analysis_mode", analysis_mode) if tkp_comparison else analysis_mode,
        "supplier_quotes_found": tkp_comparison.get("supplier_quotes_found", 0) if tkp_comparison else 0,
        "items_extracted": tkp_comparison.get("items_extracted", 0) if tkp_comparison else 0,
        "suppliers": tkp_comparison.get("suppliers", []) if tkp_comparison else [],
        "items": tkp_comparison.get("items", []) if tkp_comparison else [],
        "comparison_summary": tkp_comparison.get("comparison_summary", {}) if tkp_comparison else {},
        "warnings": tkp_comparison.get("warnings", []) if tkp_comparison else [],
        "limitations": tkp_comparison.get("limitations", []) if tkp_comparison else [],
        "highlights": (
            [
                f"Найдено распознанных ТКП: {tkp_comparison.get('supplier_quotes_found', 0)}.",
                f"Извлечено сопоставимых позиций: {tkp_comparison.get('items_extracted', 0)}.",
                "Сравнение выполнено локально, в детерминированном демо-режиме без внешних действий.",
            ]
            if tkp_comparison
            else [
                "ТКП загружены, но не распознаны как структурированные таблицы для автоматического сравнения.",
                "Нужна ручная проверка цен, сроков и гарантий по исходным файлам или повторная загрузка ТКП в XLS/XLSX.",
            ]
            if quote_files_present
            else [
                "ТКП не загружены или не распознаны как структурированные таблицы.",
                "Агент подготовил RFQ и список вопросов для дальнейшей ручной работы.",
            ]
        ),
        "manual_checks": (
            [item.get("message", "") for item in tkp_comparison.get("manual_checks", [])]
            if tkp_comparison
            else []
        )
        or (
            ["Проверить реальные значения цены, срока и гарантий по загруженным ТКП."]
            if quote_files_present
            else ["Собрать ТКП вручную и повторно запустить анализ после загрузки коммерческих предложений."]
        ),
    }

    if procurement_kind == "goods":
        economics_payload = _build_goods_economics_payload(metadata, documents, analysis_mode, economics)
    elif procurement_kind == "services" and not economics:
        service_items = preliminary_analysis.get("service_items", [])
        economics_payload = {
            "analysis_mode": analysis_mode,
            "currency": "RUB",
            "economics_status": "insufficient_data",
            "supplier_cost_min": None,
            "supplier_cost_selected": None,
            "expected_revenue": None,
            "preliminary_bid_price": None,
            "gross_margin_amount": None,
            "gross_margin_percent": None,
            "logistics_reserve": None,
            "risk_reserve": None,
            "payment_delay_days": None,
            "cash_gap_estimate": None,
            "selected_supplier_name": None,
            "result": "Экономика не рассчитана: отсутствуют фактический объём и подтверждённые коммерческие входы.",
            "status": "blocked",
            "metrics": [
                {"label": "НМЦК", "value": _extract_notice_price(metadata, technical_spec_text, contract_draft_text, notice_text) or "не указана"},
                {"label": "Единичные расценки", "value": f"извлечено строк: {len(service_items)}"},
                {"label": "Фактический объём", "value": "не определён документацией"},
                {"label": "Что запросить", "value": "внутренняя стоимость нормо-часа и стоимость релевантных операций"},
                {"label": "Что запросить", "value": "подход к учёту материалов, запасных частей и логистики"},
                {"label": "Что запросить", "value": "проект контракта и условия оплаты/обеспечения"},
            ],
            "drivers": ["НМЦК и единичные расценки не являются прибылью или ожидаемой выручкой без объёма услуг."],
            "manual_checks": ["Собрать коммерческие входы и сопоставить расценки с себестоимостью до финансового решения."],
            "warnings": ["Экономические результаты, маржа и рентабельность не рассчитывались."],
            "limitations": ["Фактический объём, себестоимость, поставщик и проект контракта отсутствуют."],
            "assumptions": {},
        }
    else:
        economics_payload = {
            "analysis_mode": economics.get("analysis_mode", analysis_mode) if economics else analysis_mode,
            "currency": economics.get("currency", "RUB") if economics else "RUB",
            "economics_status": economics.get("economics_status", "insufficient_data") if economics else "insufficient_data",
            "supplier_cost_min": economics.get("supplier_cost_min") if economics else None,
            "supplier_cost_selected": economics.get("supplier_cost_selected") if economics else None,
            "expected_revenue": economics.get("expected_revenue") if economics else None,
            "preliminary_bid_price": economics.get("preliminary_bid_price") if economics else None,
            "gross_margin_amount": economics.get("gross_margin_amount") if economics else None,
            "gross_margin_percent": economics.get("gross_margin_percent") if economics else None,
            "logistics_reserve": economics.get("logistics_reserve") if economics else None,
            "risk_reserve": economics.get("risk_reserve") if economics else None,
            "payment_delay_days": economics.get("payment_delay_days") if economics else None,
            "cash_gap_estimate": economics.get("cash_gap_estimate") if economics else None,
            "selected_supplier_name": economics.get("selected_supplier_name") if economics else None,
            "result": (
                "Экономика требует запроса КП / оценки подрядчика"
                if not economics
                else (
                    "Экономика выглядит условно приемлемой"
                    if economics.get("economics_status") == "conditionally_viable"
                    else "Экономика требует ручной проверки"
                )
            ),
            "status": economics.get("status", "blocked") if economics else "blocked",
            "metrics": (
                [
                    {"label": "НМЦК", "value": _extract_notice_price(metadata, technical_spec_text, contract_draft_text, notice_text) or "не указана"},
                    {"label": "Закупочная себестоимость", "value": "не определена, требуется ТКП/оценка подрядчика"},
                    {"label": "Что запросить", "value": "оценка трудозатрат по предмету закупки"},
                    {"label": "Что запросить", "value": "коммерческие входы, подтверждающие себестоимость и риски"},
                ]
                if not economics
                else [
                    {"label": "Минимальная закупочная стоимость", "value": economics.get("supplier_cost_min", "unknown")},
                    {"label": "Выбранная закупочная стоимость", "value": economics.get("supplier_cost_selected", "unknown")},
                    {"label": "Резерв логистики", "value": economics.get("logistics_reserve", "unknown")},
                    {"label": "Резерв риска", "value": economics.get("risk_reserve", "unknown")},
                    {"label": "Целевая маржа", "value": f"{economics.get('gross_margin_percent')}%" if economics.get("gross_margin_percent") is not None else "unknown"},
                    {"label": "Предварительная цена подачи", "value": economics.get("preliminary_bid_price", "unknown")},
                    {"label": "Оценка кассового разрыва", "value": economics.get("cash_gap_estimate", "unknown")},
                ]
            ),
            "drivers": (
                [
                    f"Выбран поставщик: {economics.get('selected_supplier_name') or 'не определён'}.",
                    "Ожидаемая выручка не рассчитывается автоматически без цены заказчика.",
                    "Расчёт построен на локальных ТКП и операторских параметрах из демо-формы.",
                ]
                if economics
                else ["Без КП и оценки трудозатрат экономика ограничивается НМЦК и перечнем данных, которые нужно запросить."]
            ),
            "manual_checks": ([item.get("message", "") for item in economics.get("manual_checks", [])] if economics else [])
            or ["Запросить КП/оценку подрядчика по подтверждённому предмету закупки."],
            "warnings": economics.get("warnings", []) if economics else [],
            "limitations": economics.get("limitations", []) if economics else [],
            "assumptions": economics.get("assumptions", {}) if economics else {},
        }

    grounded_risks = _build_document_grounded_risks(procurement_kind, documents, contract_draft_text)
    risk_candidates = calibrated_risks if claim_bound_mode else (grounded_risks or calibrated_risks)

    def normalized_risk_evidence_locators(value: Any) -> list[dict[str, str]]:
        """Pass only safe, customer-readable report locators downstream."""
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                return []
            document, locator = item.get("document"), item.get("locator")
            if not isinstance(document, str) or not document.strip():
                return []
            if not isinstance(locator, str) or not locator.strip():
                return []
            if (
                "/" in document
                or "\\" in document
                or locator.strip().startswith(("/", "file:"))
                or "/Volumes/" in locator
                or "/Users/" in locator
                or re.fullmatch(r"[0-9a-f]{64}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", document.strip(), flags=re.IGNORECASE)
            ):
                return []
            normalized.append({"document": document.strip(), "locator": locator.strip()})
        return normalized
    risks_payload = {
        "summary": "Найдены ограничения и риски, требующие ручной проверки.",
        "risks": [
            {
                "risk": _translate_user_text(risk.get("clause", "Ограничение")),
                "severity": "needs_review" if risk.get("classification") == "deal_breaker_candidate" else "warning",
                "impact": _translate_user_text(risk.get("impact", "")),
                "mitigation": _translate_user_text(risk.get("mitigation", "")),
                "risk_id": risk.get("risk_id"),
                "category": risk.get("category", "unknown"),
                "evidence_ids": [value for value in str(risk.get("evidence_ids") or "").split(", ") if value],
                "evidence_locators": normalized_risk_evidence_locators(risk.get("evidence_locators")),
                "status": "blocker" if risk.get("classification") == "deal_breaker_candidate" else "requires_review",
            }
            for risk in risk_candidates
        ]
        or ([] if claim_bound_mode else [
            {
                "risk": "Недостаточно данных по договорным условиям",
                "severity": "needs_review",
                "impact": "Часть контрактных рисков не может быть оценена автоматически.",
                "mitigation": "Проверить договор и комплектность вручную.",
            }
        ]),
        "manual_checks": [
            "Проверить договорные ограничения и совместимость аналогов вручную."
        ],
    }

    economics_ready = bool(economics and economics.get("economics_status") in {"conditionally_viable", "viable"})
    service_contract_missing = procurement_kind == "services" and bool(preliminary_analysis.get("missing_documents"))
    if core_complete and quote_files_present and economics_ready and not service_contract_missing:
        recommendation = DemoRecommendationCode.PARTICIPATE_CONDITIONALLY
        label = "участвовать условно"
        rationale = [
            "Базовый контролируемый путь раннера выполнен на локально загруженных документах.",
            "ТКП структурированы и сопоставлены в локальном deterministic parser слое.",
            "Экономика выглядит условно приемлемой, но решение всё ещё требует проверки оператором.",
            "Рекомендация остаётся предварительной и не заменяет решение человека.",
        ]
    elif procurement_kind == "goods":
        largest_position = preliminary_analysis.get("largest_position")
        recommendation = DemoRecommendationCode.MANUAL_REVIEW_REQUIRED
        label = "нужна ручная проверка"
        rationale = [
            "Документы извлечены, и агент выделил конкретные позиции поставки, количества и характеристики.",
            "Для участия нужно получить КП и подтверждение ГОСТ, сертификатов и сроков поставки по каждой позиции.",
            f"Особое внимание требует самая объёмная позиция: {largest_position}." if largest_position else "Нужно проверить наличие товара и срок поставки по всем позициям.",
            "Финальное решение возможно только после проверки цены, логистики и документов качества.",
        ]
    else:
        recommendation = DemoRecommendationCode.MANUAL_REVIEW_REQUIRED
        label = "нужна ручная проверка"
        rationale = [
            "Документы извлечены и дают предметное понимание закупки, но ценовая модель и подтверждение ресурсов отсутствуют.",
            "Проект контракта, коммерческие входы и подтверждение ресурсов отсутствуют или требуют проверки.",
            "Следующее действие должен подтвердить оператор после получения проекта контракта и внутренней оценки исполнения.",
        ]

    final_recommendation = {
        "recommendation": recommendation.value,
        "label": label,
        "rationale": rationale,
        "key_requirements": [item["title"] for item in requirement_rows[:4]] or ["Проверка комплектности документов"],
        "open_questions": supplier_questions_payload["questions"][:3],
        "risks": [item["risk"] for item in risks_payload["risks"][:4]],
        "economics": [f"{item['label']}: {item['value']}" for item in economics_payload["metrics"]],
        "manual_checks": [
            "Проверить исходные документы и роли файлов.",
            "Подтвердить RFQ и вопросы перед внешними коммуникациями.",
            "Проверить нормализацию Excel-таблиц и сопоставление позиций перед финансовым решением.",
            "Сделать финальное решение только после ручной проверки.",
        ],
    }

    trace = {
        "documents_considered": [doc.display_name for doc in documents],
        "procurement_context": {
            "source": metadata.get("procurement_source"),
            "procurement_id": metadata.get("procurement_id"),
            "procurement_url": metadata.get("procurement_url"),
            "attachments_status": metadata.get("attachments_status"),
        },
        "fields_extracted": [
            *preliminary_analysis.get("extracted_fields", []),
        ],
        "risk_signals": [
            "При отсутствии LLM используется документ-зависимый детерминированный анализ без шаблонов из чужой предметной области.",
            "Внешние действия отключены по design policy.",
            "Нормализация Excel-таблиц использует deterministic parser + heuristics без LLM.",
            "Часть выводов требует ручного подтверждения по исходным файлам.",
        ],
        "decision_factors": rationale,
        "overall_explanation": (
            "Агент использовал локально загруженные файлы, безопасное извлечение текста и документ-зависимый детерминированный анализ. "
            "Если LLM недоступен или ТКП отсутствуют, интерфейс показывает предметные выводы по документам и честно отмечает ограничения вместо подстановки шаблонов из другой предметной области."
        ),
        "per_step": {
            "documents": "Файлы сохранены локально, имена нормализованы, опасные пути отброшены.",
            "requirements": "Требования и предварительный анализ собраны из ТЗ, извещения, проекта договора и документа по заявке с безопасным локальным извлечением текста.",
            "questions": "Сформирован список вопросов для ручной коммуникации с поставщиками.",
            "rfq": "Подготовлен draft RFQ для ручной отправки вне системы.",
            "quotes": "Сравнение ТКП использует детерминированный парсер таблиц и честно помечает частичные результаты и зоны ручной проверки.",
            "economics": "Экономика строится только на доступных локальных данных и операторских параметрах, без выдуманной выручки.",
            "risks": "Риски агрегированы из доступного контракта и ограничений demo-mode.",
            "decision": "Итог всегда требует подтверждения человеком и не приводит к внешним действиям.",
        },
        "human_control_note": "Демо- и пилотный режим. Нет подачи заявок, писем, ЭЦП или действий на площадках без человека.",
        "limitations": metadata.get("limitations", []) + output_warnings,
    }

    return {
        "tender_summary": tender_summary,
        "requirements": requirements_payload,
        "supplier_questions": supplier_questions_payload,
        "rfq_draft": rfq_payload,
        "quotes_comparison": quotes_payload,
        "economics": economics_payload,
        "contract_risks": risks_payload,
        "final_recommendation": final_recommendation,
        "trace": trace,
    }


def _document_type_label(item: dict[str, Any]) -> str:
    role = str(item.get("role_hint") or item.get("document_kind") or "").strip().lower()
    labels = {
        "notice": "электронное извещение ЕИС",
        "eis_notice": "электронное извещение ЕИС",
        "technical_spec": "техническое задание / техническая часть",
        "technical_specification": "техническое задание / техническая часть",
        "procurement_object_description": "описание объекта закупки",
        "contract_draft": "проект контракта",
        "specification": "спецификация",
        "estimate": "смета",
        "form": "форма",
        "attachment": "приложение",
        "supporting": "вспомогательный документ",
    }
    return labels.get(role, "документ закупки")


def _build_downloaded_documents_inventory(metadata: dict[str, Any]) -> list[dict[str, str]]:
    inventory: list[dict[str, str]] = []
    for item in metadata.get("files", []):
        inventory.append(
            {
                "name": str(item.get("display_name") or item.get("original_name") or "Документ"),
                "type": _document_type_label(item),
                "download_status": "downloaded",
                "text_status": "extracted" if item.get("extracted_text_available") else str(item.get("text_extraction_status") or "pending"),
                "source": str(item.get("source_type") or item.get("source") or "runtime"),
            }
        )
    return inventory


def _build_steps_from_outputs(metadata: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> list[DemoStep]:
    requirements = outputs["requirements"]
    questions = outputs["supplier_questions"]
    rfq = outputs["rfq_draft"]
    quotes = outputs["quotes_comparison"]
    economics = outputs["economics"]
    risks = outputs["contract_risks"]
    final_recommendation = outputs["final_recommendation"]
    trace = outputs["trace"]["per_step"]

    quote_blocked = quotes["status"] == "blocked"
    quote_partial = quotes["status"] in {"partial", "needs_review"}
    economics_blocked = economics["status"] == "blocked"
    economics_partial = economics["status"] in {"partial", "needs_review"}
    core_limitations = outputs["trace"].get("limitations", [])
    partial_requirements = any("fallback" in item.lower() for item in core_limitations)
    file_count = len(metadata.get("files", []))
    docs_status = DemoStepStatus.DONE if file_count else DemoStepStatus.BLOCKED
    preliminary_analysis = requirements.get("preliminary_analysis", {})

    steps: list[DemoStep] = []
    if metadata.get("procurement_source"):
        procurement_title = metadata.get("tender_title", "Закупка")
        procurement_findings = [
            f"Источник: {metadata.get('procurement_source')}.",
            f"Идентификатор закупки: {metadata.get('procurement_id') or 'не указан'}.",
            f"Статус документации: {metadata.get('attachments_status') or 'не определён'}.",
        ]
        if metadata.get("procurement_url"):
            procurement_findings.append(f"Карточка закупки: {metadata.get('procurement_url')}.")
        steps.append(
            DemoStep(
                key="procurement_search",
                order=0,
                title="Поиск закупки",
                short_title="Поиск закупки",
                status=DemoStepStatus.DONE,
                description="Read-only поиск закупки и выбор карточки оператором.",
                agent_action=f"Найдена и выбрана закупка '{procurement_title}' из безопасного procurement discovery слоя.",
                result_summary=f"Выбрана закупка {metadata.get('procurement_id') or metadata.get('run_id')}.",
                findings=procurement_findings,
                human_review=[
                    "Проверить релевантность найденной закупки перед продолжением.",
                    "Подтвердить, что источник не требует авторизации, если будет подключаться реальный коннектор.",
                ],
                trace="Поиск выполнялся в безопасном режиме только чтения без авторизации, обхода captcha и внешних действий.",
                result_sections=[
                    DemoDetailSection(
                        title="Контекст поиска",
                        kind="bullets",
                        items=[
                            f"Запрос: {metadata.get('procurement_query') or 'не указан'}",
                            f"Источник: {metadata.get('procurement_source')}",
                            f"Статус документации: {metadata.get('attachments_status') or 'не определён'}",
                        ],
                    )
                ],
            )
        )

    steps.extend([
        DemoStep(
            key="documents",
            order=1,
            title="Документы",
            short_title="Документы",
            status=docs_status,
            description="Локальная загрузка и безопасная подготовка файлов к анализу.",
            agent_action="Файлы сохранены в локальную demo-run директорию, имена нормализованы, опасные пути удалены.",
            result_summary=(
                f"Загружено {file_count} файлов."
                if file_count
                else "Документы ещё не загружены. Для продолжения нужен ручной upload."
            ),
            findings=[item["display_name"] for item in metadata.get("files", [])]
            or ["Автоматически доступных документов нет, требуется ручная загрузка."],
            human_review=[
                "Проверить, что каждому файлу назначена корректная роль."
            ]
            if file_count
            else ["Загрузить документацию вручную и только потом запускать анализ."],
            trace=trace["documents"],
            result_sections=[
                DemoDetailSection(
                    title="Загруженные файлы",
                    kind="table",
                    columns=["Файл", "Расширение", "Размер"],
                    rows=[
                        {
                            "Файл": item["display_name"],
                            "Расширение": item["extension"],
                            "Размер": f"{item['size_bytes']} bytes",
                        }
                        for item in metadata.get("files", [])
                    ],
                )
            ],
        ),
        DemoStep(
            key="requirements",
            order=2,
            title="Требования",
            short_title="Требования",
            status=DemoStepStatus.PARTIAL if partial_requirements else DemoStepStatus.DONE,
            description="Извлечение ключевых требований и обязательных документов из доступного локального пакета.",
            agent_action="Собран снимок требований с помощью контролируемого парсера и fallback-адаптера.",
            result_summary=(
                preliminary_analysis.get("overview", [f"Выделено требований: {len(requirements['requirements'])}."])[0]
                if preliminary_analysis.get("overview")
                else f"Выделено требований: {len(requirements['requirements'])}."
            ),
            findings=(preliminary_analysis.get("overview", []) + [item["title"] for item in requirements["requirements"]])[:10],
            human_review=requirements["manual_review_points"],
            trace=trace["requirements"],
            result_sections=[
                DemoDetailSection(
                    title="Предварительный анализ закупки",
                    kind="bullets",
                    items=(
                        preliminary_analysis.get("overview", [])
                        + preliminary_analysis.get("compliance_highlights", [])[:3]
                        + preliminary_analysis.get("contract_highlights", [])[:2]
                    )[:10],
                ),
                DemoDetailSection(
                    title="Требования",
                    kind="table",
                    columns=["Требование", "Тип", "Приоритет", "Деталь", "Источник"],
                    rows=[
                        {
                            "Требование": item["title"],
                            "Тип": item.get("type", "общее"),
                            "Приоритет": item.get("priority", "medium"),
                            "Деталь": item["detail"],
                            "Источник": item["source"],
                        }
                        for item in requirements["requirements"]
                    ],
                )
            ],
        ),
        DemoStep(
            key="supplier_search",
            order=3,
            title="Поиск поставщиков",
            short_title="Поставщики",
            status=DemoStepStatus.DONE if metadata.get("supplier_search", {}).get("suppliers") else DemoStepStatus.PARTIAL,
            description="Интернет-поиск потенциальных поставщиков через Yandex Search API.",
            agent_action="Выполнен поиск поставщиков на основе требований закупки.",
            result_summary=f"Найдено поставщиков: {metadata.get('supplier_search', {}).get('total_found', 0)}." if metadata.get("supplier_search", {}).get("suppliers") else "Поиск поставщиков не выполнялся или не настроен.",
            findings=[f"{s['name']} — {s['site']}" for s in metadata.get("supplier_search", {}).get("suppliers", [])[:5]],
            human_review=["Проверить найденных поставщиков вручную перед отправкой RFQ."],
            trace=trace.get("supplier_search", "Поиск поставщиков выполнен через Yandex Search API без внешних изменений."),
            result_sections=[
                DemoDetailSection(
                    title="Найденные поставщики",
                    kind="table",
                    columns=["Поставщик", "Сайт", "Сигналы"],
                    rows=[
                        {"Поставщик": s["name"], "Сайт": s["site"], "Сигналы": ", ".join(s.get("signals", []) or ["—"])}
                        for s in metadata.get("supplier_search", {}).get("suppliers", [])[:10]
                    ],
                )
                if metadata.get("supplier_search", {}).get("suppliers")
                else DemoDetailSection(title="Статус поиска", kind="bullets", items=[
                    metadata.get("supplier_search", {}).get("query", "Поиск не выполнялся"),
                    f"Поставщиков не найдено или API не настроено.",
                ]),
            ],
        ),
        DemoStep(
            key="questions",
            order=4,
            title="Вопросы",
            short_title="Вопросы",
            status=DemoStepStatus.NEEDS_REVIEW,
            description="Формирование вопросника по неоднозначностям и отсутствующим данным.",
            agent_action="Подготовлен набор вопросов для RFQ под контролем оператора.",
            result_summary=f"Подготовлено вопросов: {len(questions['questions'])}.",
            findings=questions["ambiguities"],
            human_review=questions["manual_checks"],
            trace=trace["questions"],
            result_sections=[
                DemoDetailSection(title="Вопросы поставщикам", kind="bullets", items=questions["questions"])
            ],
        ),
        DemoStep(
            key="rfq",
            order=5,
            title="RFQ",
            short_title="RFQ",
            status=DemoStepStatus.DONE if requirements["requirements"] else DemoStepStatus.PARTIAL,
            description="Подготовка draft RFQ для ручной отправки.",
            agent_action="Сформирован черновик RFQ на основе извлечённых требований и вопросов поставщикам.",
            result_summary="RFQ готов как внутренний черновик.",
            findings=rfq["sections"],
            human_review=rfq["manual_checks"],
            trace=trace["rfq"],
            result_sections=[
                DemoDetailSection(title="Секции RFQ", kind="bullets", items=rfq["sections"]),
                DemoDetailSection(
                    title="Позиции RFQ",
                    kind="table",
                    columns=["№", "Позиция", "Кол-во", "Ед.", "Обязательные характеристики", "ГОСТ / норматив", "Цена за ед.", "Сумма"],
                    rows=rfq.get("items", [])[:20],
                ),
            ] if rfq.get("items") else [DemoDetailSection(title="Секции RFQ", kind="bullets", items=rfq["sections"])],
        ),
        DemoStep(
            key="quotes",
            order=6,
            title="ТКП",
            short_title="ТКП",
            status=DemoStepStatus.BLOCKED if quote_blocked else (DemoStepStatus.PARTIAL if quote_partial else DemoStepStatus.DONE),
            description="Сопоставление коммерческих предложений, если они были загружены.",
            agent_action="Проверено наличие ТКП и собран локальный снимок сравнения с нормализацией таблиц.",
            result_summary=(
                "ТКП не загружены."
                if quote_blocked
                else (
                    "ТКП загружены, но требуют ручной нормализации."
                    if quotes.get("supplier_quotes_found", 0) == 0
                    else f"Найдено ТКП: {quotes.get('supplier_quotes_found', 0)}, позиций: {quotes.get('items_extracted', 0)}."
                )
            ),
            findings=quotes["highlights"],
            human_review=quotes["manual_checks"],
            trace=trace["quotes"],
            result_sections=[
                DemoDetailSection(
                    title="Извлечённые ТКП",
                    kind="table",
                    columns=["Поставщик", "Файл", "Сумма", "Валюта", "Позиций", "Уверенность"],
                    rows=[
                        {
                            "Поставщик": item.get("supplier_name", "Поставщик"),
                            "Файл": item.get("source_file", "unknown"),
                            "Сумма": item.get("total_amount", "unknown"),
                            "Валюта": item.get("currency", "unknown"),
                            "Позиций": item.get("items_count", "unknown"),
                            "Уверенность": item.get("price_confidence", "unknown"),
                        }
                        for item in quotes["suppliers"]
                    ],
                )
                if quotes["suppliers"]
                else DemoDetailSection(title="Статус ТКП", kind="bullets", items=quotes["highlights"]),
                DemoDetailSection(
                    title="Сравнение предложений",
                    kind="table",
                    columns=["Позиция", "Лучшая цена", "Разброс %", "Нужна проверка"],
                    rows=[
                        {
                            "Позиция": item.get("normalized_name", "unknown"),
                            "Лучшая цена": item.get("best_price_supplier", "unknown"),
                            "Разброс %": item.get("price_spread_percent", "unknown"),
                            "Нужна проверка": "да" if item.get("needs_review") else "нет",
                        }
                        for item in quotes.get("items", [])[:20]
                    ],
                )
                if quotes["suppliers"]
                else DemoDetailSection(title="Статус ТКП", kind="bullets", items=quotes["highlights"])
            ],
        ),
        DemoStep(
            key="economics",
            order=7,
            title="Экономика",
            short_title="Экономика",
            status=DemoStepStatus.BLOCKED if economics_blocked else (DemoStepStatus.PARTIAL if economics_partial else DemoStepStatus.NEEDS_REVIEW),
            description="Расчёт экономики только по доступным локальным данным.",
            agent_action="Собран снимок экономики без притворства полной автоматизации при нехватке данных.",
            result_summary=economics["result"],
            findings=economics["drivers"],
            human_review=economics["manual_checks"],
            trace=trace["economics"],
            result_sections=[
                DemoDetailSection(
                    title="Снимок экономики",
                    kind="table",
                    columns=["Показатель", "Значение"],
                    rows=[
                        {"Показатель": item["label"], "Значение": item["value"]}
                        for item in economics["metrics"]
                    ],
                )
            ],
        ),
        DemoStep(
            key="risks",
            order=9,
            title="Риски",
            short_title="Риски",
            status=DemoStepStatus.WARNING,
            description="Сводка рисков и ограничений demo-mode.",
            agent_action="Риски агрегированы в единый блок для удобной ручной проверки.",
            result_summary=risks["summary"],
            findings=[item["risk"] for item in risks["risks"]],
            human_review=risks["manual_checks"],
            trace=trace["risks"],
            result_sections=[
                DemoDetailSection(
                    title="Риски",
                    kind="table",
                    columns=["Риск", "Серьёзность", "Влияние", "Смягчение"],
                    rows=[
                        {
                            "Риск": item["risk"],
                            "Серьёзность": item["severity"],
                            "Влияние": item["impact"],
                            "Смягчение": item["mitigation"],
                        }
                        for item in risks["risks"]
                    ],
                )
            ],
        ),
        DemoStep(
            key="decision",
            order=9,
            title="Решение",
            short_title="Решение",
            status=DemoStepStatus.NEEDS_REVIEW,
            description="Предварительная рекомендация без внешних действий и без снятия human control.",
            agent_action="Собран итоговый блок рекомендации с открытыми вопросами и ручными проверками.",
            result_summary=f"Рекомендация: {final_recommendation['label']}.",
            findings=final_recommendation["rationale"],
            human_review=final_recommendation["manual_checks"],
            trace=trace["decision"],
            result_sections=[
                DemoDetailSection(title="Открытые вопросы", kind="bullets", items=final_recommendation["open_questions"])
            ],
        ),
    ])
    return steps


def _build_final_recommendation(outputs: dict[str, dict[str, Any]]) -> DemoFinalRecommendation:
    final_recommendation = outputs["final_recommendation"]
    return DemoFinalRecommendation(
        recommendation=DemoRecommendationCode(final_recommendation["recommendation"]),
        label=final_recommendation["label"],
        rationale=final_recommendation["rationale"],
        key_requirements=final_recommendation["key_requirements"],
        open_questions=final_recommendation["open_questions"],
        risks=final_recommendation["risks"],
        economics=final_recommendation["economics"],
        manual_checks=final_recommendation["manual_checks"],
        trace=outputs["trace"]["overall_explanation"],
    )


def _preliminary_analysis_supply_section_title(preliminary_analysis: dict[str, Any]) -> str:
    columns = preliminary_analysis.get("spec_table", {}).get("columns", []) or []
    if "Блок работ / результат" in columns:
        return "Состав работ / поставки / услуг"
    return "Состав поставки"


def _preliminary_analysis_supply_section_markdown(preliminary_analysis: dict[str, Any]) -> str:
    rows = preliminary_analysis.get("spec_table", {}).get("rows", []) or []
    if not rows:
        return "Структурированный состав поставки не выделен автоматически. Требуется ручная проверка ТЗ и приложений."

    note = preliminary_analysis.get("supply_section_note", "").strip()
    columns = preliminary_analysis.get("spec_table", {}).get("columns", []) or []
    lines: list[str] = [note] if note else []
    if "Блок работ / результат" in columns:
        lines.extend(
            [
                (
                    f"- {row.get('№', '—')}. {row.get('Блок работ / результат', 'Блок работ')} | "
                    f"что сделать: {row.get('Что нужно сделать', 'не указано')} | "
                    f"системы: {row.get('Входные/внешние системы', 'не указано')} | "
                    f"результат: {row.get('Результат для заказчика', 'не указано')} | "
                    f"приемка: {row.get('Критерии приёмки', 'не указано')} | "
                    f"источник: {row.get('Источник', 'не указан')}"
                )
                for row in rows
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            (
                f"- {row.get('№', '—')}. {row.get('Наименование', 'Позиция')} | "
                f"кол-во: {row.get('Кол-во', 'не указано')} | "
                f"ед.: {row.get('Ед. изм.', '—')} | "
                f"характеристики: {row.get('Ключевые характеристики', row.get('Характеристики', '—'))} | "
                f"ГОСТ: {row.get('ГОСТ / норматив', '—')} | "
                f"эквивалент: {row.get('Эквивалент', '—')} | "
                f"цена: {row.get('Цена за ед., руб.', '—')} | "
                f"источник: {row.get('Источник', 'не указан')}"
            )
            for row in rows
        ]
    )
    return "\n".join(lines)


def _build_report_markdown(metadata: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> str:
    final_recommendation = outputs["final_recommendation"]
    quotes = outputs["quotes_comparison"]
    economics = outputs["economics"]
    preliminary_analysis = outputs["requirements"].get("preliminary_analysis", {})
    downloaded_docs = _build_downloaded_documents_inventory(metadata)
    procurement_block = ""
    if metadata.get("procurement_source"):
        procurement = metadata.get("procurement", {})
        documentation = procurement.get("attachment_names") or [item.get("display_name", "") for item in metadata.get("files", [])]
        documentation_block = "\n".join(f"- {item}" for item in documentation) or "- Документация не получена."
        downloaded_documents_block = (
            "\n".join(
                f"- {item['type']}: {item['name']} | download={item['download_status']} | text={item['text_status']} | source={item['source']}"
                for item in downloaded_docs
            )
            or "- Документы не скачаны."
        )
        blocked_note = (
            "\nДокументация не получена. Анализ невозможен до ручной загрузки файлов.\n"
            if metadata.get("attachments_status") == "manual_upload_required" or not metadata.get("files")
            else ""
        )
        procurement_block = (
            "## Источник закупки\n"
            f"- Источник: {metadata.get('procurement_source')}\n"
            f"- Номер извещения: {metadata.get('notice_number') or metadata.get('procurement_id')}\n"
            f"- Заказчик: {metadata.get('customer_name')}\n"
            f"- Закон: {metadata.get('law') or procurement.get('category') or 'не указан'}\n"
            f"- НМЦК: {procurement.get('initial_price') or 'не указана'} {procurement.get('currency') or '₽'}\n"
            f"- Дата публикации: {metadata.get('publication_date') or procurement.get('publication_date') or 'не указана'}\n"
            f"- Срок подачи: {metadata.get('deadline') or 'не указан'}\n"
            f"- Источник сведений: {procurement.get('structured_source_label') or metadata.get('notice_source_label') or 'карточка ЕИС'}\n"
            f"- Ссылка на источник: {metadata.get('procurement_url')}\n"
            f"- Статус скачивания: {metadata.get('attachments_status')}\n"
            f"- Ручная загрузка требовалась: {'да' if metadata.get('manual_upload_required') else 'нет'}\n"
            f"- Скачано/добавлено файлов: {metadata.get('downloaded_files_count', len(metadata.get('files', [])))}\n\n"
            "### Документация\n"
            f"{documentation_block}\n"
            "### Загруженные документы\n"
            f"{downloaded_documents_block}\n"
            f"{blocked_note}\n"
        )
    return (
        "# Отчёт по загруженному прогону тендерного агента\n\n"
        f"- Run ID: {metadata['run_id']}\n"
        f"- Закупка: {metadata['tender_title']}\n"
        f"- Категория: {metadata['tender_category']}\n"
        f"- Заказчик: {metadata['customer_name']}\n"
        f"- Статус: {metadata['status']}\n"
        f"- Режим анализа: {metadata['analysis_mode']}\n"
        f"- Код рекомендации: {final_recommendation['recommendation']}\n\n"
        + procurement_block
        + "## Краткий вывод\n"
        + "\n".join(f"- {item}" for item in final_recommendation["rationale"])
        + "\n\n## Предварительный анализ закупки\n"
        + (
            "\n".join(f"- {item}" for item in preliminary_analysis.get("overview", []))
            if preliminary_analysis.get("overview")
            else "- Пока не удалось извлечь структурированные выводы из ТЗ."
        )
        + "\n\n### Ключевые требования и ограничения\n"
        + (
            "\n".join(f"- {item}" for item in preliminary_analysis.get("compliance_highlights", []))
            if preliminary_analysis.get("compliance_highlights")
            else "- Требуется ручная валидация ключевых требований по исходным документам."
        )
        + "\n\n### Ключевые условия договора\n"
        + (
            "\n".join(f"- {item}" for item in preliminary_analysis.get("contract_highlights", []))
            if preliminary_analysis.get("contract_highlights")
            else "- Ключевые условия договора нужно проверить вручную."
        )
        + f"\n\n## {_preliminary_analysis_supply_section_title(preliminary_analysis)}\n"
        + _preliminary_analysis_supply_section_markdown(preliminary_analysis)
        + "\n\n## Извлечённые ТКП\n"
        + (
            "\n".join(
                f"- {item.get('supplier_name', 'Поставщик')}: сумма={item.get('total_amount', 'unknown')} {item.get('currency', '')}, позиций={item.get('items_count', 'unknown')}"
                for item in quotes.get("suppliers", [])
            )
            if quotes.get("suppliers")
            else "- ТКП не загружены или не распознаны."
        )
        + "\n\n## Экономика\n"
        + "\n".join(f"- {item['label']}: {item['value']}" for item in economics["metrics"])
        + "\n\n## Ключевые требования\n"
        + (
            "\n".join(
                f"- {item['title']} | тип: {item.get('type', 'общее')} | приоритет: {item.get('priority', 'medium')} | источник: {item['source']}"
                for item in outputs["requirements"].get("requirements", [])
            )
            if outputs["requirements"].get("requirements")
            else "- Требования не выделены автоматически."
        )
        + "\n\n## Ручные проверки\n"
        + "\n".join(f"- {item}" for item in final_recommendation["manual_checks"])
        + "\n"
    )


def _render_report_html(metadata: dict[str, Any], outputs: dict[str, dict[str, Any]]) -> str:
    def list_html(items: list[str]) -> str:
        return "".join(f"<li>{html.escape(item)}</li>" for item in items)

    def is_missing(value: Any) -> bool:
        if value is None:
            return True
        text = str(value).strip()
        return not text or text.lower() in {"не указан", "none", "null", "n/a", "—"}

    def format_value(value: Any, *, fallback: str = "не указано") -> str:
        return fallback if is_missing(value) else str(value).strip()

    def format_date_ru(value: Any) -> str:
        text = str(value).strip() if value else ""
        import re
        iso_match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
        if iso_match:
            return f"{iso_match.group(3)}.{iso_match.group(2)}.{iso_match.group(1)}"
        rus_match = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", text)
        if rus_match:
            return text
        return text

    def format_price(amount: Any, currency: Any) -> str:
        if amount in (None, ""):
            return "не указана"
        if isinstance(amount, float):
            amount_text = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
        else:
            amount_text = str(amount)
        currency_text = str(currency).strip() if currency else ""
        if not currency_text:
            currency_text = "₽"
        return f"{amount_text} {currency_text}".strip()

    def build_archive_button_html(run_id: str, archive_available: bool) -> str:
        if not archive_available:
            return ""
        return (
            f'<a class="action-button primary" href="/api/demo/tender-agent/runs/{html.escape(run_id)}/archive/download">Скачать архив</a>'
        )

    def build_export_buttons_html(run_id: str) -> str:
        return (
            f'<a class="action-button" href="/api/demo/tender-agent/runs/{html.escape(run_id)}/export/docx">Скачать DOCX</a>'
            f'<a class="action-button" href="/api/demo/tender-agent/runs/{html.escape(run_id)}/export/pdf">Скачать PDF</a>'
        )

    def build_document_list_html(run_id: str, files_payload: list[dict[str, Any]]) -> str:
        items: list[str] = []
        for item in files_payload:
            file_id = str(item.get("file_id") or "").strip()
            display_name = format_value(item.get("display_name"), fallback="Документ")
            if not file_id:
                continue
            items.append(
                f'<li><a class="doc-link" href="/api/demo/tender-agent/runs/{html.escape(run_id)}/files/{html.escape(file_id)}/download">{html.escape(display_name)}</a></li>'
            )
        if not items:
            return '<div class="muted">Документы для скачивания пока не доступны.</div>'
        return "".join(items)

    def build_document_toggle_html(run_id: str, files_payload: list[dict[str, Any]]) -> str:
        content = build_document_list_html(run_id, files_payload)
        if content.startswith("<div"):
            return content
        return (
            '<details class="document-toggle">'
            '<summary class="action-button">Показать документы</summary>'
            f'<ul class="document-list">{content}</ul>'
            '</details>'
        )

    def format_publication_update(publication_date: Any, updated_date: Any) -> str:
        publication = format_value(publication_date)
        updated = format_value(updated_date, fallback="")
        if updated and updated != publication:
            return f"{publication} / {updated}"
        return publication

    def render_table(columns: list[str], rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "<p>Нет данных для отображения.</p>"
        header_html = "".join(f"<th>{html.escape(column)}</th>" for column in columns)
        body_html = "".join(
            "<tr>"
            + "".join(f"<td>{html.escape(str(row.get(column, '')))}</td>" for column in columns)
            + "</tr>"
            for row in rows
        )
        return f"<table><thead><tr>{header_html}</tr></thead><tbody>{body_html}</tbody></table>"

    requirements = outputs["requirements"]
    questions = outputs["supplier_questions"]
    quotes = outputs["quotes_comparison"]
    economics = outputs["economics"]
    risks = outputs["contract_risks"]
    final_recommendation = outputs["final_recommendation"]
    trace = outputs["trace"]
    preliminary_analysis = requirements.get("preliminary_analysis", {})
    downloaded_docs = _build_downloaded_documents_inventory(metadata)
    files = metadata.get("files", [])
    procurement = metadata.get("procurement", {})
    procurement_manual_required = bool(metadata.get("manual_upload_required") or metadata.get("attachments_status") == "manual_upload_required" or not files)
    procurement_url = str(metadata.get("procurement_url") or procurement.get("source_url") or "").strip()
    notice_number = format_value(metadata.get("notice_number") or metadata.get("procurement_id") or procurement.get("procurement_number"))
    notice_number_html = (
        f'<a class="inline-link" href="{html.escape(procurement_url)}" target="_blank" rel="noopener noreferrer">{html.escape(notice_number)}</a>'
        if procurement_url and notice_number != "не указано"
        else html.escape(notice_number)
    )
    publication_update = format_publication_update(
        format_date_ru(metadata.get("publication_date") or procurement.get("publication_date")),
        format_date_ru(metadata.get("updated_date") or procurement.get("updated_date")),
    )
    deadline_ru = format_date_ru(metadata.get("deadline") or procurement.get("deadline"))
    delivery_term_ru = format_date_ru(procurement.get("delivery_term"))
    downloaded_files_count = int(metadata.get("downloaded_files_count", len(files)))
    archive_available = (get_demo_run_input_dir(str(metadata.get("run_id"))) / "documentation-archive.zip").is_file()
    archive_button_html = build_archive_button_html(str(metadata.get("run_id")), archive_available)
    document_toggle_html = build_document_toggle_html(str(metadata.get("run_id")), files)
    export_buttons_html = build_export_buttons_html(str(metadata.get("run_id")))

    return f"""
    <html lang="ru">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Отчёт по загруженному прогону тендерного агента</title>
        <style>
          body {{
            margin: 0;
            font-family: Arial, sans-serif;
            background: #001432;
            color: #ffffff;
          }}
          .page {{
            max-width: 1080px;
            margin: 0 auto;
            padding: 24px;
          }}
          .card {{
            background: rgba(255,255,255,0.06);
            border: 1px solid rgba(200,210,220,0.16);
            border-radius: 18px;
            padding: 20px;
            margin-bottom: 16px;
          }}
          h1, h2, h3 {{ margin-top: 0; }}
          .badge {{
            display: inline-block;
            padding: 8px 12px;
            border-radius: 999px;
            background: rgba(0,200,160,0.15);
            border: 1px solid rgba(120,250,230,0.25);
            margin-right: 8px;
            margin-bottom: 8px;
          }}
          table {{ width: 100%; border-collapse: collapse; }}
          th, td {{ text-align: left; padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.1); }}
          th {{ color: #78FAE6; font-size: 12px; text-transform: uppercase; }}
          ul {{ margin: 0; padding-left: 18px; }}
          .muted {{ color: rgba(255,255,255,0.75); }}
          .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 12px 18px;
            margin-top: 18px;
          }}
          .metric {{
            padding: 12px 14px;
            border-radius: 14px;
            background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.08);
          }}
          .metric-label {{
            display: block;
            font-size: 12px;
            text-transform: uppercase;
            color: #78FAE6;
            margin-bottom: 6px;
          }}
          .metric-value {{
            display: block;
            font-size: 15px;
            line-height: 1.4;
          }}
          .downloads {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-top: 16px;
            align-items: flex-start;
          }}
          .action-button {{
            display: inline-flex;
            align-items: center;
            padding: 10px 14px;
            border-radius: 999px;
            color: #ffffff;
            text-decoration: none;
            border: 1px solid rgba(120,250,230,0.3);
            background: rgba(255,255,255,0.05);
          }}
          .action-button.primary {{
            background: rgba(0,200,160,0.18);
          }}
          .inline-link {{
            color: #9cfbee;
            text-decoration: none;
            border-bottom: 1px dashed rgba(156,251,238,0.5);
          }}
          .document-toggle {{
            min-width: 240px;
          }}
          .document-toggle summary {{
            list-style: none;
          }}
          .document-toggle summary::-webkit-details-marker {{
            display: none;
          }}
          .document-list {{
            margin-top: 12px;
            padding-left: 18px;
          }}
          .document-list li + li {{
            margin-top: 8px;
          }}
          .doc-link {{
            color: #ffffff;
            text-decoration: none;
            border-bottom: 1px dashed rgba(255,255,255,0.35);
          }}
          .table-scroll {{
            overflow-x: auto;
            margin-top: 12px;
          }}
        </style>
      </head>
      <body>
        <div class="page">
          <div class="card">
            <div class="badge">Демо / пилотный режим</div>
            <div class="badge">Без внешних действий</div>
            <div class="badge">Требуется подтверждение человека</div>
            <h1>{html.escape(metadata['tender_title'])}</h1>
            <div class="summary-grid">
              <div class="metric"><span class="metric-label">Номер извещения</span><span class="metric-value">{notice_number_html}</span></div>
              <div class="metric"><span class="metric-label">Категория закупки</span><span class="metric-value">{html.escape(format_value(metadata.get('law') or metadata.get('tender_category') or procurement.get('category')))}</span></div>
              <div class="metric"><span class="metric-label">Заказчик</span><span class="metric-value">{html.escape(format_value(metadata.get('customer_name') or procurement.get('customer_name')))}</span></div>
              <div class="metric"><span class="metric-label">НМЦК</span><span class="metric-value">{html.escape(format_price(procurement.get('initial_price'), procurement.get('currency')))}</span></div>
              <div class="metric"><span class="metric-label">Дата публикации / обновления</span><span class="metric-value">{html.escape(publication_update)}</span></div>
              <div class="metric"><span class="metric-label">Срок подачи</span><span class="metric-value">{html.escape(deadline_ru or 'не указан')}</span></div>
              <div class="metric"><span class="metric-label">Срок поставки</span><span class="metric-value">{html.escape(delivery_term_ru or 'не указан')}</span></div>
              <div class="metric"><span class="metric-label">Источник сведений</span><span class="metric-value">{html.escape(procurement.get('structured_source_label') or metadata.get('notice_source_label') or 'карточка ЕИС')}</span></div>
              <div class="metric"><span class="metric-label">Статус подключения</span><span class="metric-value">{html.escape("Документы получены через ЕИС" if metadata.get("procurement_source") else "Документы загружены вручную")}</span></div>
              <div class="metric"><span class="metric-label">Скачано документов</span><span class="metric-value">{downloaded_files_count}</span></div>
            </div>
            <div class="downloads">{export_buttons_html}{archive_button_html}{document_toggle_html}</div>
            {('<p class="muted">Документация не получена. Анализ невозможен до ручной загрузки файлов.</p>' if procurement_manual_required and not files else '')}
          </div>

          <div class="card">
            <h2>Загруженные документы</h2>
            {render_table(
                ["Тип", "Файл", "Статус скачивания", "Статус текста", "Источник"],
                [
                    {
                        "Тип": item["type"],
                        "Файл": item["name"],
                        "Статус скачивания": item["download_status"],
                        "Статус текста": item["text_status"],
                        "Источник": item["source"],
                    }
                    for item in downloaded_docs
                ],
            )}
            {('<p class="muted">По закупке не найдены публичные вложения кроме электронного извещения. Анализ технической части ограничен.</p>' if len(downloaded_docs) <= 1 else '')}
          </div>

          <div class="card">
            <h2>Предварительный анализ закупки</h2>
            <ul>{list_html(preliminary_analysis.get('overview', [])) or "<li>Пока не удалось извлечь структурированные выводы из ТЗ.</li>"}</ul>
            {(
                f"<h3>{html.escape(_preliminary_analysis_supply_section_title(preliminary_analysis))}</h3>"
                + f"<p>{html.escape(preliminary_analysis.get('supply_section_note', 'Состав поставки собран по техническим документам.'))}</p>"
                + '<div class="table-scroll">'
                + render_table(
                    preliminary_analysis.get('spec_table', {}).get('columns', []),
                    preliminary_analysis.get('spec_table', {}).get('rows', []),
                )
                + '</div>'
            ) if preliminary_analysis.get('spec_table', {}).get('rows') else ''}
            <h3>Ключевые требования и ограничения</h3>
            <ul>{list_html(preliminary_analysis.get('compliance_highlights', [])) or "<li>Требуется ручная валидация ключевых требований по исходным документам.</li>"}</ul>
            <h3>Модель исполнения</h3>
            <ul>{list_html(preliminary_analysis.get('delivery_model', [])) or "<li>Формат исполнения нужно уточнить вручную.</li>"}</ul>
            <h3>Ключевые условия договора</h3>
            <ul>{list_html(preliminary_analysis.get('contract_highlights', [])) or "<li>Ключевые условия договора нужно проверить вручную.</li>"}</ul>
            <h3>Что делать дальше</h3>
            <ul>{list_html(preliminary_analysis.get('next_actions', []))}</ul>
          </div>

          <div class="card">
            <h2>Извлечённые требования</h2>
            {render_table(
                ["Требование", "Тип", "Приоритет", "Источник"],
                [
                    {
                        "Требование": item.get("title", ""),
                        "Тип": item.get("type", "общее"),
                        "Приоритет": item.get("priority", "medium"),
                        "Источник": item.get("source", "не указан"),
                    }
                    for item in requirements['requirements']
                ],
            )}
          </div>

          <div class="card">
            <h2>Вопросы поставщикам</h2>
            <ul>{list_html(questions['questions'])}</ul>
          </div>

          <div class="card">
            <h2>RFQ draft</h2>
            <ul>{list_html(outputs['rfq_draft']['sections'])}</ul>
          </div>

          <div class="card">
            <h2>Извлечённые ТКП</h2>
            {render_table(
                ["Поставщик", "Файл", "Сумма", "Валюта", "Позиций", "Уверенность"],
                [
                    {
                        "Поставщик": item.get("supplier_name", "Поставщик"),
                        "Файл": item.get("source_file", "unknown"),
                        "Сумма": item.get("total_amount", "unknown"),
                        "Валюта": item.get("currency", "unknown"),
                        "Позиций": item.get("items_count", "unknown"),
                        "Уверенность": item.get("price_confidence", "unknown"),
                    }
                    for item in quotes.get("suppliers", [])
                ],
            )}
            <p class="muted">{html.escape(" ".join(quotes.get("limitations", [])))}</p>
          </div>

          <div class="card">
            <h2>Сравнение ТКП</h2>
            <ul>{list_html(quotes['highlights'])}</ul>
            {render_table(
                ["Позиция", "Лучшая цена", "Разброс %", "Нужна проверка"],
                [
                    {
                        "Позиция": item.get("normalized_name", "unknown"),
                        "Лучшая цена": item.get("best_price_supplier", "unknown"),
                        "Разброс %": item.get("price_spread_percent", "unknown"),
                        "Нужна проверка": "да" if item.get("needs_review") else "нет",
                    }
                    for item in quotes.get("items", [])[:24]
                ],
            )}
          </div>

          <div class="card">
            <h2>Экономика</h2>
            <ul>{list_html([f"{item['label']}: {item['value']}" for item in economics['metrics']])}</ul>
            <ul>{list_html(economics.get('manual_checks', []))}</ul>
            <p class="muted">{html.escape(" ".join(economics.get("limitations", [])))}</p>
          </div>

          <div class="card">
            <h2>Контрактные риски</h2>
            <ul>{list_html([item['risk'] for item in risks['risks']])}</ul>
          </div>

          <div class="card">
            <h2>Финальная рекомендация</h2>
            <p><strong>{html.escape(final_recommendation['label'])}</strong></p>
            <ul>{list_html(final_recommendation['rationale'])}</ul>
            <ul>{list_html(final_recommendation['manual_checks'])}</ul>
          </div>

          <div class="card">
            <h2>Трассировка и обоснование</h2>
            <p>{html.escape(trace['overall_explanation'])}</p>
            <ul>{list_html(trace.get('limitations', []))}</ul>
          </div>
        </div>
      </body>
    </html>
    """


def _normalize_report_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _is_missing_metadata_value(value: Any) -> bool:
    if value is None:
        return True
    text = _normalize_report_text(str(value))
    return not text or text.lower() in {"не указан", "none", "null", "n/a", "—"}


def _extract_customer_name_from_text(*texts: str | None) -> str | None:
    xml_patterns = (
        r"<(?:\w+:)?customerName>([^<]+)</(?:\w+:)?customerName>",
        r"<(?:\w+:)?fullName>([^<]+)</(?:\w+:)?fullName>",
    )
    text_patterns = (
        r"(?im)^\s*Заказчик(?:а|у|ом|е)?\s*[:\-]\s*([^\n]{4,200})",
        r"(?im)^\s*([А-ЯA-Z][^\n]{3,180})\s+в лице[^\n]+именуем[а-яё ]+«Заказчик»",
    )
    for raw_text in texts:
        if not raw_text:
            continue
        for pattern in xml_patterns:
            match = re.search(pattern, raw_text)
            if match:
                candidate = _normalize_report_text(html.unescape(match.group(1)))
                if candidate:
                    return candidate
        for pattern in text_patterns:
            match = re.search(pattern, raw_text)
            if match:
                candidate = _normalize_report_text(html.unescape(match.group(1) if match.groups() else match.group(0)))
                if candidate:
                    return candidate
    return None


def _extract_updated_date_from_text(*texts: str | None) -> str | None:
    for raw_text in texts:
        if not raw_text:
            continue
        match = re.search(r"<(?:\w+:)?directDT>(\d{4})-(\d{2})-(\d{2})T", raw_text)
        if match:
            return f"{match.group(3)}.{match.group(2)}.{match.group(1)}"
        match = re.search(r"(?im)обновлено\s*[:\-]?\s*(\d{2}\.\d{2}\.\d{4})", raw_text)
        if match:
            return match.group(1)
    return None


def _enrich_procurement_metadata_from_documents(
    metadata: dict[str, Any],
    *,
    documents: list[AnalyzedDocument] | None = None,
    combined_text: str | None = None,
    notice_text: str | None,
    technical_spec_text: str | None,
    contract_draft_text: str | None,
) -> dict[str, Any]:
    from src.modules.tender_operator_agent_demo.eis_notice_parser import (
        apply_structured_metadata_to_procurement,
        extract_notice_metadata,
        merge_structured_metadata,
    )

    procurement = dict(metadata.get("procurement") or {})

    eis_notice_meta: dict[str, Any] = {}
    if documents:
        for doc in documents:
            if doc.role == "notice" and doc.extension == ".xml" and doc.raw_content:
                raw_text = doc.raw_content.decode("utf-8", errors="replace")
                parsed = extract_notice_metadata(raw_text)
                if parsed.get("_has_notice_data"):
                    eis_notice_meta = parsed
                    metadata["notice_source_label"] = parsed.get("source_label", "электронное извещение ЕИС")
                    break

    card_meta = {
        "nmck": procurement.get("initial_price"),
        "publication_date": procurement.get("publication_date"),
        "submission_deadline": procurement.get("deadline"),
        "delivery_term": procurement.get("delivery_term"),
        "customer_name": procurement.get("customer_name"),
        "procedure_type": procurement.get("procedure_type"),
    }

    doc_meta: dict[str, Any] = {}
    customer_candidate = _extract_customer_name_from_text(combined_text, notice_text, contract_draft_text, technical_spec_text)
    if customer_candidate:
        doc_meta["customer_name"] = customer_candidate
        if metadata.get("mode") == "procurement_search_intake" or _is_missing_metadata_value(metadata.get("customer_name")):
            metadata["customer_name"] = customer_candidate
        if metadata.get("mode") == "procurement_search_intake" or _is_missing_metadata_value(procurement.get("customer_name")):
            procurement["customer_name"] = customer_candidate

    if _is_missing_metadata_value(metadata.get("updated_date")) and _is_missing_metadata_value(procurement.get("updated_date")):
        updated_date = _extract_updated_date_from_text(notice_text)
        if updated_date:
            metadata["updated_date"] = updated_date
            procurement["updated_date"] = updated_date

    structured = merge_structured_metadata(eis_notice_meta, card_meta, doc_meta)
    apply_structured_metadata_to_procurement(procurement, structured)
    metadata["_structured_metadata"] = structured

    if structured.get("procurement_subject"):
        subject_entry = structured["procurement_subject"]
        procurement["procurement_subject"] = subject_entry["value"]
        procurement["title_source_reference"] = subject_entry.get("source_reference")
        metadata["procurement_title"] = subject_entry["value"]
        metadata["tender_title"] = subject_entry["value"]
    for key in ("customer_name", "customer_inn", "customer_kpp"):
        entry = structured.get(key)
        if entry:
            procurement[key] = entry["value"]
            metadata[key] = entry["value"]
    delivery_entry = structured.get("delivery_place")
    if delivery_entry:
        delivery_value = delivery_entry["value"]
        procurement["delivery_place"] = delivery_value
        metadata["delivery_place"] = delivery_value
        metadata["delivery_address"] = delivery_value
        metadata["delivery_region"] = delivery_value.split(",", 1)[0]
        metadata["delivery_status"] = "known"
    okpd2_entry = structured.get("okpd2_codes")
    if okpd2_entry:
        metadata["okpd2_codes"] = okpd2_entry["value"]
        procurement["okpd2_codes"] = okpd2_entry["value"]
    for key, metadata_key in (("publication_date", "publication_date"), ("deadline", "deadline")):
        entry = structured.get(key)
        if entry:
            metadata[metadata_key] = entry["value"]
    metadata["_field_evidence"] = {
        field: entry.get("source_reference")
        for field, entry in (
            ("procurement_title", structured.get("procurement_subject")),
            ("publication_datetime", structured.get("publication_date")),
            ("application_deadline", structured.get("deadline")),
            ("nmck", structured.get("initial_price")),
            ("customer_name", structured.get("customer_name")),
            ("delivery_place", structured.get("delivery_place")),
            ("okpd2_codes", structured.get("okpd2_codes")),
        )
        if entry
    }

    if eis_notice_meta.get("_has_notice_data"):
        metadata["notice_source_label"] = eis_notice_meta.get("source_label", "электронное извещение ЕИС")
        procurement["structured_source_label"] = metadata["notice_source_label"]
    elif not procurement.get("structured_source_label"):
        procurement["structured_source_label"] = "карточка ЕИС"
        metadata["notice_source_label"] = "карточка ЕИС"

    if procurement:
        metadata["procurement"] = procurement
    return metadata


def _render_canonical_report_html(model: dict[str, Any]) -> str:
    """Presentation-only web renderer for the canonical report model."""
    return _render_product_report_html(model, customer=False)


def _render_customer_report_html(model: dict[str, Any]) -> str:
    """Customer projection deliberately excludes operational report metadata."""
    return _render_product_report_html(model)


def _render_product_report_html(model: dict[str, Any], *, customer: bool = True) -> str:
    def esc(value: Any) -> str:
        return html.escape(str(value if value not in (None, "") else "Данных недостаточно — требуется проверка"))

    decision = model["customer_decision"]
    documents = "".join(f"<li>{esc(item['name'])} ({esc(item['type'])})</li>" for item in model["customer_documents"])
    bullets = lambda values: "".join(f"<li>{esc(value)}</li>" for value in values)
    rows = "".join(f"<tr><td>{row['sequence']}</td><td>{esc(row['original_name'])}</td><td>{esc(row['quantity_display'])}</td><td>{esc(row['unit_original'])}</td><td>{esc(row.get('okpd2') or 'Не извлечён')}</td><td>Извещение о закупке — раздел «Объект закупки», {esc(row['source_row'] or 'позиция не указана')}</td></tr>" for row in model["line_items"])
    evidence = bullets([f"{item['document']} — {item.get('document_type', 'документ')}, {item['row']}" for item in model["evidence_map"]])
    risks = bullets([f"{risk.get('risk')}: {risk.get('impact')}. Что сделать: {risk.get('mitigation')}" for risk in model["risks"]])
    economics = ""
    if model.get("unit_economics"):
        item = model["unit_economics"]
        value = f"{item['value']:,.2f}".replace(",", " ").replace(".", ",")
        economics = f"<section><h2>Экономический ориентир</h2><p>НМЦК, делённая на подтверждённый объём, составляет ориентировочно <strong>{value} ₽ за {esc(item['unit'])}</strong>.</p><p>Это арифметический ориентир по НМЦК, а не подтверждённая закупочная себестоимость.</p></section>"
    if customer:
        return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Анализ закупки № {esc(model.get('procurement_number'))}</title><style>body{{margin:0;background:#f5f8fa;color:#10243e;font:16px Arial,sans-serif}}main{{max-width:1180px;margin:auto;padding:24px}}section{{background:#fff;border:1px solid #dce5eb;border-radius:12px;padding:20px;margin:16px 0}}h1,h2{{color:#003b5c}}.decision{{border-left:6px solid #d08300}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:760px}}th,td{{border-bottom:1px solid #dce5eb;padding:9px;text-align:left;vertical-align:top}}th{{background:#e9f7f5}}</style></head><body><main>
<section><h1>Анализ закупки № {esc(model.get('procurement_number'))}</h1><p>Отчёт для принятия решения об участии</p><details><summary>Документы комплекта ({len(model['customer_documents'])})</summary><ul>{documents}</ul></details></section>
<section><h2>{esc(model.get('procurement_title'))}</h2><p>Заказчик: {esc(model.get('customer_name'))}</p><p>Дата публикации: {esc(model.get('publication_datetime_display'))}</p><p>Окончание подачи заявок: {esc(model.get('application_deadline_display'))}</p><p>НМЦК: {esc(model.get('nmck'))} ₽</p><p>Место поставки: {esc(model.get('delivery_place'))}</p>{f'<p>Отчёт сформирован по состоянию на: {esc(model.get("analysis_as_of"))}</p>' if model.get('analysis_as_of') != 'Данных недостаточно — требуется проверка' else ''}</section>
<section class="decision"><h2>Решение: {esc(decision['recommendation'])}</h2><h3>Ключевые основания</h3><ul>{bullets(decision['reasons'])}</ul><h3>Подтверждено документами</h3><ul>{bullets(decision['confirmed'])}</ul>{('<h3>Не удалось оценить</h3><ul>' + bullets(decision['not_evaluated']) + '</ul>') if decision['not_evaluated'] else ''}<p><strong>Следующее действие:</strong> {esc(decision['next_action'])}</p></section>
{f'<section><h2>Состав и объём закупки</h2><div class="scroll"><table><thead><tr><th>№</th><th>Наименование</th><th>Количество</th><th>Единица</th><th>ОКПД2</th><th>Подтверждённый источник</th></tr></thead><tbody>{rows}</tbody></table></div><p>Зимний класс, ГОСТ, экологический класс и другие детальные характеристики не извлечены из текущего комплекта документов.</p></section>' if rows else ''}
{economics}<section><h2>Коммерческие предложения</h2><p>Коммерческие предложения не загружены; экономика участия не рассчитана.</p></section>{f'<section><h2>Риски, подтверждённые документами</h2><ul>{risks}</ul></section>' if risks else ''}
{f'<section><h2>Вопросы для уточнения</h2><ul>{bullets(model["customer_questions"])}</ul></section>' if model['customer_questions'] else '<section><h2>Вопросы для уточнения</h2><p>Сначала необходимо запросить отсутствующие документы. Предметные вопросы по договорным и техническим условиям будут сформированы после их получения.</p></section>'}
{f'<section><h2>Источники</h2><ul>{evidence}</ul></section>' if evidence else ''}{f'<section><h2>Ограничения комплекта документов</h2><ul>{bullets(model["corpus_limitations"])}</ul></section>' if model['corpus_limitations'] else ''}</main></body></html>'''
    def esc(value: Any) -> str:
        return html.escape(str(value if value not in (None, "") else "Данных недостаточно — требуется проверка"))
    summary, passport, meta = model["executive_summary"], model["procurement_passport"], model["metadata"]
    compatibility = model.get("compatibility_sections", {})
    rows = "".join(
        f"<tr><td>{row['sequence']}</td><td>{esc(row['original_name'])}</td><td>{esc(row['quantity_display'])}</td><td>{esc(row['unit_original'])}</td><td>{esc(row['quantity_status'])}</td><td>{esc(row['source_document_id'])}, {row['source_row']} [{esc(', '.join(row['evidence_ids']))}]</td></tr>"
        for row in model["line_items"]
    ) or '<tr><td colspan="6">Позиции и количество не удалось извлечь из доступных документов; требуется проверка первоисточника.</td></tr>'
    risks = "".join(f"<li><strong>{esc(risk.get('status', 'Требует проверки'))}</strong>: {esc(risk.get('risk'))}. {esc(risk.get('impact'))}</li>" for risk in model["risks"])
    evidence = "".join(f"<li id='{esc(item['evidence_id'])}'><strong>[{esc(item['evidence_id'])}]</strong> {esc(item['document'])}, строка {esc(item['row'])}: {esc(item['short_excerpt'])}</li>" for item in model["evidence_map"])
    bullets = lambda values: "".join(f"<li>{esc(value)}</li>" for value in values) or "<li>Не применимо — подтверждённых данных нет.</li>"
    compatibility_rows = "".join("<tr>" + "".join(f"<td>{esc(row.get(column))}</td>" for column in compatibility.get("spec_columns", [])) + "</tr>" for row in compatibility.get("spec_rows", []))
    # The canonical service catalogue below is the authoritative customer-facing
    # item table.  Rendering the compatibility spec rows as well duplicates every
    # service and makes row-level parity ambiguous.
    compatibility_table = (f"<div class='scroll'><table><thead><tr>{''.join(f'<th>{esc(column)}</th>' for column in compatibility.get('spec_columns', []))}</tr></thead><tbody>{compatibility_rows}</tbody></table></div>" if compatibility.get("spec_rows") and not model.get("service_catalog") else "")
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Анализ закупки {esc(meta.get('procurement_number'))}</title><style>
body{{margin:0;background:#f5f8fa;color:#10243e;font:16px Arial,sans-serif}}main{{max-width:1180px;margin:auto;padding:24px}}section{{background:#fff;border:1px solid #dce5eb;border-radius:12px;padding:20px;margin:16px 0}}h1,h2{{color:#003b5c}}.decision{{border-left:6px solid #d08300}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:760px}}th,td{{border-bottom:1px solid #dce5eb;padding:9px;text-align:left;vertical-align:top}}th{{background:#e9f7f5}}.label{{font-weight:bold;color:#006b66}}@media print{{body{{background:#fff}}section{{break-inside:avoid}}}}@media(max-width:700px){{main{{padding:12px}}section{{padding:14px}}}}</style></head><body><main>
<section><h1>{esc(compatibility.get('report_title'))}</h1><p>Номер извещения: {esc(compatibility.get('notice_number'))}</p><p>{esc(compatibility.get('source_status'))}</p><p>Скачано документов: {esc(compatibility.get('downloaded_files_count'))}</p><details><summary>Показать документы</summary><p>Документы текущего run доступны через защищённые ссылки интерфейса.</p></details>{('<a href="#archive">Скачать архив</a>' if compatibility.get('archive_available') else '')}<p class="label">Версия: {esc(meta.get('report_version'))}; полнота источников: {esc(meta.get('completeness_status'))}</p></section>
<section><h2>Резюме для принятия решения</h2><p><strong>Название закупки: {esc(model.get('procurement_title'))}</strong></p><p>Номер закупки: {esc(model.get('procurement_number'))}</p><p>Дата публикации: {esc(model.get('publication_datetime'))}</p><p>Окончание подачи заявок: {esc(model.get('application_deadline'))}</p><p>НМЦК: {esc(model.get('nmck'))} {esc(model.get('currency'))}</p><p>Заказчик: {esc(model.get('customer_name'))}</p><p>Место поставки: {esc(model.get('delivery_place'))}</p><p>Проект контракта: {esc('приложен' if model.get('contract_draft_status') == 'present' else ('приложен, но автоматически разобрать его не удалось' if model.get('contract_draft_status') == 'parse_failed' else model.get('contract_draft_status')))}</p><p>Позиций: {esc(len(model['line_items']))}</p><div class="decision"><strong>{esc(model.get('decision'))}</strong><ul>{bullets(summary['blockers'])}</ul><p>Следующее действие: {esc(summary['next_action'])}</p></div></section>
<section><h2>Паспорт закупки</h2><ul><li>Категория: {esc(passport.get('category'))}</li><li>ОКПД2: {esc(passport.get('okpd2'))}</li><li>Статус объёма: {esc(model.get('procurement_volume_status'))}</li><li>Причина статуса объёма: {esc(model.get('volume_status_reason'))}</li><li>Заказчик: {esc(passport.get('customer'))}</li><li>Место поставки: {esc(passport.get('delivery_place'))}</li></ul></section>
<section><h2>Предварительный анализ закупки</h2><ul>{bullets(compatibility.get('preliminary_overview', []))}</ul><h3>Состав поставки</h3>{compatibility_table}<h3>Ключевые условия договора</h3><ul>{bullets(compatibility.get('contract_highlights', []))}</ul></section>
<section><h2>Состав и объём закупки</h2><div class="scroll"><table><thead><tr><th>№</th><th>Наименование / Услуга</th><th>Количество</th><th>Единица</th><th>Статус количества</th><th>Источник и evidence</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<section><h2>Извлечённые ТКП</h2><ul>{bullets(compatibility.get('quotes', []))}</ul><h3>Сравнение ТКП</h3><ul>{bullets(compatibility.get('quotes', []))}</ul></section><section><h2>Экономика</h2><ul>{bullets(compatibility.get('economics', []))}</ul></section>
<section><h2>Недостающие данные и ограничения</h2><ul>{bullets([item['description'] + ': ' + item['required_action'] for item in model['missing_data']] + model['limitations'])}</ul></section>
<section><h2>Риски</h2><ul>{risks}</ul></section><section><h2>Вопросы заказчику/внутренней команде</h2><ul>{bullets(model['customer_questions'])}</ul></section><section><h2>Evidence map</h2><ul>{evidence}</ul></section>
</main></body></html>'''


def _persist_outputs(run_id: str, metadata: dict[str, Any], outputs: dict[str, dict[str, Any]], steps: list[DemoStep]) -> None:
    from src.modules.procurement_analysis.frozen_producer import persist_frozen_r7_outputs
    renderer = _render_customer_report_html if metadata.get("analysis_mode") == "production_llm_r10_1" else _render_canonical_report_html
    persist_frozen_r7_outputs(output_dir=_output_dir(run_id), run_id=run_id, metadata=metadata, outputs=outputs, steps=steps, render_html=renderer, now_factory=_safe_datetime)


def _render_procurement_blocked_report_html(metadata: dict[str, Any]) -> str:
    procurement = metadata.get("procurement", {})
    procurement_url = str(metadata.get("procurement_url") or procurement.get("source_url") or "").strip()
    notice_number = str(metadata.get("notice_number") or metadata.get("procurement_id") or procurement.get("procurement_number") or "не указано")
    notice_number_html = (
        f'<a class="inline-link" href="{html.escape(procurement_url)}" target="_blank" rel="noopener noreferrer">{html.escape(notice_number)}</a>'
        if procurement_url and notice_number.strip() and notice_number != "не указано"
        else html.escape(notice_number)
    )
    import re as _re
    def _format_d(v: Any) -> str:
        t = str(v).strip() if v else ""
        m = _re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else t
    publication_date = _format_d(metadata.get("publication_date") or procurement.get("publication_date") or "не указано")
    updated_date = _format_d(metadata.get("updated_date") or procurement.get("updated_date") or "").strip()
    publication_update = f"{publication_date} / {updated_date}" if updated_date and updated_date != publication_date else publication_date
    deadline_ru = _format_d(metadata.get("deadline") or procurement.get("deadline"))
    source_label = html.escape(procurement.get("structured_source_label") or metadata.get("notice_source_label") or "карточка ЕИС")
    return f"""
    <html lang="ru">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Источник закупки: документация требуется</title>
        <style>
          body {{ margin:0; font-family: Arial, sans-serif; background:#001432; color:#fff; }}
          .page {{ max-width:960px; margin:0 auto; padding:24px; }}
          .card {{ background:rgba(255,255,255,.06); border:1px solid rgba(200,210,220,.16); border-radius:18px; padding:20px; margin-bottom:16px; }}
          .badge {{ display:inline-block; padding:8px 12px; border-radius:999px; background:rgba(0,200,160,.15); border:1px solid rgba(120,250,230,.25); margin-right:8px; }}
          .warning {{ color:#78FAE6; font-weight:700; }}
          .summary-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px 18px; margin-top:18px; }}
          .metric {{ padding:12px 14px; border-radius:14px; background:rgba(255,255,255,.04); border:1px solid rgba(255,255,255,.08); }}
          .metric-label {{ display:block; font-size:12px; text-transform:uppercase; color:#78FAE6; margin-bottom:6px; }}
          .metric-value {{ display:block; font-size:15px; line-height:1.4; }}
          .inline-link {{ color:#9cfbee; text-decoration:none; border-bottom:1px dashed rgba(156,251,238,.5); }}
        </style>
      </head>
      <body>
        <div class="page">
          <div class="card">
            <span class="badge">Демо / пилотный режим</span>
            <span class="badge">Без внешних действий</span>
            <span class="badge">Требуется подтверждение человека</span>
            <h1>{html.escape(str(metadata.get("tender_title") or "Закупка"))}</h1>
            <div class="summary-grid">
              <div class="metric"><span class="metric-label">Номер извещения</span><span class="metric-value">{notice_number_html}</span></div>
              <div class="metric"><span class="metric-label">Категория закупки</span><span class="metric-value">{html.escape(str(metadata.get("law") or procurement.get("category") or "не указана"))}</span></div>
              <div class="metric"><span class="metric-label">Заказчик</span><span class="metric-value">{html.escape(str(metadata.get("customer_name") or procurement.get("customer_name") or "не указан"))}</span></div>
              <div class="metric"><span class="metric-label">НМЦК</span><span class="metric-value">{html.escape(str(procurement.get("initial_price") or "не указана"))} {html.escape(str(procurement.get("currency") or "₽"))}</span></div>
              <div class="metric"><span class="metric-label">Дата публикации / обновления</span><span class="metric-value">{html.escape(publication_update)}</span></div>
              <div class="metric"><span class="metric-label">Срок подачи</span><span class="metric-value">{html.escape(deadline_ru or "не указан")}</span></div>
              <div class="metric"><span class="metric-label">Источник сведений</span><span class="metric-value">{source_label}</span></div>
              <div class="metric"><span class="metric-label">Статус подключения</span><span class="metric-value">Документы получены через ЕИС</span></div>
              <div class="metric"><span class="metric-label">Скачано документов</span><span class="metric-value">{html.escape(str(metadata.get("downloaded_files_count", len(metadata.get("files", [])))))}</span></div>
            </div>
            <p class="warning">Документация не получена. Анализ невозможен до ручной загрузки файлов.</p>
          </div>
        </div>
      </body>
    </html>
    """


def _ensure_procurement_blocked_report_html(run_id: str) -> Path | None:
    path = _report_html_path(run_id)
    if path.is_file():
        return path
    metadata = _load_metadata(run_id)
    if metadata.get("mode") != "procurement_search_intake" or metadata.get("files"):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render_procurement_blocked_report_html(metadata), encoding="utf-8")
    return path


def analyze_uploaded_demo_run(run_id: str) -> TenderOperatorUploadedRunAnalyzeResponse:
    metadata = _load_metadata(run_id)
    if not metadata.get("files") and metadata.get("status") == TenderOperatorUploadedRunStatus.DOCS_REQUIRED.value:
        metadata["analysis_status"] = "blocked"
        _save_metadata(run_id, metadata)
        append_demo_run_event(
            run_id,
            "analysis_blocked",
            "Анализ остановлен: документация не получена автоматически, требуется ручная загрузка.",
            {"status": metadata.get("status")},
        )
        raise HTTPException(status_code=409, detail="Документация ещё не загружена. Добавьте файлы вручную и повторите анализ.")

    metadata["status"] = TenderOperatorUploadedRunStatus.ANALYZING.value
    metadata["analysis_mode"] = "analyzing"
    metadata["analysis_status"] = "analyzing"
    _save_metadata(run_id, metadata)
    append_demo_run_event(
        run_id,
        "analysis_started",
        "Запущен контролируемый анализ локального demo-run.",
        {"mode": metadata.get("mode"), "files": len(metadata.get("files", []))},
    )

    try:
        ai_provenance = _runtime_ai_provenance()
        documents = _collect_documents(run_id, metadata)
        warnings = list(dict.fromkeys(metadata.get("warnings", []) + [warning for doc in documents for warning in doc.warnings]))

        notice_text = _collect_role_text(documents, "notice") or _collect_role_text(documents, "supporting") or metadata["tender_title"]
        technical_spec_text = _collect_role_text(documents, "technical_spec")
        contract_draft_text = _collect_role_text(documents, "contract_draft")
        combined_text = "\n\n".join(doc.text for doc in documents if doc.text)
        quote_paths = _collect_quote_paths(run_id, metadata)
        spreadsheet_sources = _collect_spreadsheet_sources(documents)
        economics_inputs = metadata.get("economics_inputs", {})
        metadata = _enrich_procurement_metadata_from_documents(
            metadata,
            documents=documents,
            combined_text=combined_text,
            notice_text=notice_text,
            technical_spec_text=technical_spec_text,
            contract_draft_text=contract_draft_text,
        )

        profile = None if metadata.get("mode") == "procurement_search_intake" else get_supplier_profile()
        doc_relevance = score_procurement_document_text(text=combined_text or "", profile=profile)
        metadata["document_relevance"] = doc_relevance
        append_demo_run_event(
            run_id,
            "relevance_document_scoring_completed",
            f"Скоринг документов выполнен: найдено {len(doc_relevance.get('document_matched_terms', []))} совпадений.",
            {"document_score": doc_relevance.get("document_score")},
        )

        from src.modules.tender_operator_agent_demo.eis_notice_parser import (
            build_notice_priority_prompt_section,
            build_technical_documents_prompt_section,
        )
        procurement = metadata.get("procurement", {})
        priority_section = build_notice_priority_prompt_section(procurement)
        if priority_section and notice_text:
            notice_text = priority_section + "\n\n" + notice_text
        technical_docs_section = build_technical_documents_prompt_section(metadata.get("files", []))
        if technical_docs_section:
            technical_spec_text = technical_docs_section + "\n\n" + (technical_spec_text or "")

        core_complete = bool(technical_spec_text and contract_draft_text and notice_text)
        if not technical_spec_text and combined_text:
            technical_spec_text = combined_text[:6000]
            warnings.append("Технические документы не выделены отдельно; используется общий текст документов.")
        if not contract_draft_text:
            warnings.append("Contract draft text was not fully extracted; contract risks are partially inferred.")

        provider_mode = "llm"
        llm_result = _try_run_llm_workflow(
            run_id=run_id,
            notice_text=notice_text,
            technical_spec_text=technical_spec_text,
            contract_draft_text=contract_draft_text,
            quote_paths=quote_paths,
            provider_mode=provider_mode,
        )
        if llm_result is not None:
            ai_provenance.update({"analysis_engine": "deterministic_with_local_llm", "llm_invoked": True, "llm_calls_count": 1, "fallback_reason": None})
            requirements = llm_result.get("requirements", {})
            calibrated_risks = llm_result.get("contract_risks", [])
            supplier_questions = llm_result.get("supplier_questions", [])
            rfq_draft = llm_result.get("rfq_draft", {})
            analysis_mode = llm_result.get("analysis_mode", "llm_tender_operator_provider")
            append_demo_run_event(
                run_id,
                "llm_analysis_completed",
                f"LLM-анализ выполнен через {llm_result.get('resolved_provider', 'llm')}.",
                {"analysis_mode": analysis_mode, "resolved_provider": llm_result.get("resolved_provider")},
            )
        else:
            requirements = {
                "technical_requirements": [],
                "document_requirements": [],
                "qualification_requirements": [],
                "evaluation_criteria": [],
            }
            calibrated_risks = []
            supplier_questions = []
            rfq_draft = {}
            analysis_mode = "fallback_deterministic_adapter"
            append_demo_run_event(
                run_id,
                "stub_analysis_fallback",
                "LLM-анализ недоступен, используется документ-зависимый детерминированный fallback.",
                {"core_complete": core_complete},
            )
        metadata["ai_runtime_provenance"] = ai_provenance

        spreadsheet_comparison = build_quote_comparison(spreadsheet_sources, analysis_mode)
        if spreadsheet_comparison.supplier_quotes_found:
            tkp_comparison = _serialize_quote_comparison(spreadsheet_comparison)
            economics_summary = build_economics_summary(
                quote_comparison=spreadsheet_comparison,
                analysis_mode=analysis_mode,
                target_margin_percent=float(economics_inputs.get("target_margin_percent", DEFAULT_TARGET_MARGIN_PERCENT)),
                logistics_reserve_percent=float(economics_inputs.get("logistics_reserve_percent", DEFAULT_LOGISTICS_RESERVE_PERCENT)),
                risk_reserve_percent=float(economics_inputs.get("risk_reserve_percent", DEFAULT_RISK_RESERVE_PERCENT)),
                payment_delay_days=int(economics_inputs.get("payment_delay_days", DEFAULT_PAYMENT_DELAY_DAYS)),
            )
            economics = _serialize_economics_summary(economics_summary)
        else:
            tkp_comparison = None
            economics = None
        bid_decision = llm_result.get("bid_decision") if llm_result else None

        supplier_search_outcome = _run_supplier_internet_search(
            tender_title=metadata.get("tender_title", ""),
            notice_text=notice_text,
            technical_spec_text=technical_spec_text,
        )
        metadata["supplier_search"] = {
            "query": supplier_search_outcome.query_used,
            "total_found": supplier_search_outcome.total_found,
            "suppliers": [
                {"name": s.name, "site": s.site, "snippet": s.snippet[:200], "signals": s.relevance_signals}
                for s in supplier_search_outcome.suppliers
            ],
        }
        if supplier_search_outcome.error:
            append_demo_run_event(run_id, "supplier_search_warning", f"Поиск поставщиков недоступен: {supplier_search_outcome.error}", {})
        elif supplier_search_outcome.total_found:
            append_demo_run_event(
                run_id, "supplier_search_completed", f"Найдено {supplier_search_outcome.total_found} потенциальных поставщиков.",
                {"query": supplier_search_outcome.query_used, "count": supplier_search_outcome.total_found},
            )

        limitations = list(dict.fromkeys(metadata.get("limitations", [])))
        if not core_complete:
            limitations.append("Full runner integration was partially applied because the uploaded package did not produce all core extracted texts.")
        quote_inputs_present = bool(quote_paths or spreadsheet_sources)
        if not quote_inputs_present:
            limitations.append("TKP not uploaded. Supplier comparison and economics remain blocked or partial.")
        elif not spreadsheet_sources and quote_paths:
            limitations.append("Quote files were uploaded in non-spreadsheet format; structured comparison and economics require manual review or XLS/XLSX.")
        if spreadsheet_sources:
            limitations.append("Spreadsheet normalization uses deterministic heuristics and may require manual review for нестандартные таблицы.")
            if tkp_comparison:
                limitations.extend(tkp_comparison.get("limitations", []))
            if economics:
                limitations.extend(economics.get("limitations", []))
        elif any(item["extension"] in {".xlsx", ".xls"} for item in metadata.get("files", [])):
            limitations.append("Spreadsheet files were uploaded, but structured extraction could not start.")

        metadata["warnings"] = sorted(set(warnings))
        metadata["limitations"] = limitations
        metadata["analysis_mode"] = analysis_mode
        metadata["analysis_status"] = "completed"

        outputs = _build_output_payloads(
            metadata=metadata,
            documents=documents,
            analysis_mode=analysis_mode,
            requirements=requirements,
            calibrated_risks=calibrated_risks,
            supplier_questions=supplier_questions,
            tkp_comparison=tkp_comparison,
            economics=economics,
            bid_decision=bid_decision,
            core_complete=core_complete,
            quote_inputs_present=quote_inputs_present,
        )
        steps = _build_steps_from_outputs(metadata, outputs)
        final_recommendation = _build_final_recommendation(outputs)
        status = TenderOperatorUploadedRunStatus.COMPLETED if core_complete and tkp_comparison else TenderOperatorUploadedRunStatus.COMPLETED_WITH_WARNINGS
        if final_recommendation.recommendation == DemoRecommendationCode.MANUAL_REVIEW_REQUIRED:
            status = TenderOperatorUploadedRunStatus.COMPLETED_WITH_WARNINGS if quote_inputs_present else TenderOperatorUploadedRunStatus.NEEDS_REVIEW

        metadata["status"] = status.value
        _save_metadata(run_id, metadata)
        _persist_outputs(run_id, metadata, outputs, steps)
        append_demo_run_event(
            run_id,
            "analysis_completed",
            "Анализ завершён в контролируемом demo-контуре.",
            {"status": status.value, "analysis_mode": analysis_mode},
        )

        return TenderOperatorUploadedRunAnalyzeResponse(
            run_id=run_id,
            status=status,
            analysis_mode=analysis_mode,
            warnings=metadata["warnings"],
            limitations=metadata["limitations"],
            steps=steps,
            final_recommendation=final_recommendation,
        )
    except HTTPException:
        raise
    except Exception as exc:
        metadata["status"] = TenderOperatorUploadedRunStatus.FAILED.value
        metadata["analysis_mode"] = "failed"
        metadata["analysis_status"] = "failed"
        metadata["warnings"] = list(dict.fromkeys(metadata.get("warnings", []) + [f"Analysis failed safely: {exc}"]))
        metadata["limitations"] = list(dict.fromkeys(metadata.get("limitations", []) + ["Fallback report generation failed. Manual operator review required."]))
        _save_metadata(run_id, metadata)
        append_demo_run_event(
            run_id,
            "analysis_blocked",
            "Анализ завершился безопасной остановкой из-за внутренней ошибки.",
            {"error": str(exc)},
        )

        failed_outputs = {
            "final_recommendation": {
                "recommendation": DemoRecommendationCode.MANUAL_REVIEW_REQUIRED.value,
                "label": "нужна ручная проверка",
                "rationale": ["Анализ не завершился полностью и был остановлен в безопасном режиме."],
                "key_requirements": ["Проверка пакета документов вручную"],
                "open_questions": ["Нужно повторно проверить загруженные файлы и формат документов."],
                "risks": ["Автоматический анализ не завершён"],
                "economics": ["Недостаточно данных"],
                "manual_checks": ["Повторно просмотреть пакет документов вручную."],
            },
            "trace": {
                "overall_explanation": "Система не выполнила внешних действий и остановила анализ в безопасном режиме после внутренней ошибки.",
                "per_step": {step: "Анализ остановлен в safe mode." for step in ("documents", "requirements", "supplier_search", "questions", "rfq", "quotes", "economics", "risks", "decision")},
                "limitations": metadata["limitations"],
            },
        }
        final_recommendation = _build_final_recommendation(failed_outputs)
        steps = [
            DemoStep(
                key="documents",
                order=1,
                title="Документы",
                short_title="Документы",
                status=DemoStepStatus.NEEDS_REVIEW,
                description="Анализ остановлен до полного прохождения pipeline.",
                agent_action="Система сохранила локальные файлы, но не завершила разбор.",
                result_summary="Run остановлен в safe mode.",
                findings=metadata["warnings"],
                human_review=["Проверить формат и содержимое загруженных файлов вручную."],
                trace="Безопасная остановка без внешних действий.",
                result_sections=[],
            )
        ]
        return TenderOperatorUploadedRunAnalyzeResponse(
            run_id=run_id,
            status=TenderOperatorUploadedRunStatus.FAILED,
            analysis_mode="failed",
            warnings=metadata["warnings"],
            limitations=metadata["limitations"],
            steps=steps,
            final_recommendation=final_recommendation,
        )


def _report_json_path(run_id: str) -> Path:
    return _output_dir(run_id) / "report.json"


def _report_html_path(run_id: str) -> Path:
    return _output_dir(run_id) / "report.html"


def _load_report_json(run_id: str) -> dict[str, Any]:
    path = _report_json_path(run_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Report is not available yet")
    return _read_json(path)


def get_uploaded_demo_run(run_id: str) -> TenderOperatorUploadedRunResponse:
    metadata = _load_metadata(run_id)
    steps: list[DemoStep] = []
    final_recommendation: DemoFinalRecommendation | None = None
    quote_comparison = None
    economics_summary = None
    outputs_path = _output_dir(run_id)
    if (outputs_path / "final_recommendation.json").is_file() and (outputs_path / "trace.json").is_file():
        outputs = {
            "final_recommendation": _read_json(outputs_path / "final_recommendation.json"),
            "trace": _read_json(outputs_path / "trace.json"),
        }
        final_recommendation = _build_final_recommendation(outputs)
    if (outputs_path / "report.json").is_file():
        report_json = _load_report_json(run_id)
        steps = [
            DemoStep.model_validate(
                {
                    "key": f"section-{index}",
                    "order": index,
                    "title": section["title"],
                    "short_title": section["title"],
                    "status": DemoStepStatus.DONE,
                    "description": section["title"],
                    "agent_action": section["title"],
                    "result_summary": section["title"],
                    "findings": section.get("items", []),
                    "human_review": [],
                    "trace": "Saved report section.",
                    "result_sections": [],
                }
            )
            for index, section in enumerate(report_json.get("sections", []), start=1)
        ]
    stored_steps_path = outputs_path / "steps.json"
    if stored_steps_path.is_file():
        steps = [DemoStep.model_validate(item) for item in _read_json(stored_steps_path).get("steps", [])]
    quote_path = outputs_path / "quotes_comparison.json"
    economics_path = outputs_path / "economics.json"
    if quote_path.is_file():
        quote_comparison = _coerce_quote_comparison_payload(_read_json(quote_path))
    if economics_path.is_file():
        economics_summary = _coerce_economics_summary_payload(_read_json(economics_path))
    report_path = _report_html_path(run_id)
    if not report_path.is_file():
        report_path = _ensure_procurement_blocked_report_html(run_id)

    return TenderOperatorUploadedRunResponse(
        run_id=metadata["run_id"],
        created_at=datetime.fromisoformat(metadata["created_at"]),
        mode=metadata["mode"],
        tender_title=metadata["tender_title"],
        tender_category=metadata["tender_category"],
        customer_name=metadata["customer_name"],
        notes=metadata.get("notes"),
        status=TenderOperatorUploadedRunStatus(metadata["status"]),
        analysis_mode=metadata.get("analysis_mode", "not_started"),
        files=[TenderOperatorUploadedFile.model_validate(item) for item in metadata.get("files", [])],
        limitations=metadata.get("limitations", []),
        warnings=metadata.get("warnings", []),
        human_in_the_loop=metadata.get("human_in_the_loop", True),
        external_actions=metadata.get("external_actions", False),
        no_platform_submission=metadata.get("no_platform_submission", True),
        no_email_sending=metadata.get("no_email_sending", True),
        no_digital_signature=metadata.get("no_digital_signature", True),
        procurement_source=metadata.get("procurement_source"),
        procurement_id=metadata.get("procurement_id"),
        procurement_url=metadata.get("procurement_url"),
        procurement_query=metadata.get("procurement_query"),
        procurement_notice_number=metadata.get("notice_number"),
        procurement_law=metadata.get("law"),
        token_owner=metadata.get("token_owner"),
        soap_method=metadata.get("soap_method"),
        eis_ref_id=metadata.get("getdocs_ref_id"),
        archive_url_present=metadata.get("archive_url_present", False),
        archive_downloaded=metadata.get("archive_downloaded", False),
        archive_download_status=metadata.get("archive_download_status"),
        archive_download_attempts=metadata.get("archive_download_attempts", 0),
        archive_source_host=metadata.get("archive_source_host"),
        archive_source_path=metadata.get("archive_source_path"),
        documents_extracted_count=metadata.get("documents_extracted_count", 0),
        downloaded_files_count=metadata.get("downloaded_files_count", len(metadata.get("files", []))),
        manual_upload_required=metadata.get("manual_upload_required", False),
        attachments_status=metadata.get("attachments_status"),
        steps=steps,
        final_recommendation=final_recommendation,
        quote_comparison=quote_comparison,
        economics_summary=economics_summary,
        report_html_url=f"/demo/tender-agent/runs/{run_id}/report" if report_path and report_path.is_file() else None,
        report_download_url=f"/api/demo/tender-agent/runs/{run_id}/report/download" if report_path and report_path.is_file() else None,
        uploaded_files_note="Используются только локальные данные. Абсолютные server-path намеренно скрыты из интерфейса.",
        events=load_demo_run_events(run_id),
        document_relevance=metadata.get("document_relevance"),
    )


def get_uploaded_demo_run_steps(run_id: str) -> TenderOperatorUploadedRunStepsResponse:
    steps_path = _output_dir(run_id) / "steps.json"
    metadata = _load_metadata(run_id)
    if not steps_path.is_file():
        return TenderOperatorUploadedRunStepsResponse(
            run_id=run_id,
            status=TenderOperatorUploadedRunStatus(metadata["status"]),
            steps=[],
        )
    payload = _read_json(steps_path)
    return TenderOperatorUploadedRunStepsResponse(
        run_id=run_id,
        status=TenderOperatorUploadedRunStatus(metadata["status"]),
        steps=[DemoStep.model_validate(item) for item in payload.get("steps", [])],
    )


def save_uploaded_demo_steps(run_id: str, steps: list[DemoStep]) -> None:
    _write_json(_output_dir(run_id) / "steps.json", {"steps": [item.model_dump(mode="json") for item in steps]})


def get_uploaded_demo_report(run_id: str) -> TenderOperatorDemoReportResponse:
    payload = _load_report_json(run_id)
    return TenderOperatorDemoReportResponse.model_validate(payload)


def get_uploaded_demo_report_download(run_id: str) -> FileResponse:
    path = _ensure_procurement_blocked_report_html(run_id) or _report_html_path(run_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Report HTML is not available yet")
    return FileResponse(path, media_type="text/html; charset=utf-8", filename=f"{run_id}_report.html")


def get_uploaded_demo_report_html(run_id: str) -> str:
    path = _ensure_procurement_blocked_report_html(run_id) or _report_html_path(run_id)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Report HTML is not available yet")
    return path.read_text(encoding="utf-8")


def get_uploaded_demo_source_file_download(run_id: str, file_id: str) -> FileResponse:
    metadata = _load_metadata(run_id)
    descriptor = next((item for item in metadata.get("files", []) if item.get("file_id") == file_id), None)
    if descriptor is None:
        raise HTTPException(status_code=404, detail="Source file was not found")
    input_dir = get_demo_run_input_dir(run_id).resolve()
    target = (input_dir / str(descriptor.get("stored_name") or "")).resolve()
    if input_dir not in target.parents and target != input_dir:
        raise HTTPException(status_code=400, detail="Invalid stored file path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Stored source file is not available")
    return FileResponse(
        target,
        media_type=str(descriptor.get("content_type") or "application/octet-stream"),
        filename=str(descriptor.get("original_name") or descriptor.get("display_name") or target.name),
    )


def get_uploaded_demo_archive_download(run_id: str) -> FileResponse:
    path = get_demo_run_input_dir(run_id) / "documentation-archive.zip"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Documentation archive is not available")
    return FileResponse(path, media_type="application/zip", filename="documentation-archive.zip")
