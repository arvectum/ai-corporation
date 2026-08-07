from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError as PydanticValidationError

from src.modules.production_llm_analysis.evidence import (
    canonical_json_bytes,
    canonical_sha256,
    text_sha256,
)
from src.modules.production_llm_analysis.schemas import (
    CompactWireEvidenceFragment,
    CompactWireProviderClaim,
    CompactWireProviderResponse,
    EvidenceReference,
    ProductionLLMAnalysisRequest,
    ProviderAnalysisResponse,
    ProviderClaim,
)
from src.modules.production_llm_analysis.transport_boundary import (
    write_transport_start_marker,
)
from src.shared.llm.transport import (
    HTTPClient,
    HTTPRequest,
    HTTPResponse,
    InvalidProviderResponseError,
    ProviderBudgetExceededError,
    ProviderPermanentError,
    ProviderTimeoutError,
    ProviderTransientError,
    UrllibHTTPClient,
)

_TRANSIENT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
_PROTECTED_HEADERS = {"accept", "authorization", "content-type"}

# Set by install_llama_non_reasoning_mode() to enable zero-generation
# verification of the final body immediately before HTTPClient.send. When
# False (tests and non-controlled transports), the transport sends the body
# unverified so existing raw-transport tests remain unaffected.
_LIVE_BOUNDARY_VERIFICATION_ENABLED = False

# ---------------------------------------------------------------------------
# Bounded compact-wire output contract (repository-owned, inline, deterministic)
# ---------------------------------------------------------------------------
# All provider-owned dimensions are finitely bounded so that any
# schema-valid response is guaranteed to fit within the approved
# 4096-token output budget with at least 512 tokens of safety margin.
# These limits are minimal sufficient for procurement analysis and
# are the sole transport boundary — no post-generation truncation occurs.
CLAIM_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
CLAIM_ID_MAX_LENGTH = 64
EVIDENCE_REFERENCES_MAX_ITEMS = 2
EVIDENCE_REFERENCES_MIN_ITEMS = 1
FRAGMENT_ID_PATTERN = r"^[0-9a-f]{64}$"
QUOTE_MAX_LENGTH = 800
QUOTE_MIN_LENGTH = 1
REQUIREMENT_STRING_MAX_LENGTH = 800
REQUIREMENT_LIST_MAX_ITEMS = 3
RISK_LIST_MAX_ITEMS = 2
QUESTION_LIST_MAX_ITEMS = 2
RISK_CLAUSE_MAX_LENGTH = 200
RISK_DESCRIPTION_MAX_LENGTH = 400
RISK_IMPACT_MAX_LENGTH = 400
RISK_MITIGATION_MAX_LENGTH = 400
QUESTION_MAX_LENGTH = 400
CATEGORY_MAX_LENGTH = 100
_ALLOWED_RISK_CLASSIFICATIONS = (
    "market_standard_harsh_term",
    "commercially_material_risk",
    "deal_breaker_candidate",
)


