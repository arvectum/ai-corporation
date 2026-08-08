import pytest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.modules.production_llm_analysis.openai_compatible import OpenAICompatibleProductionLLMProvider, OpenAICompatibleTransportConfig
from src.modules.production_llm_analysis.schemas import ProductionLLMAnalysisRequest, BudgetPolicy, BudgetLimits, ProviderPricing, EvidenceFragmentInput
from src.modules.production_llm_analysis.evidence import build_evidence_packet
from src.modules.production_llm_analysis.llama_schema_constraint import install_llama_schema_constraint

def create_mock_request():
    fragments = [EvidenceFragmentInput(document_id="doc1", document_name="doc1.pdf", chunk_id="chunk1", locator={"page": 1}, text="This is a test claim value.")]
    packet = build_evidence_packet(customer_id="cust1", project_id="proj1", procurement_case_id="case1", run_id="run1", registry_number="reg1", fragments=fragments)
    budget = BudgetPolicy(limits=BudgetLimits(max_input_tokens=1000, max_output_tokens=4096, timeout_ms=60000, max_retries=0, max_total_latency_ms=60000, max_estimated_cost=1.0), pricing=ProviderPricing(input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=0.0, pricing_table_version="v1"))
    return ProductionLLMAnalysisRequest(request_id="a" * 64, customer_id="cust1", project_id="proj1", procurement_case_id="case1", run_id="run1", registry_number="reg1", provider="openai_compatible", model="arvectum-gemma4-12b-it-qat-q4_0", prompt_id="p1", prompt_version="v1", output_schema_id="s1", output_schema_version="v1", grounding_policy_version="g1", evidence_packet=packet, budget_policy=budget, provider_wire_contract_version="compact-safe-v2", max_claims=3, allowed_field_paths=[], context_profile="32k", tokenizer_identity="tok1", evidence_budget=20000, chat_template_overhead=32, execution_deadline_ms=7200000, batch_plan_hash="b" * 64, batch_ordinal=1, batch_count=1)

def mock_sentinel_response():
    return {"choices": [{ "message": { "content": json.dumps({ "claims": [{ "claim_id": "__ARVECTUM_SERVER_CLAIM_ID__", "field_path": "requirements.technical_requirements", "value": "__ARVECTUM_SERVER_FRAGMENT_VALUE__", "evidence_references": [{ "fragment_id": "doc1:fulltext:v1", "quote": "__ARVECTUM_SERVER_FRAGMENT_QUOTE__" }] }]}}}]}

def test_sentinel_rewrite_success_with_adapter():
    install_llama_schema_constraint()
    request = create_mock_request()
    provider = OpenAICompatibleProductionLLMProvider(config=OpenAICompatibleTransportConfig(base_url='http://localhost', api_key='key'))
    class MockResponse:
        def __init__(self, body):
            self.body = body; self.status_code = 200; self.headers = {}
    response_obj = MockResponse(json.dumps(mock_sentinel_response()).encode('utf-8'))
    result = provider._parse_success_response(response=response_obj, request=request, retry_count=0, attempt_latencies_ rimu_latencies_ms=(), total_latency_ms=0)
    assert result.claims[0].claim_id != "__ARVECTUM_SERVER_CLAIM_ID__"
    assert result.claims[0].value == "This is a test claim value."
    assert result.claims[0].evidence_references[0].quote == "This is a test claim value."

def test_invalid_response_diagnostic_preserved():
    install_llama_schema_constraint()
    request = create_mock_request()
    provider = OpenAICompatibleProductionLLMProvider(config=OpenAICompatibleTransportConfig(base_url='http://localhost', api_key='key'))
    invalid_response = {"choices": [{ "message": { "content": json.dumps({ "claims": [{ "claim_id": "__ARVECTUM_SERVER_CLAIM_ID__", "field_path": "requirements.technical_requirements", "value": "__ARVECTUM_SERVER_FRAGMENT_VALUE__", "evidence_references": [{ "fragment_id": "doc1:fulltext:v1", "quote": "WRONG QUOTE"}] }]}}}]}
    class MockResponse:
        def __init__(self, body):
            self.body = body; self.status_code = 200; self.headers = {}
    response_obj = MockResponse(json.dumps(invalid_response).encode('utf-8'))
    with pytest.raises(Exception) as excinfo:
        provider._parse_success_response(response=response_obj, request=request, retry_count=0, attempt_latencies_ms=(), total_latency_ms=0)
    assert "provider_wire_quote_not_found" in str(excinfo.value)

def test_final_body_verification_enabled():
    from src.modules.production_llm_analysis.openai_compatible import enable_live_boundary_verification
    enable_live_boundary_verification()
    request = create_mock_request()
    provider = OpenAICompatibleProductionLLMProvider(config=OpenAICompatibleTransportConfig(base_url='http://localhost', api_key='key'))
    from src.modules.production_llm_analysis.openai_compatible import verify_final_live_request_body
    body = provider._build_request_body(request)
    verification = verify_final_live_request_body(body, request)
    assert verification["final_request_body_sha256"] is not None
    assert verification["enable_thinking_false"] is True
    assert verification["reasoning_format"] == "none"
