from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from src.modules.procurement_analysis.frozen_types import AnalyzedDocument
from src.modules.procurement_analysis.r10_1_producer import _map_supported_claims
from src.modules.production_llm_analysis.schemas import (
    AnalysisStatus,
    ConfidenceBasis,
    EvidenceReference,
    GroundedClaim,
    SupportStatus,
)
from src.modules.tender_operator_agent_demo.report_model import (
    build_customer_report_projection,
    build_procurement_report_model,
)
from src.modules.tender_operator_agent_demo.upload_service import (
    _build_output_payloads,
    _enrich_procurement_metadata_from_documents,
    _render_customer_report_html,
)

NOTICE_XML = """<notification>
  <publishDTInEIS>2026-07-07T12:00:00+03:00</publishDTInEIS>
  <endDT>2026-07-15T12:00:00+03:00</endDT>
  <initialPrice>1234.50</initialPrice>
  <purchaseObjectInfo>Поставка тестового топлива</purchaseObjectInfo>
  <customer><fullName>Тестовый заказчик</fullName></customer>
</notification>"""


def _notice() -> AnalyzedDocument:
    return AnalyzedDocument(
        display_name="notice.xml",
        extension=".xml",
        role="notice",
        text=NOTICE_XML,
        extracted_text_available=True,
        warnings=[],
        source="manual_upload",
        file_id="notice-1",
        raw_content=NOTICE_XML.encode(),
    )


def _metadata() -> dict:
    return {
        "run_id": "sanitized-run",
        "procurement_id": "sanitized-number",
        "tender_title": "Закупка sanitized-number",
        "tender_category": "Закупка",
        "customer_name": "CUS-TEST-0001",
        "files": [{"file_id": "notice-1"}],
        "warnings": [],
        "limitations": [],
        "procurement": {},
        "status": "completed",
        "mode": "production_llm_r10_1",
        "analysis_mode": "production_llm_r10_1",
    }


def _supported_risk_claim(*, locator: dict) -> GroundedClaim:
    return GroundedClaim(
        claim_id="sanitized-risk",
        field_path="contract_risks",
        value=[
            {
                "clause": "Подтверждённое ограничение",
                "description": "Описание ограничения.",
                "classification": "deal_breaker_candidate",
                "impact": "Подтверждено источником.",
                "mitigation": "Проверить условие.",
                "operator_decision_required": True,
            }
        ],
        support_status=SupportStatus.SUPPORTED,
        evidence_references=[
            EvidenceReference(
                procurement_case_id="sanitized-case",
                registry_number="sanitized-number",
                fragment_id="a" * 64,
                document_id="sanitized-document",
                document_name="notice.xml",
                chunk_id="sanitized-chunk",
                locator=locator,
                quote="sanitized supporting text",
                quote_sha256="b" * 64,
            )
        ],
        provider_confidence=0.9,
        validated_confidence=0.9,
        confidence_basis=ConfidenceBasis.DIRECT_EXACT_EVIDENCE,
    )


def _map_risk(claim: GroundedClaim) -> list[dict]:
    result = SimpleNamespace(
        status=AnalysisStatus.SUCCESS,
        canonical_input_eligible=True,
        rejected_claims=[],
        accepted_claims=[claim],
        sanitized_error_code=None,
    )
    _requirements, risks, _questions = _map_supported_claims(result)
    return risks


def test_manual_notice_metadata_reaches_customer_report_without_internal_id(
    monkeypatch,
):
    monkeypatch.setenv("AI_CORP_SOURCE_GRAPH_MODE", "legacy")
    document = _notice()
    metadata = _enrich_procurement_metadata_from_documents(
        _metadata(),
        documents=[document],
        combined_text=NOTICE_XML,
        notice_text=NOTICE_XML,
        technical_spec_text="",
        contract_draft_text="",
    )
    outputs = _build_output_payloads(
        metadata=metadata,
        documents=[document],
        analysis_mode="production_llm_r10_1",
        requirements={},
        calibrated_risks=[],
        supplier_questions=[],
        tkp_comparison=None,
        economics=None,
        bid_decision=None,
        core_complete=False,
        quote_inputs_present=False,
    )
    model = build_procurement_report_model(metadata, outputs)

    assert model["procurement_title"] == "Поставка тестового топлива."
    assert model["customer_name"] == "Тестовый заказчик"
    assert model["publication_datetime"] == "07.07.2026 12:00:00 +03:00"
    assert model["application_deadline"] == "15.07.2026 12:00:00 +03:00"
    assert model["nmck"] == "1 234,50"
    assert "CUS-TEST-0001" not in str(model)


def test_internal_customer_identifier_is_never_rendered_without_source_value(
    monkeypatch,
):
    monkeypatch.setenv("AI_CORP_SOURCE_GRAPH_MODE", "legacy")
    outputs = _build_output_payloads(
        metadata=_metadata(),
        documents=[],
        analysis_mode="production_llm_r10_1",
        requirements={},
        calibrated_risks=[],
        supplier_questions=[],
        tkp_comparison=None,
        economics=None,
        bid_decision=None,
        core_complete=False,
        quote_inputs_present=False,
    )
    model = build_procurement_report_model(_metadata(), outputs)

    assert model["customer_name"] == "Заказчик не извлечён"


