from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.modules.procurement_analysis.frozen_types import AnalyzedDocument
from src.modules.procurement_analysis.r10_1_producer import R10_1AnalysisRejectedError
from src.modules.production_llm_analysis import controlled_evidence
from src.modules.production_llm_analysis.controlled_evidence import (
    ApprovedControlledProviderPolicy,
    ControlledEvidenceConflictError,
    ControlledEvidenceError,
    build_sanitized_controlled_evidence_manifest,
    load_approved_provider_policy,
    run_controlled_provider_evidence,
)
from src.modules.production_llm_analysis.evidence import text_sha256
from src.modules.production_llm_analysis.schemas import (
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
DOCUMENT_TEXT = (
    "Cable AVVG-P is required. Quantity is 10 meters. "
    "Delivery term is 20 days. Payment term is 30 calendar days after acceptance."
)


def _policy() -> ApprovedControlledProviderPolicy:
    return ApprovedControlledProviderPolicy(
        policy_version="gate5-approved-test-v1",
        provider="approved-provider",
        model="approved-model-v1",
        budget=BudgetPolicy(
            limits=BudgetLimits(
                max_input_tokens=100_000,
                max_output_tokens=2_000,
                timeout_ms=5_000,
                max_retries=1,
                max_total_latency_ms=10_000,
                max_estimated_cost=10.0,
                chars_per_token_estimate=4,
            ),
            pricing=ProviderPricing(
                input_cost_per_1k_tokens=0.01,
                output_cost_per_1k_tokens=0.02,
                currency="USD",
                pricing_table_version="gate5-pricing-test-v1",
            ),
        ),
    )


def _documents() -> list[AnalyzedDocument]:
    return [
        AnalyzedDocument(
            display_name="specification.txt",
            extension=".txt",
            role="technical_spec",
            text=DOCUMENT_TEXT,
            extracted_text_available=True,
            warnings=[],
            source="persisted_procurement_intake",
            file_id=DOCUMENT_ID,
        )
    ]


def _metadata() -> dict:
    return {
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


def _reference(request, quote: str) -> EvidenceReference:
    fragment = request.evidence_packet.fragments[0]
    return EvidenceReference(
        procurement_case_id=request.procurement_case_id,
        registry_number=request.registry_number,
        fragment_id=fragment.fragment_id,
        document_id=fragment.document_id,
        document_name=fragment.document_name,
        chunk_id=fragment.chunk_id,
        locator=fragment.locator,
        quote=quote,
        quote_sha256=text_sha256(quote),
    )


class StableProvider:
    def __init__(self, *, sequence: int = 1):
        self.sequence = sequence

    def generate(self, request):
        quote = "Cable AVVG-P is required."
        return ProviderAnalysisResponse(
            provider_request_id=f"provider-request-{self.sequence}",
            claims=[
                ProviderClaim(
                    claim_id="technical-requirement-1",
                    field_path="requirements.technical_requirements",
                    value=quote,
                    provider_confidence=0.90 + self.sequence / 100,
                    evidence_references=[_reference(request, quote)],
                )
            ],
            input_tokens=99 + self.sequence,
            output_tokens=29 + self.sequence,
            attempt_latencies_ms=[4 + self.sequence],
            total_latency_ms=4 + self.sequence,
            retry_count=0,
            raw_response_sha256=("a" if self.sequence == 1 else "b") * 64,
        )


class DivergentProvider:
    def __init__(self, state: dict[str, int]):
        self.state = state

    def generate(self, request):
        self.state["calls"] += 1
        if self.state["calls"] == 1:
            quote = "Cable AVVG-P is required."
            field_path = "requirements.technical_requirements"
            claim_id = "technical-requirement-1"
        else:
            quote = "Delivery term is 20 days."
            field_path = "requirements.document_requirements"
            claim_id = "document-requirement-1"
        return ProviderAnalysisResponse(
            provider_request_id=f"provider-request-{self.state['calls']}",
            claims=[
                ProviderClaim(
                    claim_id=claim_id,
                    field_path=field_path,
                    value=quote,
                    evidence_references=[_reference(request, quote)],
                )
            ],
            input_tokens=100,
            output_tokens=30,
            total_latency_ms=5,
        )


class TimeoutProvider:
    def generate(self, request):
        raise TimeoutError("sensitive upstream timeout")


def _run(output_root: Path, provider_factory):
    return run_controlled_provider_evidence(
        output_root=output_root,
        customer_id=CUSTOMER_ID,
        project_id=PROJECT_ID,
        procurement_case_id=CASE_ID,
        registry_number=REGISTRY_NUMBER,
        run_id=RUN_ID,
        metadata=_metadata(),
        documents=_documents(),
        provider_factory=provider_factory,
        policy=_policy(),
    )


def test_matching_semantics_publish_despite_volatile_provider_metadata(tmp_path):
    output_root = tmp_path / "controlled"
    state = {"created": 0}

    def provider_factory():
        state["created"] += 1
        return StableProvider(sequence=state["created"])

    bundle = _run(output_root, provider_factory)

    assert bundle.manifest_path.exists()
    assert (
        bundle.manifest["manifest_version"] == "r10.1-controlled-provider-evidence-v3"
    )
    assert bundle.manifest["repeat_count"] == 2
    assert bundle.manifest["repeat_identity_verified"] is True
    assert (
        bundle.manifest["stable_identity"]["request_id"]
        == bundle.first.llm_result.request_id
    )
    assert (
        bundle.manifest["stable_identity"]["evidence_packet_hash"]
        == bundle.second.llm_result.evidence_packet_hash
    )
    assert bundle.manifest["stable_identity"]["grounded_claims_hash"]
    for field in (
        "provider_wire_contract_version",
        "prompt_id",
        "prompt_version",
        "output_schema_id",
        "output_schema_version",
        "grounding_policy_version",
        "batch_plan_version",
    ):
        assert bundle.manifest["stable_identity"][field] == getattr(
            bundle.first.llm_result, field
        )
        assert bundle.manifest["stable_identity"][field] == getattr(
            bundle.second.llm_result, field
        )
    assert bundle.manifest["wire_contract"] == {
        "provider_wire_contract_version": "compact-safe-v1",
        "input_fragment_schema": [
            "fragment_id",
            "document_order",
            "chunk_index",
            "text",
        ],
        "output_reference_schema": ["fragment_id", "quote"],
        "server_side_reference_expansion": True,
        "full_grounding_revalidation": True,
        "provider_metadata_authority": False,
    }
    assert (
        bundle.manifest["executions"][0]["provider_request_id"]
        != bundle.manifest["executions"][1]["provider_request_id"]
    )
    assert (
        bundle.manifest["executions"][0]["validated_result_hash"]
        != bundle.manifest["executions"][1]["validated_result_hash"]
    )
    assert bundle.manifest["executions"][0]["budget"]["actual_input_tokens"] == 100
    assert bundle.manifest["executions"][1]["budget"]["actual_input_tokens"] == 101
    assert bundle.manifest["executions"][0]["raw_response_stored"] is False

    text = bundle.manifest_path.read_text(encoding="utf-8")
    assert DOCUMENT_TEXT not in text
    assert "Cable AVVG-P is required." not in text
    assert "sensitive-api-key" not in text
    assert "/Users/" not in text
    assert "quote_sha256" in text
    assert (output_root / "execution-1" / "canonical_report.json").exists()
    assert (output_root / "execution-2" / "canonical_report.json").exists()


def test_manifest_is_sanitized_and_hash_is_deterministic(tmp_path):
    bundle = _run(tmp_path / "controlled", StableProvider)
    repeat = build_sanitized_controlled_evidence_manifest(
        policy=_policy(),
        productions=[bundle.first, bundle.second],
    )

    serialized = json.dumps(bundle.manifest, sort_keys=True)
    assert bundle.manifest["manifest_hash"]
    assert bundle.manifest["manifest_hash"] == repeat["manifest_hash"]
    for prohibited in (
        DOCUMENT_TEXT,
        "Cable AVVG-P is required.",
        DOCUMENT_ID,
        "specification.txt",
        REGISTRY_NUMBER,
        CUSTOMER_ID,
        PROJECT_ID,
        RUN_ID,
        "/Users/",
        "postgresql://",
        "sensitive-api-key",
    ):
        assert prohibited not in serialized


@pytest.mark.parametrize(
    "field",
    [
        "provider_wire_contract_version",
        "prompt_id",
        "prompt_version",
        "output_schema_id",
        "output_schema_version",
        "grounding_policy_version",
        "batch_plan_version",
    ],
)
def test_execution_contract_drift_fails_closed_without_publication(
    tmp_path, monkeypatch, field
):
    source = _run(tmp_path / "source", StableProvider)
    changed_value = "drift-value"
    mutated_result = source.second.llm_result.model_copy(update={field: changed_value})
    productions = iter(
        (source.first, replace(source.second, llm_result=mutated_result))
    )
    monkeypatch.setattr(
        controlled_evidence,
        "produce_r10_1_canonical_analysis",
        lambda **_kwargs: next(productions),
    )
    output_root = tmp_path / "controlled"

    with pytest.raises(
        ControlledEvidenceConflictError,
        match="controlled_evidence_execution_contract_mismatch",
    ) as raised:
        _run(output_root, StableProvider)

    message = str(raised.value)
    assert changed_value not in message
    assert all(
        value not in message
        for value in (CUSTOMER_ID, CASE_ID, REGISTRY_NUMBER, "/Users/")
    )
    assert not output_root.exists()
    assert not list(tmp_path.glob(".controlled.partial.*"))


def test_divergent_provider_outputs_fail_closed_without_publication(tmp_path):
    output_root = tmp_path / "controlled"
    state = {"calls": 0}

    with pytest.raises(
        ControlledEvidenceConflictError,
        match="controlled_evidence_repeat_identity_mismatch",
    ):
        _run(output_root, lambda: DivergentProvider(state))

    assert not output_root.exists()
    assert not list(tmp_path.glob(".controlled.partial.*"))


def test_provider_failure_removes_partial_customer_outputs(tmp_path):
    output_root = tmp_path / "controlled"

    with pytest.raises(R10_1AnalysisRejectedError):
        _run(output_root, TimeoutProvider)

    assert not output_root.exists()
    assert not list(tmp_path.glob(".controlled.partial.*"))


def test_approved_policy_rejects_credentials_and_unknown_fields(tmp_path):
    path = tmp_path / "policy.json"
    payload = _policy().model_dump(mode="json")
    payload["api_key"] = "must-not-be-accepted"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ControlledEvidenceError, match="approved_provider_policy_invalid"
    ):
        load_approved_provider_policy(path)


def test_existing_output_root_is_never_overwritten(tmp_path):
    output_root = tmp_path / "controlled"
    output_root.mkdir()
    marker = output_root / "marker.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(
        ControlledEvidenceConflictError,
        match="controlled_evidence_target_exists",
    ):
        _run(output_root, StableProvider)

    assert marker.read_text(encoding="utf-8") == "keep"
