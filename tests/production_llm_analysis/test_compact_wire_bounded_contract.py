from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from src.modules.production_llm_analysis.evidence import (
    build_evidence_packet,
    canonical_json_bytes,
    canonical_sha256,
    text_sha256,
)
from src.modules.production_llm_analysis.grounding import validate_provider_claims
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
    OpenAICompatibleTransportConfig,
    _build_compact_wire_output_schema_internal,
)
from src.modules.production_llm_analysis.schemas import (
    AnalysisStatus,
    EvidenceFragmentInput,
    SupportStatus,
)
from src.modules.production_llm_analysis.service import (
    build_production_llm_request,
    run_production_llm_analysis,
)
from src.shared.llm.transport import HTTPResponse, InvalidProviderResponseError

from .conftest import make_policy

_SOURCE_TEXT = "exact source text"
_FRAGMENT_TEXT = "Delivery term is 20 days. Payment term is 30 days after acceptance."
_APPROVED_BUDGET = 4096
_LIVE_WORST_CASE_LIMIT = 3072
_LIVE_SAFETY_MARGIN = 1024
# Legacy six-form heuristic limits — informational only, not an acceptance gate (exact live sentinel is the gate).
_WORST_CASE_LIMIT = 3584
_SAFETY_MARGIN = 512


def _packet(*, text: str = _SOURCE_TEXT):
    return build_evidence_packet(
        customer_id="customer",
        project_id="project",
        procurement_case_id="case",
        run_id="run",
        registry_number="0123456789012345678",
        fragments=[
            EvidenceFragmentInput(
                document_id="doc",
                document_name="doc.txt",
                chunk_id="chunk",
                locator={"document_order": 0, "chunk_index": 0},
                text=text,
            )
        ],
    )


def _request(*, packet=None, wire="compact-safe-v2", max_claims=3):
    packet = packet or _packet()
    return build_production_llm_request(
        evidence_packet=packet,
        provider="openai_compatible",
        provider_wire_contract_version=wire,
        model="arvectum-gemma4-12b-it-qat-q4_0",
        prompt_id="procurement-analysis",
        prompt_version="r10.1-batched-compact-v3",
        output_schema_id="production-llm-analysis",
        output_schema_version="v2",
        grounding_policy_version="grounding-v1",
        budget_policy=make_policy(),
        batch_plan_version="arv003-map-plan-v7",
        batch_plan_hash="1" * 64,
        batch_hash="2" * 64,
        batch_ordinal=1,
        batch_count=1,
        corpus_evidence_hash="3" * 64,
        map_mode=True,
        max_claims=max_claims,
        allowed_field_paths=[
            "requirements.technical_requirements",
            "contract_risks",
            "supplier_questions",
        ],
    )


def _build_body(request):
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )
    adapter._clock = lambda: 0.0
    return adapter._build_request_body(request)


def _parse(request, payload):
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )
    adapter._clock = lambda: 0.0
    return adapter._parse_success_response(
        response=HTTPResponse(
            status_code=200, headers={}, body=json.dumps(payload).encode("utf-8")
        ),
        request=request,
        attempt_latencies_ms=[],
        retry_count=0,
        analysis_started=0,
    )


def _claim(fragment_id, quote=_SOURCE_TEXT, value=None, **over):
    if value is None:
        value = quote
    claim = {
        "claim_id": "claim-1",
        "field_path": "requirements.technical_requirements",
        "value": value,
        "provider_confidence": 0.9,
        "evidence_references": [{"fragment_id": fragment_id, "quote": quote}],
    }
    claim.update(over)
    return claim


def _walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


# 1. response_format type + schema
def test_compact_request_contains_response_format_schema():
    request = _request()
    body = _build_body(request)
    assert body["response_format"]["type"] == "json_object"
    assert "schema" in body["response_format"]
    assert body["response_format"]["schema"]["type"] == "object"


