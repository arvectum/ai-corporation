from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from typing import Any

from src.modules.procurement_analysis import r10_1_producer
from src.modules.production_llm_analysis.evidence import canonical_json_bytes
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)
from src.modules.production_llm_analysis.schemas import (
    AnalysisStatus,
    CompactWireProviderResponse,
    ProductionLLMAnalysisRequest,
)
from src.shared.llm.transport import HTTPResponse, InvalidProviderResponseError

_ORIGINAL_BUILD_REQUEST_BODY = OpenAICompatibleProductionLLMProvider._build_request_body
_ORIGINAL_PARSE_SUCCESS_RESPONSE = (
    OpenAICompatibleProductionLLMProvider._parse_success_response
)
_ORIGINAL_RUN_PRODUCTION_ANALYSIS = r10_1_producer.run_production_llm_analysis
_SCHEMA_PATCH_MARKER = "_arv003_llama_schema_constraint_v4"
_PARSE_PATCH_MARKER = "_arv003_llama_invalid_response_capture_v3"
_RUN_PATCH_MARKER = "_arv003_llama_invalid_response_surface_v1"
_LLAMA_SCHEMA_PROFILE = "fragment-grounded-extractive-v2"
_SERVER_CLAIM_ID_SENTINEL = "__ARVECTUM_SERVER_CLAIM_ID__"
_SERVER_FRAGMENT_VALUE_SENTINEL = "__ARVECTUM_SERVER_FRAGMENT_VALUE__"
_SERVER_FRAGMENT_QUOTE_SENTINEL = "__ARVECTUM_SERVER_FRAGMENT_QUOTE__"
_REQUIREMENT_FIELD_PATHS = (
    "requirements.document_requirements",
    "requirements.evaluation_criteria",
    "requirements.qualification_requirements",
    "requirements.technical_requirements",
)
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
        "provider_response_invalid",
        "provider_response_invalid_envelope",
        "provider_response_invalid_json",
        "provider_usage_invalid",
        "provider_wire_claim_id_sentinel_invalid",
        "provider_wire_claim_schema_invalid",
        "provider_wire_duplicate_reference",
        "provider_wire_field_path_not_extractive",
        "provider_wire_fragment_not_found",
        "provider_wire_quote_empty",
        "provider_wire_quote_not_found",
        "provider_wire_quote_sentinel_invalid",
        "provider_wire_reference_count_invalid",
        "provider_wire_reference_schema_invalid",
        "provider_wire_value_sentinel_invalid",
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


def _extractive_field_paths(request: ProductionLLMAnalysisRequest) -> list[str]:
    approved = [
        field_path
        for field_path in request.allowed_field_paths
        if field_path in _REQUIREMENT_FIELD_PATHS
    ]
    if not approved:
        raise ValueError("llama_schema_extractive_field_paths_missing")
    return sorted(set(approved))


def compact_response_schema(request: ProductionLLMAnalysisRequest) -> dict[str, Any]:
    """Build one flat, batch-bound, server-grounded llama.cpp schema."""

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

    claim_properties["claim_id"] = {
        "type": "string",
        "const": _SERVER_CLAIM_ID_SENTINEL,
    }
    claim_properties["field_path"]["enum"] = _extractive_field_paths(request)
    claim_properties["value"] = {
        "type": "string",
        "const": _SERVER_FRAGMENT_VALUE_SENTINEL,
    }

    fragment_ids = sorted(
        {fragment.fragment_id for fragment in request.evidence_packet.fragments}
    )
    if not fragment_ids:
        raise ValueError("llama_schema_fragment_ids_missing")
    reference_properties["fragment_id"]["enum"] = fragment_ids
    reference_properties["quote"] = {
        "type": "string",
        "const": _SERVER_FRAGMENT_QUOTE_SENTINEL,
    }

    references_schema["minItems"] = 1
    references_schema["maxItems"] = 1
    return schema


