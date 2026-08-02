from src.modules.tender_operator_agent_demo.customer_report_contract import (
    build_customer_detail_projection,
)
from src.modules.tender_operator_agent_demo.report_model import (
    _clean_complete_document_model,
    build_customer_report_projection,
)


def test_requirement_source_hides_private_paths_and_storage_hashes():
    model = {
        "requirements": [
            {
                "title": "Предоставить декларацию",
                "detail": "Документ входит в состав заявки",
                "type": "документальное требование",
                "source": "/Users/master/private/"
                + "a" * 64
                + ".xml",
            }
        ],
        "compatibility_sections": {"contract_highlights": []},
        "risks": [],
    }

    projection = build_customer_detail_projection(model)

    assert projection["application_requirements"][0]["source"] == (
        "Документы закупки"
    )


def test_complete_corpus_cleanup_removes_stale_missing_document_question():
    model = {
        "customer_questions": [
            {"question": "Получить проект контракта", "category": "contract"},
            {"question": "Уточнить график поставки", "category": "delivery"},
        ]
    }

    _clean_complete_document_model(model, {"status": "complete"})

    assert model["customer_questions"] == [
        {"question": "Уточнить график поставки", "category": "delivery"}
    ]


def test_projection_keeps_safe_characteristics_and_single_item_okpd2():
    model = {
        "metadata": {
            "document_set_summary": {
                "status": "complete",
                "physical_file_count": 10,
                "logical_document_count": 6,
                "logical_documents": [],
            }
        },
        "line_items": [
            {
                "sequence": 1,
                "original_name": "Топливо дизельное",
                "quantity_display": "140",
                "unit_original": "т",
                "source_row": 1,
                "characteristics": ["Соответствие техническому описанию"],
            }
        ],
        "okpd2_codes": [{"code": "19.20.21.300"}],
        "customer_decision": {},
        "evidence_map": [],
        "risks": [],
        "customer_questions": [],
        "corpus_limitations": [],
    }

    projection = build_customer_report_projection(model)

    assert projection["line_items"][0]["okpd2"] == "19.20.21.300"
    assert projection["line_items"][0]["characteristics"] == [
        "Соответствие техническому описанию"
    ]
    assert projection["line_items"][0]["source_display"] == (
        "Извещение о закупке — раздел «Объект закупки», позиция 1"
    )


def test_projection_hides_uuid_evidence_document_identifier():
    model = {
        "metadata": {
            "document_set_summary": {
                "status": "complete",
                "physical_file_count": 10,
                "logical_document_count": 6,
                "logical_documents": [],
            }
        },
        "line_items": [],
        "customer_decision": {},
        "evidence_map": [
            {
                "document": "11111111-1111-4111-8111-111111111111",
                "row": 1,
            }
        ],
        "risks": [],
        "customer_questions": [],
        "corpus_limitations": [],
    }

    projection = build_customer_report_projection(model)

    assert projection["evidence_map"] == [
        {
            "document_label": "Документы закупки",
            "document_type": "подтверждающий документ",
            "location": "раздел «Объект закупки», позиция 1",
        }
    ]