def test_claim_bound_report_drops_template_risks_and_keeps_locator_backed_risks(
    monkeypatch,
):
    monkeypatch.setenv("AI_CORP_SOURCE_GRAPH_MODE", "legacy")
    metadata = _metadata()
    risks = _map_risk(
        _supported_risk_claim(locator={"path": "xml:/notification/condition"})
    )
    outputs = _build_output_payloads(
        metadata=metadata,
        documents=[],
        analysis_mode="production_llm_r10_1",
        requirements={},
        calibrated_risks=risks,
        supplier_questions=[],
        tkp_comparison=None,
        economics=None,
        bid_decision=None,
        core_complete=False,
        quote_inputs_present=False,
    )
    model = build_procurement_report_model(metadata, outputs)

    assert [risk["risk"] for risk in model["risks"]] == [
        "Подтверждённое ограничение"
    ]
    assert model["evidence_map"][-1]["document"] == "notice.xml"
    assert model["evidence_map"][-1]["row"] == (
        "путь: xml:/notification/condition"
    )
    assert model["evidence_map"][-1]["evidence_id"] == "risk:1:locator:1"
    assert "sanitized supporting text" not in str(model["evidence_map"])
    assert not model["customer_questions"]


def test_risk_without_valid_locator_does_not_create_evidence_map_entry(
    monkeypatch,
):
    monkeypatch.setenv("AI_CORP_SOURCE_GRAPH_MODE", "legacy")
    metadata = _metadata()
    risks = _map_risk(
        _supported_risk_claim(locator={"fragment_id": "a" * 64})
    )
    assert risks[0]["evidence_locators"] == []
    risks[0]["evidence_locators"] = [
        {"document": "notice.xml", "locator": ""}
    ]
    outputs = _build_output_payloads(
        metadata=metadata,
        documents=[],
        analysis_mode="production_llm_r10_1",
        requirements={},
        calibrated_risks=risks,
        supplier_questions=[],
        tkp_comparison=None,
        economics=None,
        bid_decision=None,
        core_complete=False,
        quote_inputs_present=False,
    )
    model = build_procurement_report_model(metadata, outputs)

    assert model["risks"]
    assert model["evidence_map"] == []


def test_customer_report_uses_separate_projection_and_tonne_economics(
    monkeypatch,
):
    monkeypatch.setenv("AI_CORP_SOURCE_GRAPH_MODE", "legacy")
    metadata = _metadata()
    metadata.update(
        {
            "analysis_completed_at": "2026-07-31T23:02:05+00:00",
            "files": [
                {
                    "display_name": "a" * 64 + ".xml",
                    "role_hint": "notice",
                }
            ]
            * 5,
            "procurement": {"initial_price": 25_200_000},
        }
    )
    outputs = _build_output_payloads(
        metadata=metadata,
        documents=[],
        analysis_mode="production_llm_r10_1",
        requirements={},
        calibrated_risks=[],
        supplier_questions=[],
        tkp_comparison=None,
        economics=None,
        bid_decision=None,
        core_complete=False,
        quote_inputs_present=False,
    )
    preliminary = outputs["requirements"]["preliminary_analysis"]
    preliminary["canonical_procurement_model"] = {}
    preliminary["procurement_kind"] = "goods"
    preliminary["supply_items"] = [
        {
            "original_name": "Топливо дизельное",
            "quantity": 140,
            "unit": "т",
            "source_document": "a" * 64 + ".xml",
            "source_row_number": 1,
            "evidence_ids": ["notice:item:1"],
            "name_source_type": "structured_xml",
        }
    ]
    outputs["requirements"]["analysis_context"].update(
        {
            "procurement_category": "goods",
            "nmck": 25_200_000,
            "missing_documents": ["draft_contract"],
        }
    )
    model = build_procurement_report_model(metadata, outputs)
    snapshot = deepcopy(model)
    projection = build_customer_report_projection(model)
    rendered = _render_customer_report_html(model)

    assert model == snapshot
    assert model["line_items"][0]["quantity_status"] == "specified"
    assert model["evidence_map"][0]["evidence_id"] == "notice:item:1"
    assert model["evidence_map"][0]["short_excerpt"] == "Топливо дизельное"
    assert "evidence_id" not in projection["evidence_map"][0]
    assert "short_excerpt" not in projection["evidence_map"][0]
    assert projection["documents_count"] == 1
    assert projection["physical_files_count"] == 5
    assert projection["analysis_as_of"] == "31.07.2026 23:02 (UTC)"
    assert projection["line_items"][0]["source_display"] == (
        "Извещение о закупке — раздел «Объект закупки», позиция 1"
    )
    assert "Документы комплекта (1)" in rendered
    assert "Анализ закупки №" in rendered
    assert "180 000,00 ₽ за тонну" in rendered
    assert "НМЦК на метр" not in rendered
    assert "CUS-TEST-0001" not in rendered
    assert "canonical" not in rendered.lower()
    assert "загруженному прогону" not in rendered.lower()
    assert "a" * 64 not in rendered
    assert (
        "Извещение о закупке — раздел «Объект закупки», позиция 1"
        in rendered
    )
    assert (
        "Проект контракта не найден в предоставленном комплекте из пяти документов"
        in rendered
    )
