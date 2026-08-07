"""Regression tests for the ARV-001 live output boundary.

These tests lock the repository-owned grammar-whitespace contract, the maximal
live-sentinel payload, the exact-token budget proof (fail-closed, no ``//4``
heuristic), and the generate()-level final-body verification that the transport
actually sends. They are privacy-safe: they never log or persist raw provider
content or reasoning.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

import pytest

from src.modules.production_llm_analysis.evidence import (
    build_evidence_packet,
)
from src.modules.production_llm_analysis.llama_schema_constraint import (
    build_live_compact_llama_schema,
    install_llama_schema_constraint,
)
from src.modules.production_llm_analysis.llama_reasoning_control import (
    install_llama_non_reasoning_mode,
)
from src.modules.production_llm_analysis.live_output_boundary import (
    build_maximal_live_completion_payload,
    verify_exact_live_output_budget,
    ExactLiveOutputTokenizerUnavailable,
    ExactLiveOutputTokensExceeded,
    OutputSafetyMarginBelowThreshold,
    GRAMMAR_WHITESPACE_CONTRACT_VERSION,
    GRAMMAR_WHITESPACE_MAX_BYTES_PER_SLOT,
    GRAMMAR_WHITESPACE_SLOT,
)
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
    OpenAICompatibleTransportConfig,
)
from src.modules.production_llm_analysis.schemas import (
    BudgetLimits,
    BudgetPolicy,
    EvidenceFragmentInput,
    ProviderPricing,
)
from src.modules.production_llm_analysis.service import build_production_llm_request
from src.shared.llm.transport import HTTPResponse


def _policy():
    return BudgetPolicy(
        limits=BudgetLimits(
            max_input_tokens=100_000,
            max_output_tokens=4096,
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
    )


def _request():
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
                text="Cable AVVG-P quantity 10 meters. Delivery in 20 days.",
            )
        ],
    )
    return build_production_llm_request(
        evidence_packet=packet,
        provider="openai_compatible",
        provider_wire_contract_version="compact-safe-v2",
        model="arvectum-gemma4-12b-it-qat-q4_0",
        prompt_id="procurement-analysis",
        prompt_version="r10.1-batched-compact-v3",
        output_schema_id="production-llm-analysis",
        output_schema_version="v2",
        grounding_policy_version="grounding-v1",
        budget_policy=_policy(),
        map_mode=True,
        max_claims=3,
        allowed_field_paths=[
            "requirements.technical_requirements",
            "contract_risks",
            "supplier_questions",
        ],
    )


class FakePersistentTokenizer:
    identity = "planner-test-persistent-tokenizer-v1"
    persistent = True

    def __init__(self, tokens: int):
        self._tokens = tokens

    def __call__(self, text: str) -> int:
        return self._tokens


class FakeNonPersistentTokenizer(FakePersistentTokenizer):
    persistent = False


# 1. grammar-whitespace contract is the b10240 SPACE_RULE bound
def test_grammar_whitespace_contract_is_b10240_spacerule():
    assert GRAMMAR_WHITESPACE_CONTRACT_VERSION == "llama-cpp-b10240-spacerule-v1"
    assert GRAMMAR_WHITESPACE_MAX_BYTES_PER_SLOT == 22
    assert GRAMMAR_WHITESPACE_SLOT == ("\n" * 2) + (" " * 20)
    assert len(GRAMMAR_WHITESPACE_SLOT.encode("utf-8")) == 22


# 2. maximal payload is derived from the single live schema with server sentinels
def test_maximal_payload_is_live_schema_derived():
    request = _request()
    tokenizer = FakePersistentTokenizer(10)
    maximal = build_maximal_live_completion_payload(request, tokenizer)
    payload = json.loads(maximal.content)
    assert len(payload["claims"]) == 3
    claim = payload["claims"][0]
    assert claim["claim_id"] == "__ARVECTUM_SERVER_CLAIM_ID__"
    assert claim["value"] == "__ARVECTUM_SERVER_FRAGMENT_VALUE__"
    assert claim["evidence_references"][0]["quote"] == "__ARVECTUM_SERVER_FRAGMENT_QUOTE__"
    # provider_confidence is removed from live generatable schema
    assert "provider_confidence" not in claim
    schema = build_live_compact_llama_schema(request)
    refs = schema["properties"]["claims"]["items"]["properties"]["evidence_references"]
    assert refs["maxItems"] == 1


# 3. whitespace slot count is deterministic
def test_whitespace_slot_count_is_deterministic():
    request = _request()
    tokenizer = FakePersistentTokenizer(10)
    a = build_maximal_live_completion_payload(request, tokenizer)
    b = build_maximal_live_completion_payload(request, tokenizer)
    from src.modules.production_llm_analysis.live_output_boundary import _maximal_whitespace_slot
    ws = _maximal_whitespace_slot(tokenizer)
    assert a.grammar_whitespace_slots == a.content.count(ws)
    assert a.grammar_whitespace_slots == b.grammar_whitespace_slots
    assert a.content_sha256 == b.content_sha256


# 4. exact budget proof: persistent tokenizer only, fail-closed
def test_budget_proof_requires_persistent_identity():
    with pytest.raises(ExactLiveOutputTokenizerUnavailable):
        verify_exact_live_output_budget(
            _request(), tokenizer=FakeNonPersistentTokenizer(10)
        )


def test_budget_proof_succeeds_with_persistent_exact_tokenizer():
    descriptor = verify_exact_live_output_budget(
        _request(), tokenizer=FakePersistentTokenizer(1200)
    )
    assert descriptor["exact_live_output_tokens"] <= 3072
    assert descriptor["safety_margin_tokens"] >= 1024
    assert descriptor["output_budget"] == 4096
    assert descriptor["grammar_whitespace_included"] is True


def test_budget_proof_exceeds_tokens_limit_fails_closed():
    with pytest.raises(ExactLiveOutputTokensExceeded):
        verify_exact_live_output_budget(
            _request(), tokenizer=FakePersistentTokenizer(4000)
        )


def test_budget_proof_below_safety_margin_fails_closed():
    with pytest.raises(OutputSafetyMarginBelowThreshold):
        verify_exact_live_output_budget(
            _request(),
            tokenizer=FakePersistentTokenizer(3050),
            minimum_margin=1100,
            tokens_limit=3600,
        )


def test_budget_proof_descriptor_has_grammar_ws_fields():
    d = verify_exact_live_output_budget(
        _request(), tokenizer=FakePersistentTokenizer(1000)
    )
    assert d["live_schema_sha256"]
    assert d["maximal_payload_sha256"]
    assert d["grammar_whitespace_contract_version"] == GRAMMAR_WHITESPACE_CONTRACT_VERSION
    assert d["tokenizer_identity"] == "planner-test-persistent-tokenizer-v1"


# 6. generate() with wrappers installed still verifies the final body
def test_generate_sends_verified_final_body_when_installed():
    install_llama_schema_constraint()
    install_llama_non_reasoning_mode()

    @dataclass
    class RecordingClient:
        sent: bytes | None = None

        def send(self, http):
            self.sent = http.body
            fid = _request().evidence_packet.fragments[0].fragment_id
            quoted = json.dumps(
                {
                    "claims": [
                        {
                            "claim_id": "__ARVECTUM_SERVER_CLAIM_ID__",
                            "field_path": "requirements.technical_requirements",
                            "value": "__ARVECTUM_SERVER_FRAGMENT_VALUE__",
                            "evidence_references": [
                                {"fragment_id": fid, "quote": "__ARVECTUM_SERVER_FRAGMENT_QUOTE__"}
                            ],
                        }
                    ]
                }
            )
            envelope = {
                "id": "chatcmpl-x",
                "choices": [{"finish_reason": "stop", "message": {"content": quoted}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
            return HTTPResponse(
                status_code=200, headers={}, body=json.dumps(envelope).encode()
            )

    record = RecordingClient()
    provider = OpenAICompatibleProductionLLMProvider(
        OpenAICompatibleTransportConfig(
            base_url="https://example.invalid/v1", api_key="test-secret"
        ),
        http_client=record,
    )
    provider.generate(_request())
    assert record.sent is not None
    assert provider._last_boundary_verification is not None
    ver = provider._last_boundary_verification
    assert ver["final_request_body_sha256"] == hashlib.sha256(record.sent).hexdigest()
    assert ver["schemas_identical"] is True
    assert ver["reasoning_format"] == "none"


# 7. truncation carries sanitized diagnostics, never raw content
def test_truncation_diagnostics_are_sanitized():
    import src.modules.production_llm_analysis.openai_compatible as _m

    adapter = _m.OpenAICompatibleProductionLLMProvider.__new__(
        _m.OpenAICompatibleProductionLLMProvider
    )
    adapter._clock = lambda: 0.0
    body = json.dumps(
        {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": "SECRET-CONTENT-NOT-JSON"},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4096},
        }
    ).encode()

    with pytest.raises(_m.InvalidProviderResponseError) as excinfo:
        _m.OpenAICompatibleProductionLLMProvider._parse_success_response(
            adapter,
            response=HTTPResponse(status_code=200, headers={}, body=body),
            request=_request(),
            attempt_latencies_ms=[],
            retry_count=0,
            analysis_started=0.0,
        )
    err = excinfo.value
    assert str(err) == "provider_response_truncated"
    assert err.truncation_finish_reason == "length"
    assert err.truncation_completion_tokens == 4096
    assert err.truncation_prompt_tokens == 10
    assert err.truncation_response_utf8_bytes is not None
    # privacy: the raw truncated content never appears anywhere in the error
    assert "SECRET-CONTENT" not in str(err)
    assert "SECRET-CONTENT" not in repr(err) and "SECRET-CONTENT" not in repr(err.__dict__)


# 8. fail-closed: non-empty allow-list with no extractive path is rejected
def test_non_extractive_allowlist_fails_closed():
    request = _request().model_copy(
        update={"allowed_field_paths": ["contract_risks", "supplier_questions"]}
    )
    with pytest.raises(ValueError) as excinfo:
        build_live_compact_llama_schema(request)
    assert str(excinfo.value) == "llama_schema_extractive_field_paths_missing"


# 9. legacy six-form schema hash is absent from the live final body schema
def test_legacy_schema_hash_absent_from_live_wire():
    from src.modules.production_llm_analysis.openai_compatible import (
        _build_compact_wire_output_schema_internal,
    )
    from src.modules.production_llm_analysis.evidence import canonical_sha256 as _sha

    legacy = _build_compact_wire_output_schema_internal(
        max_claims=3,
        allowed_field_paths=["requirements.technical_requirements"],
    )
    live = build_live_compact_llama_schema(_request())
    assert _sha(legacy) != _sha(live)
    # the wire schema is inline, no $ref/$defs/definitions
    dumped = json.dumps(live)
    for stop in ("$ref", "$defs", "definitions"):
        assert stop not in dumped