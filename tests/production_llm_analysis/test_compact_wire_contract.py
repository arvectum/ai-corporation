import json

import pytest
from pydantic import ValidationError

from src.modules.production_llm_analysis.evidence import build_evidence_packet
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)
from src.modules.production_llm_analysis.schemas import (
    CompactWireEvidenceFragment,
    CompactWireProviderResponse,
    EvidenceFragmentInput,
)
from src.modules.production_llm_analysis.service import build_production_llm_request
from src.shared.llm.transport import HTTPResponse, InvalidProviderResponseError

from .conftest import make_policy


def _request(wire="compact-safe-v1"):
    packet=build_evidence_packet(customer_id="c",project_id="p",procurement_case_id="case",run_id="run",registry_number="registry",fragments=[EvidenceFragmentInput(document_id="doc",document_name="doc.txt",chunk_id="chunk",locator={"document_order":1,"chunk_index":0},text="exact source text")])
    return build_production_llm_request(evidence_packet=packet,provider="p",provider_wire_contract_version=wire,model="m",prompt_id="p",prompt_version="v",output_schema_id="s",output_schema_version="v",grounding_policy_version="v",budget_policy=make_policy(),map_mode=True)


def test_compact_wire_schema_forbids_server_metadata():
    with pytest.raises(ValidationError):
        CompactWireProviderResponse.model_validate({"claims": [{"claim_id": "c", "field_path": "x", "value": "v", "evidence_references": [{"fragment_id": "0" * 64, "quote": "q", "locator": {}}]}]})


@pytest.mark.parametrize("value", [True, 1.0, "1", -1])
def test_document_order_is_strict(value):
    with pytest.raises(ValidationError):
        CompactWireEvidenceFragment(fragment_id="0" * 64, document_order=value, chunk_index=0, text="text")


@pytest.mark.parametrize("value", [True, 1.0, "1", -1])
def test_chunk_index_is_strict(value):
    with pytest.raises(ValidationError):
        CompactWireEvidenceFragment(fragment_id="0" * 64, document_order=0, chunk_index=value, text="text")


def test_compact_request_only_exposes_safe_fragment_fields():
    request = _request()
    adapter = OpenAICompatibleProductionLLMProvider.__new__(OpenAICompatibleProductionLLMProvider)
    body = adapter._build_request_body(request)
    task = json.loads(body["messages"][1]["content"])
    assert set(task["evidence_fragments"][0]) == {"fragment_id", "document_order", "chunk_index", "text"}
    assert "procurement_case_id" not in task and "registry_number" not in task


def test_controlled_map_rejects_full_wire():
    request = _request("full-v1")
    adapter = OpenAICompatibleProductionLLMProvider.__new__(OpenAICompatibleProductionLLMProvider)
    with pytest.raises(ValueError, match="provider_wire_contract_unsupported"):
        adapter._build_request_body(request)


def _parse(request, claims):
    adapter = OpenAICompatibleProductionLLMProvider.__new__(OpenAICompatibleProductionLLMProvider)
    adapter._clock = lambda: 0.0
    payload = {"id": "mock", "choices": [{"message": {"content": json.dumps({"claims": claims})}}]}
    return adapter._parse_success_response(response=HTTPResponse(status_code=200, headers={}, body=json.dumps(payload).encode()), request=request, attempt_latencies_ms=[], retry_count=0, analysis_started=0)


def test_compact_response_expands_canonical_reference():
    request = _request(); fragment = request.evidence_packet.fragments[0]
    result = _parse(request, [{"claim_id": "claim", "field_path": "field", "value": "exact source text", "provider_confidence": 0.9, "evidence_references": [{"fragment_id": fragment.fragment_id, "quote": "exact source text"}]}])
    reference = result.claims[0].evidence_references[0]
    assert (reference.procurement_case_id, reference.registry_number, reference.document_id, reference.document_name, reference.chunk_id, reference.locator) == (request.procurement_case_id, request.registry_number, fragment.document_id, fragment.document_name, fragment.chunk_id, fragment.locator)
    assert reference.quote_sha256


@pytest.mark.parametrize("reference", [{"fragment_id": "0" * 64, "quote": "exact source text"}, {"fragment_id": "1" * 64, "quote": "not present"}, {"fragment_id": "1" * 64, "quote": ""}])
def test_compact_response_rejects_invalid_references(reference):
    request = _request(); fragment = request.evidence_packet.fragments[0]
    if reference["fragment_id"] == "1" * 64:
        reference = {**reference, "fragment_id": fragment.fragment_id}
    with pytest.raises(InvalidProviderResponseError) as raised:
        _parse(request, [{"claim_id": "claim", "field_path": "field", "value": "v", "evidence_references": [reference]}])
    assert "exact source text" not in str(raised.value)
