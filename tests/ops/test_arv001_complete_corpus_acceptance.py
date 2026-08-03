from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.arv001 import run_complete_corpus_acceptance as runner
from scripts.arv001.complete_corpus_contract import artifact_shape
from src.modules.tender_operator_agent_demo.customer_report_contract import (
    build_customer_detail_projection,
)
from src.modules.tender_operator_agent_demo.report_model import (
    build_customer_report_projection,
)
from src.modules.tender_operator_agent_demo.upload_service import (
    _render_customer_report_html,
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_repository_contract_preflight_uses_current_customer_and_run_schemas():
    report = runner._static_contract_preflight()

    assert report["schema_mismatches"] == []
    assert report["customer_status"] == "PROSPECT"
    assert report["start_run_fields"] == ["registry_number"]
    assert report["analysis_mode_db_field_required"] is False
    assert "serialize_graph" in report["source_graph_entry_points"]


def test_intake_mapping_uses_metadata_stored_name_and_verifies_bytes(tmp_path: Path):
    first = tmp_path / "stored-a.txt"
    second = tmp_path / "stored-b.txt"
    first.write_text("Первый документ закупки содержит достаточно текста.", encoding="utf-8")
    second.write_text("Второй документ закупки также содержит достаточно текста.", encoding="utf-8")
    physical = [
        {
            "original_name": "A.txt",
            "sha256": _digest(first),
            "size_bytes": first.stat().st_size,
            "content_type": "text/plain",
            "document_kind": "notice",
            "source_type": "test",
        },
        {
            "original_name": "B.txt",
            "sha256": _digest(second),
            "size_bytes": second.stat().st_size,
            "content_type": "text/plain",
            "document_kind": "contract_draft",
            "source_type": "test",
        },
    ]
    metadata = {
        "files": [
            {"original_name": "B.txt", "stored_name": "stored-b.txt"},
            {"original_name": "A.txt", "stored_name": "stored-a.txt"},
        ]
    }

    prepared = runner._prepare_documents(
        physical=physical,
        metadata=metadata,
        intake_root=tmp_path,
        max_chars=100_000,
        chunk_size=64,
        chunk_overlap=8,
    )

    assert [item.original_name for item in prepared] == ["A.txt", "B.txt"]
    assert [item.stored_name for item in prepared] == ["stored-a.txt", "stored-b.txt"]
    assert all(item.chunks for item in prepared)
    assert runner._corpus_hash(physical) == runner._corpus_hash(
        list(reversed(physical))
    )


def test_mapping_rejects_hash_mismatch_before_persistence(tmp_path: Path):
    source = tmp_path / "stored.txt"
    source.write_text("Достаточно длинный текст документа закупки.", encoding="utf-8")

    with pytest.raises(runner.AcceptanceBlocked, match="source_file_sha256_mismatch"):
        runner._prepare_documents(
            physical=[
                {
                    "original_name": "Документ.txt",
                    "sha256": "0" * 64,
                    "size_bytes": source.stat().st_size,
                }
            ],
            metadata={
                "files": [
                    {"original_name": "Документ.txt", "stored_name": "stored.txt"}
                ]
            },
            intake_root=tmp_path,
            max_chars=100_000,
            chunk_size=64,
            chunk_overlap=8,
        )


def _complete_model() -> dict:
    logical_documents = [
        {"name": "Извещение о закупке", "type": "извещение"},
        {"name": "Описание объекта закупки", "type": "техническая документация"},
        {"name": "Обоснование НМЦК", "type": "ценовое обоснование"},
        {"name": "Требования к составу заявки", "type": "требования к заявке"},
        {"name": "Проект контракта", "type": "проект контракта"},
        {
            "name": "Реквизиты обеспечения исполнения контракта",
            "type": "обеспечение исполнения контракта",
        },
    ]
    return {
        "metadata": {
            "document_count": 10,
            "document_set_summary": {
                "status": "complete",
                "physical_file_count": 10,
                "logical_document_count": 6,
                "logical_documents": logical_documents,
            },
        },
        "ai_runtime_provenance": {"producer": "production_llm_r10_1"},
        "procurement_number": "0388100001826000047",
        "procurement_title": "Поставка дизельного топлива",
        "customer_name": "Заказчик",
        "publication_datetime_display": "01.08.2026 10:00",
        "application_deadline_display": "10.08.2026 10:00",
        "analysis_as_of": "02.08.2026 10:00",
        "nmck": "25 200 000",
        "delivery_place": "г. Анадырь",
        "customer_decision": {
            "recommendation": "Требуется проверка",
            "reasons": ["Подтверждены предмет и объём"],
            "confirmed": ["Дизельное топливо — 140 т"],
            "next_action": "Проверить себестоимость",
        },
        "line_items": [
            {
                "sequence": 1,
                "original_name": "Топливо дизельное",
                "quantity_display": "140",
                "unit_original": "т",
                "okpd2": "19.20.21.300",
                "source_row": 1,
            }
        ],
        "requirements": [
            {
                "title": "Дизельное топливо должно соответствовать техническому описанию",
                "detail": "Объём поставки 140 т",
                "type": "техническое требование",
                "source": "Описание объекта закупки",
            },
            {
                "title": "Состав заявки",
                "detail": "Предоставить документы участника",
                "type": "документальное требование",
                "source": "Требования к составу заявки",
            },
        ],
        "compatibility_sections": {
            "contract_highlights": [
                "Оплата после подписания документа о приёмке.",
                "Поставка и приёмка выполняются по условиям проекта контракта.",
                "Предусмотрено обеспечение исполнения контракта.",
                "За нарушение обязательств предусмотрены штрафы и ответственность.",
            ]
        },
        "unit_economics": {"unit": "тонну", "value": 180000.0},
        "evidence_map": [],
        "risks": [],
        "customer_questions": [],
        "corpus_limitations": [],
    }


def test_complete_corpus_customer_report_has_six_documents_and_contract_sections():
    model = _complete_model()

    projection = build_customer_report_projection(model)
    detail = build_customer_detail_projection(model)
    rendered = _render_customer_report_html(model)

    assert projection["documents_count"] == 6
    assert projection["physical_files_count"] == 10
    assert projection["document_set_complete"] is True
    assert detail["has_grounded_requirements"] is True
    assert detail["has_contract_terms"] is True
    assert "Документы комплекта (6)" in rendered
    assert "Технические требования" in rendered
    assert "Требования к заявке и участнику" in rendered
    assert "Условия контракта" in rendered
    assert "Оплата" in rendered
    assert "Поставка и приёмка" in rendered
    assert "Обеспечение" in rendered
    assert "Ответственность и штрафы" in rendered
    assert "180 000,00 ₽ за тонну" in rendered
    assert "Проект контракта не найден" not in rendered
    assert "Отдельное техническое задание" not in rendered


def test_report_validator_rejects_private_ids_and_stale_missing_document_text():
    good = _render_customer_report_html(_complete_model())
    assert runner._validate_customer_report(
        good, "0388100001826000047"
    )["forbidden_content_present"] is False

    with pytest.raises(
        runner.AcceptanceBlocked,
        match="customer_report_private_or_stale_content_detected",
    ):
        runner._validate_customer_report(
            good
            + "<p>customer_id=11111111-1111-4111-8111-111111111111</p>"
            + "<p>Проект контракта не найден</p>",
            "0388100001826000047",
        )


def test_artifact_contract_shapes_are_reported_without_values(tmp_path: Path):
    payload = [{"original_name": "A.xml", "sha256": "a" * 64}]
    path = tmp_path / "physical-files.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    shape = artifact_shape(runner._read_json(path))

    assert shape == {
        "type": "array",
        "count": 1,
        "item_keys": ["original_name", "sha256"],
    }
