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
                chunk_id="chunk-1",
                locator={"document_order": 0, "chunk_index": 0},
                text="Exact source sentence one.",
            ),
            EvidenceFragmentInput(
                document_id="document",
                document_name="document.txt",
                chunk_id="chunk-2",
                locator={"document_order": 0, "chunk_index": 1},
                text="Exact source sentence two.",
            ),
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
    return schema["properties"]["claims"]["items"]


def _reference_schema(schema):
    return _claim_schema(schema)["properties"]["evidence_references"]["items"]


def _walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def test_compact_response_schema_is_flat_and_batch_bound():
    request = _request()
    schema = compact_response_schema(request)

    assert schema["additionalProperties"] is False
    assert schema["properties"]["claims"]["maxItems"] == 3
    assert _claim_schema(schema)["properties"]["field_path"]["enum"] == [
        _ALLOWED_FIELD
    ]
    assert _claim_schema(schema)["properties"]["claim_id"]["maxLength"] == 128
    assert (
        _claim_schema(schema)["properties"]["evidence_references"]["minItems"]
        == 1
    )
    assert _reference_schema(schema)["properties"]["fragment_id"]["enum"] == sorted(
        fragment.fragment_id for fragment in request.evidence_packet.fragments
    )
    assert _reference_schema(schema)["properties"]["quote"]["maxLength"] == 1024
    assert all("$ref" not in item for item in _walk(schema) if isinstance(item, dict))
    assert all("$defs" not in item for item in _walk(schema) if isinstance(item, dict))


def test_llama_compact_body_uses_flat_schema_constrained_json():
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
    assert all("$ref" not in item for item in _walk(schema) if isinstance(item, dict))


def test_non_compact_body_keeps_existing_json_mode():
    request = _request(wire="full-v1")
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )

    body = build_llama_schema_constrained_request_body(adapter, request)

    assert body["response_format"] == {"type": "json_object"}
