from __future__ import annotations

import html
import io
import json
import re
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import ProxyHandler, Request, build_opener

from fastapi import HTTPException

from src.modules.tender_operator_agent_demo import schemas as tender_schemas
from src.modules.tender_operator_agent_demo.attachment_downloader import (
    download_procurement_attachments,
)
from src.modules.tender_operator_agent_demo.document_set_completeness import (
    apply_document_set_summary,
)
from src.modules.tender_operator_agent_demo.eis_notice_parser import (
    extract_notice_attachments,
)
from src.modules.tender_operator_agent_demo.procurement_discovery import (
    get_demo_procurement,
    get_procurement_details,
    search_procurements,
)
from src.modules.tender_operator_agent_demo.procurement_schemas import (
    DocsArchiveResult,
    ProcurementAttachment,
    ProcurementDetails,
)
from src.modules.tender_operator_agent_demo.procurement_sources import (
    get_procurement_source_descriptors,
)
from src.modules.tender_operator_agent_demo.public_44fz_search import (
    normalize_public_eis_law,
)
from src.modules.tender_operator_agent_demo.schemas import (
    EisDocsArchiveRunRequest,
    ProcurementAttachmentManifestItem,
    ProcurementRunCreateRequest,
    ProcurementRunDetailsResponse,
    ProcurementRunResponse,
    ProcurementSearchResult,
    TenderOperatorUploadedRunStatus,
)
from src.modules.tender_operator_agent_demo.settings import get_zakupki_soap_settings
from src.modules.tender_operator_agent_demo.upload_service import (
    ALLOWED_EXTENSIONS,
    MAX_ZIP_ENTRY_COUNT,
    MAX_ZIP_TOTAL_BYTES,
    analyze_uploaded_demo_run,
    append_demo_run_event,
    build_demo_file_descriptor,
    ensure_demo_run_structure,
    get_demo_run_input_dir,
    get_demo_run_procurement_dir,
    load_demo_run_events,
    load_demo_run_metadata,
    make_demo_run_id,
    sanitize_demo_filename,
    save_demo_run_metadata,
)
from src.modules.tender_operator_agent_demo.zakupki_soap_client import ZakupkiSoapClient
from src.tender_research.providers.public_44fz_search import (
    _parse_detail_metadata,
    _parse_document_links,
)

ARCHIVE_DOWNLOAD_RETRY_ATTEMPTS = 5
ARCHIVE_DOWNLOAD_RETRY_DELAY_SECONDS = 10
PUBLIC_EIS_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
PUBLIC_EIS_USER_AGENT = "Mozilla/5.0 (compatible; ArvectumTenderAgent/0.1; read-only)"
MAX_PROCUREMENT_DOCUMENTS = 100


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _find_descriptor(code: str):
    for item in get_procurement_source_descriptors():
        if item.code == code:
            return item
    return None


def _law_label_for_run(law: str) -> str:
    normalized = normalize_public_eis_law(law)
    if normalized == "223fz":
        return "223-ФЗ"
    if normalized == "capital_repair":
        return "Капремонт"
    return "44-ФЗ"


def _source_for_public_law(law: str) -> str:
    normalized = normalize_public_eis_law(law)
    if normalized == "223fz":
        return "public_eis_html_223fz"
    if normalized == "capital_repair":
        return "public_eis_html_capital_repair"
    return "public_eis_html_44fz"


def _normalize_search_result_law(request: tender_schemas.SearchResultHandoffRequest) -> str:
    if request.law:
        return normalize_public_eis_law(request.law)
    source = (request.source or "").strip().lower()
    if "223" in source:
        return "223fz"
    if "615" in source or "capital" in source or "капрем" in source:
        return "capital_repair"
    source_url = (request.source_url or "").strip().lower()
    if "/223/" in source_url:
        return "223fz"
    if "ea615" in source_url or "615" in source_url:
        return "capital_repair"
    return "44fz"


def _legacy_result_from_details(details: ProcurementDetails) -> ProcurementSearchResult:
    procurement = details.procurement
    downloadable_count = sum(1 for item in details.attachments if item.can_download)
    source_note = "; ".join(details.warnings) if details.warnings else details.raw_source_summary
    return ProcurementSearchResult(
        procurement_id=procurement.procurement_id,
        source=procurement.source,
        title=procurement.title,
        procurement_number=procurement.notice_number or procurement.registry_number,
        customer_name=procurement.customer_name,
        category=procurement.law or "Закупка",
        publication_date=procurement.publication_date,
        deadline=procurement.deadline,
        initial_price=procurement.initial_price,
        currency=procurement.currency,
        region=None,
        source_url=procurement.source_url,
        attachments_status=procurement.attachments_status,
        attachments_count=max(procurement.attachments_count, len(details.attachments)),
        available_attachments_count=downloadable_count,
        summary=procurement.status or details.raw_source_summary,
        attachment_names=[item.name for item in details.attachments],
        source_note=source_note,
    )


def _write_procurement_artifacts(
    run_id: str,
    *,
    selected: ProcurementSearchResult,
    manifest: list[ProcurementAttachmentManifestItem],
) -> None:
    procurement_dir = get_demo_run_procurement_dir(run_id)
    (procurement_dir / "procurement_metadata.json").write_text(
        selected.model_dump_json(indent=2),
        encoding="utf-8",
    )
    (procurement_dir / "attachments_manifest.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in manifest], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (procurement_dir / "source_summary.txt").write_text(
        "\n".join(
            [
                f"Источник: {selected.source}",
                f"Номер закупки: {selected.procurement_number or 'не указан'}",
                f"Карточка: {selected.source_url}",
                f"Статус документации: {selected.attachments_status}",
                f"Примечание: {selected.source_note or '—'}",
            ]
        ),
        encoding="utf-8",
    )


def _sanitize_archive_url_summary(archive_url: str | None) -> dict[str, Any]:
    if not archive_url:
        return {
            "host": None,
            "path": None,
            "has_query": False,
        }
    parsed = urlparse(archive_url)
    return {
        "host": parsed.hostname or "",
        "path": parsed.path or "/",
        "has_query": bool(parsed.query),
    }


