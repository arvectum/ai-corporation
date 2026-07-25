from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.modules.procurement_analysis.frozen_producer import produce_frozen_canonical_analysis
from src.modules.procurement_analysis.frozen_types import AnalyzedDocument
from src.modules.procurement_analysis.r10_1_producer import (
    CanonicalAnalysisMode,
    R10_1AnalysisRejectedError,
    R10_1ClaimMappingError,
    R10_1IdentityError,
    produce_canonical_analysis,
    produce_r10_1_canonical_analysis,
)
from src.modules.production_llm_analysis.evidence import text_sha256
from src.modules.production_llm_analysis.schemas import (
    AnalysisStatus,
    BudgetLimits,
    BudgetPolicy,
    EvidenceReference,
    ProviderAnalysisResponse,
    ProviderClaim,
    ProviderPricing,
)


CUSTOMER_ID = "customer-1"
PROJECT_ID = "project-1"
CASE_ID = "case-1"
RUN_ID = "run-1"
REGISTRY_NUMBER = "0123456789012345678"
DOCUMENT_ID = "1" * 64
FIXED_NOW = "2026-07-25T00:00:00+00:00"


def _policy(**overrides) -> BudgetPolicy:
    values = {
        "max_input_tokens": 100_000,
        "max_output_tokens": 2_000,
        "timeout_ms": 5_000,
        "max_retries": 1,
        "max_total_latency_ms": 10_000,
        "max_estimated_cost": 10.0,
        "chars_per_token_estimate": 4,
    }
    values.update(overrides)
    return BudgetPolicy(
        limits=BudgetLimits(**values),
        pricing=ProviderPricing(
            input_cost_per_1k_tokens=0.01,
            output_cost_per_1k_tokens=0.02,
            currency="USD",
            pricing_table_version="gate4-test-v1",
        ),
    )


def _documents() -> list[AnalyzedDocument]:
    return [
        AnalyzedDocument(
            display_name="specification.txt",
            extension=".txt",
            role="technical_spec",
            text=(
                "Cable AVVG-P is required. Quantity is 10 meters. "
                "Delivery term is 20 days. Payment term is 30 calendar days after acceptance."
            ),
            extracted_text_available=True,
            warnings=[],
            source="persisted_procurement_intake",
            file_id=DOCUMENT_ID,
        )
    ]


def _metadata(**overrides):
    values = {
        "customer_id": CUSTOMER_ID,
        "project_id": PROJECT_ID,
        "run_id": RUN_ID,
        "procurement_id": REGISTRY_NUMBER,
        "tender_title": f"Закупка {REGISTRY_NUMBER}",
        "tender_category": "Закупка",
        "customer_name": CUSTOMER_ID,
        "status": "analyzing",
        "warnings": [],
        "limitations": [],
        "files": [],
        "procurement": {
            "registry_number": REGISTRY_NUMBER,
            "case_id": CASE_ID,
        },
    }
    values.update(overrides)
    return values


def _reference(request, quote: str, *, fragment_index: int = 0, **overrides) -> EvidenceReference:
    fragment = request.evidence_packet.fragments[fragment_index]
    values = {
        "procurement_case_id": request.procurement_case_id,
        "registry_number": request.registry_number,
        "fragment_id": fragment.fragment_id,
        "document_id": fragment.document_id,
        "document_name": fragment.document_name,
        "chunk_id": fragment.chunk_id,
        "locator": fragment.locator,
        "quote": quote,
        "quote_sha256": text_sha256(quote),
    }
    values.update(overrides)
    return EvidenceReference(**values)


class SupportedProvider:
    calls = 0

    def generate(self, request):
        self.calls += 1
        quote = "Cable AVVG-P"
        return ProviderAnalysisResponse(
            provider_request_id="provider-request-1",
            claims=[
                ProviderClaim(
                    claim_id="technical-requirement-1",
                    field_path="requirements.technical_requirements",
                    value=quote,
                    provider_confidence=0.99,
                    evidence_references=[_reference(request, quote)],
                )
            ],
            input_tokens=40,
            output_tokens=20,
            attempt_latencies_ms=[4],
            total_latency_ms=4,
            retry_count=0,
            raw_response_sha256="a" * 64,
        )


