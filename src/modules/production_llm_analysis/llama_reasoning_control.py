from __future__ import annotations

from typing import Any

from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)
from src.modules.production_llm_analysis.schemas import ProductionLLMAnalysisRequest

_LLAMA_REASONING_PROFILE = "thinking-disabled-reasoning-separated-json-v4"
_PATCH_MARKER = "_arv003_llama_reasoning_disabled_v4"


def apply_llama_non_reasoning_mode(
    body: dict[str, Any],
    request: ProductionLLMAnalysisRequest,
) -> dict[str, Any]:
    """Disable reasoning generation while keeping reasoning/content separated."""

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

    # Gemma4's llama.cpp parser inlines optional thought blocks into
    # message.content when reasoning_format=none. `auto` keeps an unexpected
    # thought block structurally separate in message.reasoning_content while
    # reasoning generation itself remains disabled by enable_thinking=false and
    # reasoning_effort=none. The schema adapter now owns the same values so
    # install order cannot weaken the final transport boundary.
    current_format = body.get("reasoning_format")
    if current_format is not None and current_format not in {"none", "auto"}:
        raise ValueError("llama_reasoning_format_conflict")
    body["reasoning_format"] = "auto"

    if body.get("reasoning_effort") is not None and body["reasoning_effort"] != "none":
        raise ValueError("llama_reasoning_effort_conflict")
    body["reasoning_effort"] = "none"
    return body


def install_llama_non_reasoning_mode() -> None:
    """Wrap the request builder with disabled generation and separated parsing."""

    current = OpenAICompatibleProductionLLMProvider._build_request_body
    if not bool(getattr(current, _PATCH_MARKER, False)):

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