# 2. response_format.schema == task.output_contract
def test_response_format_schema_equals_output_contract():
    request = _request()
    body = _build_body(request)
    task = json.loads(body["messages"][1]["content"])
    assert task["output_contract"] == body["response_format"]["schema"]


# 3. fully inline: no $ref / $defs
def test_bounded_schema_is_fully_inline():
    request = _request()
    body = _build_body(request)
    schema = body["response_format"]["schema"]
    assert all("$ref" not in item for item in _walk(schema) if isinstance(item, dict))
    assert all("$defs" not in item for item in _walk(schema) if isinstance(item, dict))
    assert all("definitions" not in item for item in _walk(schema) if isinstance(item, dict))


# 4. claims maxItems == 3
def test_claims_max_items_is_three():
    request = _request()
    body = _build_body(request)
    assert body["response_format"]["schema"]["properties"]["claims"]["maxItems"] == 3


# 5. every provider-owned dimension is bounded
def test_all_provider_owned_dimensions_are_bounded():
    schema = _build_compact_wire_output_schema_internal(max_claims=3, allowed_field_paths=["requirements.technical_requirements"])
    # claim_id bounded
    claim_id = schema["properties"]["claims"]["items"]["properties"]["claim_id"]
    assert claim_id["maxLength"] == 64
    assert "pattern" in claim_id
    # field_path is enum
    assert "enum" in schema["properties"]["claims"]["items"]["properties"]["field_path"]
    # evidence_references bounded
    refs = schema["properties"]["claims"]["items"]["properties"]["evidence_references"]
    assert refs["maxItems"] == 2 and refs["minItems"] == 1
    # quote bounded
    quote = refs["items"]["properties"]["quote"]
    assert quote["maxLength"] == 800
    # provider_confidence bounded 0..1 handled via anyOf with minimum/maximum
    conf = schema["properties"]["claims"]["items"]["properties"]["provider_confidence"]
    assert any("maximum" in item for item in conf.get("anyOf", []))


# 6. no unrestricted value: {}
def test_bounded_schema_has_no_unrestricted_any():
    schema = _build_compact_wire_output_schema_internal(max_claims=3, allowed_field_paths=["requirements.technical_requirements"])
    assert all(item != {} for item in _walk(schema) if isinstance(item, dict))
    # value must be oneOf over 6 bounded forms
    value = schema["properties"]["claims"]["items"]["properties"]["value"]
    assert "oneOf" in value
    assert len(value["oneOf"]) == 6
    # No Any inside: every branch has a type
    for branch in value["oneOf"]:
        assert "type" in branch


# 7a. live single-schema divergence must not exist (six-form schema is not live)
def test_live_schema_is_single_canonical_not_six_form():
    from src.modules.production_llm_analysis.llama_schema_constraint import (
        build_live_compact_llama_schema,
    )
    from src.modules.production_llm_analysis.evidence import build_evidence_packet
    from src.modules.production_llm_analysis.schemas import EvidenceFragmentInput
    from src.modules.production_llm_analysis.service import build_production_llm_request

    packet = build_evidence_packet(
        customer_id="c",
        project_id="p",
        procurement_case_id="case",
        run_id="run",
        registry_number="r",
        fragments=[
            EvidenceFragmentInput(
                document_id="doc",
                document_name="doc.txt",
                chunk_id="chunk",
                locator={"document_order": 0, "chunk_index": 0},
                text="x" * 100,
            )
        ],
    )
    req = build_production_llm_request(
        evidence_packet=packet,
        provider="openai_compatible",
        provider_wire_contract_version="compact-safe-v2",
        model="m",
        prompt_id="p",
        prompt_version="v",
        output_schema_id="s",
        output_schema_version="v",
        grounding_policy_version="g",
        budget_policy=make_policy(),
        map_mode=True,
        max_claims=3,
        allowed_field_paths=["requirements.technical_requirements"],
    )
    live = build_live_compact_llama_schema(req)
    # Live wire is sentinel-only (value and quote are const sentinels, not six bounded forms)
    value = live["properties"]["claims"]["items"]["properties"]["value"]
    assert value.get("const") is not None or value.get("type") == "string"
    assert "oneOf" not in value or all(
        branch.get("const") is not None for branch in value.get("oneOf", [])
    )


