from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import (
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
    getproxies_environment,
)
from uuid import uuid4
from xml.etree import ElementTree as ET

from src.modules.tender_operator_agent_demo.procurement_schemas import (
    DocsArchiveResult,
    DownloadedAttachment,
    ProcurementAttachment,
    ProcurementDetails,
    ProcurementSearchRequest,
    ProcurementSearchResult,
)
from src.modules.tender_operator_agent_demo.settings import ZakupkiSoapSettings
from src.modules.tender_operator_agent_demo.zakupki_soap_templates import (
    build_attachments_envelope,
    build_details_envelope,
    build_get_docs_by_org_region_envelope,
    build_get_docs_by_reestr_number_envelope,
    build_get_nsi_envelope,
    build_search_envelope,
)
from src.shared.network.etp_trust import TrustPolicy, resolve_host_policy
from src.shared.network.http_client import create_urllib_context

SoapTransport = Callable[[str, str | None, int], str]
HttpTransport = Callable[[str, dict[str, str], int, int], tuple[bytes, str | None]]
MAX_XML_CHARS = 5_000_000


class ZakupkiSoapClient:
    def __init__(
        self,
        settings: ZakupkiSoapSettings,
        transport: SoapTransport | None = None,
        http_transport: HttpTransport | None = None,
        *,
        trust_policy: TrustPolicy | None = None,
        diagnostics_dir: Path | None = None,
        runtime_status_enabled: bool = True,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._http_transport = http_transport
        self._trust_policy = trust_policy
        self._diagnostics_dir = diagnostics_dir
        self._runtime_status_enabled = runtime_status_enabled

    def is_configured(self) -> bool:
        return self.settings.configured

    def search_procurements(
        self, request: ProcurementSearchRequest
    ) -> list[ProcurementSearchResult]:
        if not self.is_configured():
            return []
        envelope = build_search_envelope(request, self.settings.token)
        xml = self._post_soap(
            envelope,
            soap_action=self.settings.search_action,
            endpoint_url=self.settings.base_url,
            method_name="legacy_searchProcurements",
        )
        return parse_search_response(xml)[: self.settings.max_results]

    def get_procurement_details(self, procurement_id: str) -> ProcurementDetails:
        if not self.is_configured():
            raise RuntimeError(
                "Источник ЕИС не настроен для получения карточки закупки."
            )
        envelope = build_details_envelope(procurement_id, self.settings.token)
        xml = self._post_soap(
            envelope,
            soap_action=self.settings.details_action,
            endpoint_url=self.settings.base_url,
            method_name="legacy_getProcurementDetails",
        )
        return parse_details_response(xml)

    def list_attachments(self, procurement_id: str) -> list[ProcurementAttachment]:
        if not self.is_configured():
            return []
        envelope = build_attachments_envelope(procurement_id, self.settings.token)
        xml = self._post_soap(
            envelope,
            soap_action=self.settings.attachments_action,
            endpoint_url=self.settings.base_url,
            method_name="legacy_listAttachments",
        )
        return parse_attachments_response(xml)[: self.settings.max_attachments]

    def get_docs_by_reestr_number(
        self, reestr_number: str, subsystem_type: str = "PRIZ"
    ) -> DocsArchiveResult:
        if not self.is_configured():
            raise RuntimeError("Источник ЕИС не настроен для getDocsIP.")
        request_id, created_time = _request_meta()
        envelope = build_get_docs_by_reestr_number_envelope(
            token=self.settings.token,
            namespace=self.settings.individual_namespace,
            token_header_name=self.settings.token_header_name,
            request_id=request_id,
            created_time=created_time,
            mode=self.settings.mode,
            reestr_number=reestr_number,
            subsystem_type=subsystem_type,
        )
        xml = self._post_soap(
            envelope,
            soap_action=self.settings.soap_action_uri,
            endpoint_url=self.settings.individual_base_url,
            method_name="getDocsByReestrNumberRequest",
        )
        return parse_getdocs_response(
            xml,
            request_id=request_id,
            expected_response_tag="getDocsByReestrNumberResponse",
            expected_request_tag="getDocsByReestrNumberRequest",
        )

    def get_docs_by_org_region(
        self,
        org_region: str,
        exact_date: str,
        document_type44: str,
        subsystem_type: str = "PRIZ",
    ) -> DocsArchiveResult:
        if not self.is_configured():
            raise RuntimeError("Источник ЕИС не настроен для getDocsIP.")
        request_id, created_time = _request_meta()
        envelope = build_get_docs_by_org_region_envelope(
            token=self.settings.token,
            namespace=self.settings.individual_namespace,
            token_header_name=self.settings.token_header_name,
            request_id=request_id,
            created_time=created_time,
            mode=self.settings.mode,
            org_region=org_region,
            exact_date=exact_date,
            document_type44=document_type44,
            subsystem_type=subsystem_type,
        )
        xml = self._post_soap(
            envelope,
            soap_action=self.settings.soap_action_uri,
            endpoint_url=self.settings.individual_base_url,
            method_name="getDocsByOrgRegionRequest",
        )
        return parse_getdocs_response(
            xml,
            request_id=request_id,
            expected_response_tag="getDocsByOrgRegionResponse",
            expected_request_tag="getDocsByOrgRegionRequest",
        )

    def get_nsi(
        self, nsi_code44: str = "nsiAllList", nsi_kind: str = "all"
    ) -> DocsArchiveResult:
        if not self.is_configured():
            raise RuntimeError("Источник ЕИС не настроен для getDocsIP.")
        request_id, created_time = _request_meta()
        envelope = build_get_nsi_envelope(
            token=self.settings.token,
            namespace=self.settings.individual_namespace,
            token_header_name=self.settings.token_header_name,
            request_id=request_id,
            created_time=created_time,
            mode=self.settings.mode,
            nsi_code44=nsi_code44,
            nsi_kind=nsi_kind,
        )
        xml = self._post_soap(
            envelope,
            soap_action=self.settings.soap_action_uri,
            endpoint_url=self.settings.individual_base_url,
            method_name="getNsiRequest",
        )
        return parse_getdocs_response(
            xml,
            request_id=request_id,
            expected_response_tag="getNsiResponse",
            expected_request_tag="getNsiRequest",
        )

    def probe_xsd(self) -> dict[str, object]:
        if not self.is_configured():
            return {
                "status": "not_configured",
                "token_present": self.settings.token_configured,
            }
        try:
            payload, _content_type = self._http_get(
                self.settings.individual_xsd_url,
                headers={"User-Agent": "ai-corporation-tender-demo/1.0"},
                timeout=self.settings.timeout_seconds,
                max_bytes=512 * 1024,
            )
        except Exception as exc:  # noqa: BLE001
            message = _sanitize_error(str(exc), self.settings.token)
            self._write_runtime_status(
                last_status="error",
                last_error=message,
                soap_action="xsd_probe",
                endpoint_url=self.settings.individual_xsd_url,
                method_name="xsd_probe",
            )
            return {
                "status": "error",
                "token_present": self.settings.token_configured,
                "error": message,
            }

        text = payload.decode("utf-8", errors="replace")
        self._write_runtime_status(
            last_status="ok",
            soap_action="xsd_probe",
            endpoint_url=self.settings.individual_xsd_url,
            method_name="xsd_probe",
        )
        return {
            "status": "ok",
            "token_present": self.settings.token_configured,
            "contains_schema": "<schema" in text.lower()
            or "<xsd:schema" in text.lower(),
            "bytes": len(payload),
        }

    def download_archive(
        self, archive_url: str, target_dir: Path
    ) -> DownloadedAttachment:
        if not self.is_configured():
            raise RuntimeError("Источник ЕИС не настроен для скачивания архива.")
        parsed = urlparse(archive_url)
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError(
                "Разрешены только http/https ссылки для скачивания архива."
            )
        if not _is_allowed_eis_host(
            (parsed.hostname or "").lower(), self.settings.allowed_hosts
        ):
            raise RuntimeError("Хост архива не входит в allowlist ЕИС.")

        max_bytes = self.settings.max_download_mb * 1024 * 1024
        headers = {
            "User-Agent": self.settings.user_agent,
            self.settings.token_header_name: self.settings.token,
        }
        payload, content_type = self._http_get(
            archive_url,
            headers=headers,
            timeout=max(self.settings.timeout_seconds, 120),
            max_bytes=max_bytes,
        )
        if len(payload) > max_bytes:
            raise RuntimeError("Размер архива превышает лимит скачивания.")
        raw_name = Path(parsed.path or "").name or "archive"
        file_name = raw_name if raw_name.lower().endswith(".zip") else f"{raw_name}.zip"
        if not payload.startswith(b"PK"):
            raise RuntimeError("Поддерживается только ZIP-архив документации.")

        target_dir.mkdir(parents=True, exist_ok=True)
        stored_name = "documentation-archive.zip"
        (target_dir / stored_name).write_bytes(payload)
        return DownloadedAttachment(
            file_name=file_name,
            stored_name=stored_name,
            size_bytes=len(payload),
            content_type=content_type,
            source_url_host=parsed.hostname or "",
            source_url_path=parsed.path or "/",
        )

    def _post_soap(
        self,
        envelope: str,
        *,
        soap_action: str | None,
        endpoint_url: str,
        method_name: str,
    ) -> str:
        if not self.is_configured():
            raise RuntimeError("Источник ЕИС не настроен для SOAP-запросов.")
        self._write_debug_artifact(
            "last_request.xml", _sanitize_xml(envelope, self.settings.token)
        )
        try:
            if self._transport is not None:
                xml = self._transport(
                    envelope, soap_action, self.settings.timeout_seconds
                )
            else:
                xml = self._default_transport(envelope, soap_action, endpoint_url)
            self._write_debug_artifact(
                "last_response.xml", _sanitize_xml(xml, self.settings.token)
            )
            self._write_runtime_status(
                last_status="ok",
                soap_action=soap_action,
                endpoint_url=endpoint_url,
                method_name=method_name,
            )
            return xml
        except Exception as exc:  # noqa: BLE001
            message = _sanitize_error(str(exc), self.settings.token)
            self._write_debug_artifact("last_error.txt", message)
            self._write_runtime_status(
                last_status="error",
                last_error=message,
                soap_action=soap_action,
                endpoint_url=endpoint_url,
                method_name=method_name,
            )
            raise RuntimeError(
                f"SOAP-запрос к ЕИС завершился ошибкой: {message}"
            ) from None

    def _default_transport(
        self, envelope: str, soap_action: str | None, endpoint_url: str
    ) -> str:
        headers = {
            "Content-Type": self.settings.content_type,
            "User-Agent": self.settings.user_agent,
        }
        if soap_action and self.settings.use_soap_action:
            headers["SOAPAction"] = soap_action
        request = Request(
            endpoint_url,
            data=envelope.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            opener, _route_mode = _build_http_opener(
                self.settings, endpoint_url, policy=self._trust_policy
            )
            with opener.open(
                request, timeout=self.settings.timeout_seconds
            ) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(str(exc.reason)) from exc

    def _http_get(
        self, url: str, *, headers: dict[str, str], timeout: int, max_bytes: int
    ) -> tuple[bytes, str | None]:
        if self._http_transport is not None:
            return self._http_transport(url, headers, timeout, max_bytes)
        request = Request(url, headers=headers, method="GET")
        try:
            opener, _route_mode = _build_http_opener(
                self.settings, url, policy=self._trust_policy
            )
            with opener.open(request, timeout=timeout) as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_bytes:
                    raise RuntimeError("file exceeds size limit")
                payload = response.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    raise RuntimeError("file exceeds size limit")
                return payload, response.headers.get("Content-Type")
        except HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(str(exc.reason)) from exc

    def _write_debug_artifact(self, name: str, content: str) -> None:
        if not self.settings.debug and name != "last_error.txt":
            return
        target_dir = self._diagnostics_dir or _diagnostics_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / name).write_text(content, encoding="utf-8")

    def _write_runtime_status(
        self,
        *,
        last_status: str,
        soap_action: str | None,
        endpoint_url: str,
        method_name: str,
        last_error: str | None = None,
    ) -> None:
        if not self._runtime_status_enabled:
            return
        parsed = urlparse(endpoint_url)
        target_host = parsed.hostname or ""
        env_proxy_detected = _env_proxy_detected()
        no_proxy_value = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
        target_host_allowed = _is_allowed_eis_host(
            target_host, self.settings.allowed_hosts
        )
        direct_for_eis = bool(
            self.settings.disable_proxy_for_eis and target_host_allowed
        )
        client_trust_env = not direct_for_eis and self.settings.trust_env_proxy
        payload = {
            "configured": self.settings.configured,
            "token_present": self.settings.token_configured,
            "token_owner": self.settings.token_owner,
            "endpoint_host": parsed.hostname or "",
            "endpoint_path": parsed.path or "/",
            "soap_action": soap_action or "",
            "method_name": method_name,
            "last_status": last_status,
            "last_error": last_error or "",
            "mode": self.settings.mode,
            "system_proxy_detected": env_proxy_detected,
            "env_proxy_detected": env_proxy_detected,
            "client_trust_env": client_trust_env,
            "eis_proxy_disabled": self.settings.disable_proxy_for_eis,
            "target_host": target_host,
            "target_host_allowed": target_host_allowed,
            "no_proxy_contains_zakupki": "zakupki.gov.ru" in no_proxy_value.lower(),
            "route_mode": "direct_for_eis"
            if direct_for_eis
            else ("env_proxy" if client_trust_env else "unknown"),
        }
        target_dir = self._diagnostics_dir or _diagnostics_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        (target_dir / "last_status.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def parse_search_response(xml: str) -> list[ProcurementSearchResult]:
    root = _safe_parse_xml(xml)
    results: list[ProcurementSearchResult] = []
    for node in _candidate_nodes(root, {"procurement", "purchase", "notice"}):
        procurement_id = _first_text(
            node, "procurement_id", "procurementId", "id", "guid"
        )
        title = _first_text(node, "title", "name", "purchaseName", "subject")
        customer_name = _first_text(
            node, "customer_name", "customerName", "customer", "organizationName"
        )
        source_url = _first_text(node, "source_url", "sourceUrl", "url", "href")
        if not procurement_id or not title:
            continue
        attachments_count = (
            _to_int(
                _first_text(
                    node, "attachments_count", "attachmentsCount", "documentsCount"
                )
            )
            or 0
        )
        can_download = attachments_count > 0
        warnings: list[str] = []
        notice_number = _first_text(
            node, "notice_number", "noticeNumber", "purchaseNumber"
        )
        if not customer_name:
            warnings.append("Не удалось извлечь заказчика из SOAP-ответа.")
        if not source_url:
            warnings.append(
                "Ссылка на карточку не сформирована: формат URL требует уточнения."
            )
        if not _first_text(node, "law", "fz", "lawName"):
            warnings.append("Не удалось определить закон закупки из SOAP-ответа.")
        results.append(
            ProcurementSearchResult(
                procurement_id=procurement_id,
                notice_number=notice_number,
                registry_number=_first_text(node, "registry_number", "registryNumber"),
                title=title,
                customer_name=customer_name or "Не указан",
                customer_inn=_first_text(node, "customer_inn", "customerInn", "inn"),
                law=_first_text(node, "law", "fz", "lawName"),
                source="zakupki_gov_ru_soap_legacy",
                source_url=source_url or "https://zakupki.gov.ru/",
                publication_date=_parse_date_text(
                    _first_text(
                        node, "publication_date", "publicationDate", "publishDate"
                    )
                ),
                deadline=_parse_date_text(
                    _first_text(node, "deadline", "endDate", "submissionDeadline")
                ),
                initial_price=_to_float(
                    _first_text(node, "initial_price", "initialPrice", "maxPrice")
                ),
                currency=_first_text(node, "currency", "currencyCode") or "RUB",
                status=_first_text(node, "status", "state", "statusName"),
                attachments_count=attachments_count,
                attachments_status="downloadable"
                if can_download
                else "manual_upload_required",
                can_download_attachments=can_download,
                requires_manual_upload=not can_download,
                warnings=warnings,
            )
        )
    return results


def parse_details_response(xml: str) -> ProcurementDetails:
    root = _safe_parse_xml(xml)
    results = parse_search_response(xml)
    warnings: list[str] = []
    if results:
        procurement = results[0]
    else:
        warnings.append(
            "SOAP details method не дал нормализованный procurement node; используется partial fallback."
        )
        procurement = ProcurementSearchResult(
            procurement_id=_first_text(root, "procurement_id", "procurementId", "id")
            or "unknown",
            notice_number=_first_text(
                root, "notice_number", "noticeNumber", "purchaseNumber"
            ),
            registry_number=_first_text(root, "registry_number", "registryNumber"),
            title=_first_text(root, "title", "name", "purchaseName", "subject")
            or "Закупка без названия",
            customer_name=_first_text(root, "customer_name", "customerName", "customer")
            or "Не указан",
            law=_first_text(root, "law", "fz", "lawName"),
            source="zakupki_gov_ru_soap_legacy",
            source_url=_first_text(root, "source_url", "sourceUrl", "url", "href")
            or "https://zakupki.gov.ru/",
            attachments_count=0,
            attachments_status="manual_upload_required",
            can_download_attachments=False,
            requires_manual_upload=True,
            warnings=["Использован partial details fallback."],
        )
    return ProcurementDetails(
        procurement=procurement,
        attachments=parse_attachments_response(xml),
        raw_source_summary=_first_text(root, "description", "summary"),
        warnings=warnings + procurement.warnings,
    )


def parse_attachments_response(xml: str) -> list[ProcurementAttachment]:
    root = _safe_parse_xml(xml)
    attachments: list[ProcurementAttachment] = []
    for node in _candidate_nodes(root, {"attachment", "document", "file"}):
        name = _first_text(node, "name", "fileName", "filename", "documentName")
        attachment_id = _first_text(
            node, "attachment_id", "attachmentId", "id", "fileId", "documentId", "docId"
        )
        if not name:
            continue
        url = _first_text(node, "url", "downloadUrl", "href")
        warnings: list[str] = []
        if not url and attachment_id:
            warnings.append(
                "В ответе ЕИС есть идентификатор документа, но нет прямой ссылки на скачивание."
            )
        attachments.append(
            ProcurementAttachment(
                attachment_id=attachment_id or name,
                name=name,
                url=url,
                content_type=_first_text(
                    node, "content_type", "contentType", "mimeType"
                ),
                size_bytes=_to_int(
                    _first_text(node, "size_bytes", "sizeBytes", "fileSize")
                ),
                extension=_extension_from_name(name),
                can_download=bool(url),
                requires_manual_upload=not bool(url),
                warnings=warnings
                or ([] if url else ["В ответе ЕИС нет ссылки на скачивание."]),
            )
        )
    return attachments


def parse_getdocs_response(
    xml: str,
    *,
    request_id: str | None = None,
    expected_response_tag: str,
    expected_request_tag: str,
) -> DocsArchiveResult:
    root = _safe_parse_xml(xml)
    raw_summary = _first_text(
        root, "faultstring", "message", "errorMessage", "description", "resultMessage"
    )
    fault = _find_first_node(root, {"Fault"})
    if fault is not None:
        return DocsArchiveResult(
            request_id=request_id or _first_text(root, "id") or "unknown",
            ref_id=_first_text(root, "refId", "refID"),
            archive_url=None,
            status="soap_fault",
            warnings=[raw_summary or "SOAP Fault returned by EIS getDocsIP."],
            raw_summary=raw_summary,
            safe_diagnostic={"response_kind": "soap_fault"},
        )

    response_node = _find_first_node(root, {expected_response_tag})
    request_echo_node = _find_first_node(root, {expected_request_tag})
    archive_urls = _all_texts(root, "archiveUrl", "archiveURL")
    archive_url = archive_urls[0] if archive_urls else None
    parsed_request_id = _first_text(root, "id") or request_id or "unknown"
    ref_id = _first_text(root, "refId", "refID")
    warnings: list[str] = []
    error_info = _find_first_node(root, {"errorInfo"})
    error_code = _first_text(error_info, "code") if error_info is not None else None
    error_message = (
        _first_text(error_info, "message", "errorMessage", "description")
        if error_info is not None
        else None
    )
    no_data = (_first_text(root, "noData") or "").strip().lower() == "true"
    archive_name = _first_text(root, "archiveName", "name")
    if archive_name is None and archive_url:
        archive_name = Path(urlparse(archive_url).path or "").name or None

    if _looks_like_validation_error(root, raw_summary):
        status = "validation_error"
    elif response_node is None and request_echo_node is not None:
        status = "echo_request_unprocessed"
        warnings.append(
            "Ответ похож на echo request без обработанного response payload."
        )
    elif error_info is not None:
        status = "processing_error"
        if error_message:
            warnings.append(error_message)
        elif error_code:
            warnings.append(f"ЕИС вернул errorInfo code={error_code}.")
        else:
            warnings.append("ЕИС вернул errorInfo без archiveUrl.")
    elif no_data:
        status = "no_data"
        warnings.append("ЕИС сообщил, что документы по запросу отсутствуют.")
    elif archive_url:
        status = "completed"
    else:
        status = "no_archive_url"
        warnings.append("В ответе getDocsIP не найден archiveUrl.")

    if response_node is None and status != "echo_request_unprocessed":
        warnings.append("Ожидаемый response tag не найден в SOAP-ответе.")
    if (
        not archive_url
        and _find_first_node(root, {"dataInfo"}) is not None
        and status == "no_archive_url"
    ):
        warnings.append("dataInfo присутствует, но archiveUrl отсутствует.")

    return DocsArchiveResult(
        request_id=parsed_request_id,
        ref_id=ref_id,
        archive_url=archive_url,
        archive_urls=archive_urls,
        archive_name=archive_name,
        archive_size=_to_int(_first_text(root, "archiveSize", "sizeBytes")),
        status=status,
        warnings=warnings,
        raw_summary=raw_summary or error_message,
        safe_diagnostic={
            "response_kind": status,
            "archive_url_present": bool(archive_url),
            "archive_urls_count": len(archive_urls),
            "expected_response_tag": expected_response_tag,
            "ref_id_present": bool(ref_id),
            "error_code": error_code,
            "no_data": no_data,
        },
    )


def _safe_parse_xml(xml: str) -> ET.Element:
    if len(xml) > MAX_XML_CHARS:
        raise ValueError("SOAP XML response is too large")
    lowered = xml[:1000].lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError(
            "SOAP XML response contains forbidden DTD or entity declarations"
        )
    return ET.fromstring(xml)


def _sanitize_error(message: str, token: str) -> str:
    if token:
        message = message.replace(token, "[redacted]")
    return message


def _sanitize_xml(xml: str, token: str) -> str:
    if token:
        xml = xml.replace(token, "[redacted]")
    return xml


def _diagnostics_dir() -> Path:
    configured = os.environ.get("AI_CORP_ZAKUPKI_SOAP_DIAGNOSTICS_DIR")
    if configured:
        return Path(configured)
    new_default = Path("company_agent_runs/zakupki_soap_diagnostics")
    old_default = Path("company_agent_runs/zakupki_soap_live_diagnostics")
    return (
        new_default if new_default.exists() or not old_default.exists() else old_default
    )


def _request_meta() -> tuple[str, str]:
    return str(uuid4()), datetime.now(UTC).replace(microsecond=0).isoformat()


def _build_http_opener(
    settings: ZakupkiSoapSettings,
    target_url: str,
    *,
    policy: TrustPolicy | None = None,
):
    if policy is not None:
        _validate_injected_policy(settings, target_url, policy)
    ssl_ctx, policy_bypass = create_urllib_context(target_url, policy=policy)
    hostname = (urlparse(target_url).hostname or "").lower()
    target_allowed = _is_allowed_eis_host(hostname, settings.allowed_hosts)
    if settings.disable_proxy_for_eis and target_allowed and policy_bypass:
        return build_opener(
            HTTPSHandler(context=ssl_ctx), ProxyHandler({})
        ), "direct_for_eis"
    if settings.trust_env_proxy:
        return build_opener(HTTPSHandler(context=ssl_ctx)), "env_proxy"
    return build_opener(
        HTTPSHandler(context=ssl_ctx), ProxyHandler({})
    ), "direct_no_env"


def _validate_injected_policy(
    settings: ZakupkiSoapSettings, target_url: str, policy: TrustPolicy
) -> None:
    parsed = urlparse(target_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise RuntimeError("ETP transport requires HTTPS")
    if not _is_allowed_eis_host(hostname, settings.allowed_hosts):
        raise RuntimeError("ETP host is not allowed")
    if settings.require_direct_ru_route:
        host_policy = resolve_host_policy(hostname, policy)
        if (
            not policy.enabled
            or not policy.proxy_bypass_enabled
            or not host_policy
            or not host_policy.direct_connection
        ):
            raise RuntimeError("ETP direct route policy is required")


def _is_allowed_eis_host(hostname: str, allowed_hosts: tuple[str, ...]) -> bool:
    for host in allowed_hosts:
        normalized = host.strip().lower()
        if not normalized:
            continue
        if normalized.startswith("."):
            if hostname.endswith(normalized):
                return True
            continue
        if hostname == normalized or hostname.endswith(f".{normalized}"):
            return True
    return False


def _env_proxy_detected() -> bool:
    proxies = getproxies_environment()
    return any(proxies.get(key) for key in ("http", "https", "all"))


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _candidate_nodes(root: ET.Element, names: set[str]) -> list[ET.Element]:
    return [node for node in root.iter() if _local_name(node.tag) in names]


def _find_first_node(root: ET.Element, names: set[str]) -> ET.Element | None:
    for node in root.iter():
        if _local_name(node.tag) in names:
            return node
    return None


def _first_text(node: ET.Element, *names: str) -> str | None:
    lowered = {name.lower() for name in names}
    for child in node.iter():
        if (
            _local_name(child.tag).lower() in lowered
            and child.text
            and child.text.strip()
        ):
            return child.text.strip()
    return None


def _all_texts(node: ET.Element, *names: str) -> list[str]:
    lowered = {name.lower() for name in names}
    values: list[str] = []
    for child in node.iter():
        if (
            _local_name(child.tag).lower() not in lowered
            or not child.text
            or not child.text.strip()
        ):
            continue
        values.append(child.text.strip())
    return values


def _looks_like_validation_error(root: ET.Element, raw_summary: str | None) -> bool:
    if raw_summary and any(
        token in raw_summary.lower()
        for token in ("validation", "валидац", "schema", "xsd")
    ):
        return True
    for node in root.iter():
        local = _local_name(node.tag).lower()
        if "validation" in local or "schema" in local or "error" == local:
            text = (node.text or "").strip().lower()
            if any(
                token in text
                for token in ("validation", "валидац", "schema", "xsd", "order")
            ):
                return True
    return False


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value.replace(" ", "").replace(",", ".")))
    except ValueError:
        return None


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _parse_date_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%d.%m.%Y",
        "%d.%m.%Y %H:%M",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(text, fmt).date().isoformat()  # noqa: DTZ007
        except ValueError:
            continue
    return text


def _extension_from_name(name: str) -> str | None:
    if "." not in name:
        return None
    return "." + name.rsplit(".", 1)[-1].lower()