def _create_public_html_run_from_search_result(
    request: tender_schemas.SearchResultHandoffRequest,
    *,
    normalized_law: str,
) -> tender_schemas.SearchResultHandoffResponse:
    reestr_number = request.reestr_number.strip()
    source_url = (request.source_url or "").strip() or "https://zakupki.gov.ru/"
    page_context = _extract_public_page_context(source_url) if source_url else {}
    law_label = _law_label_for_run(normalized_law)
    publication_date = request.publication_date or page_context.get("publication_date")
    deadline = request.deadline or page_context.get("deadline")
    initial_price = request.initial_price if request.initial_price is not None else page_context.get("initial_price")
    currency = request.currency or page_context.get("currency") or "RUB"
    selected = ProcurementSearchResult(
        procurement_id=reestr_number,
        source=_source_for_public_law(normalized_law),
        title=(request.title or "").strip() or page_context.get("title") or f"Закупка {reestr_number}",
        procurement_number=reestr_number,
        customer_name=(request.customer_name or "").strip() or page_context.get("customer_name") or "Не указан",
        category=law_label,
        publication_date=publication_date,
        deadline=deadline,
        initial_price=initial_price,
        currency=currency,
        region=None,
        source_url=source_url,
        attachments_status="manual_upload_required",
        attachments_count=0,
        available_attachments_count=0,
        summary=f"Карточка закупки {law_label} получена через публичный интерфейс ЕИС.",
        attachment_names=[],
        source_note="Документация будет догружена с публичной вкладки ЕИС, если вложения доступны без авторизации.",
    )

    run_id = make_demo_run_id()
    ensure_demo_run_structure(run_id, exist_ok=False)
    created_at = _now_iso()
    append_demo_run_event(
        run_id,
        "public_search_handoff_started",
        "Создан run из найденной закупки через публичный интерфейс ЕИС.",
        {"reestr_number": reestr_number, "law": normalized_law, "source_url": source_url},
    )

    metadata = {
        "run_id": run_id,
        "created_at": created_at,
        "mode": "procurement_search_intake",
        "tender_title": selected.title,
        "tender_category": law_label,
        "customer_name": selected.customer_name,
        "notes": None,
        "status": TenderOperatorUploadedRunStatus.DOCS_REQUIRED.value,
        "analysis_mode": "not_started",
        "procurement_source": selected.source,
        "procurement_id": reestr_number,
        "procurement_url": source_url,
        "procurement_query": reestr_number,
        "publication_date": publication_date,
        "updated_date": page_context.get("updated_date"),
        "deadline": deadline,
        "attachments_status": "manual_upload_required",
        "downloaded_files_count": 0,
        "manual_upload_required": True,
        "source": selected.source,
        "notice_number": reestr_number,
        "law": law_label,
        "files": [],
        "warnings": [],
        "limitations": [
            "Поиск и получение документации выполняются только в read-only режиме через публичный интерфейс ЕИС.",
            "Без авторизации, без обхода captcha, без подачи заявки, писем и ЭЦП.",
            "Если часть вложений недоступна публично, их нужно дозагрузить вручную перед анализом.",
        ],
        "human_in_the_loop": True,
        "external_actions": False,
        "no_platform_submission": True,
        "no_email_sending": True,
        "no_digital_signature": True,
        "procurement": selected.model_dump(mode="json"),
        "analysis_status": "not_started",
        "requested_analyze_after_download": request.analyze_after_download,
    }
    save_demo_run_metadata(run_id, metadata)
    _write_procurement_artifacts(run_id, selected=selected, manifest=[])
    _apply_search_result_context(run_id, request)
    saved_count = _supplement_run_with_public_notice_attachments(run_id, request.source_url)
    _apply_document_set_completeness(run_id)

    current_metadata = load_demo_run_metadata(run_id)
    current_status = current_metadata.get("status", TenderOperatorUploadedRunStatus.DOCS_REQUIRED.value)
    analysis_status = current_metadata.get("analysis_status")
    warnings = list(current_metadata.get("warnings", []))
    if request.analyze_after_download and current_status == TenderOperatorUploadedRunStatus.READY_TO_ANALYZE.value:
        analysis_result = analyze_uploaded_demo_run(run_id)
        current_status = analysis_result.status.value if hasattr(analysis_result.status, "value") else str(analysis_result.status)
        analysis_status = current_status

    append_demo_run_event(
        run_id,
        "public_documents_checked",
        (
            "Публичные документы ЕИС догружены и готовы к анализу."
            if saved_count > 0
            else "Публичные документы ЕИС проверены. Для продолжения может потребоваться ручная дозагрузка."
        ),
        {"saved_files": saved_count, "status": current_status},
    )

    return tender_schemas.SearchResultHandoffResponse(
        run_id=run_id,
        status=current_status,
        archive_url_present=False,
        archive_downloaded=False,
        documents_extracted_count=int(current_metadata.get("downloaded_files_count") or 0),
        analysis_status=analysis_status,
        run_url=f"/demo/tender-agent/runs/{run_id}",
        report_url=f"/demo/tender-agent/runs/{run_id}/report",
        warnings=warnings,
    )


def _build_getdocs_result(reestr_number: str, archive_result: DocsArchiveResult) -> ProcurementSearchResult:
    warnings = list(archive_result.warnings)
    if archive_result.status != "completed":
        warnings.append("Документация getDocsIP не была получена автоматически.")
    return ProcurementSearchResult(
        procurement_id=reestr_number,
        source="zakupki_gov_ru_getdocs_ip",
        title=f"Документация ЕИС по номеру {reestr_number}",
        procurement_number=reestr_number,
        customer_name="Не указан",
        category="44-ФЗ",
        publication_date=None,
        deadline=None,
        initial_price=None,
        currency="RUB",
        region=None,
        source_url="https://zakupki.gov.ru/",
        attachments_status="downloadable" if archive_result.archive_url else "manual_upload_required",
        attachments_count=1 if archive_result.archive_url else 0,
        available_attachments_count=1 if archive_result.archive_url else 0,
        summary=archive_result.raw_summary or f"Статус getDocsIP: {archive_result.status}",
        attachment_names=["documentation-archive.zip"] if archive_result.archive_url else [],
        source_note="Read-only getDocsIP intake для токена физлица.",
    )


