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
_SCHEMA_PATCH_MARKER = "_arv003_llama_schema_constraint_v5"
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
        "provider_response_truncated",
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
    if request.allowed_field_paths and not approved:
        # Fail closed: the live schema's field_path enum is derived from the
        # extractive requirement paths only. A non-empty allow-list that supplies
        # no extractive path must not silently fall back to a canonical set, which
        # would let a non-extractive allow-list reach the wire.
        raise ValueError("llama_schema_extractive_field_paths_missing")
    if not approved:
        # Empty allow-list is the planner/measurement default: derive the enum
        # from the canonical extractive requirement set for schema measurement.
        return sorted(set(_REQUIREMENT_FIELD_PATHS))
    return sorted(set(approved))


def build_live_compact_llama_schema(request: ProductionLLMAnalysisRequest) -> dict[str, Any]:
    """Single canonical live schema for transport, hashing, measurement and proof.

    This implementation is used simultaneously for final response_format.schema,
    task.output_contract, request-body hashing, batch measurement, exact-tokenizer
    proof, parser/rewrite contract, pre-provider verification and test fixtures.
    Server-owned grounding (claim_id/value/quote sentinels, one reference,
    field_path enum of extractive requirement paths, claims maxItems ≤3) is
    preserved. The payload is designed to fit substantially below the 4096 budget
    so that output-limit truncation surfaces as a distinct provider_schema_boundary_missing
    style code only when the schema is absent or mismatched.
    """

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
    # Remove provider_confidence from the generatable live schema
    claim_properties.pop("provider_confidence", None)
    schema["additionalProperties"] = False
    return schema


# Backward-compatible alias for tests still importing compact_response_schema.
compact_response_schema = build_live_compact_llama_schema


def build_llama_schema_constrained_request_body(
    self: OpenAICompatibleProductionLLMProvider,
    request: ProductionLLMAnalysisRequest,
) -> dict[str, Any]:
    """Reuse the canonical adapter and add deterministic server-owned grounding."""

    body = _ORIGINAL_BUILD_REQUEST_BODY(self, request)
    if request.provider_wire_contract_version in {"compact-safe-v1", "compact-safe-v2"}:
        schema = build_live_compact_llama_schema(request)
        body["response_format"] = {
            "type": "json_object",
            "schema": schema,
        }
        body["chat_template_kwargs"] = {"enable_thinking": False}
        # Gemma4 may still emit an optional thought block despite thinking being
        # disabled. `auto` keeps that block out of message.content, preserving a
        # JSON-only content boundary, while reasoning_effort=none keeps generation
        # itself disabled. Keep these controls in the schema adapter so reinstall
        # order cannot strip the production boundary.
        body["reasoning_format"] = "auto"
        body["reasoning_effort"] = "none"
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
            truncation_finish_reason=getattr(exc, "truncation_finish_reason", None),
            truncation_prompt_tokens=getattr(exc, "truncation_prompt_tokens", None),
            truncation_completion_tokens=getattr(
                exc, "truncation_completion_tokens", None
            ),
            truncation_response_utf8_bytes=getattr(
                exc, "truncation_response_utf8_bytes", None
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



def verify_final_live_request_body(body: dict[str, Any], request: ProductionLLMAnalysisRequest) -> dict[str, Any]:
    """Zero-generation verification of the final HTTP request body after all wrappers.

    The check runs on the same method and order used immediately before HTTPClient.send.
    It is sanitized (hashes/flags only) and fail-closed.
    """

    if body.get("response_format", {}).get("schema") is None:
        raise ValueError("final_body_schema_missing")
    schema = body["response_format"]["schema"]
    import json as _json
    from src.modules.production_llm_analysis.evidence import canonical_sha256 as _sha

    try:
        task = _json.loads(body["messages"][1]["content"])
    except Exception:
        raise ValueError("final_body_task_invalid")
    output_contract = task.get("output_contract")
    if output_contract is None:
        raise ValueError("final_body_output_contract_missing")
    response_hash = _sha(schema)
    contract_hash = _sha(output_contract)
    if response_hash != contract_hash:
        raise ValueError("final_body_schema_identity_mismatch")
    dumped = _json.dumps(schema, sort_keys=True)
    if "$ref" in dumped or "$defs" in dumped or "definitions" in dumped:
        raise ValueError("final_body_schema_not_inline")
    expected = build_live_compact_llama_schema(request)
    if _sha(expected) != response_hash:
        raise ValueError("final_body_live_schema_mismatch")
    if body.get("max_tokens") != 4096:
        raise ValueError("final_body_max_tokens_mismatch")
    claims = schema.get("properties", {}).get("claims", {})
    if claims.get("maxItems") != (request.max_claims if request.max_claims is not None else 3):
        raise ValueError("final_body_max_claims_mismatch")
    ref_max = schema["properties"]["claims"]["items"]["properties"]["evidence_references"].get("maxItems")
    if ref_max != 1:
        raise ValueError("final_body_reference_limit_mismatch")
    chat_kwargs = body.get("chat_template_kwargs") or {}
    if chat_kwargs.get("enable_thinking") is not False:
        raise ValueError("final_body_enable_thinking_not_false")
    if body.get("reasoning_format") != "auto":
        raise ValueError("final_body_reasoning_format_not_auto")
    if body.get("reasoning_effort") != "none":
        raise ValueError("final_body_reasoning_effort_not_none")
    return {
        "final_request_body_sha256": _sha(body),
        "response_schema_sha256": response_hash,
        "output_contract_sha256": contract_hash,
        "schemas_identical": response_hash == contract_hash,
        "schema_inline_no_refs": True,
        "max_tokens": 4096,
        "max_claims": request.max_claims if request.max_claims is not None else 3,
        "reference_limit": ref_max,
        "sentinel_contract_enabled": True,
        "enable_thinking_false": True,
        "reasoning_format": "auto",
        "reasoning_effort": "none",
        "provider_wire_contract_version": request.provider_wire_contract_version,
        "prompt_version": request.prompt_version,
        "output_schema_version": request.output_schema_version,
        "batch_plan_version": request.batch_plan_version,
    }


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
