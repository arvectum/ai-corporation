import json

import pytest
from pydantic import ValidationError

from src.modules.production_llm_analysis.evidence import (
    build_evidence_packet,
    text_sha256,
)
from src.modules.production_llm_analysis.grounding import validate_provider_claims
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)
from src.modules.production_llm_analysis.schemas import (
    CompactWireEvidenceFragment,
    CompactWireProviderResponse,
    EvidenceFragmentInput,
    SupportStatus,
)
from src.modules.production_llm_analysis.service import build_production_llm_request
from src.shared.llm.transport import HTTPResponse, InvalidProviderResponseError

from .conftest import make_policy

_SOURCE_TEXT = "exact source text"
_SENSITIVE_VALUES = ("exact source text", "doc.txt", "registry", "0" * 64)


def _request(*, text=_SOURCE_TEXT, document_id="doc", wire="compact-safe-v1"):
    packet = build_evidence_packet(
        customer_id="c",
        project_id="p",
        procurement_case_id="case",
        run_id="run",
        registry_number="registry",
        fragments=[
            EvidenceFragmentInput(
                document_id=document_id,
                document_name="doc.txt",
                chunk_id="chunk",
                locator={"document_order": 1, "chunk_index": 0},
                text=text,
            )
        ],
    )
    return build_production_llm_request(
        evidence_packet=packet,
        provider="p",
        provider_wire_contract_version=wire,
        model="m",
        prompt_id="p",
        prompt_version="v",
        output_schema_id="s",
        output_schema_version="v",
        grounding_policy_version="v",
        budget_policy=make_policy(),
        map_mode=True,
    )


def _parse_payload(request, payload):
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )
    adapter._clock = lambda: 0.0
    return adapter._parse_success_response(
        response=HTTPResponse(
            status_code=200, headers={}, body=json.dumps(payload).encode()
        ),
        request=request,
        attempt_latencies_ms=[],
        retry_count=0,
        analysis_started=0,
    )


def _parse(request, claims):
    return _parse_payload(
        request,
        {
            "id": "mock",
            "choices": [{"message": {"content": json.dumps({"claims": claims})}}],
        },
    )


def _claim(fragment_id, quote=_SOURCE_TEXT, **updates):
    claim = {
        "claim_id": "claim",
        "field_path": "field",
        "value": quote,
        "provider_confidence": 0.9,
        "evidence_references": [{"fragment_id": fragment_id, "quote": quote}],
    }
    claim.update(updates)
    return claim


def _assert_sanitized(error):
    message = str(error)
    assert all(value not in message for value in _SENSITIVE_VALUES)
    assert '{"claims"' not in message


def test_compact_wire_schema_forbids_server_metadata():
    with pytest.raises(ValidationError):
        CompactWireProviderResponse.model_validate(
            {
                "claims": [
                    {
                        "claim_id": "c",
                        "field_path": "x",
                        "value": "v",
                        "evidence_references": [
                            {"fragment_id": "0" * 64, "quote": "q", "locator": {}}
                        ],
                    }
                ]
            }
        )


@pytest.mark.parametrize("value", [True, 1.0, "1", -1])
def test_document_order_is_strict(value):
    with pytest.raises(ValidationError):
        CompactWireEvidenceFragment(
            fragment_id="0" * 64, document_order=value, chunk_index=0, text="text"
        )


@pytest.mark.parametrize("value", [True, 1.0, "1", -1])
def test_chunk_index_is_strict(value):
    with pytest.raises(ValidationError):
        CompactWireEvidenceFragment(
            fragment_id="0" * 64, document_order=0, chunk_index=value, text="text"
        )


def test_compact_request_only_exposes_safe_fragment_fields():
    request = _request()
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )
    body = adapter._build_request_body(request)
    task = json.loads(body["messages"][1]["content"])
    assert set(task["evidence_fragments"][0]) == {
        "fragment_id",
        "document_order",
        "chunk_index",
        "text",
    }
    assert "procurement_case_id" not in task and "registry_number" not in task


def test_controlled_map_rejects_full_wire():
    request = _request(wire="full-v1")
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )
    with pytest.raises(ValueError, match="provider_wire_contract_unsupported"):
        adapter._build_request_body(request)


def test_compact_response_expands_canonical_reference_and_revalidates_grounding():
    request = _request()
    fragment = request.evidence_packet.fragments[0]
    result = _parse(request, [_claim(fragment.fragment_id)])

    reference = result.claims[0].evidence_references[0]
    assert reference.procurement_case_id == request.procurement_case_id
    assert reference.registry_number == request.registry_number
    assert reference.fragment_id == fragment.fragment_id
    assert reference.document_id == fragment.document_id
    assert reference.document_name == fragment.document_name
    assert reference.chunk_id == fragment.chunk_id
    assert reference.locator == fragment.locator
    assert reference.quote == _SOURCE_TEXT
    assert reference.quote_sha256 == text_sha256(reference.quote)

    grounded = validate_provider_claims(request.evidence_packet, result.claims)
    assert [claim.support_status for claim in grounded] == [SupportStatus.SUPPORTED]
    assert grounded[0].evidence_references == [reference]


