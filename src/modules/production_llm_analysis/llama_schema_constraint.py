from __future__ import annotations

from typing import Any

from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)
from src.modules.production_llm_analysis.schemas import (
    CompactWireProviderResponse,
    ProductionLLMAnalysisRequest,
)

_ORIGINAL_BUILD_REQUEST_BODY = OpenAICompatibleProductionLLMProvider._build_request_body
_PATCH_MARKER = "_arv003_llama_schema_constraint_v1"


def _decode_json_pointer_segment(value: str) -> str:
    return value.replace("~1", "/").replace("~0", "~")


def _resolve_local_reference(schema: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError("llama_schema_reference_not_local")
    current: Any = schema
    for raw_segment in reference[2:].split("/"):
        segment = _decode_json_pointer_segment(raw_segment)
        if not isinstance(current, dict) or segment not in current:
            raise ValueError("llama_schema_reference_unresolved")
        current = current[segment]
    if not isinstance(current, dict):
        raise ValueError("llama_schema_reference_invalid")
    return current


def compact_response_schema(request: ProductionLLMAnalysisRequest) -> dict[str, Any]:
    """Build one root-valid JSON schema for llama.cpp constrained decoding."""

    schema = CompactWireProviderResponse.model_json_schema()
    try:
        claims_schema = schema["properties"]["claims"]
        claim_items = claims_schema["items"]
    except (KeyError, TypeError):
        raise ValueError("llama_schema_contract_invalid") from None

    if request.max_claims is not None:
        claims_schema["maxItems"] = request.max_claims

    claim_schema = claim_items
    if isinstance(claim_items, dict) and "$ref" in claim_items:
        claim_schema = _resolve_local_reference(schema, str(claim_items["$ref"]))
    if not isinstance(claim_schema, dict):
        raise ValueError("llama_schema_claim_contract_invalid")

    if request.allowed_field_paths:
        try:
            field_path_schema = claim_schema["properties"]["field_path"]
        except (KeyError, TypeError):
            raise ValueError("llama_schema_field_path_contract_invalid") from None
        field_path_schema["enum"] = list(request.allowed_field_paths)

    return schema


def build_llama_schema_constrained_request_body(
    self: OpenAICompatibleProductionLLMProvider,
    request: ProductionLLMAnalysisRequest,
) -> dict[str, Any]:
    """Reuse the canonical adapter and add llama.cpp schema-constrained JSON."""

    body = _ORIGINAL_BUILD_REQUEST_BODY(self, request)
    if request.provider_wire_contract_version == "compact-safe-v1":
        body["response_format"] = {
            "type": "json_object",
            "schema": compact_response_schema(request),
        }
    return body


setattr(build_llama_schema_constrained_request_body, _PATCH_MARKER, True)


def install_llama_schema_constraint() -> None:
    """Install the process-local patch before planning and provider construction."""

    current = OpenAICompatibleProductionLLMProvider._build_request_body
    if bool(getattr(current, _PATCH_MARKER, False)):
        return
    OpenAICompatibleProductionLLMProvider._build_request_body = (
        build_llama_schema_constrained_request_body
    )