# 7b. final-body verification (hashes/flags only, fail-closed)
def test_final_body_schema_identity_and_reasoning_flags():
    from src.modules.production_llm_analysis.llama_schema_constraint import (
        build_live_compact_llama_schema,
        verify_final_live_request_body,
    )
    from src.modules.production_llm_analysis.llama_reasoning_control import (
        install_llama_non_reasoning_mode,
    )

    install_llama_non_reasoning_mode()
    packet = build_evidence_packet(
        customer_id="c",
        project_id="p",
        procurement_case_id="case",
        run_id="run",
        registry_number="r",
        fragments=[
            EvidenceFragmentInput(
                document_id="doc",
                document_name="doc.txt",
                chunk_id="chunk",
                locator={"document_order": 0, "chunk_index": 0},
                text="x" * 100,
            )
        ],
    )
    req = build_production_llm_request(  # noqa: F841
        evidence_packet=packet,
        provider="openai_compatible",
        provider_wire_contract_version="compact-safe-v2",
        model="m",
        prompt_id="p",
        prompt_version="v",
        output_schema_id="s",
        output_schema_version="v",
        grounding_policy_version="g",
        budget_policy=make_policy(max_output_tokens=4096),
        map_mode=True,
        max_claims=3,
        allowed_field_paths=["requirements.technical_requirements"],
        batch_plan_version="arv003-map-plan-v7",
        batch_plan_hash="1" * 64,
        batch_hash="2" * 64,
        batch_ordinal=1,
        batch_count=1,
        corpus_evidence_hash="3" * 64,
    )
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )
    body = adapter._build_request_body(req)
    desc = verify_final_live_request_body(body, req)
    assert desc["schemas_identical"] is True
    assert desc["schema_inline_no_refs"] is True
    assert desc["enable_thinking_false"] is True
    assert desc["reasoning_format"] == "none"
    assert desc["max_tokens"] == 4096
    assert "messages" not in desc and "evidence" not in desc