class UnknownPathProvider:
    def generate(self, request):
        quote = "Cable AVVG-P"
        return ProviderAnalysisResponse(
            claims=[
                ProviderClaim(
                    claim_id="unknown-1",
                    field_path="procurement.title",
                    value=quote,
                    evidence_references=[_reference(request, quote)],
                )
            ],
            input_tokens=20,
            output_tokens=10,
            total_latency_ms=1,
        )


class MixedGroundingProvider:
    def generate(self, request):
        quote = "Cable AVVG-P"
        missing = "Unsupported invented requirement"
        return ProviderAnalysisResponse(
            claims=[
                ProviderClaim(
                    claim_id="supported-1",
                    field_path="requirements.technical_requirements",
                    value=quote,
                    evidence_references=[_reference(request, quote)],
                ),
                ProviderClaim(
                    claim_id="rejected-1",
                    field_path="requirements.document_requirements",
                    value=missing,
                    evidence_references=[_reference(request, missing)],
                ),
            ],
            input_tokens=20,
            output_tokens=10,
            total_latency_ms=1,
        )


class TimeoutProvider:
    calls = 0

    def generate(self, request):
        self.calls += 1
        raise TimeoutError("secret provider details must not escape")


class PositiveDecisionProvider:
    def generate(self, request):
        quote = "Cable AVVG-P"
        return ProviderAnalysisResponse(
            claims=[
                ProviderClaim(
                    claim_id="decision-1",
                    field_path="bid_decision.recommendation",
                    value="GO",
                    evidence_references=[_reference(request, quote)],
                )
            ],
            input_tokens=20,
            output_tokens=10,
            total_latency_ms=1,
        )


def _produce(output_dir: Path, provider=None, metadata=None):
    return produce_r10_1_canonical_analysis(
        customer_id=CUSTOMER_ID,
        project_id=PROJECT_ID,
        procurement_case_id=CASE_ID,
        registry_number=REGISTRY_NUMBER,
        run_id=RUN_ID,
        output_dir=output_dir,
        metadata=metadata or _metadata(),
        documents=_documents(),
        provider=provider or SupportedProvider(),
        budget_policy=_policy(),
        provider_name="fake-provider",
        model="fake-model-v1",
    )


def test_supported_claims_produce_verified_canonical_output(monkeypatch, tmp_path):
    from src.modules.tender_operator_agent_demo import upload_service

    monkeypatch.setattr(upload_service, "_safe_datetime", lambda: FIXED_NOW)
    production = _produce(tmp_path)

    assert production.llm_result.status == AnalysisStatus.SUCCESS
    assert production.llm_result.canonical_input_eligible is True
    assert production.persisted.canonical_report_path.exists()
    assert production.persisted.requirements_path.exists()
    assert production.source_graph_hash == production.persisted.source_graph_hash

    canonical = json.loads(production.persisted.canonical_report_bytes)
    provenance = canonical["ai_runtime_provenance"]
    assert provenance["producer"] == "production_llm_r10_1"
    assert provenance["request_id"] == production.llm_result.request_id
    assert provenance["evidence_packet_hash"] == production.llm_result.evidence_packet_hash
    assert provenance["accepted_claims"][0]["claim_id"] == "technical-requirement-1"
    assert provenance["accepted_claims"][0]["validated_confidence"] == 0.95
    assert provenance["raw_response_stored"] is False


def test_unknown_field_path_blocks_publication(monkeypatch, tmp_path):
    from src.modules.tender_operator_agent_demo import upload_service

    monkeypatch.setattr(upload_service, "_safe_datetime", lambda: FIXED_NOW)
    with pytest.raises(R10_1ClaimMappingError, match="unknown_field_path"):
        _produce(tmp_path, provider=UnknownPathProvider())
    assert not (tmp_path / "canonical_report.json").exists()
    assert not (tmp_path / "requirements.json").exists()