def build_llama_schema_constrained_request_body(
    self: OpenAICompatibleProductionLLMProvider,
    request: ProductionLLMAnalysisRequest,
) -> dict[str, Any]:
    """Reuse the canonical adapter and add deterministic server-owned grounding."""

    body = _ORIGINAL_BUILD_REQUEST_BODY(self, request)
    if request.provider_wire_contract_version in {"compact-safe-v1", "compact-safe-v2"}:
        schema = compact_response_schema(request)
        body["response_format"] = {
            "type": "json_object",
            "schema": schema,
        }
        system_message = body["messages"][0]
        system_message["content"] = (
            f"{system_message['content']} "
            f"For the {_LLAMA_SCHEMA_PROFILE} profile, emit only requirement field paths. "
            f"Set every claim_id to {_SERVER_CLAIM_ID_SENTINEL} and every claim value "
            f"to {_SERVER_FRAGMENT_VALUE_SENTINEL}. Return exactly one evidence reference "
            f"per claim and set its quote to {_SERVER_FRAGMENT_QUOTE_SENTINEL}. The server "
            "will derive the final claim identity, value and quote from the selected fragment "
            "and re-run exact lexical grounding."
        )
        task = json.loads(body["messages"][1]["content"])
        task["output_contract"] = schema
        task["map_contract"]["allowed_field_paths"] = _extractive_field_paths(request)
        task["map_contract"]["llama_schema_profile"] = _LLAMA_SCHEMA_PROFILE
        task["map_contract"]["server_owned_claim_identity"] = True
        task["map_contract"]["server_owned_fragment_grounding"] = True
        body["messages"][1]["content"] = canonical_json_bytes(task).decode("utf-8")
    return body


def _invalid_response(
    code: str,
    *,
    raw_response_sha256: str,
    retry_count: int,
    attempt_latencies_ms: tuple[int, ...],
    total_latency_ms: int | None,
) -> InvalidProviderResponseError:
    return InvalidProviderResponseError(
        code,
        retry_count=retry_count,
        attempt_latencies_ms=attempt_latencies_ms,
        total_latency_ms=total_latency_ms,
        raw_response_sha256=raw_response_sha256,
    )


def _server_claim_id(
    request: ProductionLLMAnalysisRequest,
    *,
    field_path: str,
    fragment_id: str,
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "batch_hash": request.batch_hash,
                "batch_ordinal": request.batch_ordinal,
                "field_path": field_path,
                "fragment_id": fragment_id,
            }
        )
    ).hexdigest()


