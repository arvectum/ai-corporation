from __future__ import annotations

from typing import Any

from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)
from src.modules.production_llm_analysis.schemas import ProductionLLMAnalysisRequest

_LLAMA_REASONING_PROFILE = "thinking-disabled-json-v1"
_PATCH_MARKER = "_arv003_llama_reasoning_disabled_v1"


def apply_llama_non_reasoning_mode(
    body: dict[str, Any],
    request: ProductionLLMAnalysisRequest,
) -> dict[str, Any]:
    """Disable template-level thinking for compact schema-constrained requests."""

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
    return body


def install_llama_non_reasoning_mode() -> None:
    """Wrap the currently installed request builder with thinking disabled."""

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