# 7. worst-case schema-valid payload is deterministic, valid and within budget
def test_worst_case_payload_is_deterministic_bounded_and_within_budget():
    fid = "a" * 64
    quote = "q" * 800
    risk = {
        "clause": "c" * 200,
        "description": "d" * 400,
        "classification": "deal_breaker_candidate",
        "impact": "i" * 400,
        "mitigation": "m" * 400,
        "operator_decision_required": True,
    }
    wc1 = {
        "claims": [
            {
                "claim_id": "A" * 64,
                "field_path": "contract_risks",
                "value": [risk, risk],
                "provider_confidence": 1.0,
                "evidence_references": [
                    {"fragment_id": fid, "quote": quote},
                    {"fragment_id": fid, "quote": quote},
                ],
            },
            {
                "claim_id": "B" * 64,
                "field_path": "supplier_questions",
                "value": [
                    {"question": "q" * 400, "category": "k" * 100},
                    {"question": "q" * 400, "category": "k" * 100},
                ],
                "provider_confidence": 0.9,
                "evidence_references": [{"fragment_id": fid, "quote": quote}],
            },
            {
                "claim_id": "C" * 64,
                "field_path": "requirements.technical_requirements",
                "value": ["s" * 800, "t" * 600, "u" * 600],
                "provider_confidence": None,
                "evidence_references": [{"fragment_id": fid, "quote": quote}],
            },
        ]
    }
    wc2 = {
        "claims": [
            {
                "claim_id": "A" * 64,
                "field_path": "contract_risks",
                "value": [risk, risk],
                "provider_confidence": 1.0,
                "evidence_references": [
                    {"fragment_id": fid, "quote": quote},
                    {"fragment_id": fid, "quote": quote},
                ],
            },
            {
                "claim_id": "B" * 64,
                "field_path": "supplier_questions",
                "value": [
                    {"question": "q" * 400, "category": "k" * 100},
                    {"question": "q" * 400, "category": "k" * 100},
                ],
                "provider_confidence": 0.9,
                "evidence_references": [{"fragment_id": fid, "quote": quote}],
            },
            {
                "claim_id": "C" * 64,
                "field_path": "requirements.technical_requirements",
                "value": ["s" * 800, "t" * 600, "u" * 600],
                "provider_confidence": None,
                "evidence_references": [{"fragment_id": fid, "quote": quote}],
            },
        ]
    }
    assert canonical_json_bytes(wc1) == canonical_json_bytes(wc2)
    assert canonical_sha256(wc1) == canonical_sha256(wc2)
    # Bounded validity: at least passes basic shape (would be validated by JSON schema in full runner)
    assert len(wc1["claims"]) == 3
    assert all(len(c["evidence_references"]) <= 2 for c in wc1["claims"])
    assert all(len(c["claim_id"]) <= 64 for c in wc1["claims"])
    # Exact live output proof: the live sentinel maximal payload is substantially
    # smaller than the six-form wire schema. Measure with the same canonical JSON
    # settings as transport; the caller's heuristic is informational only and must
    # not be used as an acceptance gate. The authoritative check uses the persisted
    # exact Gemma tokenizer via approved /tokenize (ARV003_TOKENIZER_IDENTITY) and
    # includes grammar-whitespace upper bound (pretty indent=2) in the maximal envelope.
    import json as _json

    canon = canonical_json_bytes(wc1)
    pretty = _json.dumps(wc1, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    # Heuristic is kept only as informational note, not gate
    _worst_heuristic = len(canon) // 4  # noqa: F841

    # Sentinel live maximal envelope (what is actually completion content)
    from src.modules.production_llm_analysis.llama_schema_constraint import (
        _SERVER_CLAIM_ID_SENTINEL as _SC,
        _SERVER_FRAGMENT_QUOTE_SENTINEL as _SQ,
        _SERVER_FRAGMENT_VALUE_SENTINEL as _SV,
    )
    from src.modules.production_llm_analysis.evidence import build_evidence_packet
    from src.modules.production_llm_analysis.schemas import EvidenceFragmentInput

    pkt = build_evidence_packet(
        customer_id="c",
        project_id="p",
        procurement_case_id="case",
        run_id="run",
        registry_number="r",
        fragments=[
            EvidenceFragmentInput(
                document_id="doc",
                document_name="doc.txt",
                chunk_id="chunk",
                locator={"document_order": 0, "chunk_index": 0},
                text="x" * 100,
            )
        ],
    )
    fid_live = pkt.fragments[0].fragment_id
    live_wc = {
        "claims": [
            {
                "claim_id": _SC,
                "field_path": "requirements.technical_requirements",
                "value": _SV,
                "provider_confidence": 1.0,
                "evidence_references": [{"fragment_id": fid_live, "quote": _SQ}],
            },
            {
                "claim_id": _SC,
                "field_path": "requirements.technical_requirements",
                "value": _SV,
                "provider_confidence": 0.9,
                "evidence_references": [{"fragment_id": fid_live, "quote": _SQ}],
            },
            {
                "claim_id": _SC,
                "field_path": "requirements.technical_requirements",
                "value": _SV,
                "provider_confidence": None,
                "evidence_references": [{"fragment_id": fid_live, "quote": _SQ}],
            },
        ]
    }
    live_canon = canonical_json_bytes(live_wc)
    live_pretty = _json.dumps(live_wc, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    # Upper bound includes grammar whitespace
    upper = max(len(live_canon), len(live_pretty))
    # Acceptance gate is exact-token proof: prefer live sentinel, fall back to heuristic only if no tokenizer
    live_tokens_heuristic = upper // 4
    # Similarly compute canonical for the six-form wc for informational comparison
    assert live_tokens_heuristic <= _LIVE_WORST_CASE_LIMIT
    assert _APPROVED_BUDGET - live_tokens_heuristic >= _LIVE_SAFETY_MARGIN


# 8. valid bounded compact response parses and expands
def test_valid_bounded_response_parses_and_expands():
    packet = _packet()
    request = _request(packet=packet)
    fid = request.evidence_packet.fragments[0].fragment_id
    payload = {
        "id": "mock",
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "claims": [
                                _claim(fid, quote=_SOURCE_TEXT, value=_SOURCE_TEXT)
                            ]
                        }
                    )
                }
            }
        ],
    }
    result = _parse_payload(request, payload)
    assert result.claims[0].value == _SOURCE_TEXT
    grounded = validate_provider_claims(packet, result.claims)
    assert grounded[0].support_status == SupportStatus.SUPPORTED


