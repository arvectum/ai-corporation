from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from src.modules.procurement_analysis import r10_1_producer
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)
from src.modules.production_llm_analysis.schemas import (
    AnalysisStatus,
    CompactWireProviderResponse,
    ProductionLLMAnalysisRequest,
)
from src.shared.llm.transport import InvalidProviderResponseError

_ORIGINAL_BUILD_REQUEST_BODY = OpenAICompatibleProductionLLMProvider._build_request_body
_ORIGINAL_PARSE_SUCCESS_RESPONSE = (
    OpenAICompatibleProductionLLMProvider._parse_success_response
)
_ORIGINAL_RUN_PRODUCTION_ANALYSIS = r10_1_producer.run_production_llm_analysis
_SCHEMA_PATCH_MARKER = "_arv003_llama_schema_constraint_v2"
_PARSE_PATCH_MARKER = "_arv003_llama_invalid_response_capture_v1"
_RUN_PATCH_MARKER = "_arv003_llama_invalid_response_surface_v1"
_LAST_INVALID_RESPONSE_CODE: ContextVar[str | None] = ContextVar(
    "arv003_llama_invalid_response_code",
    default=None,
)
_SAFE_INVALID_RESPONSE_CODES = frozenset(
    {
        "provider_claims_not_list",
        "provider_content_schema_mismatch",
        "provider_input_tokens_invalid",
        "provider_message_content_invalid",
        "provider_message_content_missing",
        "provider_output_tokens_invalid",
        "provider_request_id_invalid",
        "provider_response_invalid_envelope",
        "provider_response_invalid_json",
        "provider_usage_invalid",
        "provider_wire_claim_schema_invalid",
        "provider_wire_duplicate_reference",
        "provider_wire_fragment_not_found",
        "provider_wire_quote_empty",
        "provider_wire_quote_not_found",
        "provider_wire_reference_schema_invalid",
    }
)


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


def _inline_local_references(
    value: Any,
    *,
    root_schema: dict[str, Any],
    reference_stack: tuple[str, ...] = (),
) -> Any:
    """Inline Pydantic local refs for llama.cpp's limited schema converter."""

    if isinstance(value, list):
        return [
            _inline_local_references(
                item,
                root_schema=root_schema,
                reference_stack=reference_stack,
            )
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    reference = value.get("$ref")
    if reference is not None:
        if set(value) != {"$ref"}:
            raise ValueError("llama_schema_reference_siblings_unsupported")
        reference = str(reference)
        if reference in reference_stack:
            raise ValueError("llama_schema_reference_cycle")
        target = _resolve_local_reference(root_schema, reference)
        return _inline_local_references(
            target,
            root_schema=root_schema,
            reference_stack=(*reference_stack, reference),
        )

    return {
        key: _inline_local_references(
            item,
            root_schema=root_schema,
            reference_stack=reference_stack,
        )
        for key, item in value.items()
        if key not in {"$defs", "definitions"}
    }


def compact_response_schema(request: ProductionLLMAnalysisRequest) -> dict[str, Any]:
    """Build one flat, batch-bound JSON schema for llama.cpp decoding."""

    source_schema = CompactWireProviderResponse.model_json_schema()
    schema = _inline_local_references(source_schema, root_schema=source_schema)
    try:
        claims_schema = schema["properties"]["claims"]
        claim_schema = claims_schema["items"]
        claim_properties = claim_schema["properties"]
        references_schema = claim_properties["evidence_references"]
        reference_schema = references_schema["items"]
        reference_properties = reference_schema["properties"]
    except (KeyError, TypeError):
        raise ValueError("llama_schema_contract_invalid") from None

    if request.max_claims is not None:
        claims_schema["maxItems"] = request.max_claims

    if request.allowed_field_paths:
        claim_properties["field_path"]["enum"] = list(request.allowed_field_paths)

    fragment_ids = sorted(
        {fragment.fragment_id for fragment in request.evidence_packet.fragments}
    )
    if not fragment_ids:
        raise ValueError("llama_schema_fragment_ids_missing")
    reference_properties["fragment_id"]["enum"] = fragment_ids

    claim_properties["claim_id"]["maxLength"] = 128
    references_schema["minItems"] = 1
    reference_properties["quote"]["maxLength"] = 1024
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


def _parse_success_response_with_safe_diagnostics(
    self: OpenAICompatibleProductionLLMProvider,
    *args: Any,
    **kwargs: Any,
) -> Any:
    try:
        return _ORIGINAL_PARSE_SUCCESS_RESPONSE(self, *args, **kwargs)
    except InvalidProviderResponseError as exc:
        candidate = str(exc).strip().lower()
        _LAST_INVALID_RESPONSE_CODE.set(
            candidate
            if candidate in _SAFE_INVALID_RESPONSE_CODES
            else "provider_response_invalid"
        )
        raise


def _run_production_analysis_with_safe_diagnostics(
    request: ProductionLLMAnalysisRequest,
    provider: Any,
) -> Any:
    token = _LAST_INVALID_RESPONSE_CODE.set(None)
    try:
        result = _ORIGINAL_RUN_PRODUCTION_ANALYSIS(request, provider)
        diagnostic = _LAST_INVALID_RESPONSE_CODE.get()
        if result.status == AnalysisStatus.INVALID_RESPONSE and diagnostic:
            raise r10_1_producer.R10_1AnalysisRejectedError(
                f"evidence_batch_invalid_response:{diagnostic}"
            )
        return result
    finally:
        _LAST_INVALID_RESPONSE_CODE.reset(token)


setattr(build_llama_schema_constrained_request_body, _SCHEMA_PATCH_MARKER, True)
setattr(
    _parse_success_response_with_safe_diagnostics,
    _PARSE_PATCH_MARKER,
    True,
)
setattr(
    _run_production_analysis_with_safe_diagnostics,
    _RUN_PATCH_MARKER,
    True,
)


def install_llama_schema_constraint() -> None:
    """Install process-local schema and sanitized diagnostic patches."""

    current_builder = OpenAICompatibleProductionLLMProvider._build_request_body
    if not bool(getattr(current_builder, _SCHEMA_PATCH_MARKER, False)):
        OpenAICompatibleProductionLLMProvider._build_request_body = (
            build_llama_schema_constrained_request_body
        )

    current_parser = OpenAICompatibleProductionLLMProvider._parse_success_response
    if not bool(getattr(current_parser, _PARSE_PATCH_MARKER, False)):
        OpenAICompatibleProductionLLMProvider._parse_success_response = (
            _parse_success_response_with_safe_diagnostics
        )

    current_runner = r10_1_producer.run_production_llm_analysis
    if not bool(getattr(current_runner, _RUN_PATCH_MARKER, False)):
        r10_1_producer.run_production_llm_analysis = (
            _run_production_analysis_with_safe_diagnostics
        )
