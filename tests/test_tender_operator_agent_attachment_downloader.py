from pathlib import Path
from urllib.error import URLError

import pytest

from src.modules.tender_operator_agent_demo.attachment_downloader import (
    download_procurement_attachments,
)
from src.modules.tender_operator_agent_demo.procurement_schemas import (
    ProcurementAttachment,
)


def _attachment(
    name: str,
    url: str,
    *,
    extension: str | None = None,
) -> ProcurementAttachment:
    return ProcurementAttachment(
        attachment_id=name,
        name=name,
        url=url,
        extension=extension,
        can_download=True,
    )


def test_attachment_downloader_saves_safe_http_attachment(tmp_path: Path):
    result = download_procurement_attachments(
        [
            _attachment(
                "Документация закупки.pdf",
                "https://zakupki.gov.ru/docs/file.pdf",
            )
        ],
        target_dir=tmp_path,
        max_attachments=5,
        max_file_size_bytes=1024,
        max_total_size_bytes=1024,
        transport=lambda _url, _limit: (b"pdf-content", "application/pdf"),
    )

    assert len(result.saved) == 1
    assert result.saved[0].stored_name
    assert "/" not in result.saved[0].stored_name
    assert (tmp_path / result.saved[0].stored_name).read_bytes() == b"pdf-content"


def test_attachment_downloader_allows_legacy_doc(tmp_path: Path):
    result = download_procurement_attachments(
        [
            _attachment(
                "Описание объекта закупки.doc",
                "https://zakupki.gov.ru/docs/spec.doc",
            )
        ],
        target_dir=tmp_path,
        max_attachments=5,
        max_file_size_bytes=1024,
        max_total_size_bytes=1024,
        transport=lambda _url, _limit: (b"doc-content", "application/msword"),
    )

    assert len(result.saved) == 1
    assert result.saved[0].extension == ".doc"
    assert (tmp_path / result.saved[0].stored_name).read_bytes() == b"doc-content"


def test_attachment_downloader_rejects_unsafe_scheme(tmp_path: Path):
    result = download_procurement_attachments(
        [_attachment("notice.pdf", "file:///etc/passwd")],
        target_dir=tmp_path,
        max_attachments=5,
        max_file_size_bytes=1024,
        max_total_size_bytes=1024,
        transport=lambda _url, _limit: (b"never", None),
    )

    assert not result.saved
    assert result.skipped[0].status == "skipped"
    assert "http/https" in (result.skipped[0].note or "")


def test_attachment_downloader_rejects_foreign_domain(tmp_path: Path):
    result = download_procurement_attachments(
        [_attachment("notice.pdf", "https://evil.example/file.pdf")],
        target_dir=tmp_path,
        max_attachments=5,
        max_file_size_bytes=1024,
        max_total_size_bytes=1024,
        transport=lambda _url, _limit: (b"never", None),
    )

    assert not result.saved
    assert "allowlist" in (result.skipped[0].note or "")


def test_attachment_downloader_rejects_unsupported_extension(tmp_path: Path):
    result = download_procurement_attachments(
        [_attachment("malware.exe", "https://zakupki.gov.ru/docs/malware.exe")],
        target_dir=tmp_path,
        max_attachments=5,
        max_file_size_bytes=1024,
        max_total_size_bytes=1024,
        transport=lambda _url, _limit: (b"never", None),
    )

    assert not result.saved
    assert result.skipped[0].extension == ".exe"


def test_attachment_downloader_sanitizes_path_traversal_filename(
    tmp_path: Path,
):
    result = download_procurement_attachments(
        [_attachment("../secret.txt", "https://zakupki.gov.ru/docs/secret.txt")],
        target_dir=tmp_path,
        max_attachments=5,
        max_file_size_bytes=1024,
        max_total_size_bytes=1024,
        transport=lambda _url, _limit: (b"safe", "text/plain"),
    )

    stored_name = result.saved[0].stored_name
    assert stored_name
    assert ".." not in stored_name
    assert "/" not in stored_name
    assert (tmp_path / stored_name).is_file()


def test_attachment_downloader_continues_after_download_error(tmp_path: Path):
    def transport(url: str, _limit: int) -> tuple[bytes, str | None]:
        if "bad" in url:
            raise RuntimeError("timeout")
        return b"ok", "text/plain"

    result = download_procurement_attachments(
        [
            _attachment("bad.txt", "https://zakupki.gov.ru/docs/bad.txt"),
            _attachment("good.txt", "https://zakupki.gov.ru/docs/good.txt"),
        ],
        target_dir=tmp_path,
        max_attachments=5,
        max_file_size_bytes=1024,
        max_total_size_bytes=1024,
        transport=transport,
    )

    assert [item.name for item in result.saved] == ["good.txt"]
    assert [item.name for item in result.skipped] == ["bad.txt"]


def test_attachment_downloader_respects_total_size_limit(tmp_path: Path):
    result = download_procurement_attachments(
        [
            _attachment("one.txt", "https://zakupki.gov.ru/docs/one.txt"),
            _attachment("two.txt", "https://zakupki.gov.ru/docs/two.txt"),
        ],
        target_dir=tmp_path,
        max_attachments=5,
        max_file_size_bytes=10,
        max_total_size_bytes=5,
        transport=lambda _url, _limit: (b"1234", "text/plain"),
    )

    assert [item.name for item in result.saved] == ["one.txt"]
    assert [item.name for item in result.skipped] == ["two.txt"]
    assert "Общий размер" in (result.skipped[0].note or "")


def test_default_transport_uses_repository_verified_opener(monkeypatch):
    from src.modules.tender_operator_agent_demo import attachment_downloader

    url = "https://zakupki.gov.ru/docs/file.pdf"
    opened: list[tuple[str, int]] = []
    created_for: list[str] = []

    class FakeResponse:
        headers = {
            "Content-Length": "7",
            "Content-Type": "application/pdf",
        }

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return False

        def read(self, limit: int) -> bytes:
            assert limit == 1025
            return b"payload"

    class FakeOpener:
        def open(self, request, timeout: int):
            opened.append((request.full_url, timeout))
            return FakeResponse()

    def fake_create_urllib_opener(target_url: str):
        created_for.append(target_url)
        return FakeOpener()

    monkeypatch.setattr(
        attachment_downloader,
        "create_urllib_opener",
        fake_create_urllib_opener,
    )

    payload, content_type = attachment_downloader._default_transport(url, 1024)

    assert payload == b"payload"
    assert content_type == "application/pdf"
    assert created_for == [url]
    assert opened == [(url, 30)]
    assert not hasattr(
        attachment_downloader,
        "_download_with_unverified_context",
    )


def test_default_transport_fails_closed_on_certificate_error(monkeypatch):
    from src.modules.tender_operator_agent_demo import attachment_downloader

    url = "https://zakupki.gov.ru/docs/file.pdf"

    class FailingOpener:
        calls = 0

        def open(self, _request, timeout: int):
            assert timeout == 30
            self.calls += 1
            raise URLError("CERTIFICATE_VERIFY_FAILED")

    opener = FailingOpener()
    monkeypatch.setattr(
        attachment_downloader,
        "create_urllib_opener",
        lambda _url: opener,
    )

    with pytest.raises(RuntimeError, match="CERTIFICATE_VERIFY_FAILED"):
        attachment_downloader._default_transport(url, 1024)

    assert opener.calls == 1
    assert not hasattr(
        attachment_downloader,
        "_download_with_unverified_context",
    )