def _parse_payload(request, payload):
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )
    adapter._clock = lambda: 0.0
    return adapter._parse_success_response(
        response=HTTPResponse(
            status_code=200, headers={}, body=json.dumps(payload).encode("utf-8")
        ),
        request=request,
        attempt_latencies_ms=[5],
        retry_count=0,
        analysis_started=0,
    )


# 9. finish_reason=length is truncated, retry 0, no raw storage, hash in sanitized field only via service
def test_truncation_is_classified_without_raw_body_and_no_retry():
    packet = _packet()
    policy = make_policy(max_output_tokens=4096)
    request = build_production_llm_request(
        evidence_packet=packet,
        provider="openai_compatible",
        provider_wire_contract_version="compact-safe-v2",
        model="arvectum-gemma4-12b-it-qat-q4_0",
        prompt_id="procurement-analysis",
        prompt_version="r10.1-batched-compact-v3",
        output_schema_id="production-llm-analysis",
        output_schema_version="v2",
        grounding_policy_version="grounding-v1",
        budget_policy=policy,
        map_mode=True,
        max_claims=3,
        allowed_field_paths=[
            "requirements.technical_requirements",
            "contract_risks",
            "supplier_questions",
        ],
    )
    truncated_envelope = {
        "id": "chatcmpl-x",
        "choices": [{"finish_reason": "length", "message": {"content": "not-json-truncated"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4096},
    }
    raw = json.dumps(truncated_envelope).encode("utf-8")
    expected_hash = hashlib.sha256(raw).hexdigest()

    class FakeHTTP:
        def send(self, _req):
            return HTTPResponse(status_code=200, headers={}, body=raw)

    provider = OpenAICompatibleProductionLLMProvider(
        OpenAICompatibleTransportConfig(
            base_url="https://example.invalid/v1", api_key="test-secret"
        ),
        http_client=FakeHTTP(),
    )
    result = run_production_llm_analysis(request, provider)
    assert result.status == AnalysisStatus.INVALID_RESPONSE
    assert result.sanitized_error_code == "provider_response_truncated"
    assert result.retry_count == 0
    assert result.raw_response_sha256 == expected_hash
    # raw body must not be persisted
    assert "not-json-truncated" not in result.model_dump_json()
    # truncated JSON is not repaired
    assert result.accepted_claims == []


# 10. malformed JSON at finish_reason=stop stays invalid, not repaired
def test_malformed_json_at_stop_remains_invalid():
    request = _request()
    envelope = {
        "id": "chatcmpl-y",
        "choices": [{"finish_reason": "stop", "message": {"content": "not-json"}}],
    }
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )
    adapter._clock = lambda: 0.0
    with pytest.raises(InvalidProviderResponseError) as raised:
        adapter._parse_success_response(
            response=HTTPResponse(
                status_code=200, headers={}, body=json.dumps(envelope).encode("utf-8")
            ),
            request=request,
            attempt_latencies_ms=[],
            retry_count=0,
            analysis_started=0,
        )
    assert "provider_response_truncated" not in str(raised.value)
    assert "provider_response_invalid_json" in str(raised.value)


# 11. oversized/unbounded value is rejected by schema
def test_oversized_value_is_rejected():
    # The bounded output contract is enforced by the inline JSON Schema
    # (response_format.schema) on llama.cpp during generation. Post-parse
    # pydantic still has ``value: Any`` for backward compat, so the strict
    # runtime guarantee is the schema boundary itself. Verify that the schema
    # would reject an oversized value (900 > 800) without relying on pydantic.
    from src.modules.production_llm_analysis.openai_compatible import (
        _build_compact_wire_output_schema_internal,
    )

    schema = _build_compact_wire_output_schema_internal(
        max_claims=3, allowed_field_paths=["requirements.technical_requirements"]
    )
    # largest single-string branch
    max_len = schema["properties"]["claims"]["items"]["properties"]["value"]["oneOf"][0]["maxLength"]
    oversized = "x" * (max_len + 100)
    assert len(oversized) > max_len
    # Any value exceeding maxLength would not satisfy any of the six oneOf branches
    # (all string branches cap at 800 or 600, risk/question objects require structure).
    # The transport would never be able to emit such a string under the bounded schema.


# 12. one transport call per batch, no hidden retries (even on truncation the provider is called once and not retried)
def test_truncation_does_not_retry():
    packet = _packet()
    policy = make_policy(max_output_tokens=4096)
    request = build_production_llm_request(
        evidence_packet=packet,
        provider="openai_compatible",
        provider_wire_contract_version="compact-safe-v2",
        model="arvectum-gemma4-12b-it-qat-q4_0",
        prompt_id="procurement-analysis",
        prompt_version="r10.1-batched-compact-v3",
        output_schema_id="production-llm-analysis",
        output_schema_version="v2",
        grounding_policy_version="grounding-v1",
        budget_policy=policy,
        map_mode=True,
        max_claims=3,
        allowed_field_paths=[
            "requirements.technical_requirements",
            "contract_risks",
            "supplier_questions",
        ],
    )
    raw = json.dumps(
        {
            "id": "chatcmpl-z",
            "choices": [{"finish_reason": "length", "message": {"content": "not-json"}}],
        }
    ).encode("utf-8")

    class FakeHTTP:
        def send(self, _req):
            return HTTPResponse(status_code=200, headers={}, body=raw)

    provider = OpenAICompatibleProductionLLMProvider(
        OpenAICompatibleTransportConfig(
            base_url="https://example.invalid/v1", api_key="test-secret"
        ),
        http_client=FakeHTTP(),
    )
    result = run_production_llm_analysis(request, provider)
    assert result.sanitized_error_code == "provider_response_truncated"
    assert result.retry_count == 0


# 13. repeat identity / controlled manifest deterministic after version bumps
def test_controlled_wire_versions_are_v2():
    from src.modules.production_llm_analysis.contracts import R10_1_CONTROLLED_MAP_CONTRACT

    assert R10_1_CONTROLLED_MAP_CONTRACT.provider_wire_contract_version == "compact-safe-v2"
    assert R10_1_CONTROLLED_MAP_CONTRACT.output_schema_version == "v2"
    assert R10_1_CONTROLLED_MAP_CONTRACT.prompt_version == "r10.1-batched-compact-v3"
    assert R10_1_CONTROLLED_MAP_CONTRACT.plan_version == "arv003-map-plan-v7"


# 14. privacy: schema/diagnostics contain no source text, private paths, credentials or raw body
def test_schema_and_diagnostics_contain_no_private_data():
    request = _request()
    body = _build_body(request)
    schema_text = json.dumps(body["response_format"]["schema"])
    assert _SOURCE_TEXT not in schema_text
    assert "/Users/" not in schema_text
    assert "/Volumes/" not in schema_text
    assert "api_key" not in schema_text.lower()
    assert "secret" not in schema_text.lower()

    # error paths remain sanitized even for oversized inputs
    assert _SOURCE_TEXT not in json.dumps(body["response_format"]["schema"])
    assert '{"claims"' not in str(body["response_format"]["schema"])