@pytest.mark.parametrize(
    ("reference", "code"),
    [
        (
            {"fragment_id": "0" * 64, "quote": _SOURCE_TEXT},
            "provider_wire_fragment_not_found",
        ),
        (
            {"fragment_id": "same", "quote": "not present"},
            "provider_wire_quote_not_found",
        ),
        ({"fragment_id": "same", "quote": ""}, "provider_wire_quote_empty"),
    ],
)
def test_compact_response_rejects_invalid_references_safely(reference, code):
    request = _request()
    fragment = request.evidence_packet.fragments[0]
    reference = {
        **reference,
        "fragment_id": fragment.fragment_id
        if reference["fragment_id"] == "same"
        else reference["fragment_id"],
    }
    with pytest.raises(InvalidProviderResponseError, match=code) as raised:
        _parse(request, [_claim(fragment.fragment_id, evidence_references=[reference])])
    _assert_sanitized(raised.value)


def test_compact_response_rejects_cross_batch_fragment_safely():
    request_a = _request(document_id="a")
    request_b = _request(document_id="b")
    foreign_id = request_b.evidence_packet.fragments[0].fragment_id
    with pytest.raises(
        InvalidProviderResponseError, match="provider_wire_fragment_not_found"
    ) as raised:
        _parse(request_a, [_claim(foreign_id)])
    _assert_sanitized(raised.value)


def test_compact_response_rejects_duplicate_reference_safely():
    request = _request()
    fragment_id = request.evidence_packet.fragments[0].fragment_id
    reference = {"fragment_id": fragment_id, "quote": _SOURCE_TEXT}
    with pytest.raises(
        InvalidProviderResponseError, match="provider_wire_duplicate_reference"
    ) as raised:
        _parse(
            request, [_claim(fragment_id, evidence_references=[reference, reference])]
        )
    _assert_sanitized(raised.value)


def test_compact_response_rejects_duplicate_claim_id_safely():
    request = _request()
    fragment_id = request.evidence_packet.fragments[0].fragment_id
    with pytest.raises(
        InvalidProviderResponseError, match="provider_wire_claim_schema_invalid"
    ) as raised:
        _parse(request, [_claim(fragment_id), _claim(fragment_id)])
    _assert_sanitized(raised.value)


@pytest.mark.parametrize(
    "metadata",
    [
        {"locator": {}},
        {"document_id": "doc"},
        {"document_name": "doc.txt"},
        {"chunk_id": "chunk"},
        {"quote_sha256": "a" * 64},
        {"procurement_case_id": "case"},
        {"registry_number": "registry"},
    ],
)
def test_compact_response_rejects_metadata_injection_safely(metadata):
    request = _request()
    fragment_id = request.evidence_packet.fragments[0].fragment_id
    reference = {"fragment_id": fragment_id, "quote": _SOURCE_TEXT, **metadata}
    with pytest.raises(
        InvalidProviderResponseError, match="provider_wire_reference_schema_invalid"
    ) as raised:
        _parse(request, [_claim(fragment_id, evidence_references=[reference])])
    _assert_sanitized(raised.value)


def test_compact_response_rejects_invalid_fragment_hash_format_safely():
    request = _request()
    with pytest.raises(
        InvalidProviderResponseError, match="provider_wire_reference_schema_invalid"
    ) as raised:
        _parse(request, [_claim("invalid")])
    _assert_sanitized(raised.value)


@pytest.mark.parametrize("confidence", [-0.1, 1.1, "high"])
def test_compact_response_rejects_invalid_confidence_safely(confidence):
    request = _request()
    fragment_id = request.evidence_packet.fragments[0].fragment_id
    with pytest.raises(
        InvalidProviderResponseError, match="provider_wire_claim_schema_invalid"
    ) as raised:
        _parse(request, [_claim(fragment_id, provider_confidence=confidence)])
    _assert_sanitized(raised.value)


def test_compact_response_rejects_non_list_claims_safely():
    request = _request()
    with pytest.raises(
        InvalidProviderResponseError, match="provider_claims_not_list"
    ) as raised:
        _parse_payload(
            request, {"choices": [{"message": {"content": json.dumps({"claims": {}})}}]}
        )
    _assert_sanitized(raised.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": "not-json"}}]},
    ],
)
def test_compact_response_rejects_malformed_envelope_safely(payload):
    request = _request()
    with pytest.raises(InvalidProviderResponseError) as raised:
        _parse_payload(request, payload)
    _assert_sanitized(raised.value)


def test_compact_response_accepts_empty_claims_as_empty_map():
    result = _parse(_request(), [])
    assert result.claims == []
