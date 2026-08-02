from src.modules.tender_operator_agent_demo.report_model import (
    _clean_complete_document_model,
)


def test_complete_document_set_removes_notice_only_conclusions():
    model = {
        "customer_decision": {
            "reasons": [
                "Проект контракта не найден в предоставленном комплекте.",
                "Основные реквизиты подтверждены.",
            ],
            "next_action": "Сначала необходимо запросить отсутствующие документы.",
        },
        "corpus_limitations": [
            "Проект контракта не найден в предоставленном комплекте из пяти документов.",
            "Нет подтверждённой собственной себестоимости.",
        ],
        "limitations": ["Отсутствует проект контракта", "Нет supplier profile"],
        "missing_data": [
            {"description": "Проект контракта отсутствует"},
            {"description": "Нет коммерческих предложений"},
        ],
        "bid_decision": {
            "blockers": ["Отсутствует проект контракта"],
            "conditions": ["Получить проект контракта", "Проверить себестоимость"],
            "rationale": ["Основные реквизиты подтверждены"],
        },
        "document_coverage": {
            "missing": ["Проект контракта"],
            "impact": "Договорный анализ ограничен",
        },
        "action_plan": ["Проверить поставщика и логистику."],
    }

    _clean_complete_document_model(model, {"status": "complete"})

    assert model["customer_decision"]["reasons"] == [
        "Основные реквизиты подтверждены.",
        "Техническая документация и проект контракта включены в комплект анализа.",
    ]
    assert model["customer_decision"]["next_action"] == (
        "Проверить поставщика и логистику."
    )
    assert model["corpus_limitations"] == [
        "Нет подтверждённой собственной себестоимости."
    ]
    assert model["limitations"] == ["Нет supplier profile"]
    assert model["missing_data"] == [
        {"description": "Нет коммерческих предложений"}
    ]
    assert model["bid_decision"]["blockers"] == []
    assert model["bid_decision"]["conditions"] == ["Проверить себестоимость"]
    assert model["document_coverage"] == {"missing": [], "impact": ""}


def test_incomplete_document_set_keeps_missing_document_conclusions():
    model = {
        "customer_decision": {
            "reasons": ["Проект контракта не найден."],
            "next_action": "Получить проект контракта.",
        }
    }

    _clean_complete_document_model(model, {"status": "incomplete"})

    assert model["customer_decision"]["reasons"] == [
        "Проект контракта не найден."
    ]
    assert model["customer_decision"]["next_action"] == (
        "Получить проект контракта."
    )
