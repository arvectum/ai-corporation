from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from src.modules.tender_operator_agent_demo.document_set_completeness import (
    build_document_set_summary,
)
from src.modules.tender_operator_agent_demo.eis_notice_parser import (
    extract_notice_attachments,
)


def test_document_set_notice_only_is_fail_closed():
    summary = build_document_set_summary(
        [
            {
                "original_name": f"notice-{index}.xml",
                "role_hint": "notice",
                "document_kind": "eis_notice",
            }
            for index in range(1, 6)
        ]
    )

    assert summary["status"] == "notice_only"
    assert summary["analysis_allowed"] is False
    assert summary["physical_file_count"] == 5
    assert summary["logical_document_count"] == 1
    assert summary["missing_required_document_kinds"] == [
        "technical_specification",
        "contract_draft",
    ]
    assert summary["logical_documents"] == [
        {
            "name": "Извещение о закупке",
            "type": "извещение",
            "kind": "notice",
            "physical_file_count": 5,
        }
    ]


def test_document_set_requires_contract_and_technical_document():
    files = [
        {"original_name": "notice.xml", "role_hint": "notice"},
        {"original_name": "Техническое задание.pdf", "role_hint": "technical_spec"},
        {"original_name": "Проект контракта.docx", "role_hint": "contract_draft"},
    ]

    summary = build_document_set_summary(files)

    assert summary["status"] == "complete"
    assert summary["analysis_allowed"] is True
    assert summary["physical_file_count"] == 3
    assert summary["logical_document_count"] == 3
    assert summary["missing_required_document_kinds"] == []


def test_complete_eis_corpus_keeps_six_customer_logical_documents():
    files = [
        {
            "original_name": f"technical-{index}.xml",
            "role_hint": "notice",
        }
        for index in range(1, 6)
    ] + [
        {"original_name": "Описание объекта закупки.docx"},
        {"original_name": "Обоснование НМЦК.xlsx"},
        {"original_name": "Требования к составу заявки.docx"},
        {"original_name": "Проект контракта.docx"},
        {
            "original_name": (
                "Реквизиты обеспечения исполнения контракта.docx"
            )
        },
    ]

    summary = build_document_set_summary(files)

    assert summary["status"] == "complete"
    assert summary["analysis_allowed"] is True
    assert summary["physical_file_count"] == 10
    assert summary["logical_document_count"] == 6
    assert [item["name"] for item in summary["logical_documents"]] == [
        "Извещение о закупке",
        "Описание объекта закупки",
        "Обоснование НМЦК",
        "Требования к составу заявки",
        "Проект контракта",
        "Реквизиты обеспечения исполнения контракта",
    ]
    assert summary["kind_counts"] == {
        "application_requirements": 1,
        "contract_draft": 1,
        "contract_performance_security": 1,
        "notice": 5,
        "price_justification": 1,
        "technical_specification": 1,
    }


def test_notice_attachment_parser_supports_elements_and_href_attributes():
    xml = """
    <epNotification>
      <documents>
        <attachment>
          <fileName>Техническое задание.pdf</fileName>
          <downloadUrl>https://int44.zakupki.gov.ru/files/technical.pdf</downloadUrl>
        </attachment>
        <document documentName="Проект контракта.docx"
                  href="/files/contract.docx" />
        <document documentName="unsafe.txt"
                  href="https://example.org/unsafe.txt" />
      </documents>
    </epNotification>
    """

    attachments = extract_notice_attachments(xml)

    assert attachments == [
        {
            "name": "Техническое задание.pdf",
            "url": "https://int44.zakupki.gov.ru/files/technical.pdf",
            "document_kind": "technical_specification",
        },
        {
            "name": "Проект контракта.docx",
            "url": "/files/contract.docx",
            "document_kind": "contract_draft",
        },
    ]


def test_getdocs_extractor_expands_nested_archives_safely(monkeypatch, tmp_path: Path):
    from src.modules.tender_operator_agent_demo import (
        procurement_intake_service as service,
    )

    runs_root = tmp_path / "runs"
    monkeypatch.setenv("AI_CORP_TENDER_OPERATOR_DEMO_RUNS_DIR", str(runs_root))
    run_id = "RUN-NESTED-01"
    service.ensure_demo_run_structure(run_id, exist_ok=False)

    nested_bytes = BytesIO()
    with ZipFile(nested_bytes, "w") as nested:
        nested.writestr("Техническое задание.txt", "Технические требования")
        nested.writestr("Проект контракта.txt", "Условия договора")

    archive_path = tmp_path / "documentation.zip"
    with ZipFile(archive_path, "w") as outer:
        outer.writestr("notice.xml", "<epNotification />")
        outer.writestr("documents.zip", nested_bytes.getvalue())

    files, manifest, extracted_count = service._extract_safe_archive_into_run(
        run_id,
        archive_path,
    )

    assert extracted_count == 3
    assert len(files) == 3
    assert {item["role_hint"] for item in files} == {
        "notice",
        "technical_spec",
        "contract_draft",
    }
    assert all(item.status == "saved" for item in manifest)
    assert build_document_set_summary(files)["status"] == "complete"


