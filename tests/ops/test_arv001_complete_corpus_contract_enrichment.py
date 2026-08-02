from src.modules.tender_operator_agent_demo.customer_report_contract import (
    build_customer_detail_projection,
)
from src.modules.tender_operator_agent_demo.upload_service import (
    _default_okpd2,
    _liability_contract_highlight,
)


def test_liability_summary_requires_contract_language_and_keeps_details_conservative():
    text = (
        "Ответственность сторон. За нарушение обязательств поставщик уплачивает "
        "штраф и пени в порядке, установленном контрактом."
    )

    assert _liability_contract_highlight(text) == (
        "Проект контракта содержит условия ответственности сторон и штрафные "
        "санкции за нарушение обязательств."
    )
    assert _liability_contract_highlight("Цена контракта твердая.") is None


def test_okpd2_falls_back_to_canonical_procurement_passport():
    assert _default_okpd2(
        {
            "okpd2_codes": [{"code": "19.20.21.300"}],
            "procurement_passport": {"okpd2": "не указан"},
        }
    ) == "19.20.21.300"
    assert _default_okpd2(
        {"procurement_passport": {"okpd2": "19.20.21.300 — Топливо дизельное"}}
    ) == "19.20.21.300 — Топливо дизельное"


def test_contract_grouping_does_not_treat_contract_word_as_acceptance_act():
    projection = build_customer_detail_projection(
        {
            "requirements": [],
            "compatibility_sections": {
                "contract_highlights": [
                    "Предусмотрено обеспечение исполнения контракта.",
                    "Поставка и приёмка выполняются по условиям проекта контракта.",
                ]
            },
            "risks": [],
        }
    )

    assert projection["contract_term_groups"] == [
        {
            "title": "Поставка и приёмка",
            "items": [
                "Поставка и приёмка выполняются по условиям проекта контракта."
            ],
        },
        {
            "title": "Обеспечение",
            "items": ["Предусмотрено обеспечение исполнения контракта."],
        },
    ]
