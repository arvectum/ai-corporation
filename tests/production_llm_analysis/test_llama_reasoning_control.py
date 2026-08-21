from __future__ import annotations

from types import SimpleNamespace

import pytest

from scripts.r10_1.probe_llama_batch_shape import (
    _FRAGMENT_COUNT,
    _build_batch_shaped_request,
)
from src.modules.production_llm_analysis.llama_reasoning_control import (
    _LLAMA_REASONING_PROFILE,
    apply_llama_non_reasoning_mode,
)
from src.modules.production_llm_analysis.llama_schema_constraint import (
    build_llama_schema_constrained_request_body,
)
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)

from .conftest import make_policy


def _policy():
    return SimpleNamespace(
        provider="openai_compatible",
        model="arvectum-gemma4-12b-q4km",
        budget=make_policy(max_output_tokens=4096, max_retries=2),
    )


def test_non_reasoning_mode_disables_reasoning_and_preserves_other_kwargs():
    request = _build_batch_shaped_request(_policy())
    body = {"chat_template_kwargs": {"custom": "kept"}}

    result = apply_llama_non_reasoning_mode(body, request)

    assert result is body
    assert result["chat_template_kwargs"] == {
        "custom": "kept",
        "enable_thinking": False,
    }
    assert result["reasoning_format"] == "auto"
    assert result["reasoning_effort"] == "none"
    assert _LLAMA_REASONING_PROFILE == "thinking-disabled-reasoning-separated-json-v4"


def test_non_reasoning_mode_accepts_schema_adapter_none_as_baseline():
    request = _build_batch_shaped_request(_policy())

    result = apply_llama_non_reasoning_mode(
        {"reasoning_format": "none"},
        request,
    )

    assert result["reasoning_format"] == "auto"
    assert result["reasoning_effort"] == "none"


def test_non_reasoning_mode_rejects_conflicting_thinking_enablement():
    request = _build_batch_shaped_request(_policy())

    with pytest.raises(ValueError, match="llama_thinking_mode_conflict"):
        apply_llama_non_reasoning_mode(
            {"chat_template_kwargs": {"enable_thinking": True}},
            request,
        )


def test_non_reasoning_mode_rejects_conflicting_reasoning_format():
    request = _build_batch_shaped_request(_policy())

    with pytest.raises(ValueError, match="llama_reasoning_format_conflict"):
        apply_llama_non_reasoning_mode(
            {"reasoning_format": "deepseek"},
            request,
        )


def test_non_reasoning_mode_rejects_conflicting_reasoning_effort():
    request = _build_batch_shaped_request(_policy())

    with pytest.raises(ValueError, match="llama_reasoning_effort_conflict"):
        apply_llama_non_reasoning_mode(
            {"reasoning_effort": "high"},
            request,
        )


def test_non_reasoning_mode_leaves_full_wire_unchanged():
    request = _build_batch_shaped_request(_policy()).model_copy(
        update={"provider_wire_contract_version": "full-v1", "map_mode": False}
    )
    body = {"response_format": {"type": "json_object"}}

    assert apply_llama_non_reasoning_mode(body, request) == body
    assert "chat_template_kwargs" not in body
    assert "reasoning_effort" not in body


def test_batch_shaped_probe_matches_real_map_shape_without_customer_data():
    request = _build_batch_shaped_request(_policy())

    assert len(request.evidence_packet.fragments) == _FRAGMENT_COUNT == 96
    assert request.batch_count == 17
    assert request.batch_ordinal == 1
    assert request.max_claims == 3
    assert request.context_profile == "32k"
    assert request.evidence_budget == 24488
    assert request.budget_policy.limits.max_output_tokens == 4096
    assert request.budget_policy.limits.max_retries == 0
    total_chars = sum(
        len(item.text) for item in request.evidence_packet.fragments
    )
    assert total_chars > 90_000
    assert all(
        item.document_id.startswith("synthetic-")
        for item in request.evidence_packet.fragments
    )


def test_schema_adapter_natively_owns_separated_reasoning_boundary():
    request = _build_batch_shaped_request(_policy())
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )

    body = build_llama_schema_constrained_request_body(adapter, request)

    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["reasoning_format"] == "auto"
    assert body["reasoning_effort"] == "none"


def test_batch_shaped_request_body_uses_schema_and_separates_reasoning():
    request = _build_batch_shaped_request(_policy())
    adapter = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )

    body = build_llama_schema_constrained_request_body(adapter, request)
    body = apply_llama_non_reasoning_mode(body, request)

    assert body["response_format"]["type"] == "json_object"
    claims_schema = body["response_format"]["schema"]["properties"]["claims"]
    assert claims_schema["maxItems"] == 3
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["reasoning_format"] == "auto"
    assert body["reasoning_effort"] == "none"
