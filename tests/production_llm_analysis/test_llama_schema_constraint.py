from __future__ import annotations

from src.modules.production_llm_analysis.evidence import build_evidence_packet
from src.modules.production_llm_analysis.llama_schema_constraint import (
    build_llama_schema_constrained_request_body,
    compact_response_schema,
)
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)
from src.modules.production_llm_analysis.schemas import EvidenceFragmentInput
from src.modules.production_llm_analysis.service import build_production_llm_request

from .conftest import make_policy

_ALLOWED_FIELD = "requirements.technical_requirements"


def _request(*, wire: str = "compact-safe-v1"):
    packet = build_evidence_packet(
        customer_id="customer",
        project_id="project",
        procurement_case_id="case",
        run_id="run",
        registry_number="registry",
        fragments=[
            EvidenceFragmentInput(
                document_id="document",
                document_name="document.txt",
                chunk_id="chunk",
                locator={"document_order": 0, "chunk_index": 0},
                text="Exact source sentence.",
            )
        ],
    )
    return build_production_llm_request(
        evidence_packet=packet,
        provider="openai_compatible",
        provider_wire_contract_version=wire,
        model="arvectum-gemma4-12b-q4km",
        prompt_id="r10.1-batched-compact",
        prompt_version="v2",
        output_schema_id="r10.1-map",
        output_schema_version="v2",
        grounding_policy_version="v1",
        budget_policy=make_policy(),
        map_mode=wire == "compact-safe-v1",
        max_claims=3,
        allowed_field_paths=[_ALLOWED_FIELD],
    )


def _claim_schema(schema):
    items = schema["properties"]["claims"]["items"]
    reference = items["$ref"]
    assert reference.startswith("#/$defs/")
    return schema["$defs"][reference.rsplit("/", 1)[-1]]


def test_compact_response_schema_applies_claim_and_field_limits():
    schema = compact_response_schema(_request())

    assert schema["additionalProperties"] is False
    assert schema["properties"]["claims"]["maxItems"] == 3
    claim_schema = _claim_schema(schema)
    assert claim_schema["properties"]["field_path"]["enum"] == [_ALLOWED_FIELD]


def test_llama_compact_body_uses_schema_constrained_json():
    request = _request()
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )

    body = build_llama_schema_constrained_request_body(adapter, request)

    assert body["response_format"]["type"] == "json_object"
    schema = body["response_format"]["schema"]
    assert schema["properties"]["claims"]["maxItems"] == 3
    assert _claim_schema(schema)["properties"]["field_path"]["enum"] == [
        _ALLOWED_FIELD
    ]


def test_non_compact_body_keeps_existing_json_mode():
    request = _request(wire="full-v1")
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )

    body = build_llama_schema_constrained_request_body(adapter, request)

    assert body["response_format"] == {"type": "json_object"}