def _build_compact_wire_output_schema_internal(
    *,
    max_claims: int | None,
    allowed_field_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Return deterministic inline JSON Schema for the compact wire output.

    The schema is fully inline (no ``$ref`` / ``$defs``), uses
    ``additionalProperties: false`` everywhere, and bounds every
    provider-owned dimension. The same object is used for
    ``response_format.schema``, the task ``output_contract``, hashing
    and worst-case tests — divergence is forbidden.
    """

    field_paths = sorted(allowed_field_paths or [])
    if not field_paths:
        # Fallback to the six map-allowed field paths for offline/worst-case usage;
        # controlled callers always supply an explicit allow-list.
        from src.modules.procurement_analysis.r10_1_producer import (
            _MAP_ALLOWED_FIELD_PATHS,  # local import to avoid cycle at import time
        )

        field_paths = sorted(_MAP_ALLOWED_FIELD_PATHS)

    # Bounded value forms — exactly the six _map_supported_claims shapes
    requirement_string_schema: dict[str, Any] = {
        "type": "string",
        "minLength": 1,
        "maxLength": REQUIREMENT_STRING_MAX_LENGTH,
    }
    requirement_list_schema: dict[str, Any] = {
        "type": "array",
        "minItems": 1,
        "maxItems": REQUIREMENT_LIST_MAX_ITEMS,
        "items": {
            "type": "string",
            "minLength": 1,
            "maxLength": 600,
        },
    }
    risk_object_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "clause",
            "description",
            "classification",
            "impact",
            "mitigation",
            "operator_decision_required",
        ],
        "properties": {
            "clause": {"type": "string", "minLength": 1, "maxLength": RISK_CLAUSE_MAX_LENGTH},
            "description": {
                "type": "string",
                "minLength": 1,
                "maxLength": RISK_DESCRIPTION_MAX_LENGTH,
            },
            "classification": {"type": "string", "enum": list(_ALLOWED_RISK_CLASSIFICATIONS)},
            "impact": {"type": "string", "minLength": 1, "maxLength": RISK_IMPACT_MAX_LENGTH},
            "mitigation": {
                "type": "string",
                "minLength": 1,
                "maxLength": RISK_MITIGATION_MAX_LENGTH,
            },
            "operator_decision_required": {"type": "boolean"},
        },
    }
    risk_list_schema: dict[str, Any] = {
        "type": "array",
        "minItems": 1,
        "maxItems": RISK_LIST_MAX_ITEMS,
        "items": risk_object_schema,
    }
    question_object_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["question", "category"],
        "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": QUESTION_MAX_LENGTH},
            "category": {"type": "string", "minLength": 1, "maxLength": CATEGORY_MAX_LENGTH},
        },
    }
    question_list_schema: dict[str, Any] = {
        "type": "array",
        "minItems": 1,
        "maxItems": QUESTION_LIST_MAX_ITEMS,
        "items": question_object_schema,
    }

    claim_schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "claim_id",
            "field_path",
            "value",
            "evidence_references",
        ],
        "properties": {
            "claim_id": {
                "type": "string",
                "pattern": CLAIM_ID_PATTERN,
                "minLength": 1,
                "maxLength": CLAIM_ID_MAX_LENGTH,
            },
            "field_path": {"type": "string", "enum": field_paths},
            "value": {
                "oneOf": [
                    requirement_string_schema,
                    requirement_list_schema,
                    risk_object_schema,
                    risk_list_schema,
                    question_object_schema,
                    question_list_schema,
                ]
            },
            "provider_confidence": {
                "anyOf": [
                    {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    {"type": "null"},
                ],
                "default": None,
            },
            "evidence_references": {
                "type": "array",
                "minItems": EVIDENCE_REFERENCES_MIN_ITEMS,
                "maxItems": EVIDENCE_REFERENCES_MAX_ITEMS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["fragment_id", "quote"],
                    "properties": {
                        "fragment_id": {
                            "type": "string",
                            "pattern": FRAGMENT_ID_PATTERN,
                        },
                        "quote": {
                            "type": "string",
                            "minLength": QUOTE_MIN_LENGTH,
                            "maxLength": QUOTE_MAX_LENGTH,
                        },
                    },
                },
            },
        },
    }

    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["claims"],
        "properties": {
            "claims": {
                "type": "array",
                "maxItems": max_claims if max_claims is not None else 3,
                "items": claim_schema,
            }
        },
    }
    return schema


def enable_live_boundary_verification() -> None:
    """Turn on zero-generation final-body verification in generate()."""
    global _LIVE_BOUNDARY_VERIFICATION_ENABLED
    _LIVE_BOUNDARY_VERIFICATION_ENABLED = True


@dataclass(frozen=True)
class OpenAICompatibleTransportConfig:
    base_url: str
    api_key: str = field(repr=False)
    auth_scheme: str = "Bearer"
    endpoint_path: str = "/chat/completions"
    extra_headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL")
        if not self.api_key.strip():
            raise ValueError("api_key is required")
        if "\n" in self.api_key or "\r" in self.api_key:
            raise ValueError("api_key contains prohibited control characters")
        if not self.auth_scheme.strip() or any(character in self.auth_scheme for character in "\r\n"):
            raise ValueError("auth_scheme is invalid")
        if not self.endpoint_path.startswith("/"):
            raise ValueError("endpoint_path must start with '/'")
        for key, value in self.extra_headers.items():
            if key.strip().lower() in _PROTECTED_HEADERS:
                raise ValueError("extra_headers cannot override protected transport headers")
            if not key.strip() or any(character in key for character in "\r\n"):
                raise ValueError("extra header name is invalid")
            if any(character in value for character in "\r\n"):
                raise ValueError("extra header value is invalid")

    @property
    def endpoint_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.endpoint_path}"


class OpenAICompatibleProductionLLMProvider:
    """OpenAI-compatible JSON transport behind the Gate 2 production contract."""

    def __init__(
        self,
        config: OpenAICompatibleTransportConfig,
        *,
        http_client: HTTPClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        transport_boundary: Path | None = None,
        execution_ordinal: int = 1,
    ) -> None:
        self._config = config
        self._http_client = http_client or UrllibHTTPClient()
        self._clock = clock
        self._transport_boundary = transport_boundary
        self._execution_ordinal = execution_ordinal
        self._last_boundary_verification: dict[str, Any] | None = None

    def set_transport_boundary(
        self, transport_boundary: Path, execution_ordinal: int
    ) -> None:
        """Attach a durable boundary root (idempotent, before first HTTP send)."""
        self._transport_boundary = transport_boundary
        self._execution_ordinal = execution_ordinal

    def generate(self, request: ProductionLLMAnalysisRequest) -> ProviderAnalysisResponse:
        final_body = self._build_request_body(request)
        if _LIVE_BOUNDARY_VERIFICATION_ENABLED and request.provider_wire_contract_version in {
            "compact-safe-v1",
            "compact-safe-v2",
        }:
            from src.modules.production_llm_analysis.llama_schema_constraint import (
                verify_final_live_request_body as _verify_final_live_body,
            )

            self._last_boundary_verification = _verify_final_live_body(
                final_body, request
            )
        body = canonical_json_bytes(final_body)
        limits = request.budget_policy.limits
        estimated_attempt_cost = self._estimate_attempt_cost(request, len(body))
        maximum_attempts = limits.max_retries + 1
        analysis_started = self._clock()
        attempt_latencies_ms: list[int] = []

        for attempt_index in range(maximum_attempts):
            elapsed_ms = self._elapsed_ms(analysis_started)
            remaining_latency_ms = limits.max_total_latency_ms - elapsed_ms
            if remaining_latency_ms <= 0:
                raise ProviderBudgetExceededError(
                    "provider_total_latency_budget_exceeded",
                    **self._failure_metadata(analysis_started, attempt_latencies_ms),
                )

            projected_cost = estimated_attempt_cost * (attempt_index + 1)
            if projected_cost > limits.max_estimated_cost + 1e-12:
                raise ProviderBudgetExceededError(
                    "provider_retry_cost_budget_exceeded",
                    **self._failure_metadata(analysis_started, attempt_latencies_ms),
                )

            timeout_ms = min(limits.timeout_ms, remaining_latency_ms)
            http_request = HTTPRequest(
                url=self._config.endpoint_url,
                body=body,
                headers=self._headers(),
                timeout_ms=timeout_ms,
            )
            attempt_started = self._clock()
            self._record_transport_start(request, attempt_index)
            try:
                response = self._http_client.send(http_request)
            except ProviderPermanentError:
                attempt_latencies_ms.append(self._elapsed_ms(attempt_started))
                raise ProviderPermanentError(
                    "provider_request_rejected",
                    **self._failure_metadata(analysis_started, attempt_latencies_ms),
                ) from None
            except ProviderTimeoutError:
                attempt_latencies_ms.append(self._elapsed_ms(attempt_started))
                if attempt_index + 1 >= maximum_attempts:
                    raise ProviderTimeoutError(
                        "provider_timeout",
                        **self._failure_metadata(analysis_started, attempt_latencies_ms),
                    ) from None
                continue
            except (ProviderTransientError, ConnectionError, OSError):
                attempt_latencies_ms.append(self._elapsed_ms(attempt_started))
                if attempt_index + 1 >= maximum_attempts:
                    raise ProviderTransientError(
                        "provider_transient_failure",
                        **self._failure_metadata(analysis_started, attempt_latencies_ms),
                    ) from None
                continue

            attempt_latencies_ms.append(self._elapsed_ms(attempt_started))
            response_hash = hashlib.sha256(response.body).hexdigest()
            if response.status_code in _TRANSIENT_STATUS_CODES:
                if attempt_index + 1 >= maximum_attempts:
                    raise ProviderTransientError(
                        "provider_transient_http_failure",
                        raw_response_sha256=response_hash,
                        **self._failure_metadata(analysis_started, attempt_latencies_ms),
                    )
                continue
            if not 200 <= response.status_code < 300:
                raise ProviderPermanentError(
                    "provider_request_rejected",
                    raw_response_sha256=response_hash,
                    **self._failure_metadata(analysis_started, attempt_latencies_ms),
                )

            return self._parse_success_response(
                response=response, request=request,
                attempt_latencies_ms=attempt_latencies_ms,
                retry_count=attempt_index,
                analysis_started=analysis_started,
            )

        raise ProviderTransientError(
            "provider_attempts_exhausted",
            **self._failure_metadata(analysis_started, attempt_latencies_ms),
        )


    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"{self._config.auth_scheme} {self._config.api_key}",
        }
        headers.update(dict(self._config.extra_headers))
        return headers

    def _build_request_body(self, request: ProductionLLMAnalysisRequest) -> dict[str, Any]:
        compact = request.provider_wire_contract_version in {"compact-safe-v1", "compact-safe-v2"}
        if request.provider_wire_contract_version not in {"full-v1", "compact-safe-v1", "compact-safe-v2"}:
            raise ValueError("provider_wire_contract_unsupported")
        if request.map_mode and request.provider_wire_contract_version not in {"compact-safe-v1", "compact-safe-v2"}:
            raise ValueError("provider_wire_contract_unsupported")
        if compact and request.provider_wire_contract_version == "compact-safe-v2":
            # Unified live schema: single canonical implementation used for
            # response_format.schema, output_contract, hashing, batch measurement,
            # exact tokenizer proof, parser and pre-provider verification.
            from src.modules.production_llm_analysis.llama_schema_constraint import (
                build_live_compact_llama_schema as _build_live_schema,
            )

            output_contract = _build_live_schema(request)
            claim_schema: dict[str, Any] | None = None  # live schema already built
        else:
            claim_schema = CompactWireProviderClaim.model_json_schema() if compact else ProviderClaim.model_json_schema()
            if request.allowed_field_paths and compact:
                claim_schema.setdefault("properties", {}).setdefault("field_path", {})[
                    "enum"
                ] = list(request.allowed_field_paths)
        evidence = [
            {
                "fragment_id": fragment.fragment_id,
                "document_id": fragment.document_id,
                "document_name": fragment.document_name,
                "chunk_id": fragment.chunk_id,
                "locator": fragment.locator,
                "text": fragment.text,
                "text_sha256": fragment.text_sha256,
            }
            for fragment in request.evidence_packet.fragments
        ]
        if compact:
            evidence = []
            for fragment in request.evidence_packet.fragments:
                locator = fragment.locator
                for key, code in (("document_order", "provider_wire_document_order"), ("chunk_index", "provider_wire_chunk_index")):
                    if key not in locator:
                        raise ValueError(f"{code}_missing")
                try:
                    wire_fragment = CompactWireEvidenceFragment(
                        fragment_id=fragment.fragment_id,
                        document_order=locator["document_order"],
                        chunk_index=locator["chunk_index"],
                        text=fragment.text,
                    )
                except PydanticValidationError as exc:
                    fields = {item.get("loc", (None,))[0] for item in exc.errors()}
                    code = "provider_wire_document_order_invalid" if "document_order" in fields else "provider_wire_chunk_index_invalid"
                    raise ValueError(code) from None
                evidence.append(wire_fragment.model_dump(mode="json"))
        if compact and request.provider_wire_contract_version == "compact-safe-v2":
            # Already computed above; reuse the identical object for output_contract
            pass
        else:
            output_contract = {
                "type": "object",
                "additionalProperties": False,
                "required": ["claims"],
                "properties": {
                    "claims": {
                        "type": "array",
                        "items": claim_schema,
                    }
                },
            }
            if request.max_claims is not None:
                output_contract["properties"]["claims"]["maxItems"] = request.max_claims
        task = {
            "prompt_id": request.prompt_id,
            "prompt_version": request.prompt_version,
            "output_schema_id": request.output_schema_id,
            "output_schema_version": request.output_schema_version,
            "grounding_policy_version": request.grounding_policy_version,
            "provider_wire_contract_version": request.provider_wire_contract_version,
            "procurement_case_id": request.procurement_case_id,
            "registry_number": request.registry_number,
            "evidence_packet_hash": request.evidence_packet.packet_hash,
            "batch_plan_version": request.batch_plan_version,
            "batch_plan_hash": request.batch_plan_hash,
            "batch_hash": request.batch_hash,
            "batch_ordinal": request.batch_ordinal,
            "batch_count": request.batch_count,
            "corpus_evidence_hash": request.corpus_evidence_hash,
            "evidence_fragments": evidence,
            "output_contract": output_contract,
            "map_contract": {
                "max_claims": request.max_claims,
                "allowed_field_paths": request.allowed_field_paths,
                "context_profile": request.context_profile,
                "evidence_budget": request.evidence_budget,
                "output_reserve": request.budget_policy.limits.max_output_tokens,
                "absence_is_not_corpus_negative": True,
            },
        }
        if compact:
            task.pop("procurement_case_id")
            task.pop("registry_number")
        if compact and request.provider_wire_contract_version == "compact-safe-v2":
            response_format: dict[str, Any] = {
                "type": "json_object",
                "schema": output_contract,
            }
        else:
            response_format = {"type": "json_object"}
        return {
            "model": request.model,
            "temperature": 0,
            "max_tokens": request.budget_policy.limits.max_output_tokens,
            "response_format": response_format,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a controlled internal procurement analysis component. "
                        "Return exactly one valid JSON object matching the supplied output contract. "
                        "Use only supplied evidence fragments. Every factual claim must copy exact fragment_id and quote. "
                        "Never return document metadata or a locator. Return an empty claims array when evidence is insufficient. "
                        "Analyze only the current batch; absence in this batch is not absence in the corpus. "
                        "Use only allowed field paths, return no more than max_claims, and never make GO/NO-GO "
                        "or other corpus-wide negative conclusions. "
                        "Do not authorize submission, signing, supplier outreach or any autonomous external action."
                    ),
                },
                {
                    "role": "user",
                    "content": canonical_json_bytes(task).decode("utf-8"),
                },
            ],
        }

    def _parse_success_response(
        self,
        *,
        response: HTTPResponse,
        request: ProductionLLMAnalysisRequest,
        attempt_latencies_ms: list[int],
        retry_count: int,
        analysis_started: float,
    ) -> ProviderAnalysisResponse:
        response_hash = hashlib.sha256(response.body).hexdigest()
        failure_metadata = {
            "retry_count": retry_count,
            "attempt_latencies_ms": tuple(attempt_latencies_ms),
            "total_latency_ms": self._total_latency_ms(analysis_started, attempt_latencies_ms),
            "raw_response_sha256": response_hash,
        }
        try:
            envelope = json.loads(response.body.decode("utf-8"))
            if not isinstance(envelope, dict):
                raise InvalidProviderResponseError("provider_response_invalid_envelope")

            # Detect output-limit truncation before any JSON/schema validation.
            # Truncated responses contain partial JSON; attempting to repair
            # would violate the fail-closed boundary. Surface a distinct code
            # and sanitized diagnostics only (no raw content/reasoning).
            try:
                finish_reason = envelope.get("choices", [{}])[0].get("finish_reason")  # type: ignore[union-attr]
            except (AttributeError, IndexError, TypeError, KeyError):
                finish_reason = None
            if isinstance(finish_reason, str) and finish_reason.strip().lower() == "length":
                usage = envelope.get("usage")
                try:
                    prompt_tokens = (
                        self._optional_non_negative_int(usage.get("prompt_tokens"), "provider_input_tokens_invalid")
                        if isinstance(usage, dict)
                        else None
                    )
                    completion_tokens = (
                        self._optional_non_negative_int(usage.get("completion_tokens"), "provider_output_tokens_invalid")
                        if isinstance(usage, dict)
                        else None
                    )
                except InvalidProviderResponseError:
                    prompt_tokens = None
                    completion_tokens = None
                sanitized = {
                    "truncation_finish_reason": "length",
                    "truncation_prompt_tokens": prompt_tokens,
                    "truncation_completion_tokens": completion_tokens,
                    "truncation_response_utf8_bytes": len(response.body),
                }
                raise InvalidProviderResponseError("provider_response_truncated", **sanitized, **failure_metadata)

            content_text = self._extract_message_content(envelope)
            content = json.loads(content_text)
            if not isinstance(content, dict) or set(content) != {"claims"}:
                raise InvalidProviderResponseError("provider_content_schema_mismatch")
            if not isinstance(content.get("claims"), list):
                raise InvalidProviderResponseError("provider_claims_not_list")

            usage = envelope.get("usage")
            input_tokens: int | None = None
            output_tokens: int | None = None
            if usage is not None:
                if not isinstance(usage, dict):
                    raise InvalidProviderResponseError("provider_usage_invalid")
                input_tokens = self._optional_non_negative_int(
                    usage.get("prompt_tokens"),
                    "provider_input_tokens_invalid",
                )
                output_tokens = self._optional_non_negative_int(
                    usage.get("completion_tokens"),
                    "provider_output_tokens_invalid",
                )

            provider_request_id = envelope.get("id")
            if provider_request_id is not None and not isinstance(provider_request_id, str):
                raise InvalidProviderResponseError("provider_request_id_invalid")
            if not provider_request_id:
                provider_request_id = self._header_value(response.headers, "x-request-id")
                provider_request_id = provider_request_id or self._header_value(response.headers, "request-id")

            if request.provider_wire_contract_version in {"compact-safe-v1", "compact-safe-v2"}:
                compact = CompactWireProviderResponse.model_validate(content)
                fragments = {item.fragment_id: item for item in request.evidence_packet.fragments}
                claims=[]; seen_claims=set()
                for claim in compact.claims:
                    if claim.claim_id in seen_claims: raise InvalidProviderResponseError("provider_wire_claim_schema_invalid")
                    seen_claims.add(claim.claim_id); refs=[]; seen_refs=set()
                    for ref in claim.evidence_references:
                        if ref.fragment_id in seen_refs: raise InvalidProviderResponseError("provider_wire_duplicate_reference")
                        seen_refs.add(ref.fragment_id); fragment=fragments.get(ref.fragment_id)
                        if fragment is None: raise InvalidProviderResponseError("provider_wire_fragment_not_found")
                        if not ref.quote: raise InvalidProviderResponseError("provider_wire_quote_empty")
                        if ref.quote not in fragment.text: raise InvalidProviderResponseError("provider_wire_quote_not_found")
                        refs.append(EvidenceReference(procurement_case_id=request.procurement_case_id,registry_number=request.registry_number,fragment_id=fragment.fragment_id,document_id=fragment.document_id,document_name=fragment.document_name,chunk_id=fragment.chunk_id,locator=fragment.locator,quote=ref.quote,quote_sha256=text_sha256(ref.quote)))
                    claims.append(ProviderClaim(claim_id=claim.claim_id,field_path=claim.field_path,value=claim.value,provider_confidence=claim.provider_confidence,evidence_references=refs))
            else:
                claims=content["claims"]
            return ProviderAnalysisResponse(
                provider_request_id=provider_request_id,
                claims=claims,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                attempt_latencies_ms=attempt_latencies_ms,
                total_latency_ms=failure_metadata["total_latency_ms"],
                retry_count=retry_count,
                raw_response_sha256=response_hash,
            )
        except InvalidProviderResponseError as exc:
            truncation_kwargs = {
                key: getattr(exc, key, None)
                for key in (
                    "truncation_finish_reason",
                    "truncation_prompt_tokens",
                    "truncation_completion_tokens",
                    "truncation_response_utf8_bytes",
                )
            }
            raise InvalidProviderResponseError(
                str(exc), **failure_metadata, **truncation_kwargs
            ) from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InvalidProviderResponseError("provider_response_invalid_json", **failure_metadata) from None
        except PydanticValidationError as exc:
            raise InvalidProviderResponseError(
                self._compact_schema_error_code(exc),
                **failure_metadata,
            ) from None

    @staticmethod
    def _compact_schema_error_code(exc: PydanticValidationError) -> str:
        """Classify compact-wire schema errors without exposing provider input."""

        errors = exc.errors()
        reference_errors = [
            item
            for item in errors
            if "evidence_references" in item.get("loc", ())
        ]
        if reference_errors:
            if any(
                item.get("loc", ())[-1:] == ("quote",)
                and item.get("type") == "string_too_short"
                for item in reference_errors
            ):
                return "provider_wire_quote_empty"
            return "provider_wire_reference_schema_invalid"
        return "provider_wire_claim_schema_invalid"

    @staticmethod
    def _extract_message_content(envelope: dict[str, Any]) -> str:
        try:
            content = envelope["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise InvalidProviderResponseError("provider_message_content_missing") from None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text") if item.get("type") == "text" else item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            if parts:
                return "\n".join(parts)
        raise InvalidProviderResponseError("provider_message_content_invalid")

    @staticmethod
    def _optional_non_negative_int(value: Any, error_code: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidProviderResponseError(error_code)
        return value

    @staticmethod
    def _header_value(headers: Mapping[str, str], name: str) -> str | None:
        target = name.lower()
        for key, value in headers.items():
            if str(key).lower() == target and isinstance(value, str) and value:
                return value
        return None

    def _estimate_attempt_cost(self, request: ProductionLLMAnalysisRequest, input_bytes: int) -> float:
        policy = request.budget_policy
        estimated_input_tokens = max(
            1,
            math.ceil(input_bytes / policy.limits.chars_per_token_estimate),
        )
        estimated_output_tokens = policy.limits.max_output_tokens
        return (
            (estimated_input_tokens / 1000) * policy.pricing.input_cost_per_1k_tokens
            + (estimated_output_tokens / 1000) * policy.pricing.output_cost_per_1k_tokens
        )

    def _failure_metadata(
        self,
        analysis_started: float,
        attempt_latencies_ms: list[int],
    ) -> dict[str, Any]:
        return {
            "retry_count": max(0, len(attempt_latencies_ms) - 1),
            "attempt_latencies_ms": tuple(attempt_latencies_ms),
            "total_latency_ms": self._total_latency_ms(analysis_started, attempt_latencies_ms),
        }

    def _record_transport_start(
        self,
        request: ProductionLLMAnalysisRequest,
        attempt_ordinal: int,
    ) -> None:
        """Persist a sanitized durable marker immediately before HTTP send.

        The marker proves the transport boundary was crossed even when the
        surrounding partial output stage is later removed. Only sanitized
        identifiers are stored; the prompt, tender text, credential, provider
        body, URL and private paths never enter the marker.
        """
        if self._transport_boundary is None:
            return
        request_identity_hash = canonical_sha256(
            {
                "request_id": request.request_id,
                "evidence_packet_hash": request.evidence_packet.packet_hash,
                "provider": request.provider,
                "model": request.model,
            }
        )
        write_transport_start_marker(
            self._transport_boundary,
            execution_ordinal=self._execution_ordinal,
            batch_ordinal=request.batch_ordinal,
            attempt_ordinal=attempt_ordinal,
            request_identity_hash=request_identity_hash,
        )

    def _total_latency_ms(self, analysis_started: float, attempt_latencies_ms: list[int]) -> int:
        return max(self._elapsed_ms(analysis_started), sum(attempt_latencies_ms))

    def _elapsed_ms(self, started: float) -> int:
        return max(0, round((self._clock() - started) * 1000))
