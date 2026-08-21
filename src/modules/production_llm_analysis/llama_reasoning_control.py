from __future__ import annotations

from typing import Any

from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)
from src.modules.production_llm_analysis.schemas import ProductionLLMAnalysisRequest

_LLAMA_REASONING_PROFILE = "thinking-disabled-json-v2"
_PATCH_MARKER = "_arv003_llama_reasoning_disabled_v2"


def apply_llama_non_reasoning_mode(
    body: dict[str, Any],
    request: ProductionLLMAnalysisRequest,
) -> dict[str, Any]:
    """Disable llama.cpp reasoning for compact schema-constrained requests."""

    if request.provider_wire_contract_version not in {"compact-safe-v1", "compact-safe-v2"}:
        return body

    existing = body.get("chat_template_kwargs")
    if existing is not None and not isinstance(existing, dict):
        raise ValueError("llama_chat_template_kwargs_invalid")
    kwargs = dict(existing or {})
    if kwargs.get("enable_thinking") is True:
        raise ValueError("llama_thinking_mode_conflict")
    kwargs["enable_thinking"] = False
    body["chat_template_kwargs"] = kwargs

    # `reasoning_format=none` controls how llama.cpp exposes reasoning text; it
    # does not disable reasoning generation. Keep it for raw-content semantics,
    # and also set `reasoning_effort=none`, which is the request-level control
    # that disables reasoning on current llama.cpp builds.
    if body.get("reasoning_format") is not None and body["reasoning_format"] != "none":
        raise ValueError("llama_reasoning_format_conflict")
    body["reasoning_format"] = "none"
    if body.get("reasoning_effort") is not None and body["reasoning_effort"] != "none":
        raise ValueError("llama_reasoning_effort_conflict")
    body["reasoning_effort"] = "none"
    return body


def install_llama_non_reasoning_mode() -> None:
    """Wrap the currently installed request builder with reasoning disabled."""

    current = OpenAICompatibleProductionLLMProvider._build_request_body
    if bool(getattr(current, _PATCH_MARKER, False)):
        return

    def _build_request_body_without_thinking(
        self: OpenAICompatibleProductionLLMProvider,
        request: ProductionLLMAnalysisRequest,
    ) -> dict[str, Any]:
        body = current(self, request)
        return apply_llama_non_reasoning_mode(body, request)

    setattr(_build_request_body_without_thinking, _PATCH_MARKER, True)
    OpenAICompatibleProductionLLMProvider._build_request_body = (
        _build_request_body_without_thinking
    )
    from src.modules.production_llm_analysis.openai_compatible import (
        enable_live_boundary_verification,
    )

    enable_live_boundary_verification()
