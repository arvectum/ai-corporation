from __future__ import annotations

import re
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request

from src.modules.tender_operator_agent_demo.procurement_schemas import (
    ProcurementAttachment,
)
from src.shared.network.http_client import create_urllib_opener

ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xlsx",
    ".xls",
    ".txt",
    ".csv",
    ".zip",
    ".xml",
    ".html",
    ".htm",
}
DEFAULT_ALLOWED_DOMAINS = {"zakupki.gov.ru", "int44.zakupki.gov.ru"}
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 5
AttachmentTransport = Callable[[str, int], tuple[bytes, str | None]]


class AttachmentTransportError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class AttachmentDownloadManifestItem:
    name: str
    stored_name: str | None
    extension: str
    status: str
    note: str | None = None
    size_bytes: int = 0
    source_url: str | None = None
    source_type: str | None = None
    document_kind: str | None = None
    content_type: str | None = None
    error: str | None = None


@dataclass
class AttachmentDownloadResult:
    saved: list[AttachmentDownloadManifestItem] = field(default_factory=list)
    skipped: list[AttachmentDownloadManifestItem] = field(default_factory=list)

    @property
    def manifest(self) -> list[AttachmentDownloadManifestItem]:
        return self.saved + self.skipped


def download_procurement_attachments(
    attachments: list[ProcurementAttachment],
    *,
    target_dir: Path,
    max_attachments: int,
    max_file_size_bytes: int,
    max_total_size_bytes: int,
    allowed_domains: set[str] | None = None,
    transport: AttachmentTransport | None = None,
) -> AttachmentDownloadResult:
    target_dir.mkdir(parents=True, exist_ok=True)
    result = AttachmentDownloadResult()
    total_size = 0
    allowed_domains = allowed_domains or DEFAULT_ALLOWED_DOMAINS

    for index, attachment in enumerate(attachments[:max_attachments], start=1):
        extension = _extension_for_attachment(attachment)
        display_name = Path(attachment.name or f"attachment-{index}").name
        if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
            result.skipped.append(
                AttachmentDownloadManifestItem(
                    name=display_name,
                    stored_name=None,
                    extension=extension,
                    status="skipped",
                    note="Формат вложения не входит в allowlist.",
                    source_url=attachment.url,
                    document_kind=getattr(attachment, "document_kind", None),
                    error="unsupported_extension",
                )
            )
            continue
        if not attachment.url:
            result.skipped.append(
                AttachmentDownloadManifestItem(
                    name=display_name,
                    stored_name=None,
                    extension=extension,
                    status="skipped",
                    note="В ответе источника нет ссылки на скачивание.",
                    document_kind=getattr(attachment, "document_kind", None),
                    error="missing_url",
                )
            )
            continue
        url_error = _validate_url(attachment.url, allowed_domains)
        if url_error:
            result.skipped.append(
                AttachmentDownloadManifestItem(
                    name=display_name,
                    stored_name=None,
                    extension=extension,
                    status="skipped",
                    note=url_error,
                    source_url=attachment.url,
                    document_kind=getattr(attachment, "document_kind", None),
                    error="url_rejected",
                )
            )
            continue

        try:
            if transport is None:
                payload, content_type = _default_transport(
                    attachment.url,
                    max_file_size_bytes,
                    allowed_domains=allowed_domains,
                )
            else:
                payload, content_type = transport(
                    attachment.url,
                    max_file_size_bytes,
                )
        except AttachmentTransportError as exc:
            error_code = exc.code
            result.skipped.append(
                AttachmentDownloadManifestItem(
                    name=display_name,
                    stored_name=None,
                    extension=extension,
                    status="skipped",
                    note=f"Не удалось скачать вложение ({error_code}).",
                    source_url=attachment.url,
                    source_type="remote_attachment",
                    document_kind=getattr(attachment, "document_kind", None),
                    error=error_code,
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001
            error_code = f"transport_error:{type(exc).__name__}"
            result.skipped.append(
                AttachmentDownloadManifestItem(
                    name=display_name,
                    stored_name=None,
                    extension=extension,
                    status="skipped",
                    note=f"Не удалось скачать вложение ({error_code}).",
                    source_url=attachment.url,
                    source_type="remote_attachment",
                    document_kind=getattr(attachment, "document_kind", None),
                    error=error_code,
                )
            )
            continue

        size = len(payload)
        if size > max_file_size_bytes:
            result.skipped.append(
                AttachmentDownloadManifestItem(
                    name=display_name,
                    stored_name=None,
                    extension=extension,
                    status="skipped",
                    note="Размер файла превышает лимит.",
                    size_bytes=size,
                    source_url=attachment.url,
                    source_type="remote_attachment",
                    document_kind=getattr(attachment, "document_kind", None),
                    content_type=content_type,
                    error="file_too_large",
                )
            )
            continue
        if total_size + size > max_total_size_bytes:
            result.skipped.append(
                AttachmentDownloadManifestItem(
                    name=display_name,
                    stored_name=None,
                    extension=extension,
                    status="skipped",
                    note="Общий размер скачивания превышает лимит.",
                    size_bytes=size,
                    source_url=attachment.url,
                    source_type="remote_attachment",
                    document_kind=getattr(attachment, "document_kind", None),
                    content_type=content_type,
                    error="total_size_exceeded",
                )
            )
            continue

        stored_name = _safe_stored_name(attachment.name, index, extension)
        (target_dir / stored_name).write_bytes(payload)
        total_size += size
        result.saved.append(
            AttachmentDownloadManifestItem(
                name=display_name,
                stored_name=stored_name,
                extension=extension,
                status="saved",
                note=(
                    "Файл сохранён локально. "
                    f"Content-Type: {content_type or 'unknown'}."
                ),
                size_bytes=size,
                source_url=attachment.url,
                source_type="remote_attachment",
                document_kind=getattr(attachment, "document_kind", None),
                content_type=content_type,
            )
        )

    for attachment in attachments[max_attachments:]:
        result.skipped.append(
            AttachmentDownloadManifestItem(
                name=Path(attachment.name or "attachment").name,
                stored_name=None,
                extension=_extension_for_attachment(attachment),
                status="skipped",
                note="Вложение пропущено из-за лимита количества файлов.",
                source_url=attachment.url,
                document_kind=getattr(attachment, "document_kind", None),
                error="attachment_limit_exceeded",
            )
        )

    return result


def _default_transport(
    url: str,
    max_file_size_bytes: int,
    *,
    allowed_domains: set[str] | None = None,
    max_redirects: int = MAX_REDIRECTS,
) -> tuple[bytes, str | None]:
    allowed_domains = allowed_domains or DEFAULT_ALLOWED_DOMAINS
    current_url = url
    visited: set[str] = set()

    for redirect_count in range(max_redirects + 1):
        url_error = _validate_url(current_url, allowed_domains)
        if url_error:
            code = "url_rejected" if redirect_count == 0 else "redirect_url_rejected"
            raise AttachmentTransportError(code)
        if current_url in visited:
            raise AttachmentTransportError("redirect_loop")
        visited.add(current_url)

        request = Request(
            current_url,
            headers={
                "Accept": "*/*",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "User-Agent": "ai-corporation-tender-demo/1.0",
            },
            method="GET",
        )
        try:
            opener = create_urllib_opener(
                current_url,
                follow_redirects=False,
            )
            response = opener.open(request, timeout=30)
        except HTTPError as exc:
            if exc.code in REDIRECT_STATUS_CODES:
                current_url = _next_redirect_url(
                    current_url,
                    exc.headers.get("Location") if exc.headers else None,
                    redirect_count=redirect_count,
                    max_redirects=max_redirects,
                    allowed_domains=allowed_domains,
                )
                continue
            raise AttachmentTransportError(f"http_status:{exc.code}") from exc
        except URLError as exc:
            raise _normalized_url_error(exc) from exc
        except ssl.SSLCertVerificationError as exc:
            raise AttachmentTransportError(
                "tls_certificate_verify_failed"
            ) from exc
        except ssl.SSLError as exc:
            raise AttachmentTransportError("tls_error") from exc
        except TimeoutError as exc:
            raise AttachmentTransportError("network_timeout") from exc
        except OSError as exc:
            raise AttachmentTransportError(
                f"network_os_error:{type(exc).__name__}"
            ) from exc

        with response:
            status = getattr(response, "status", None)
            if status in REDIRECT_STATUS_CODES:
                current_url = _next_redirect_url(
                    current_url,
                    response.headers.get("Location"),
                    redirect_count=redirect_count,
                    max_redirects=max_redirects,
                    allowed_domains=allowed_domains,
                )
                continue
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    if int(content_length) > max_file_size_bytes:
                        raise AttachmentTransportError("file_too_large")
                except ValueError as exc:
                    raise AttachmentTransportError(
                        "invalid_content_length"
                    ) from exc
            return (
                response.read(max_file_size_bytes + 1),
                response.headers.get("Content-Type"),
            )

    raise AttachmentTransportError("too_many_redirects")


def _next_redirect_url(
    current_url: str,
    location: str | None,
    *,
    redirect_count: int,
    max_redirects: int,
    allowed_domains: set[str],
) -> str:
    if redirect_count >= max_redirects:
        raise AttachmentTransportError("too_many_redirects")
    if not location:
        raise AttachmentTransportError("redirect_missing_location")
    next_url = urljoin(current_url, location)
    if _validate_url(next_url, allowed_domains):
        raise AttachmentTransportError("redirect_url_rejected")
    return next_url


def _normalized_url_error(exc: URLError) -> AttachmentTransportError:
    reason = exc.reason
    if isinstance(reason, ssl.SSLCertVerificationError) or (
        "CERTIFICATE_VERIFY_FAILED" in str(reason)
    ):
        return AttachmentTransportError("tls_certificate_verify_failed")
    if isinstance(reason, ssl.SSLError):
        return AttachmentTransportError("tls_error")
    if isinstance(reason, TimeoutError):
        return AttachmentTransportError("network_timeout")
    return AttachmentTransportError(
        f"network_error:{type(reason).__name__}"
    )


def _validate_url(url: str, allowed_domains: set[str]) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "Разрешены только http/https ссылки."
    hostname = (parsed.hostname or "").lower()
    if not any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in allowed_domains
    ):
        return "Домен вложения не входит в allowlist источника."
    return None


def _extension_for_attachment(attachment: ProcurementAttachment) -> str:
    if attachment.extension:
        return attachment.extension.lower()
    name = attachment.name or ""
    if "." in name:
        return "." + name.rsplit(".", 1)[-1].lower()
    url_path = urlparse(attachment.url or "").path
    if "." in url_path:
        return "." + url_path.rsplit(".", 1)[-1].lower()
    return ""


def _safe_stored_name(name: str, index: int, extension: str) -> str:
    original = Path(name or f"attachment-{index}{extension}").name
    stem = Path(original).stem.lower()
    stem = re.sub(r"[^a-z0-9._-]+", "-", stem).strip("._-")
    if not stem:
        stem = f"attachment-{index}"
    return f"{index:02d}-{stem[:60]}{extension}"