def _rewrite_server_grounded_response(
    response: HTTPResponse,
    request: ProductionLLMAnalysisRequest,
    *,
    retry_count: int,
    attempt_latencies_ms: tuple[int, ...],
    total_latency_ms: int | None,
) -> tuple[HTTPResponse, str]:
    raw_response_sha256 = hashlib.sha256(response.body).hexdigest()
    if request.provider_wire_contract_version not in {"compact-safe-v1", "compact-safe-v2"}:
        return response, raw_response_sha256

    try:
        envelope = json.loads(response.body.decode("utf-8"))
        content_text = envelope["choices"][0]["message"]["content"]
        if not isinstance(content_text, str):
            return response, raw_response_sha256
        content = json.loads(content_text)
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
        return response, raw_response_sha256

    if not isinstance(content, dict) or not isinstance(content.get("claims"), list):
        return response, raw_response_sha256

    fragments = {
        fragment.fragment_id: fragment for fragment in request.evidence_packet.fragments
    }
    extractive_field_paths = _extractive_field_paths(request)
    for claim in content["claims"]:
        if not isinstance(claim, dict):
            return response, raw_response_sha256
        if claim.get("claim_id") != _SERVER_CLAIM_ID_SENTINEL:
            raise _invalid_response(
                "provider_wire_claim_id_sentinel_invalid",
                raw_response_sha256=raw_response_sha256,
                retry_count=retry_count,
                attempt_latencies_ms=attempt_latencies_ms,
                total_latency_ms=total_latency_ms,
            )
        field_path = claim.get("field_path")
        if field_path not in extractive_field_paths:
            raise _invalid_response(
                "provider_wire_field_path_not_extractive",
                raw_response_sha256=raw_response_sha256,
                retry_count=retry_count,
                attempt_latencies_ms=attempt_latencies_ms,
                total_latency_ms=total_latency_ms,
            )
        if claim.get("value") != _SERVER_FRAGMENT_VALUE_SENTINEL:
            raise _invalid_response(
                "provider_wire_value_sentinel_invalid",
                raw_response_sha256=raw_response_sha256,
                retry_count=retry_count,
                attempt_latencies_ms=attempt_latencies_ms,
                total_latency_ms=total_latency_ms,
            )
        references = claim.get("evidence_references")
        if not isinstance(references, list) or len(references) != 1:
            raise _invalid_response(
                "provider_wire_reference_count_invalid",
                raw_response_sha256=raw_response_sha256,
                retry_count=retry_count,
                attempt_latencies_ms=attempt_latencies_ms,
                total_latency_ms=total_latency_ms,
            )
        reference = references[0]
        if not isinstance(reference, dict):
            return response, raw_response_sha256
        if reference.get("quote") != _SERVER_FRAGMENT_QUOTE_SENTINEL:
            raise _invalid_response(
                "provider_wire_quote_sentinel_invalid",
                raw_response_sha256=raw_response_sha256,
                retry_count=retry_count,
                attempt_latencies_ms=attempt_latencies_ms,
                total_latency_ms=total_latency_ms,
            )
        fragment_id = reference.get("fragment_id")
        fragment = fragments.get(fragment_id)
        if fragment is None:
            raise _invalid_response(
                "provider_wire_fragment_not_found",
                raw_response_sha256=raw_response_sha256,
                retry_count=retry_count,
                attempt_latencies_ms=attempt_latencies_ms,
                total_latency_ms=total_latency_ms,
            )
        claim["claim_id"] = _server_claim_id(
            request,
            field_path=field_path,
            fragment_id=fragment_id,
        )
        claim["value"] = fragment.text
        reference["quote"] = fragment.text

    envelope["choices"][0]["message"]["content"] = canonical_json_bytes(content).decode(
        "utf-8"
    )
    rewritten = HTTPResponse(
        status_code=response.status_code,
        headers=response.headers,
        body=canonical_json_bytes(envelope),
    )
    return rewritten, raw_response_sha256


def _parse_success_response_with_safe_diagnostics(
    self: OpenAICompatibleProductionLLMProvider,
    *args: Any,
    **kwargs: Any,
) -> Any:
    response = kwargs.get("response")
    request = kwargs.get("request")
    retry_count = int(kwargs.get("retry_count", 0))
    attempt_latencies_ms = tuple(kwargs.get("attempt_latencies_ms", ()))
    total_latency_ms = sum(attempt_latencies_ms)
    raw_response_sha256: str | None = None

    try:
        if isinstance(response, HTTPResponse) and isinstance(
            request, ProductionLLMAnalysisRequest
        ):
            rewritten, raw_response_sha256 = _rewrite_server_grounded_response(
                response,
                request,
                retry_count=retry_count,
                attempt_latencies_ms=attempt_latencies_ms,
                total_latency_ms=total_latency_ms,
            )
            kwargs["response"] = rewritten
        result = _ORIGINAL_PARSE_SUCCESS_RESPONSE(self, *args, **kwargs)
        if raw_response_sha256 is not None:
            result = result.model_copy(
                update={"raw_response_sha256": raw_response_sha256}
            )
        return result
    except InvalidProviderResponseError as exc:
        candidate = str(exc).strip().lower()
        safe_code = (
            candidate
            if candidate in _SAFE_INVALID_RESPONSE_CODES
            else "provider_response_invalid"
        )
        _LAST_INVALID_RESPONSE_CODE.set(safe_code)
        raise InvalidProviderResponseError(
            safe_code,
            retry_count=int(getattr(exc, "retry_count", retry_count)),
            attempt_latencies_ms=tuple(
                getattr(exc, "attempt_latencies_ms", attempt_latencies_ms)
            ),
            total_latency_ms=getattr(exc, "total_latency_ms", total_latency_ms),
            raw_response_sha256=(
                raw_response_sha256 or getattr(exc, "raw_response_sha256", None)
            ),
        ) from None


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
    """Install process-local schema, grounding and sanitized diagnostics."""

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
