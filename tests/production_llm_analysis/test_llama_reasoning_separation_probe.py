from __future__ import annotations

import json
from types import SimpleNamespace

from scripts.r10_1.probe_llama_batch_shape import _build_batch_shaped_request
from scripts.r10_1.probe_llama_reasoning_separation import (
    _build_auto_reasoning_body,
    _inspect_envelope,
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


def _policy():
    return SimpleNamespace(
        provider="openai_compatible",
        model="arvectum-gemma4-12b-it-qat-q4_0",
        budget=make_policy(max_output_tokens=4096, max_retries=0),
    )


def _provider_with_wrapped_builder():
    provider = OpenAICompatibleProductionLLMProvider.__new__(
        OpenAICompatibleProductionLLMProvider
    )

    def builder(request):
        body = build_llama_schema_constrained_request_body(provider, request)
        return apply_llama_non_reasoning_mode(body, request)

    provider._build_request_body = builder
    return provider


def test_auto_reasoning_probe_changes_only_response_parsing_mode():
    request = _build_batch_shaped_request(_policy(), fragment_count=8)
    provider = _provider_with_wrapped_builder()

    body = _build_auto_reasoning_body(provider, request)

    assert body["reasoning_format"] == "auto"
    assert body["reasoning_effort"] == "none"
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    assert body["max_tokens"] == 4096
    assert body["response_format"]["type"] == "json_object"
    task = json.loads(body["messages"][1]["content"])
    assert task["output_contract"] == body["response_format"]["schema"]


def test_inspect_envelope_separates_reasoning_from_valid_json_content():
    envelope = {
        "choices": [
            {
                "message": {
                    "reasoning_content": "internal thought",
                    "content": json.dumps({"claims": []}),
                }
            }
        ]
    }

    result = _inspect_envelope(json.dumps(envelope).encode())

    assert result == {
        "envelope_valid_json": True,
        "message_content_string": True,
        "message_content_valid_json": True,
        "reasoning_content_present": True,
        "reasoning_content_bytes": len("internal thought".encode()),
        "claims_object_valid": True,
    }


def test_inspect_envelope_flags_non_json_content_without_exposing_it():
    envelope = {
        "choices": [
            {
                "message": {
                    "reasoning_content": "internal thought",
                    "content": "not-json-secret",
                }
            }
        ]
    }

    result = _inspect_envelope(json.dumps(envelope).encode())

    assert result["envelope_valid_json"] is True
    assert result["message_content_string"] is True
    assert result["message_content_valid_json"] is False
    assert result["claims_object_valid"] is False
    assert result["reasoning_content_present"] is True
    assert "not-json-secret" not in repr(result)
    assert "internal thought" not in repr(result)
