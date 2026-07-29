from src.modules.production_llm_analysis.batching import (
    BatchPolicy,
    OutputBudgetMismatch,
)
from src.modules.production_llm_analysis.evidence import build_evidence_packet
from src.modules.production_llm_analysis.schemas import (
    BudgetLimits,
    BudgetPolicy,
    ProviderAnalysisResponse,
    ProviderPricing,
)
from src.modules.production_llm_analysis.service import (
    build_production_llm_request,
    run_production_llm_analysis,
)


def _policy(*, output_tokens: int = 4096) -> BudgetPolicy:
    return BudgetPolicy(
        limits=BudgetLimits(
            max_input_tokens=10000, max_output_tokens=output_tokens, timeout_ms=1000,
            max_retries=0, max_total_latency_ms=1000, max_estimated_cost=10,
        ),
        pricing=ProviderPricing(
            input_cost_per_1k_tokens=0, output_cost_per_1k_tokens=0,
            pricing_table_version="test", currency="USD",
        ),
    )


def _request(**overrides):
    packet = build_evidence_packet(
        customer_id="c", project_id="p", procurement_case_id="case",
        run_id="run", registry_number="registry", fragments=[{
            "document_id": "doc", "document_name": "doc.txt", "chunk_id": "chunk-1",
            "locator": {"chunk_index": 1}, "text": "The delivery term is twenty days.",
        }],
    )
    return build_production_llm_request(
        evidence_packet=packet, provider="fake", model="fake", prompt_id="p",
        prompt_version="v", output_schema_id="s", output_schema_version="v",
        grounding_policy_version="g", budget_policy=_policy(), map_mode=True,
        max_claims=3, allowed_field_paths=["requirements.technical_requirements"],
        **overrides,
    )


class EmptyProvider:
    def generate(self, request):
        return ProviderAnalysisResponse(input_tokens=1, output_tokens=1)


def test_empty_map_batch_is_success_without_negative_claim():
    result = run_production_llm_analysis(_request(), EmptyProvider())
    assert result.status.value == "success"
    assert result.map_empty is True
    assert result.accepted_claims == []
    assert result.rejected_claims == []


def test_approved_profile_rejects_output_budget_mismatch():
    try:
        BatchPolicy.approved_32k(tokenizer_identity="pinned-tokenizer").validate(_policy(output_tokens=2000), controlled=True)
    except OutputBudgetMismatch as exc:
        assert exc.code == "evidence_batch_output_budget_mismatch"
    else:
        raise AssertionError("output reserve mismatch was accepted")