def test_mixed_supported_and_rejected_claims_fail_closed(tmp_path):
    with pytest.raises(R10_1AnalysisRejectedError):
        _produce(tmp_path, provider=MixedGroundingProvider())
    assert list(tmp_path.iterdir()) == []


def test_positive_provider_decision_is_rejected_before_mapping(tmp_path):
    with pytest.raises(R10_1AnalysisRejectedError):
        _produce(tmp_path, provider=PositiveDecisionProvider())
    assert list(tmp_path.iterdir()) == []


def test_timeout_has_no_frozen_or_stub_fallback(tmp_path):
    provider = TimeoutProvider()
    with pytest.raises(R10_1AnalysisRejectedError, match="provider_timeout"):
        produce_canonical_analysis(
            mode=CanonicalAnalysisMode.PRODUCTION_LLM_R10_1,
            customer_id=CUSTOMER_ID,
            project_id=PROJECT_ID,
            procurement_case_id=CASE_ID,
            registry_number=REGISTRY_NUMBER,
            run_id=RUN_ID,
            output_dir=tmp_path,
            metadata=_metadata(),
            documents=_documents(),
            provider=provider,
            budget_policy=_policy(),
            provider_name="fake-provider",
            model="fake-model-v1",
        )
    assert provider.calls == 1
    assert list(tmp_path.iterdir()) == []


def test_owned_identity_mismatch_is_rejected_before_provider_call(tmp_path):
    provider = SupportedProvider()
    with pytest.raises(R10_1IdentityError, match="metadata_project_id_mismatch"):
        _produce(tmp_path, provider=provider, metadata=_metadata(project_id="other-project"))
    assert provider.calls == 0
    assert list(tmp_path.iterdir()) == []


def test_same_inputs_keep_stable_r10_1_identities_and_bytes(monkeypatch, tmp_path):
    from src.modules.tender_operator_agent_demo import upload_service

    monkeypatch.setattr(upload_service, "_safe_datetime", lambda: FIXED_NOW)
    first = _produce(tmp_path / "first")
    second = _produce(tmp_path / "second")

    assert first.llm_result.request_id == second.llm_result.request_id
    assert first.llm_result.evidence_packet_hash == second.llm_result.evidence_packet_hash
    assert first.llm_result.validated_result_hash == second.llm_result.validated_result_hash
    assert first.source_analysis_run_id == second.source_analysis_run_id
    assert first.persisted.requirements_bytes == second.persisted.requirements_bytes
    assert first.persisted.canonical_report_bytes == second.persisted.canonical_report_bytes


def test_frozen_dispatcher_is_byte_identical_to_direct_frozen_producer(monkeypatch, tmp_path):
    from src.modules.tender_operator_agent_demo import upload_service

    monkeypatch.setattr(upload_service, "_safe_datetime", lambda: FIXED_NOW)
    source_run_id = "frozen-source-run"
    direct = produce_frozen_canonical_analysis(
        registry_number=REGISTRY_NUMBER,
        run_id=RUN_ID,
        output_dir=tmp_path / "direct",
        metadata=_metadata(),
        documents=_documents(),
        source_analysis_run_id=source_run_id,
    )
    dispatched = produce_canonical_analysis(
        mode=CanonicalAnalysisMode.FROZEN_R9,
        registry_number=REGISTRY_NUMBER,
        run_id=RUN_ID,
        output_dir=tmp_path / "dispatched",
        metadata=_metadata(),
        documents=_documents(),
        source_analysis_run_id=source_run_id,
    )

    assert dispatched.source_analysis_run_id == direct.source_analysis_run_id
    assert dispatched.persisted.requirements_bytes == direct.persisted.requirements_bytes
    assert dispatched.persisted.canonical_report_bytes == direct.persisted.canonical_report_bytes
    assert dispatched.source_graph_hash == direct.source_graph_hash
    assert dispatched.production_model_hash == direct.production_model_hash
    assert dispatched.report_model_hash == direct.report_model_hash
