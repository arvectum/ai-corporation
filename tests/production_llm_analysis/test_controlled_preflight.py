from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from src.modules.customer_pilot.models import ProcurementCase
from src.modules.production_llm_analysis.controlled_preflight import (
    collect_controlled_provider_preflight,
    resolve_provider_preflight,
)
from src.shared.config.settings import Settings
from src.tender_research.models import (
    ProcurementDocumentChunk,
    ProcurementTender,
    ProcurementTenderDocument,
    TenderAnalysisRun,
)


REGISTRY_NUMBER = "0352300080626000109"


def _settings(**overrides) -> Settings:
    values = {
        "llm_provider": "openai_compatible",
        "llm_model": "approved-model-v1",
        "openai_api_key": "sensitive-api-key",
        "openai_base_url": "https://api.example.test/v1?token=must-not-leak",
    }
    values.update(overrides)
    return Settings(**values)


def _customer_run(
    session: Session,
    *,
    with_chunk: bool = True,
    run_status: str = "completed",
) -> None:
    session.add(
        ProcurementCase(
            id="case-1",
            customer_id="customer-1",
            project_id="project-1",
            procurement_number=REGISTRY_NUMBER,
            status="operator_review",
            artifact_key="case-artifact-1",
        )
    )
    session.add(
        TenderAnalysisRun(
            id="run-1",
            registry_number=REGISTRY_NUMBER,
            status=run_status,
            customer_id="customer-1",
            project_id="project-1",
            procurement_case_id="case-1",
        )
    )
    session.add(
        ProcurementTender(
            id="tender-1",
            source="eis",
            external_id="external-1",
            registry_number=REGISTRY_NUMBER,
            title="Controlled procurement",
        )
    )
    session.add(
        ProcurementTenderDocument(
            id="document-1",
            tender_id="tender-1",
            file_name="specification.pdf",
            download_status="ready",
            text_extraction_status="completed",
            document_identity_hash="a" * 64,
        )
    )
    if with_chunk:
        session.add(
            ProcurementDocumentChunk(
                id="chunk-1",
                tender_id="tender-1",
                document_id="document-1",
                chunk_index=0,
                text="Cable AVVG-P is required.",
                text_hash="b" * 64,
                char_start=0,
                char_end=27,
                token_estimate=7,
            )
        )
    session.flush()


def test_provider_preflight_records_presence_without_secret_or_url_query() -> None:
    configuration = resolve_provider_preflight(_settings())
    assert configuration.configuration_ready is True
    assert configuration.credential_present is True
    assert configuration.endpoint_host == "api.example.test"
    serialized = json.dumps(configuration.as_dict(), sort_keys=True)
    assert "sensitive-api-key" not in serialized
    assert "must-not-leak" not in serialized


def test_eligible_customer_owned_run_is_reported_without_document_text(
    session: Session,
) -> None:
    _customer_run(session)
    report = collect_controlled_provider_preflight(session, _settings())
    assert report["ready_for_controlled_execution"] is True
    assert report["eligible_run_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["eligible_for_gate5"] is True
    assert candidate["reason_codes"] == []
    assert candidate["document_count"] == 1
    assert candidate["extracted_document_count"] == 1
    assert candidate["chunk_count"] == 1
    assert candidate["token_estimate"] == 7
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert "Cable AVVG-P is required." not in serialized
    assert "sensitive-api-key" not in serialized
    assert "/Users/" not in serialized


def test_missing_chunks_and_provider_configuration_fail_closed(session: Session) -> None:
    _customer_run(session, with_chunk=False)
    report = collect_controlled_provider_preflight(
        session,
        _settings(llm_provider="stub", llm_model=None, openai_api_key=None),
    )
    assert report["configuration"]["configuration_ready"] is False
    assert report["ready_for_controlled_execution"] is False
    assert report["eligible_run_count"] == 0
    assert report["candidates"][0]["reason_codes"] == ["extracted_chunks_missing"]


def test_unfinished_analysis_run_is_not_eligible(session: Session) -> None:
    _customer_run(session, run_status="running")
    report = collect_controlled_provider_preflight(session, _settings())
    assert report["ready_for_controlled_execution"] is False
    assert report["eligible_run_count"] == 0
    assert report["candidates"][0]["reason_codes"] == [
        "analysis_run_not_completed"
    ]


def test_preflight_limit_is_bounded(session: Session) -> None:
    with pytest.raises(ValueError, match="preflight_limit_out_of_range"):
        collect_controlled_provider_preflight(session, _settings(), limit=0)
    with pytest.raises(ValueError, match="preflight_limit_out_of_range"):
        collect_controlled_provider_preflight(session, _settings(), limit=101)
