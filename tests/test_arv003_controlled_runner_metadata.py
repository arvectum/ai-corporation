from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

from scripts.r10_1.run_controlled_provider_evidence import (
    _document_file_descriptor,
    _metadata,
)
from src.modules.tender_operator_agent_demo.upload_service import (
    _build_steps_from_outputs,
)


def _document() -> SimpleNamespace:
    return SimpleNamespace(
        display_name="notice.pdf",
        extension=".pdf",
        role="notice",
        raw_content=b"pdf",
        source="customer_run",
        file_id="file-1",
        extracted_text_available=True,
        warnings=["warning"],
    )


def test_document_file_descriptor_matches_finalization_contract() -> None:
    descriptor = _document_file_descriptor(_document())

    assert descriptor["name"] == "notice.pdf"
    assert descriptor["display_name"] == "notice.pdf"
    assert descriptor["original_name"] == "notice.pdf"
    assert descriptor["extension"] == ".pdf"
    assert descriptor["size_bytes"] == 3
    assert descriptor["role_hint"] == "notice"
    assert descriptor["extracted_text_available"] is True
    assert descriptor["text_extraction_status"] == "extracted"
    assert "raw_content" not in descriptor
    assert "text" not in descriptor


def test_controlled_runner_metadata_is_consumable_by_step_builder() -> None:
    run = SimpleNamespace(
        customer_id=UUID("11111111-1111-1111-1111-111111111111"),
        project_id=UUID("22222222-2222-2222-2222-222222222222"),
        id=UUID("33333333-3333-3333-3333-333333333333"),
        registry_number="test-registry",
    )
    case = SimpleNamespace(
        id=UUID("44444444-4444-4444-4444-444444444444")
    )
    tender = SimpleNamespace(
        title="Тестовая закупка",
        law_type="44-ФЗ",
        customer_name="Тестовый заказчик",
        customer_inn="0000000000",
        customer_kpp="000000000",
        publication_date=datetime(2026, 7, 31, tzinfo=timezone.utc),
        application_deadline=datetime(2026, 8, 1, tzinfo=timezone.utc),
        nmck_amount=Decimal("100.00"),
    )

    metadata = _metadata(
        run=run,
        case=case,
        tender=tender,
        documents=[_document()],
        warnings=[],
        limitations=[],
    )

    assert metadata["customer_id"] == str(run.customer_id)
    assert metadata["project_id"] == str(run.project_id)
    assert metadata["run_id"] == str(run.id)
    assert metadata["procurement"]["case_id"] == str(case.id)
    assert set(("display_name", "extension", "size_bytes")) <= set(
        metadata["files"][0]
    )

    outputs = {
        "requirements": {
            "requirements": [
                {
                    "title": "Требование",
                    "type": "общее",
                    "priority": "medium",
                    "detail": "Деталь",
                    "source": "notice.pdf",
                }
            ],
            "preliminary_analysis": {
                "overview": ["Предмет закупки: Тестовая закупка"],
                "compliance_highlights": [],
                "contract_highlights": [],
            },
            "manual_review_points": [],
        },
        "supplier_questions": {
            "questions": ["Вопрос"],
            "ambiguities": [],
            "manual_checks": [],
        },
        "rfq_draft": {
            "sections": ["Секция"],
            "manual_checks": [],
        },
        "quotes_comparison": {
            "status": "blocked",
            "supplier_quotes_found": 0,
            "items_extracted": 0,
            "suppliers": [],
            "items": [],
            "highlights": [],
            "manual_checks": [],
        },
        "economics": {
            "status": "blocked",
            "result": "Нет данных",
            "drivers": [],
            "manual_checks": [],
            "metrics": [],
        },
        "contract_risks": {
            "summary": "Нужна проверка",
            "risks": [
                {
                    "risk": "Риск",
                    "severity": "warning",
                    "impact": "Влияние",
                    "mitigation": "Мера",
                }
            ],
            "manual_checks": [],
        },
        "final_recommendation": {
            "recommendation": "manual_review_required",
            "label": "нужна ручная проверка",
            "rationale": ["Ручная проверка обязательна"],
            "key_requirements": ["Требование"],
            "open_questions": ["Вопрос"],
            "risks": ["Риск"],
            "economics": ["Нет данных"],
            "manual_checks": [],
        },
        "trace": {
            "limitations": [],
            "overall_explanation": "Тест",
            "per_step": {
                "documents": "Документы",
                "requirements": "Требования",
                "questions": "Вопросы",
                "rfq": "RFQ",
                "quotes": "ТКП",
                "economics": "Экономика",
                "risks": "Риски",
                "decision": "Решение",
            },
        },
    }

    steps = _build_steps_from_outputs(metadata, outputs)

    documents_step = next(step for step in steps if step.key == "documents")
    assert documents_step.findings == ["notice.pdf"]
    assert documents_step.result_sections[0].rows == [
        {
            "Файл": "notice.pdf",
            "Расширение": ".pdf",
            "Размер": "3 bytes",
        }
    ]
