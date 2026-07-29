import json

import pytest
from pydantic import ValidationError

from src.modules.production_llm_analysis.evidence import build_evidence_packet
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)
from src.modules.production_llm_analysis.schemas import (
    CompactWireProviderResponse,
    EvidenceFragmentInput,
)
from src.modules.production_llm_analysis.service import build_production_llm_request

from .conftest import make_policy


def _request(wire="compact-safe-v1"):
    packet=build_evidence_packet(customer_id="c",project_id="p",procurement_case_id="case",run_id="run",registry_number="registry",fragments=[EvidenceFragmentInput(document_id="doc",document_name="doc.txt",chunk_id="chunk",locator={"document_order":1,"chunk_index":0},text="exact source text")])
    return build_production_llm_request(evidence_packet=packet,provider="p",provider_wire_contract_version=wire,model="m",prompt_id="p",prompt_version="v",output_schema_id="s",output_schema_version="v",grounding_policy_version="v",budget_policy=make_policy(),map_mode=True)


def test_compact_wire_schema_forbids_server_metadata():
    with pytest.raises(ValidationError):
        CompactWireProviderResponse.model_validate({"claims": [{"claim_id": "c", "field_path": "x", "value": "v", "evidence_references": [{"fragment_id": "0" * 64, "quote": "q", "locator": {}}]}]})


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