def test_notice_attachment_parser_rejects_lookalike_and_protocol_relative_hosts():
    xml = """
    <epNotification>
      <documents>
        <document documentName="lookalike.pdf"
                  href="https://notzakupki.gov.ru/files/lookalike.pdf" />
        <document documentName="protocol-relative.pdf"
                  href="//evil.example/files/document.pdf" />
        <document documentName="valid.pdf"
                  href="https://zakupki.gov.ru/files/valid.pdf" />
      </documents>
    </epNotification>
    """

    assert extract_notice_attachments(xml) == [
        {
            "name": "valid.pdf",
            "url": "https://zakupki.gov.ru/files/valid.pdf",
            "document_kind": "attachment",
        }
    ]


def test_direct_analysis_is_blocked_for_notice_only_corpus(monkeypatch, tmp_path: Path):
    import pytest
    from fastapi import HTTPException

    from src.modules.tender_operator_agent_demo import (
        procurement_intake_service as service,
    )

    runs_root = tmp_path / "runs"
    monkeypatch.setenv("AI_CORP_TENDER_OPERATOR_DEMO_RUNS_DIR", str(runs_root))
    run_id = "RUN-NOTICE-ONLY-01"
    service.ensure_demo_run_structure(run_id, exist_ok=False)
    service.save_demo_run_metadata(
        run_id,
        {
            "run_id": run_id,
            "procurement_source": "zakupki_gov_ru_getdocs_ip",
            "status": "ready_to_analyze",
            "analysis_status": "not_started",
            "files": [
                {
                    "file_id": "FILE-01",
                    "original_name": "notice.xml",
                    "stored_name": "notice.xml",
                    "role_hint": "notice",
                    "document_kind": "eis_notice",
                    "size_bytes": 10,
                }
            ],
            "limitations": [],
        },
    )

    called = False

    def forbidden_analysis(_run_id: str):
        nonlocal called
        called = True
        raise AssertionError("analysis must not start")

    monkeypatch.setattr(service, "analyze_uploaded_demo_run", forbidden_analysis)
    with pytest.raises(HTTPException) as exc_info:
        service.analyze_eis_archive_run(run_id)

    assert exc_info.value.status_code == 409
    assert called is False
    metadata = service.load_demo_run_metadata(run_id)
    assert metadata["status"] == "docs_required"
    assert metadata["document_set_status"] == "notice_only"
    assert metadata["analysis_status"] == "not_started"


def test_archive_limit_failure_discards_partial_extraction(monkeypatch, tmp_path: Path):
    from src.modules.tender_operator_agent_demo import (
        procurement_intake_service as service,
    )

    runs_root = tmp_path / "runs"
    monkeypatch.setenv("AI_CORP_TENDER_OPERATOR_DEMO_RUNS_DIR", str(runs_root))
    monkeypatch.setattr(service, "MAX_ZIP_TOTAL_BYTES", 32)
    run_id = "RUN-LIMIT-01"
    service.ensure_demo_run_structure(run_id, exist_ok=False)

    archive_path = tmp_path / "documentation.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("notice.xml", "<notice />")
        archive.writestr("Техническое задание.txt", "x" * 64)
        archive.writestr("Проект контракта.txt", "contract")

    files, manifest, extracted_count = service._extract_safe_archive_into_run(
        run_id,
        archive_path,
    )

    assert files == []
    assert extracted_count == 0
    assert any(item.status == "skipped" and item.extension == ".zip" for item in manifest)
    extracted_dir = service.get_demo_run_input_dir(run_id) / "extracted"
    assert list(extracted_dir.iterdir()) == []


def test_archive_extraction_failure_overrides_apparent_file_completeness():
    from src.modules.tender_operator_agent_demo.document_set_completeness import (
        apply_document_set_summary,
    )

    metadata = {
        "archive_extraction_complete": False,
        "files": [
            {"original_name": "notice.xml", "role_hint": "notice"},
            {"original_name": "Техническое задание.pdf", "role_hint": "technical_spec"},
            {"original_name": "Проект контракта.docx", "role_hint": "contract_draft"},
        ],
    }
    summary = apply_document_set_summary(metadata)

    assert summary["status"] == "incomplete_archive"
    assert summary["analysis_allowed"] is False
