from __future__ import annotations

import hashlib
import json

import pytest

from src.modules.procurement_analysis.r10_1_producer import R10_1AnalysisRejectedError
from src.modules.production_llm_analysis.evidence import build_evidence_packet
from src.modules.production_llm_analysis.grounding import validate_provider_claims
from src.modules.production_llm_analysis.llama_schema_constraint import (
    _LLAMA_SCHEMA_PROFILE,
    _SERVER_CLAIM_ID_SENTINEL,
    _SERVER_FRAGMENT_QUOTE_SENTINEL,
    _SERVER_FRAGMENT_VALUE_SENTINEL,
    _parse_success_response_with_safe_diagnostics,
    _run_production_analysis_with_safe_diagnostics,
    build_llama_schema_constrained_request_body,
    compact_response_schema,
)
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)
from src.modules.production_llm_analysis.schemas import (
    EvidenceFragmentInput,
    SupportStatus,
)
from src.modules.production_llm_analysis.service import build_production_llm_request
from src.shared.llm.transport import HTTPResponse

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
        batch_plan_version="test-plan",
        batch_plan_hash="1" * 64,
        batch_hash="2" * 64,
        batch_ordinal=1,
        batch_count=1,
        corpus_evidence_hash="3" * 64,
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


def _payload(*, fragment_id: str, value: str, quote: str, claim_id: str = _SERVER_CLAIM_ID_SENTINEL):
    return {
        "id": "local-request",
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "claims": [
                                {
                                    "claim_id": claim_id,
                                    "field_path": _ALLOWED_FIELD,
                                    "value": value,
                                    "provider_confidence": 0.9,
                                    "evidence_references": [
                                        {
                                            "fragment_id": fragment_id,
                                            "quote": quote,
                                        }
                                    ],
                                }
                            ]
                        }
                    )
                }
            }
        ],
    }


def test_compact_response_schema_is_flat_batch_bound_and_server_grounded():
    request = _request()
    schema = compact_response_schema(request)

    assert schema["additionalProperties"] is False
    assert schema["properties"]["claims"]["maxItems"] == 3
    claim_schema = _claim_schema(schema)
    reference_schema = _reference_schema(schema)
    assert claim_schema["properties"]["claim_id"] == {
        "type": "string",
        "const": _SERVER_CLAIM_ID_SENTINEL,
    }
    assert claim_schema["properties"]["field_path"]["enum"] == [_ALLOWED_FIELD]
    assert claim_schema["properties"]["value"] == {
        "type": "string",
        "const": _SERVER_FRAGMENT_VALUE_SENTINEL,
    }
    assert claim_schema["properties"]["evidence_references"]["minItems"] == 1
    assert claim_schema["properties"]["evidence_references"]["maxItems"] == 1
    assert reference_schema["properties"]["fragment_id"]["enum"] == sorted(
        fragment.fragment_id for fragment in request.evidence_packet.fragments
    )
    assert reference_schema["properties"]["quote"] == {
        "type": "string",
        "const": _SERVER_FRAGMENT_QUOTE_SENTINEL,
    }
    assert all("$ref" not in item for item in _walk(schema) if isinstance(item, dict))
    assert all("$defs" not in item for item in _walk(schema) if isinstance(item, dict))


def test_llama_compact_body_uses_same_server_grounded_contract_in_prompt():
    request = _request()
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )

    body = build_llama_schema_constrained_request_body(adapter, request)

    assert body["response_format"]["type"] == "json_object"
    schema = body["response_format"]["schema"]
    task = json.loads(body["messages"][1]["content"])
    assert task["output_contract"] == schema
    assert task["map_contract"]["allowed_field_paths"] == [_ALLOWED_FIELD]
    assert task["map_contract"]["llama_schema_profile"] == _LLAMA_SCHEMA_PROFILE
    assert task["map_contract"]["server_owned_claim_identity"] is True
    assert task["map_contract"]["server_owned_fragment_grounding"] is True
    assert _SERVER_CLAIM_ID_SENTINEL in body["messages"][0]["content"]
    assert _SERVER_FRAGMENT_VALUE_SENTINEL in body["messages"][0]["content"]
    assert _SERVER_FRAGMENT_QUOTE_SENTINEL in body["messages"][0]["content"]
    assert all("$ref" not in item for item in _walk(schema) if isinstance(item, dict))


def test_server_owned_sentinels_expand_identity_value_quote_and_preserve_raw_hash():
    request = _request()
    fragment = request.evidence_packet.fragments[0]
    payload = _payload(
        fragment_id=fragment.fragment_id,
        value=_SERVER_FRAGMENT_VALUE_SENTINEL,
        quote=_SERVER_FRAGMENT_QUOTE_SENTINEL,
    )
    raw_body = json.dumps(payload).encode()
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )
    adapter._clock = lambda: 0.0

    result = _parse_success_response_with_safe_diagnostics(
        adapter,
        response=HTTPResponse(status_code=200, headers={}, body=raw_body),
        request=request,
        attempt_latencies_ms=[1],
        retry_count=0,
        analysis_started=0.0,
    )

    claim = result.claims[0]
    reference = claim.evidence_references[0]
    assert claim.claim_id != _SERVER_CLAIM_ID_SENTINEL
    assert len(claim.claim_id) == 64
    assert claim.claim_id == hashlib.sha256(
        json.dumps(
            {
                "batch_hash": request.batch_hash,
                "batch_ordinal": request.batch_ordinal,
                "field_path": claim.field_path,
                "fragment_id": fragment.fragment_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    assert claim.value == fragment.text
    assert reference.quote == fragment.text
    assert reference.fragment_id == fragment.fragment_id
    assert result.raw_response_sha256 == hashlib.sha256(raw_body).hexdigest()
    grounded = validate_provider_claims(request.evidence_packet, result.claims)
    assert [item.support_status for item in grounded] == [SupportStatus.SUPPORTED]


def test_safe_llama_diagnostic_rejects_non_sentinel_quote_without_raw_content():
    request = _request()
    fragment_id = request.evidence_packet.fragments[0].fragment_id
    payload = _payload(
        fragment_id=fragment_id,
        value=_SERVER_FRAGMENT_VALUE_SENTINEL,
        quote="not present",
    )

    class _Provider:
        def generate(self, provider_request):
            adapter = OpenAICompatibleProductionLLMProvider.__new__(
                OpenAICompatibleProductionLLMProvider
            )
            adapter._clock = lambda: 0.0
            return _parse_success_response_with_safe_diagnostics(
                adapter,
                response=HTTPResponse(
                    status_code=200,
                    headers={},
                    body=json.dumps(payload).encode(),
                ),
                request=provider_request,
                attempt_latencies_ms=[1],
                retry_count=0,
                analysis_started=0.0,
            )

    with pytest.raises(
        R10_1AnalysisRejectedError,
        match=(
            "evidence_batch_invalid_response:"
            "provider_wire_quote_sentinel_invalid"
        ),
    ) as raised:
        _run_production_analysis_with_safe_diagnostics(request, _Provider())
    assert "not present" not in str(raised.value)


def test_non_compact_body_keeps_existing_json_mode():
    request = _request(wire="full-v1")
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )

    body = build_llama_schema_constrained_request_body(adapter, request)

    assert body["response_format"] == {"type": "json_object"}
