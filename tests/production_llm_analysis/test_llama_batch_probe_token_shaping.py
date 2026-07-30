from __future__ import annotations

from types import SimpleNamespace

from scripts.r10_1.probe_llama_batch_shape import (
    _FRAGMENT_COUNT,
    _MIN_BATCH_SHAPE_FRAGMENTS,
    _build_batch_shaped_request,
    _measurement_fits,
    _sanitized_http_code,
    _sanitized_server_error_type,
    _shape_request,
)
from src.modules.production_llm_analysis.batching import (
    BatchPolicy,
    ExactRequestMeasurement,
)
from src.modules.production_llm_analysis.llama_reasoning_control import (
    apply_llama_non_reasoning_mode,
)
from src.modules.production_llm_analysis.llama_schema_constraint import (
    build_llama_schema_constrained_request_body,
)
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)

from .conftest import make_policy


class _ExactFakeTokenizer:
    identity = "fake-persistent-tokenizer"
    persistent = True

    def __init__(self) -> None:
        self.invocations = 0

    def __call__(self, text: str) -> int:
        self.invocations += 1
        return max(1, len(text.encode("utf-8")) // 5)


class _ProbeRequestBuilder:
    def __init__(self) -> None:
        self._adapter = OpenAICompatibleProductionLLMProvider.__new__(
            OpenAICompatibleProductionLLMProvider
        )

    def _build_request_body(self, request):
        body = build_llama_schema_constrained_request_body(self._adapter, request)
        return apply_llama_non_reasoning_mode(body, request)


def _policy():
    return SimpleNamespace(
        provider="openai_compatible",
        model="arvectum-gemma4-12b-q4km",
        budget=make_policy(max_output_tokens=4096, max_retries=2),
    )


def test_batch_probe_shapes_request_with_exact_full_envelope_measurement():
    provider = _ProbeRequestBuilder()
    tokenizer = _ExactFakeTokenizer()

    request, measurement, fragment_count, batch_policy = _shape_request(
        _policy(), provider, tokenizer
    )

    assert _MIN_BATCH_SHAPE_FRAGMENTS <= fragment_count <= _FRAGMENT_COUNT
    assert len(request.evidence_packet.fragments) == fragment_count
    assert request.tokenizer_identity == tokenizer.identity
    assert _measurement_fits(measurement, batch_policy)
    assert (
        measurement.full_request_tokens
        + batch_policy.output_reserve
        + batch_policy.safety_margin
        <= batch_policy.context_window
    )
    assert tokenizer.invocations > 0
    body = provider._build_request_body(request)
    assert body["chat_template_kwargs"]["enable_thinking"] is False


def test_measurement_fit_rejects_context_and_evidence_overflow():
    policy = BatchPolicy.approved_32k(tokenizer_identity="fake")
    fitting = ExactRequestMeasurement(
        full_request_tokens=25000,
        request_body_hash="1" * 64,
        serialized_evidence_tokens=24000,
        serialized_evidence_hash="2" * 64,
        fixed_envelope_tokens=1000,
        chat_template_overhead=32,
    )
    context_overflow = ExactRequestMeasurement(
        full_request_tokens=25400,
        request_body_hash="1" * 64,
        serialized_evidence_tokens=24000,
        serialized_evidence_hash="2" * 64,
        fixed_envelope_tokens=1400,
        chat_template_overhead=32,
    )
    evidence_overflow = ExactRequestMeasurement(
        full_request_tokens=25000,
        request_body_hash="1" * 64,
        serialized_evidence_tokens=24489,
        serialized_evidence_hash="2" * 64,
        fixed_envelope_tokens=511,
        chat_template_overhead=32,
    )

    assert _measurement_fits(fitting, policy) is True
    assert _measurement_fits(context_overflow, policy) is False
    assert _measurement_fits(evidence_overflow, policy) is False


def test_probe_http_diagnostics_are_status_only_and_sanitized():
    assert _sanitized_http_code(400) == "provider_http_400"
    assert _sanitized_http_code(413) == "provider_http_413"
    assert _sanitized_http_code(None) == "provider_request_rejected"
    assert (
        _sanitized_server_error_type(
            b'{"error":{"type":"invalid_request_error","message":"private"}}'
        )
        == "invalid_request_error"
    )
    assert (
        _sanitized_server_error_type(b'{"error":{"type":"bad value /tmp/x"}}')
        is None
    )


def test_batch_request_builder_preserves_requested_fragment_count():
    request = _build_batch_shaped_request(
        _policy(), fragment_count=12, tokenizer_identity="exact-test-tokenizer"
    )

    assert len(request.evidence_packet.fragments) == 12
    assert request.batch_ordinal == 1
    assert request.batch_count == 17
    assert request.context_profile == "32k"
    assert request.tokenizer_identity == "exact-test-tokenizer"