def _strip_html_fragment(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _extract_card_value(page_html: str, label: str, *, title_class: str, content_class: str) -> str | None:
    pattern = re.compile(
        rf'<span[^>]*class="[^"]*{re.escape(title_class)}[^"]*"[^>]*>\s*{re.escape(label)}\s*</span>\s*'
        rf'<span[^>]*class="[^"]*{re.escape(content_class)}[^"]*"[^>]*>(.*?)</span>',
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(page_html)
    return _strip_html_fragment(match.group(1)) if match else None


def _extract_public_page_context(source_url: str) -> dict[str, Any]:
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        return {}
    if not hostname.endswith("zakupki.gov.ru"):
        return {}

    opener = build_opener(ProxyHandler({}))
    request = Request(source_url, headers={"User-Agent": PUBLIC_EIS_USER_AGENT}, method="GET")
    try:
        with opener.open(request, timeout=20) as response:
            page_html = response.read(PUBLIC_EIS_MAX_RESPONSE_BYTES + 1).decode("utf-8", errors="replace")
    except Exception:
        return {}
    if len(page_html) > PUBLIC_EIS_MAX_RESPONSE_BYTES:
        return {}

    parsed_metadata = _parse_detail_metadata(page_html, source_url)

    title = parsed_metadata.get("title") or _extract_card_value(
        page_html,
        "Объект закупки",
        title_class="cardMainInfo__title",
        content_class="cardMainInfo__content",
    )
    if not title:
        title = _extract_card_value(page_html, "Наименование объекта закупки", title_class="section__title", content_class="section__info")
    customer_name = parsed_metadata.get("customer_name") or _extract_card_value(
        page_html,
        "Заказчик",
        title_class="cardMainInfo__title",
        content_class="cardMainInfo__content",
    )
    publication_date = _format_public_context_datetime(parsed_metadata.get("publication_date")) or _extract_card_value(
        page_html,
        "Размещено",
        title_class="cardMainInfo__title",
        content_class="cardMainInfo__content",
    )
    updated_date = _extract_card_value(page_html, "Обновлено", title_class="cardMainInfo__title", content_class="cardMainInfo__content")
    deadline = _format_public_context_datetime(parsed_metadata.get("application_deadline")) or _extract_card_value(
        page_html,
        "Дата и время окончания срока подачи заявок",
        title_class="section__title",
        content_class="section__info",
    ) or _extract_card_value(page_html, "Окончание подачи заявок", title_class="cardMainInfo__title", content_class="cardMainInfo__content")
    initial_price_raw = _extract_card_value(
        page_html,
        "Начальная (максимальная) цена контракта",
        title_class="section__title",
        content_class="section__info",
    )
    currency = _extract_card_value(page_html, "Валюта", title_class="section__title", content_class="section__info")

    initial_price = None
    if parsed_metadata.get("nmck_amount") not in (None, ""):
        try:
            initial_price = float(parsed_metadata["nmck_amount"])
        except (TypeError, ValueError):
            initial_price = None
    if initial_price is None and initial_price_raw:
        cleaned = initial_price_raw.replace("\xa0", "").replace(" ", "").replace(",", ".")
        match = re.search(r"\d+(?:\.\d+)?", cleaned)
        if match:
            try:
                initial_price = float(match.group(0))
            except ValueError:
                initial_price = None

    return {
        "title": title,
        "customer_name": customer_name,
        "publication_date": publication_date,
        "updated_date": updated_date,
        "deadline": deadline,
        "initial_price": initial_price,
        "currency": currency,
    }


def _infer_public_documents_url(source_url: str) -> str | None:
    parsed = urlparse(source_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        return None
    if not hostname.endswith("zakupki.gov.ru"):
        return None
    if parsed.path.endswith("/documents.html"):
        return source_url
    if parsed.path.endswith("/common-info.html"):
        return parsed._replace(path=parsed.path[: -len("common-info.html")] + "documents.html").geturl()
    return None


def _format_public_context_datetime(value: Any) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.hour == 0 and value.minute == 0 and value.second == 0:
        return value.strftime("%d.%m.%Y")
    return value.strftime("%d.%m.%Y %H:%M")


def _parse_public_notice_attachments(page_html: str, *, page_url: str) -> list[ProcurementAttachment]:
    attachments: list[ProcurementAttachment] = []
    seen_urls: set[str] = set()
    for item in _parse_document_links(page_html, page_url):
        href = urljoin(page_url, html.unescape(item.url))
        if href in seen_urls:
            continue
        seen_urls.add(href)
        name = _strip_html_fragment(item.file_name) or _strip_html_fragment(item.title)
        if not name:
            continue
        parsed_href = urlparse(href)
        attachment_id = item.raw.get("uid") or parse_qs(parsed_href.query).get("uid", [Path(parsed_href.path).name or name])[0]
        extension = Path(name).suffix.lower() or Path(parsed_href.path).suffix.lower() or None
        attachments.append(
            ProcurementAttachment(
                attachment_id=attachment_id,
                name=name,
                url=href,
                extension=extension,
                can_download=True,
                requires_manual_upload=False,
            )
        )
    return attachments


def _fetch_public_notice_attachments(source_url: str) -> list[ProcurementAttachment]:
    documents_url = _infer_public_documents_url(source_url)
    if not documents_url:
        return []

    opener = build_opener(ProxyHandler({}))
    request = Request(documents_url, headers={"User-Agent": PUBLIC_EIS_USER_AGENT}, method="GET")
    try:
        with opener.open(request, timeout=20) as response:
            page_html = response.read(PUBLIC_EIS_MAX_RESPONSE_BYTES + 1).decode("utf-8", errors="replace")
    except Exception:
        return []
    if len(page_html) > PUBLIC_EIS_MAX_RESPONSE_BYTES:
        return []
    return _parse_public_notice_attachments(page_html, page_url=documents_url)


def _role_hint_from_procurement_attachment(name: str) -> str | None:
    lowered = name.lower()
    if any(token in lowered for token in ("обоснование нмцк", "обоснование начальной", "расчет нмцк", "расчёт нмцк")):
        return None
    if any(token in lowered for token in ("ткп", "кп", "коммерческое предложение", "supplier quote")):
        return "tkp"
    if any(
        token in lowered
        for token in (
            "проект контракта",
            "проект договора",
            "муниципального контракта",
            "государственного контракта",
            "contract",
            "agreement",
            "договор",
        )
    ):
        return "contract_draft"
    if any(
        token in lowered
        for token in (
            "техническое задание",
            "техзадание",
            "тз",
            "technical specification",
            "спецификац",
            "описание объекта закупки",
            "описание товара",
            "описание работ",
            "описание услуг",
            "ведомост",
        )
    ):
        return "technical_spec"
    if any(token in lowered for token in ("извещение", "notice", "epnotification")):
        return "notice"
    return None


def _document_kind_from_role_hint(role_hint: str | None) -> str:
    mapping = {
        "notice": "eis_notice",
        "technical_spec": "technical_specification",
        "contract_draft": "contract_draft",
        "tkp": "attachment",
    }
    return mapping.get(role_hint or "", "attachment")


def _discover_notice_xml_attachments(run_id: str, metadata: dict[str, Any]) -> list[ProcurementAttachment]:
    attachments: list[ProcurementAttachment] = []
    seen: set[tuple[str, str]] = set()
    for item in metadata.get("files", []):
        ext = Path(str(item.get("stored_name") or "")).suffix.lower()
        role_hint = str(item.get("role_hint") or "")
        if ext != ".xml" and role_hint != "notice":
            continue
        stored_name = str(item.get("stored_name") or "")
        if not stored_name:
            continue
        stored_path = get_demo_run_input_dir(run_id) / stored_name
        if not stored_path.is_file():
            continue
        try:
            xml_text = stored_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            xml_text = stored_path.read_text(encoding="cp1251", errors="replace")
        for idx, payload in enumerate(extract_notice_attachments(xml_text), start=1):
            name = str(payload.get("name") or "").strip() or f"notice-attachment-{idx}"
            url = str(payload.get("url") or "").strip() or None
            if url and url.startswith("/"):
                url = urljoin("https://zakupki.gov.ru", url)
            key = (name.lower(), url or "")
            if key in seen:
                continue
            seen.add(key)
            attachments.append(
                ProcurementAttachment(
                    attachment_id=f"notice-xml-{idx}",
                    name=name,
                    url=url,
                    extension=Path(name).suffix.lower() or None,
                    document_kind=str(payload.get("document_kind") or "attachment"),
                    can_download=bool(url),
                    requires_manual_upload=not bool(url),
                )
            )
    return attachments


def _dedupe_procurement_attachments(
    attachments: list[ProcurementAttachment],
    *,
    existing_files: list[dict[str, Any]],
) -> list[ProcurementAttachment]:
    existing_names = {str(item.get("original_name") or "").strip().lower() for item in existing_files}
    existing_urls = {str(item.get("source_url") or "").strip() for item in existing_files if item.get("source_url")}
    deduped: list[ProcurementAttachment] = []
    seen: set[tuple[str, str]] = set()
    for item in attachments:
        name_key = str(item.name or "").strip().lower()
        url_key = str(item.url or "").strip()
        if name_key in existing_names or (url_key and url_key in existing_urls):
            continue
        key = (name_key, url_key)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _supplement_run_with_discovered_attachments(
    run_id: str,
    *,
    attachments: list[ProcurementAttachment],
    source_type: str,
    event_label: str,
    source_note: str,
) -> int:
    if not attachments:
        return 0

    metadata = load_demo_run_metadata(run_id)
    existing_files = list(metadata.get("files", []))
    attachments = _dedupe_procurement_attachments(attachments, existing_files=existing_files)
    if not attachments:
        return 0

    settings = get_zakupki_soap_settings()
    download_limit = settings.max_download_mb * 1024 * 1024
    input_dir = get_demo_run_input_dir(run_id)
    remaining_slots = max(0, min(settings.max_attachments, MAX_PROCUREMENT_DOCUMENTS) - len(existing_files))
    if remaining_slots <= 0:
        return 0

    download_result = download_procurement_attachments(
        attachments,
        target_dir=input_dir,
        max_attachments=remaining_slots,
        max_file_size_bytes=download_limit,
        max_total_size_bytes=download_limit,
    )

    saved_count = 0
    file_index = len(existing_files)
    for item in download_result.saved:
        if not item.stored_name:
            continue
        file_index += 1
        role_hint = _role_hint_from_procurement_attachment(item.name)
        existing_files.append(
            build_demo_file_descriptor(
                file_id=f"FILE-{file_index:02d}",
                original_name=item.name,
                stored_name=item.stored_name,
                role_hint=role_hint,
                size_bytes=item.size_bytes,
                content_type=item.content_type or "application/octet-stream",
                source=source_type,
                source_type=source_type,
                source_url=item.source_url,
                document_kind=item.document_kind or _document_kind_from_role_hint(role_hint),
            )
        )
        append_demo_run_event(
            run_id,
            "attachment_saved",
            f"Документация '{item.name}' скачана из источника {source_type}.",
            {"stored_name": item.stored_name, "source": source_type, "source_url": item.source_url},
        )
        saved_count += 1

    manifest_path = get_demo_run_procurement_dir(run_id) / "attachments_manifest.json"
    manifest_payload = []
    if manifest_path.is_file():
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = [ProcurementAttachmentManifestItem.model_validate(item) for item in manifest_payload]
    for item in download_result.manifest:
        manifest.append(
            ProcurementAttachmentManifestItem(
                name=item.name,
                stored_name=item.stored_name,
                extension=item.extension,
                status=item.status,
                note=item.note,
                source_url=item.source_url,
                source_type=source_type,
                document_kind=item.document_kind,
                content_type=item.content_type,
                size_bytes=item.size_bytes,
                error=item.error,
            )
        )
        if item.status != "saved":
            append_demo_run_event(
                run_id,
                "attachment_skipped",
                f"Документация '{item.name}' пропущена при догрузке: {item.note or 'причина не указана'}",
                {"stored_name": item.stored_name, "source": source_type, "status": item.status, "source_url": item.source_url},
            )

    metadata["files"] = existing_files
    metadata["downloaded_files_count"] = len(existing_files)
    if saved_count > 0:
        metadata["attachments_status"] = "downloaded"
        metadata["manual_upload_required"] = False
        if metadata.get("status") == TenderOperatorUploadedRunStatus.DOCS_REQUIRED.value:
            metadata["status"] = TenderOperatorUploadedRunStatus.READY_TO_ANALYZE.value

    procurement_payload = dict(metadata.get("procurement") or {})
    attachment_names = list(procurement_payload.get("attachment_names") or [])
    attachment_names.extend(item.name for item in download_result.saved if item.name not in attachment_names)
    procurement_payload["attachment_names"] = attachment_names
    procurement_payload["attachments_count"] = max(int(procurement_payload.get("attachments_count") or 0), len(attachment_names))
    procurement_payload["available_attachments_count"] = max(
        int(procurement_payload.get("available_attachments_count") or 0),
        len(attachment_names),
    )
    procurement_payload["attachments_status"] = "downloaded" if saved_count > 0 else procurement_payload.get("attachments_status")
    existing_note = str(procurement_payload.get("source_note") or "").strip()
    if source_note not in existing_note:
        procurement_payload["source_note"] = f"{existing_note} {source_note}".strip()
    metadata["procurement"] = procurement_payload

    save_demo_run_metadata(run_id, metadata)
    _write_procurement_artifacts(
        run_id,
        selected=ProcurementSearchResult.model_validate(procurement_payload),
        manifest=manifest,
    )
    append_demo_run_event(
        run_id,
        event_label,
        "Дополнительные документы закупки найдены и обработаны.",
        {"attachments_found": len(attachments), "saved_files": saved_count, "skipped_files": len(download_result.skipped), "source": source_type},
    )
    return saved_count


def _supplement_run_with_public_notice_attachments(run_id: str, source_url: str | None) -> int:
    if not source_url:
        return 0
    attachments = _fetch_public_notice_attachments(source_url)
    return _supplement_run_with_discovered_attachments(
        run_id,
        attachments=attachments,
        source_type="public_notice_documents",
        event_label="public_documents_loaded",
        source_note="Публичная вкладка documents.html использована как fallback для скачивания вложений.",
    )


def _supplement_run_with_notice_xml_attachments(run_id: str) -> int:
    metadata = load_demo_run_metadata(run_id)
    attachments = _discover_notice_xml_attachments(run_id, metadata)
    return _supplement_run_with_discovered_attachments(
        run_id,
        attachments=attachments,
        source_type="notice_xml_attachments",
        event_label="notice_xml_documents_loaded",
        source_note="Ссылки на вложения извлечены из epNotification XML.",
    )


def _apply_document_set_completeness(run_id: str) -> dict[str, Any]:
    metadata = load_demo_run_metadata(run_id)
    summary = apply_document_set_summary(metadata)
    limitation = (
        "Комплект документов неполон: до анализа необходимо получить проект "
        "контракта и техническое задание или описание объекта закупки."
    )
    limitations = list(metadata.get("limitations") or [])
    if summary["analysis_allowed"]:
        if metadata.get("files"):
            metadata["status"] = TenderOperatorUploadedRunStatus.READY_TO_ANALYZE.value
            metadata["attachments_status"] = "downloaded"
            metadata["manual_upload_required"] = False
        limitations = [item for item in limitations if item != limitation]
    elif summary["physical_file_count"] == 0:
        metadata["status"] = TenderOperatorUploadedRunStatus.DOCS_REQUIRED.value
        metadata["attachments_status"] = "manual_upload_required"
        metadata["manual_upload_required"] = True
        metadata["analysis_status"] = "not_started"
    else:
        metadata["status"] = TenderOperatorUploadedRunStatus.DOCS_REQUIRED.value
        metadata["attachments_status"] = "incomplete_document_set"
        metadata["manual_upload_required"] = True
        metadata["analysis_status"] = "not_started"
        if limitation not in limitations:
            limitations.append(limitation)
    metadata["limitations"] = limitations
    save_demo_run_metadata(run_id, metadata)
    append_demo_run_event(
        run_id,
        "document_set_checked",
        (
            "Комплект документов достаточен для запуска анализа."
            if summary["analysis_allowed"]
            else "Комплект документов неполон; автоматический анализ заблокирован."
        ),
        {
            "document_set_status": summary["status"],
            "physical_file_count": summary["physical_file_count"],
            "logical_document_count": summary["logical_document_count"],
            "missing_required_document_kinds": summary[
                "missing_required_document_kinds"
            ],
        },
    )
    return summary


def _apply_search_result_context(
    run_id: str,
    request: tender_schemas.SearchResultHandoffRequest,
) -> None:
    metadata = load_demo_run_metadata(run_id)
    procurement_payload = dict(metadata.get("procurement") or {})
    source_url = (
        (request.source_url or "").strip()
        or procurement_payload.get("source_url")
        or metadata.get("procurement_url")
        or "https://zakupki.gov.ru/"
    )
    page_context = (
        _extract_public_page_context(source_url)
        if source_url and not (request.title or "").strip() and not (request.customer_name or "").strip()
        else {}
    )

    title = (
        (request.title or "").strip()
        or page_context.get("title")
        or procurement_payload.get("title")
        or metadata.get("tender_title")
    )
    customer_name = (
        (request.customer_name or "").strip()
        or page_context.get("customer_name")
        or procurement_payload.get("customer_name")
        or metadata.get("customer_name")
    )

    metadata["tender_title"] = title
    metadata["customer_name"] = customer_name
    metadata["procurement_url"] = source_url
    metadata["publication_date"] = request.publication_date or page_context.get("publication_date") or metadata.get("publication_date")
    metadata["updated_date"] = page_context.get("updated_date") or metadata.get("updated_date")
    metadata["deadline"] = request.deadline or page_context.get("deadline") or metadata.get("deadline")

    procurement_payload["title"] = title
    procurement_payload["customer_name"] = customer_name
    procurement_payload["source_url"] = source_url
    procurement_payload["procurement_number"] = procurement_payload.get("procurement_number") or request.reestr_number.strip()
    procurement_payload["procurement_id"] = procurement_payload.get("procurement_id") or request.reestr_number.strip()
    procurement_payload["source"] = procurement_payload.get("source") or "zakupki_gov_ru_getdocs_ip"
    procurement_payload["publication_date"] = request.publication_date or page_context.get("publication_date") or procurement_payload.get("publication_date")
    procurement_payload["updated_date"] = page_context.get("updated_date") or procurement_payload.get("updated_date")
    procurement_payload["deadline"] = request.deadline or page_context.get("deadline") or procurement_payload.get("deadline")
    procurement_payload["initial_price"] = request.initial_price if request.initial_price is not None else (page_context.get("initial_price") or procurement_payload.get("initial_price"))
    procurement_payload["currency"] = request.currency or page_context.get("currency") or procurement_payload.get("currency")
    procurement_payload["status"] = request.status or procurement_payload.get("status")
    procurement_payload["procedure_type"] = request.procedure_type or procurement_payload.get("procedure_type")
    procurement_payload["structured_source_label"] = "карточка ЕИС"
    metadata["procurement"] = procurement_payload

    save_demo_run_metadata(run_id, metadata)

    manifest_path = get_demo_run_procurement_dir(run_id) / "attachments_manifest.json"
    manifest_payload = []
    if manifest_path.is_file():
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = [ProcurementAttachmentManifestItem.model_validate(item) for item in manifest_payload]
    _write_procurement_artifacts(
        run_id,
        selected=ProcurementSearchResult.model_validate(procurement_payload),
        manifest=manifest,
    )


def _is_retryable_archive_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "http 404",
            "http 409",
            "http 423",
            "http 425",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "timed out",
            "temporarily unavailable",
            "connection reset",
        )
    )


def _download_archive_with_retry(
    client: ZakupkiSoapClient,
    archive_urls: list[str],
    target_dir: Path,
) -> tuple[Any | None, str, list[str], int]:
    warnings: list[str] = []
    if not archive_urls:
        return None, "no_archive_url", warnings, 0

    attempts_made = 0
    for attempt in range(1, ARCHIVE_DOWNLOAD_RETRY_ATTEMPTS + 1):
        attempts_made = attempt
        for archive_url in archive_urls:
            try:
                downloaded = client.download_archive(archive_url, target_dir)
            except RuntimeError as exc:
                message = str(exc)
                warnings.append(f"Попытка {attempt}: {message}")
                if not _is_retryable_archive_error(message):
                    return None, "download_error", warnings, attempts_made
                continue
            return downloaded, "downloaded", warnings, attempts_made
        if attempt < ARCHIVE_DOWNLOAD_RETRY_ATTEMPTS:
            time.sleep(ARCHIVE_DOWNLOAD_RETRY_DELAY_SECONDS)
    return None, "archive_not_ready", warnings, attempts_made


def _extract_safe_archive_into_run(run_id: str, archive_path: Path) -> tuple[list[dict[str, Any]], list[ProcurementAttachmentManifestItem], int]:
    files: list[dict[str, Any]] = []
    manifest: list[ProcurementAttachmentManifestItem] = []
    extracted_count = 0
    total_entries = 0
    total_unpacked = 0
    max_nested_depth = 2
    max_compression_ratio = 200
    extraction_complete = True
    extracted_dir = get_demo_run_input_dir(run_id) / "extracted"
    extracted_dir.mkdir(parents=True, exist_ok=True)

    def skipped(name: str, extension: str, note: str) -> None:
        manifest.append(
            ProcurementAttachmentManifestItem(
                name=name,
                stored_name=None,
                extension=extension,
                status="skipped",
                note=note,
            )
        )

    def walk(current: zipfile.ZipFile, *, archive_label: str, depth: int) -> bool:
        nonlocal extraction_complete, extracted_count, total_entries, total_unpacked
        members = [item for item in current.infolist() if not item.is_dir()]
        total_entries += len(members)
        if total_entries > MAX_ZIP_ENTRY_COUNT:
            extraction_complete = False
            skipped(
                archive_label,
                ".zip",
                f"ZIP archives contain too many entries. Limit: {MAX_ZIP_ENTRY_COUNT}.",
            )
            return False

        for item in members:
            entry_path = Path(item.filename)
            if entry_path.is_absolute() or ".." in entry_path.parts:
                skipped(
                    item.filename,
                    entry_path.suffix.lower(),
                    "ZIP entry was rejected because it contains an unsafe path.",
                )
                continue

            entry_name = entry_path.name
            ext = entry_path.suffix.lower()
            if item.flag_bits & 0x1:
                extraction_complete = False
                skipped(entry_name, ext, "Encrypted ZIP entries are not supported.")
                continue
            projected_total = total_unpacked + max(int(item.file_size), 0)
            if projected_total > MAX_ZIP_TOTAL_BYTES:
                extraction_complete = False
                skipped(
                    archive_label,
                    ".zip",
                    "ZIP archives exceed the safe cumulative unpacked size limit.",
                )
                return False
            if (
                item.compress_size > 0
                and item.file_size / item.compress_size > max_compression_ratio
            ):
                extraction_complete = False
                skipped(
                    entry_name,
                    ext,
                    "ZIP entry exceeds the safe compression-ratio limit.",
                )
                continue
            raw = current.read(item)
            total_unpacked += len(raw)

            if ext == ".zip":
                if depth >= max_nested_depth:
                    extraction_complete = False
                    skipped(
                        entry_name,
                        ext,
                        f"Nested ZIP depth exceeds the safe limit: {max_nested_depth}.",
                    )
                    continue
                try:
                    with zipfile.ZipFile(io.BytesIO(raw)) as nested:
                        if not walk(
                            nested,
                            archive_label=f"{archive_label}/{entry_name}",
                            depth=depth + 1,
                        ):
                            return False
                except zipfile.BadZipFile:
                    extraction_complete = False
                    skipped(entry_name, ext, "Nested ZIP archive could not be read safely.")
                continue

            if ext not in ALLOWED_EXTENSIONS:
                skipped(entry_name, ext, "Формат вложения не входит в allowlist.")
                continue

            original_name, stored_name = sanitize_demo_filename(
                entry_name,
                extracted_count + 1,
            )
            relative_stored_name = str(Path("extracted") / stored_name)
            (extracted_dir / stored_name).write_bytes(raw)
            extracted_count += 1
            role_hint = _role_hint_from_procurement_attachment(original_name)
            document_kind = _document_kind_from_role_hint(role_hint)
            files.append(
                build_demo_file_descriptor(
                    file_id=f"FILE-{extracted_count:02d}",
                    original_name=original_name,
                    stored_name=relative_stored_name,
                    role_hint=role_hint,
                    size_bytes=len(raw),
                    content_type="application/octet-stream",
                    source="eis_getdocs_archive",
                    source_type="getdocs_archive",
                    document_kind=document_kind,
                    parent_archive=archive_label,
                )
            )
            manifest.append(
                ProcurementAttachmentManifestItem(
                    name=original_name,
                    stored_name=relative_stored_name,
                    extension=ext,
                    status="saved",
                    note="Файл извлечён из getDocsIP архива в безопасном локальном режиме.",
                    source_type="getdocs_archive",
                    document_kind=document_kind,
                    content_type="application/octet-stream",
                    size_bytes=len(raw),
                )
            )
        return True

    try:
        with zipfile.ZipFile(archive_path) as archive:
            walk(archive, archive_label=archive_path.name, depth=0)
    except zipfile.BadZipFile:
        extraction_complete = False
        skipped(archive_path.name, ".zip", "ZIP archive could not be read safely.")
    if not extraction_complete:
        for file_item in files:
            stored_name = file_item.get("stored_name")
            if stored_name:
                (get_demo_run_input_dir(run_id) / stored_name).unlink(missing_ok=True)
        files = []
        extracted_count = 0
    return files, manifest, extracted_count


def _write_eis_procurement_artifacts(
    run_id: str,
    *,
    request,
    archive_result: DocsArchiveResult,
    archive_summary: dict[str, Any],
    archive_manifest: list[ProcurementAttachmentManifestItem],
) -> None:
    procurement_dir = get_demo_run_procurement_dir(run_id)
    payload = {
        "source": "zakupki_gov_ru_getdocs_ip",
        "token_owner": "individual",
        "law": request.law,
        "reestr_number": request.reestr_number,
        "subsystem_type": request.subsystem_type,
        "soap_method": request.method,
        "request_id": archive_result.request_id,
        "ref_id": archive_result.ref_id,
        "status": archive_result.status,
        "archive_url_present": bool(archive_result.archive_url or archive_result.archive_urls),
        "archive_urls_count": len(archive_result.archive_urls),
        "archive_name": archive_result.archive_name,
        "archive_summary": archive_summary,
        "warnings": archive_result.warnings,
        "safe_diagnostic": archive_result.safe_diagnostic,
    }
    (procurement_dir / "eis_getdocs_metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (procurement_dir / "archive_manifest.json").write_text(
        json.dumps([item.model_dump(mode="json") for item in archive_manifest], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def analyze_eis_archive_run(run_id: str):
    metadata = load_demo_run_metadata(run_id)
    if metadata.get("procurement_source") != "zakupki_gov_ru_getdocs_ip":
        raise HTTPException(status_code=400, detail="Run is not an EIS getDocsIP intake run")
    summary = _apply_document_set_completeness(run_id)
    if not summary["analysis_allowed"]:
        raise HTTPException(
            status_code=409,
            detail=(
                "Комплект документов неполон. До анализа получите проект "
                "контракта и техническое задание или описание объекта закупки."
            ),
        )
    return analyze_uploaded_demo_run(run_id)


def create_run_from_procurement(request: ProcurementRunCreateRequest) -> ProcurementRunResponse:
    descriptor = _find_descriptor(request.source)
    if descriptor is None:
        raise HTTPException(status_code=400, detail=f"Unknown procurement source: {request.source}")
    if not descriptor.enabled:
        raise HTTPException(status_code=400, detail=descriptor.note or "Source is disabled in demo mode")

    demo_procurement = get_demo_procurement(request.source, request.procurement_id)
    details: ProcurementDetails | None = None
    if demo_procurement is None:
        try:
            details = get_procurement_details(request.source, request.procurement_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        selected = _legacy_result_from_details(details)
        search_results_count = 1
    else:
        search_snapshot = search_procurements(query=request.query or "", source=request.source, max_results=10)
        selected = next((item for item in search_snapshot.results if item.procurement_id == request.procurement_id), None)
        if selected is None:
            selected = ProcurementSearchResult(
                procurement_id=demo_procurement.procurement_id,
                source=demo_procurement.source,
                title=demo_procurement.title,
                procurement_number=demo_procurement.procurement_number,
                customer_name=demo_procurement.customer_name,
                category=demo_procurement.category,
                publication_date=demo_procurement.publication_date,
                deadline=demo_procurement.deadline,
                initial_price=demo_procurement.initial_price,
                currency=demo_procurement.currency,
                region=demo_procurement.region,
                source_url=demo_procurement.source_url,
                attachments_status=demo_procurement.attachments_status,
                attachments_count=len(demo_procurement.attachments),
                available_attachments_count=len(demo_procurement.attachments),
                summary=demo_procurement.summary,
                attachment_names=[item.name for item in demo_procurement.attachments],
                source_note=demo_procurement.source_note,
            )
        search_results_count = len(search_snapshot.results)

    run_id = make_demo_run_id()
    ensure_demo_run_structure(run_id, exist_ok=False)
    created_at = _now_iso()

    append_demo_run_event(
        run_id,
        "procurement_search_started",
        "Запущен поиск закупки в безопасном read-only режиме.",
        {"query": request.query or "", "source": request.source},
    )
    append_demo_run_event(
        run_id,
        "procurement_search_completed",
        "Поиск закупки завершён, результаты получены из безопасного read-only источника.",
        {"results_found": search_results_count, "source": request.source},
    )
    append_demo_run_event(
        run_id,
        "procurement_selected",
        "Оператор выбрал закупку из результатов поиска.",
        {"procurement_id": selected.procurement_id, "title": selected.title},
    )
    append_demo_run_event(
        run_id,
        "procurement_details_loaded",
        "Детали выбранной закупки загружены в локальный demo-run.",
        {
            "procurement_id": selected.procurement_id,
            "notice_number": selected.procurement_number,
            "source_url": selected.source_url,
        },
    )
    attachments_expected = len(demo_procurement.attachments) if demo_procurement else len(details.attachments if details else [])
    append_demo_run_event(
        run_id,
        "attachments_list_loaded",
        "Список документации закупки получен для безопасного intake.",
        {"attachments_expected": attachments_expected, "attachments_status": selected.attachments_status},
    )

    files: list[dict[str, Any]] = []
    manifest: list[ProcurementAttachmentManifestItem] = []

    if demo_procurement and demo_procurement.attachments and request.download_attachments:
        append_demo_run_event(
            run_id,
            "attachments_download_started",
            "Начато безопасное получение публично доступной документации.",
            {"attachments_expected": len(demo_procurement.attachments)},
        )
        for index, attachment in enumerate(demo_procurement.attachments, start=1):
            original_name, stored_name = sanitize_demo_filename(attachment.name, index)
            target = get_demo_run_input_dir(run_id) / stored_name
            target.write_bytes(attachment.payload)
            files.append(
                build_demo_file_descriptor(
                    file_id=f"FILE-{index:02d}",
                    original_name=original_name,
                    stored_name=stored_name,
                    size_bytes=len(attachment.payload),
                    content_type=attachment.content_type,
                    source="procurement_download",
                )
            )
            manifest.append(
                ProcurementAttachmentManifestItem(
                    name=original_name,
                    stored_name=stored_name,
                    extension=Path(stored_name).suffix.lower(),
                    status="saved",
                    note="Файл сохранён в локальную demo-run директорию.",
                )
            )
            append_demo_run_event(
                run_id,
                "attachment_saved",
                f"Документация '{original_name}' сохранена локально.",
                {"stored_name": stored_name},
            )
        append_demo_run_event(
            run_id,
            "attachments_download_completed",
            "Документация сохранена локально и готова к анализу.",
            {"saved_files": len(files)},
        )
        status = TenderOperatorUploadedRunStatus.READY_TO_ANALYZE
    elif details and details.attachments and request.download_attachments:
        append_demo_run_event(
            run_id,
            "attachments_download_started",
            "Начато безопасное получение публично доступной документации.",
            {"attachments_expected": len(details.attachments), "source": request.source},
        )
        settings = get_zakupki_soap_settings()
        download_limit = settings.max_download_mb * 1024 * 1024
        download_result = download_procurement_attachments(
            details.attachments,
            target_dir=get_demo_run_input_dir(run_id),
            max_attachments=settings.max_attachments,
            max_file_size_bytes=download_limit,
            max_total_size_bytes=download_limit,
        )
        for index, item in enumerate(download_result.saved, start=1):
            if not item.stored_name:
                continue
            files.append(
                build_demo_file_descriptor(
                    file_id=f"FILE-{index:02d}",
                    original_name=item.name,
                    stored_name=item.stored_name,
                    size_bytes=item.size_bytes,
                    content_type="application/octet-stream",
                    source="procurement_download",
                )
            )
        for item in download_result.manifest:
            manifest_item = ProcurementAttachmentManifestItem(
                name=item.name,
                stored_name=item.stored_name,
                extension=item.extension,
                status=item.status,
                note=item.note,
            )
            manifest.append(manifest_item)
            append_demo_run_event(
                run_id,
                "attachment_saved" if item.status == "saved" else "attachment_skipped",
                (
                    f"Документация '{item.name}' сохранена локально."
                    if item.status == "saved"
                    else f"Документация '{item.name}' пропущена: {item.note or 'причина не указана'}"
                ),
                {"stored_name": item.stored_name, "status": item.status},
            )
        append_demo_run_event(
            run_id,
            "attachments_download_completed",
            "Получение документации завершено.",
            {"saved_files": len(files), "skipped_files": len(download_result.skipped)},
        )
        status = TenderOperatorUploadedRunStatus.READY_TO_ANALYZE if files else TenderOperatorUploadedRunStatus.DOCS_REQUIRED
    else:
        reason = (
            "Оператор создал run без автоматического скачивания документации."
            if not request.download_attachments
            else "Автоматическое получение документации недоступно. Загрузите документы вручную."
        )
        manifest.append(
            ProcurementAttachmentManifestItem(
                name="documentation",
                stored_name=None,
                extension="",
                status="manual_upload_required",
                note=reason,
            )
        )
        append_demo_run_event(
            run_id,
            "manual_upload_required",
            "Автоматическое получение документации недоступно. Нужна ручная загрузка документов.",
            {"attachments_status": selected.attachments_status, "download_attachments": request.download_attachments},
        )
        status = TenderOperatorUploadedRunStatus.DOCS_REQUIRED

    manual_upload_required = status == TenderOperatorUploadedRunStatus.DOCS_REQUIRED
    attachments_status = "downloaded" if files else "manual_upload_required"

    metadata = {
        "run_id": run_id,
        "created_at": created_at,
        "mode": "procurement_search_intake",
        "tender_title": selected.title,
        "tender_category": selected.category,
        "customer_name": selected.customer_name,
        "notes": request.query or None,
        "status": status.value,
        "analysis_mode": "not_started",
        "procurement_source": selected.source,
        "procurement_id": selected.procurement_id,
        "procurement_url": selected.source_url,
        "procurement_query": request.query or "",
        "publication_date": selected.publication_date,
        "deadline": selected.deadline,
        "attachments_status": attachments_status,
        "downloaded_files_count": len(files),
        "manual_upload_required": manual_upload_required,
        "source": selected.source,
        "notice_number": selected.procurement_number,
        "law": selected.category if selected.category in {"44-ФЗ", "223-ФЗ"} else None,
        "files": files,
        "warnings": [],
        "limitations": [
            "Поиск работает только в безопасном read-only режиме.",
            "Без авторизации, без обхода captcha, без внешних действий на площадках.",
            "Подача заявки, письма поставщикам и ЭЦП остаются вне этого demo-контура.",
        ],
        "human_in_the_loop": True,
        "external_actions": False,
        "no_platform_submission": True,
        "no_email_sending": True,
        "no_digital_signature": True,
        "procurement": selected.model_dump(mode="json"),
    }
    save_demo_run_metadata(run_id, metadata)

    _write_procurement_artifacts(run_id, selected=selected, manifest=manifest)
    append_demo_run_event(
        run_id,
        "run_created_from_procurement",
        "Создан run из выбранной закупки.",
        {"mode": "procurement_search_intake", "file_count": len(files)},
    )

    return ProcurementRunResponse(
        run_id=run_id,
        status=status,
        created_at=datetime.fromisoformat(created_at),
        file_count=len(files),
        run_url=f"/demo/tender-agent?run_id={run_id}",
        report_url=f"/demo/tender-agent/runs/{run_id}/report",
        downloaded_files_count=len(files),
        manual_upload_required=manual_upload_required,
        warnings=[],
        limitations=metadata["limitations"],
        attachments_status=metadata["attachments_status"],
        procurement=selected,
        attachments=manifest,
    )


def create_run_from_eis_docs_archive(request: EisDocsArchiveRunRequest) -> ProcurementRunResponse:
    if request.law.lower() != "44fz":
        raise HTTPException(status_code=400, detail="getDocsIP intake currently supports only 44fz in this demo contour")
    if request.method != "getDocsByReestrNumber":
        raise HTTPException(status_code=400, detail="Only getDocsByReestrNumber is supported for this EIS intake route")

    settings = get_zakupki_soap_settings()
    if not settings.configured:
        raise HTTPException(status_code=400, detail="Источник ЕИС не настроен: добавьте токен сервиса ЕИС в .env.local")

    client = ZakupkiSoapClient(settings)
    archive_result = client.get_docs_by_reestr_number(request.reestr_number, subsystem_type=request.subsystem_type)
    selected = _build_getdocs_result(request.reestr_number, archive_result)
    archive_summary = _sanitize_archive_url_summary(archive_result.archive_url)

    run_id = make_demo_run_id()
    ensure_demo_run_structure(run_id, exist_ok=False)
    created_at = _now_iso()

    append_demo_run_event(
        run_id,
        "eis_getdocs_started",
        "Запрошена документация по номеру закупки через ЕИС getDocsIP.",
        {
            "reestr_number": request.reestr_number,
            "subsystem_type": request.subsystem_type,
            "soap_method": request.method,
        },
    )

    manifest: list[ProcurementAttachmentManifestItem] = []
    files: list[dict[str, Any]] = []
    archive_downloaded = False
    archive_download_status = "not_requested"
    archive_download_attempts = 0
    documents_extracted_count = 0
    archive_extraction_complete: bool | None = None

    archive_urls = archive_result.archive_urls or ([archive_result.archive_url] if archive_result.archive_url else [])
    if archive_urls:
        append_demo_run_event(
            run_id,
            "eis_archive_url_received",
            "ЕИС вернула ссылку на архив документации.",
            {
                "archive_url_present": True,
                "archive_urls_count": len(archive_urls),
                "archive_source_host": archive_summary.get("host"),
                "archive_source_path": archive_summary.get("path"),
                "archive_has_query": archive_summary.get("has_query"),
                "ref_id": archive_result.ref_id,
            },
        )

    if archive_urls and request.download_archive:
        append_demo_run_event(
            run_id,
            "eis_archive_download_started",
            "Начато безопасное скачивание архива документации ЕИС.",
            {
                "archive_source_host": archive_summary.get("host"),
                "archive_source_path": archive_summary.get("path"),
            },
        )
        downloaded, archive_download_status, retry_warnings, archive_download_attempts = _download_archive_with_retry(
            client,
            archive_urls,
            get_demo_run_input_dir(run_id),
        )
        archive_result.warnings.extend(retry_warnings)
        if downloaded is None:
            note = (
                "Архив документации найден, но ещё формируется в ЕИС. Повторите попытку позже или загрузите документы вручную."
                if archive_download_status == "archive_not_ready"
                else "Архив документации не удалось скачать автоматически. Продолжение возможно после ручной загрузки документов."
            )
            manifest.append(
                ProcurementAttachmentManifestItem(
                    name=archive_result.archive_name or "documentation-archive.zip",
                    stored_name=None,
                    extension=".zip",
                    status="manual_upload_required",
                    note=note,
                )
            )
            append_demo_run_event(
                run_id,
                "eis_archive_not_ready" if archive_download_status == "archive_not_ready" else "manual_upload_required",
                (
                    "Архив документации ещё формируется в ЕИС. Повторите попытку позже или загрузите документы вручную."
                    if archive_download_status == "archive_not_ready"
                    else "Архив документации не удалось скачать автоматически. Продолжение возможно после ручной загрузки документов."
                ),
                {
                    "getdocs_status": archive_result.status,
                    "archive_url_present": bool(archive_urls),
                    "archive_download_status": archive_download_status,
                    "archive_download_attempts": archive_download_attempts,
                    "archive_source_host": archive_summary.get("host"),
                    "archive_source_path": archive_summary.get("path"),
                },
            )
            status = TenderOperatorUploadedRunStatus.DOCS_REQUIRED
        else:
            archive_downloaded = True
            append_demo_run_event(
                run_id,
                "eis_archive_downloaded",
                "Архив документации ЕИС сохранён локально.",
                {"stored_name": downloaded.stored_name, "size_bytes": downloaded.size_bytes},
            )
            manifest.append(
                ProcurementAttachmentManifestItem(
                    name=downloaded.file_name,
                    stored_name=downloaded.stored_name,
                    extension=".zip",
                    status="saved",
                    note="Архив документации скачан через getDocsIP в read-only режиме.",
                )
            )
            extracted_files, extracted_manifest, documents_extracted_count = _extract_safe_archive_into_run(
                run_id,
                get_demo_run_input_dir(run_id) / downloaded.stored_name,
            )
            files.extend(extracted_files)
            manifest.extend(extracted_manifest)
            archive_extraction_complete = not any(
                item.status == "skipped" and item.extension == ".zip"
                for item in extracted_manifest
            )
            append_demo_run_event(
                run_id,
                "eis_archive_extracted",
                "Архив документации получен и безопасно распакован.",
                {
                    "documents_extracted_count": documents_extracted_count,
                    "archive_downloaded": True,
                    "archive_download_attempts": archive_download_attempts,
                },
            )
            status = TenderOperatorUploadedRunStatus.READY_TO_ANALYZE if files else TenderOperatorUploadedRunStatus.DOCS_REQUIRED
            if status == TenderOperatorUploadedRunStatus.READY_TO_ANALYZE:
                append_demo_run_event(
                    run_id,
                    "analysis_ready",
                    "Документы распакованы и готовы к запуску анализа.",
                    {"documents_extracted_count": documents_extracted_count},
                )
    else:
        note = (
            "Архив документации не получен из ответа getDocsIP. Нужна ручная загрузка документов."
            if not archive_urls
            else "Архив не скачивался автоматически по параметрам запроса."
        )
        manifest.append(
            ProcurementAttachmentManifestItem(
                name="documentation-archive",
                stored_name=None,
                extension=".zip",
                status="manual_upload_required",
                note=note,
            )
        )
        append_demo_run_event(
            run_id,
            "manual_upload_required",
            "Архив документации не получен автоматически. Продолжение возможно после ручной загрузки документов.",
            {"getdocs_status": archive_result.status, "archive_url_present": bool(archive_urls)},
        )
        status = TenderOperatorUploadedRunStatus.DOCS_REQUIRED

    metadata = {
        "run_id": run_id,
        "created_at": created_at,
        "mode": "procurement_search_intake",
        "tender_title": selected.title,
        "tender_category": "44-ФЗ",
        "customer_name": selected.customer_name or "Не указан",
        "notes": None,
        "status": status.value,
        "analysis_mode": "not_started",
        "procurement_source": "zakupki_gov_ru_getdocs_ip",
        "procurement_id": request.reestr_number,
        "procurement_url": "https://zakupki.gov.ru/",
        "procurement_query": request.reestr_number,
        "publication_date": selected.publication_date,
        "updated_date": None,
        "deadline": selected.deadline,
        "attachments_status": "downloaded" if files else "manual_upload_required",
        "downloaded_files_count": len(files),
        "manual_upload_required": status == TenderOperatorUploadedRunStatus.DOCS_REQUIRED,
        "source": "zakupki_gov_ru_getdocs_ip",
        "notice_number": request.reestr_number,
        "law": "44-ФЗ",
        "files": files,
        "warnings": list(archive_result.warnings),
        "limitations": [
            "getDocsIP используется только для read-only получения документации по номеру закупки.",
            "Поиск закупки и получение документации разделены: keyword search не идёт через getDocsIP.",
            "Без логина, без обхода captcha, без подачи заявки, без писем и без ЭЦП.",
        ],
        "human_in_the_loop": True,
        "external_actions": False,
        "no_platform_submission": True,
        "no_email_sending": True,
        "no_digital_signature": True,
        "procurement": selected.model_dump(mode="json"),
        "token_owner": settings.token_owner,
        "reestr_number": request.reestr_number,
        "subsystem_type": request.subsystem_type,
        "soap_method": request.method,
        "archive_url_present": bool(archive_urls),
        "archive_urls_count": len(archive_urls),
        "archive_name": archive_result.archive_name,
        "archive_source_host": archive_summary.get("host"),
        "archive_source_path": archive_summary.get("path"),
        "archive_downloaded": archive_downloaded,
        "archive_download_status": archive_download_status,
        "archive_download_attempts": archive_download_attempts,
        "documents_extracted_count": documents_extracted_count,
        "archive_extraction_complete": archive_extraction_complete,
        "getdocs_status": archive_result.status,
        "getdocs_request_id": archive_result.request_id,
        "getdocs_ref_id": archive_result.ref_id,
        "archive_safe_diagnostic": archive_result.safe_diagnostic,
        "analysis_status": "not_started",
        "requested_analyze_after_download": request.analyze_after_download,
    }
    save_demo_run_metadata(run_id, metadata)
    _write_eis_procurement_artifacts(
        run_id,
        request=request,
        archive_result=archive_result,
        archive_summary=archive_summary,
        archive_manifest=manifest,
    )
    _write_procurement_artifacts(run_id, selected=selected, manifest=manifest)
    _supplement_run_with_notice_xml_attachments(run_id)
    _apply_document_set_completeness(run_id)
    append_demo_run_event(
        run_id,
        "run_created_from_eis_archive",
        "Создан run из архива документации ЕИС getDocsIP.",
        {"mode": "procurement_search_intake", "file_count": len(files), "token_owner": settings.token_owner},
    )

    current_metadata = load_demo_run_metadata(run_id)
    final_status = status
    analysis_status = current_metadata["analysis_status"]
    current_status = current_metadata.get("status", status.value)
    if request.analyze_after_download and current_status == TenderOperatorUploadedRunStatus.READY_TO_ANALYZE.value:
        analysis_result = analyze_eis_archive_run(run_id)
        final_status = analysis_result.status
        analysis_status = analysis_result.status.value
    else:
        final_status = TenderOperatorUploadedRunStatus(current_status)

    return ProcurementRunResponse(
        run_id=run_id,
        status=final_status,
        created_at=datetime.fromisoformat(created_at),
        file_count=len(current_metadata.get("files", [])),
        run_url=f"/demo/tender-agent/runs/{run_id}",
        report_url=f"/demo/tender-agent/runs/{run_id}/report",
        downloaded_files_count=int(current_metadata.get("downloaded_files_count", len(current_metadata.get("files", [])))),
        manual_upload_required=bool(current_metadata.get("manual_upload_required", final_status == TenderOperatorUploadedRunStatus.DOCS_REQUIRED)),
        warnings=list(current_metadata.get("warnings", archive_result.warnings)),
        limitations=list(current_metadata.get("limitations", metadata["limitations"])),
        attachments_status=str(current_metadata.get("attachments_status", metadata["attachments_status"])),
        procurement=selected,
        attachments=manifest,
        archive_url_present=bool(archive_urls),
        archive_downloaded=archive_downloaded,
        archive_download_status=archive_download_status,
        archive_download_attempts=archive_download_attempts,
        documents_extracted_count=documents_extracted_count,
        analysis_status=analysis_status,
        soap_method=request.method,
        ref_id=archive_result.ref_id,
        archive_source_host=archive_summary.get("host"),
        archive_source_path=archive_summary.get("path"),
    )


def create_run_from_search_result(
    request: tender_schemas.SearchResultHandoffRequest,
) -> tender_schemas.SearchResultHandoffResponse:
    if not request.reestr_number or not request.reestr_number.strip():
        raise HTTPException(status_code=400, detail="reestr_number обязателен для запуска getDocsIP.")
    normalized_law = _normalize_search_result_law(request)
    settings = get_zakupki_soap_settings()
    use_getdocs = normalized_law == "44fz" and settings.configured

    if not use_getdocs:
        return _create_public_html_run_from_search_result(request, normalized_law=normalized_law)

    eis_request = EisDocsArchiveRunRequest(
        reestr_number=request.reestr_number.strip(),
        law="44fz",
        subsystem_type="PRIZ",
        method="getDocsByReestrNumber",
        download_archive=request.download_archive,
        analyze_after_download=False,
    )
    result = create_run_from_eis_docs_archive(eis_request)
    _apply_search_result_context(result.run_id, request)
    _supplement_run_with_public_notice_attachments(result.run_id, request.source_url)
    _apply_document_set_completeness(result.run_id)

    status = result.status.value if hasattr(result.status, "value") else str(result.status)
    analysis_status = result.analysis_status
    current_metadata = load_demo_run_metadata(result.run_id)
    current_status = current_metadata.get("status", status)
    if request.analyze_after_download and current_status == TenderOperatorUploadedRunStatus.READY_TO_ANALYZE.value:
        analysis_result = analyze_eis_archive_run(result.run_id)
        status = analysis_result.status.value
        analysis_status = analysis_result.status.value
    else:
        status = current_status

    return tender_schemas.SearchResultHandoffResponse(
        run_id=result.run_id,
        status=status,
        archive_url_present=result.archive_url_present,
        archive_downloaded=result.archive_downloaded,
        documents_extracted_count=result.documents_extracted_count,
        analysis_status=analysis_status,
        run_url=result.run_url,
        report_url=result.report_url,
        warnings=list(current_metadata.get("warnings", result.warnings)),
    )


def get_procurement_for_run(run_id: str) -> ProcurementRunDetailsResponse:
    metadata = load_demo_run_metadata(run_id)
    procurement_payload = metadata.get("procurement")
    if not procurement_payload:
        raise HTTPException(status_code=404, detail="Procurement metadata is not available for this run")

    manifest_path = get_demo_run_procurement_dir(run_id) / "attachments_manifest.json"
    manifest = []
    if manifest_path.is_file():
        manifest = [ProcurementAttachmentManifestItem.model_validate(item) for item in json.loads(manifest_path.read_text(encoding="utf-8"))]

    return ProcurementRunDetailsResponse(
        run_id=run_id,
        procurement=ProcurementSearchResult.model_validate(procurement_payload),
        attachments_status=metadata.get("attachments_status", "unknown"),
        attachments=manifest,
        events=load_demo_run_events(run_id),
    )
