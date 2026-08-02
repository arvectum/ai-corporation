import json
from pathlib import Path
from zipfile import ZipFile


def _set_runs_root(monkeypatch, tmp_path: Path) -> Path:
    runs_root = tmp_path / "tender_operator_demo_runs"
    monkeypatch.setenv("AI_CORP_TENDER_OPERATOR_DEMO_RUNS_DIR", str(runs_root))
    return runs_root


def test_getdocs_archive_endpoint_creates_run_with_archive_metadata(client, monkeypatch, tmp_path):
    from src.modules.tender_operator_agent_demo import (
        procurement_intake_service as service,
    )
    from src.modules.tender_operator_agent_demo.procurement_schemas import (
        DocsArchiveResult,
        DownloadedAttachment,
    )
    from src.modules.tender_operator_agent_demo.settings import (
        clear_zakupki_soap_settings_cache,
    )

    runs_root = _set_runs_root(monkeypatch, tmp_path)
    monkeypatch.setenv("ZAKUPKI_GOV_RU_SOAP_ENABLED", "1")
    monkeypatch.setenv("ZAKUPKI_GOV_RU_SOAP_TOKEN", "test-token-value-not-real")
    clear_zakupki_soap_settings_cache()

    class FakeClient:
        def __init__(self, _settings):
            pass

        def get_docs_by_reestr_number(self, _reestr_number, subsystem_type="PRIZ"):
            return DocsArchiveResult(
                request_id="req-001",
                ref_id="ref-001",
                archive_url="https://int44.zakupki.gov.ru/archive/demo.zip?ticket=secret",
                status="completed",
                warnings=[],
                safe_diagnostic={"archive_url_present": True},
            )

        def download_archive(self, _archive_url, target_dir):
            payload = b"PK\x03\x04demo"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "documentation-archive.zip").write_bytes(payload)
            return DownloadedAttachment(
                file_name="demo.zip",
                stored_name="documentation-archive.zip",
                size_bytes=len(payload),
                content_type="application/zip",
                source_url_host="int44.zakupki.gov.ru",
                source_url_path="/archive/demo.zip",
            )

    monkeypatch.setattr(service, "ZakupkiSoapClient", FakeClient)
    monkeypatch.setattr(
        service,
        "_extract_safe_archive_into_run",
        lambda run_id, _path: (
            [
                service.build_demo_file_descriptor(
                    file_id="FILE-01",
                    original_name="notice.txt",
                    stored_name="01-notice.txt",
                    size_bytes=10,
                    content_type="text/plain",
                    source="eis_getdocs_archive",
                )
            ],
            [
                service.ProcurementAttachmentManifestItem(
                    name="notice.txt",
                    stored_name="01-notice.txt",
                    extension=".txt",
                    status="saved",
                    note="ok",
                )
            ],
            1,
        ),
    )

    response = client.post(
        "/api/demo/tender-agent/runs/from-eis-docs-archive",
        json={
            "reestr_number": "0888200000224000038",
            "law": "44fz",
            "subsystem_type": "PRIZ",
            "method": "getDocsByReestrNumber",
            "download_archive": True,
            "analyze_after_download": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "docs_required"
    assert payload["attachments_status"] == "incomplete_document_set"
    assert payload["manual_upload_required"] is True
    assert payload["archive_url_present"] is True
    assert payload["archive_downloaded"] is True
    assert payload["documents_extracted_count"] == 1
    assert payload["soap_method"] == "getDocsByReestrNumber"
    assert payload["ref_id"] == "ref-001"
    assert payload["archive_source_host"] == "int44.zakupki.gov.ru"
    assert payload["archive_source_path"] == "/archive/demo.zip"
    metadata = json.loads((runs_root / payload["run_id"] / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["source"] == "zakupki_gov_ru_getdocs_ip"
    assert metadata["token_owner"] == "individual"
    assert metadata["archive_url_present"] is True
    assert metadata["archive_downloaded"] is True
    assert metadata["documents_extracted_count"] == 1
    assert metadata["soap_method"] == "getDocsByReestrNumber"
    assert metadata["archive_source_host"] == "int44.zakupki.gov.ru"
    assert metadata["archive_source_path"] == "/archive/demo.zip"
    assert "ticket=secret" not in json.dumps(metadata, ensure_ascii=False)
    eis_metadata = json.loads((runs_root / payload["run_id"] / "procurement" / "eis_getdocs_metadata.json").read_text(encoding="utf-8"))
    assert eis_metadata["archive_summary"]["host"] == "int44.zakupki.gov.ru"
    assert eis_metadata["archive_summary"]["path"] == "/archive/demo.zip"
    assert eis_metadata["archive_summary"]["has_query"] is True
    assert "ticket=secret" not in json.dumps(eis_metadata, ensure_ascii=False)
    run_payload = client.get(f"/api/demo/tender-agent/runs/{payload['run_id']}").json()
    assert run_payload["soap_method"] == "getDocsByReestrNumber"
    assert run_payload["archive_source_host"] == "int44.zakupki.gov.ru"
    assert run_payload["archive_source_path"] == "/archive/demo.zip"
    assert "ticket=secret" not in json.dumps(run_payload, ensure_ascii=False)
    events_payload = client.get(f"/api/demo/tender-agent/runs/{payload['run_id']}/events").json()
    assert events_payload
    assert events_payload[0]["message_ru"]
    assert "ticket=secret" not in json.dumps(events_payload, ensure_ascii=False)
    assert str(runs_root) not in json.dumps(events_payload, ensure_ascii=False)
    clear_zakupki_soap_settings_cache()


def test_getdocs_archive_endpoint_falls_back_to_manual_upload(client, monkeypatch, tmp_path):
    from src.modules.tender_operator_agent_demo import (
        procurement_intake_service as service,
    )
    from src.modules.tender_operator_agent_demo.procurement_schemas import (
        DocsArchiveResult,
    )
    from src.modules.tender_operator_agent_demo.settings import (
        clear_zakupki_soap_settings_cache,
    )

    _set_runs_root(monkeypatch, tmp_path)
    monkeypatch.setenv("ZAKUPKI_GOV_RU_SOAP_ENABLED", "1")
    monkeypatch.setenv("ZAKUPKI_GOV_RU_SOAP_TOKEN", "test-token-value-not-real")
    clear_zakupki_soap_settings_cache()

    class FakeClient:
        def __init__(self, _settings):
            pass

        def get_docs_by_reestr_number(self, _reestr_number, subsystem_type="PRIZ"):
            return DocsArchiveResult(
                request_id="req-002",
                ref_id=None,
                archive_url=None,
                status="no_archive_url",
                warnings=["archiveUrl not found"],
                safe_diagnostic={"archive_url_present": False},
            )

    monkeypatch.setattr(service, "ZakupkiSoapClient", FakeClient)

    response = client.post(
        "/api/demo/tender-agent/runs/from-eis-docs-archive",
        json={"reestr_number": "0888200000224000038", "law": "44fz", "subsystem_type": "PRIZ", "download_archive": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "docs_required"
    assert payload["manual_upload_required"] is True
    assert payload["attachments_status"] == "manual_upload_required"
    assert payload["archive_url_present"] is False
    clear_zakupki_soap_settings_cache()


def test_getdocs_archive_endpoint_retries_transient_archive_download(client, monkeypatch, tmp_path):
    from src.modules.tender_operator_agent_demo import (
        procurement_intake_service as service,
    )
    from src.modules.tender_operator_agent_demo.procurement_schemas import (
        DocsArchiveResult,
        DownloadedAttachment,
    )
    from src.modules.tender_operator_agent_demo.settings import (
        clear_zakupki_soap_settings_cache,
    )

    runs_root = _set_runs_root(monkeypatch, tmp_path)
    monkeypatch.setenv("ZAKUPKI_GOV_RU_SOAP_ENABLED", "1")
    monkeypatch.setenv("ZAKUPKI_GOV_RU_SOAP_TOKEN", "test-token-value-not-real")
    clear_zakupki_soap_settings_cache()
    monkeypatch.setattr(service, "ARCHIVE_DOWNLOAD_RETRY_DELAY_SECONDS", 0)

    class FakeClient:
        attempts = 0

        def __init__(self, _settings):
            pass

        def get_docs_by_reestr_number(self, _reestr_number, subsystem_type="PRIZ"):
            return DocsArchiveResult(
                request_id="req-003",
                ref_id="ref-003",
                archive_url="https://int.zakupki.gov.ru/archive/demo.zip",
                archive_urls=["https://int.zakupki.gov.ru/archive/demo.zip"],
                status="completed",
                warnings=[],
                safe_diagnostic={"archive_url_present": True, "archive_urls_count": 1},
            )

        def download_archive(self, _archive_url, target_dir):
            type(self).attempts += 1
            if type(self).attempts == 1:
                raise RuntimeError("HTTP 404")
            payload = target_dir / "documentation-archive.zip"
            target_dir.mkdir(parents=True, exist_ok=True)
            with ZipFile(payload, "w") as archive:
                archive.writestr("notice.txt", "ok")
            return DownloadedAttachment(
                file_name="demo.zip",
                stored_name="documentation-archive.zip",
                size_bytes=payload.stat().st_size,
                content_type="application/zip",
                source_url_host="int.zakupki.gov.ru",
                source_url_path="/archive/demo.zip",
            )

    monkeypatch.setattr(service, "ZakupkiSoapClient", FakeClient)
    response = client.post(
        "/api/demo/tender-agent/runs/from-eis-docs-archive",
        json={"reestr_number": "0888200000224000038", "law": "44fz", "subsystem_type": "PRIZ", "download_archive": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "docs_required"
    metadata = json.loads((runs_root / payload["run_id"] / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["document_set_status"] == "notice_only"
    assert metadata["manual_upload_required"] is True
    assert metadata["archive_downloaded"] is True
    assert metadata["archive_download_attempts"] == 2
    assert metadata["archive_download_status"] == "downloaded"
    events_raw = (runs_root / payload["run_id"] / "events.jsonl").read_text(encoding="utf-8")
    assert "eis_archive_download_started" in events_raw
    assert "eis_archive_downloaded" in events_raw
    assert "eis_archive_extracted" in events_raw
    clear_zakupki_soap_settings_cache()


def test_getdocs_archive_endpoint_marks_archive_not_ready_after_retries(client, monkeypatch, tmp_path):
    from src.modules.tender_operator_agent_demo import (
        procurement_intake_service as service,
    )
    from src.modules.tender_operator_agent_demo.procurement_schemas import (
        DocsArchiveResult,
    )
    from src.modules.tender_operator_agent_demo.settings import (
        clear_zakupki_soap_settings_cache,
    )

    runs_root = _set_runs_root(monkeypatch, tmp_path)
    monkeypatch.setenv("ZAKUPKI_GOV_RU_SOAP_ENABLED", "1")
    monkeypatch.setenv("ZAKUPKI_GOV_RU_SOAP_TOKEN", "test-token-value-not-real")
    clear_zakupki_soap_settings_cache()
    monkeypatch.setattr(service, "ARCHIVE_DOWNLOAD_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(service, "ARCHIVE_DOWNLOAD_RETRY_DELAY_SECONDS", 0)

    class FakeClient:
        def __init__(self, _settings):
            pass

        def get_docs_by_reestr_number(self, _reestr_number, subsystem_type="PRIZ"):
            return DocsArchiveResult(
                request_id="req-004",
                ref_id="ref-004",
                archive_url="https://int.zakupki.gov.ru/archive/demo.zip",
                archive_urls=["https://int.zakupki.gov.ru/archive/demo.zip"],
                status="completed",
                warnings=[],
                safe_diagnostic={"archive_url_present": True, "archive_urls_count": 1},
            )

        def download_archive(self, _archive_url, _target_dir):
            raise RuntimeError("HTTP 404")

    monkeypatch.setattr(service, "ZakupkiSoapClient", FakeClient)
    response = client.post(
        "/api/demo/tender-agent/runs/from-eis-docs-archive",
        json={"reestr_number": "0888200000224000038", "law": "44fz", "subsystem_type": "PRIZ", "download_archive": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "docs_required"
    metadata = json.loads((runs_root / payload["run_id"] / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["archive_download_status"] == "archive_not_ready"
    assert metadata["archive_download_attempts"] == 2
    assert metadata["manual_upload_required"] is True
    events_raw = (runs_root / payload["run_id"] / "events.jsonl").read_text(encoding="utf-8")
    assert "eis_archive_not_ready" in events_raw
    clear_zakupki_soap_settings_cache()


def test_getdocs_archive_rejects_unsupported_method(client):
    response = client.post(
        "/api/demo/tender-agent/runs/from-eis-docs-archive",
        json={
            "reestr_number": "0888200000224000038",
            "law": "44fz",
            "subsystem_type": "PRIZ",
            "method": "getDocsByOrgRegion",
            "download_archive": True,
        },
    )

    assert response.status_code == 400
    assert "getDocsByReestrNumber" in response.json()["detail"]


def test_getdocs_archive_can_auto_analyze_after_download(client, monkeypatch, tmp_path):
    from src.modules.tender_operator_agent_demo import (
        procurement_intake_service as service,
    )
    from src.modules.tender_operator_agent_demo.procurement_schemas import (
        DocsArchiveResult,
        DownloadedAttachment,
    )
    from src.modules.tender_operator_agent_demo.settings import (
        clear_zakupki_soap_settings_cache,
    )

    runs_root = _set_runs_root(monkeypatch, tmp_path)
    monkeypatch.setenv("ZAKUPKI_GOV_RU_SOAP_ENABLED", "1")
    monkeypatch.setenv("ZAKUPKI_GOV_RU_SOAP_TOKEN", "test-token-value-not-real")
    clear_zakupki_soap_settings_cache()

    class FakeClient:
        def __init__(self, _settings):
            pass

        def get_docs_by_reestr_number(self, _reestr_number, subsystem_type="PRIZ"):
            return DocsArchiveResult(
                request_id="req-005",
                ref_id="ref-005",
                archive_url="https://int.zakupki.gov.ru/dstore/common/download/compound?ticket=secret",
                archive_urls=["https://int.zakupki.gov.ru/dstore/common/download/compound?ticket=secret"],
                archive_name="bundle.zip",
                status="completed",
                warnings=[],
                safe_diagnostic={"archive_url_present": True, "archive_urls_count": 1},
            )

        def download_archive(self, _archive_url, target_dir):
            payload = target_dir / "documentation-archive.zip"
            target_dir.mkdir(parents=True, exist_ok=True)
            with ZipFile(payload, "w") as archive:
                archive.writestr("notice.txt", "Извещение о закупке электротехнического оборудования.")
                archive.writestr("technical_spec.txt", "Техническая спецификация: срок поставки 45 дней, гарантия 12 месяцев.")
                archive.writestr("contract_draft.txt", "Проект договора: штрафы за просрочку, постоплата 45 дней.")
            return DownloadedAttachment(
                file_name="bundle.zip",
                stored_name="documentation-archive.zip",
                size_bytes=payload.stat().st_size,
                content_type="application/zip",
                source_url_host="int.zakupki.gov.ru",
                source_url_path="/dstore/common/download/compound",
            )

    monkeypatch.setattr(service, "ZakupkiSoapClient", FakeClient)
    response = client.post(
        "/api/demo/tender-agent/runs/from-eis-docs-archive",
        json={
            "reestr_number": "0888200000224000038",
            "law": "44fz",
            "subsystem_type": "PRIZ",
            "method": "getDocsByReestrNumber",
            "download_archive": True,
            "analyze_after_download": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"completed_with_warnings", "needs_review", "completed"}
    assert payload["analysis_status"] in {"completed", "completed_with_warnings", "needs_review"}
    report_html = (runs_root / payload["run_id"] / "output" / "report.html").read_text(encoding="utf-8")
    assert "Документы получены через ЕИС" in report_html
    assert "Скачать архив" in report_html
    assert "ticket=secret" not in report_html
    archive_download = client.get(f"/api/demo/tender-agent/runs/{payload['run_id']}/archive/download")
    assert archive_download.status_code == 200
    assert archive_download.headers["content-type"].startswith("application/zip")
    clear_zakupki_soap_settings_cache()


def test_getdocs_archive_xml_only_is_partial_not_crash(client, monkeypatch, tmp_path):
    from src.modules.tender_operator_agent_demo import (
        procurement_intake_service as service,
    )
    from src.modules.tender_operator_agent_demo.procurement_schemas import (
        DocsArchiveResult,
        DownloadedAttachment,
    )
    from src.modules.tender_operator_agent_demo.settings import (
        clear_zakupki_soap_settings_cache,
    )

    _set_runs_root(monkeypatch, tmp_path)
    monkeypatch.setenv("ZAKUPKI_GOV_RU_SOAP_ENABLED", "1")
    monkeypatch.setenv("ZAKUPKI_GOV_RU_SOAP_TOKEN", "test-token-value-not-real")
    clear_zakupki_soap_settings_cache()

    class FakeClient:
        def __init__(self, _settings):
            pass

        def get_docs_by_reestr_number(self, _reestr_number, subsystem_type="PRIZ"):
            return DocsArchiveResult(
                request_id="req-006",
                ref_id="ref-006",
                archive_url="https://int.zakupki.gov.ru/dstore/common/download/compound",
                archive_urls=["https://int.zakupki.gov.ru/dstore/common/download/compound"],
                archive_name="bundle.zip",
                status="completed",
                warnings=[],
                safe_diagnostic={"archive_url_present": True, "archive_urls_count": 1},
            )

        def download_archive(self, _archive_url, target_dir):
            payload = target_dir / "documentation-archive.zip"
            target_dir.mkdir(parents=True, exist_ok=True)
            with ZipFile(payload, "w") as archive:
                archive.writestr("notice.xml", "<notice><title>Закупка</title><delivery>45 дней</delivery></notice>")
            return DownloadedAttachment(
                file_name="bundle.zip",
                stored_name="documentation-archive.zip",
                size_bytes=payload.stat().st_size,
                content_type="application/zip",
                source_url_host="int.zakupki.gov.ru",
                source_url_path="/dstore/common/download/compound",
            )

    monkeypatch.setattr(service, "ZakupkiSoapClient", FakeClient)
    response = client.post(
        "/api/demo/tender-agent/runs/from-eis-docs-archive",
        json={
            "reestr_number": "0888200000224000038",
            "law": "44fz",
            "subsystem_type": "PRIZ",
            "method": "getDocsByReestrNumber",
            "download_archive": True,
            "analyze_after_download": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "docs_required"
    assert payload["analysis_status"] == "not_started"
    assert payload["attachments_status"] == "incomplete_document_set"
    assert payload["manual_upload_required"] is True
    clear_zakupki_soap_settings_cache()
