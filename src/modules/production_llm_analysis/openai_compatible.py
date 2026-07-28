from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from pydantic import ValidationError as PydanticValidationError

from src.modules.production_llm_analysis.evidence import canonical_json_bytes
from src.modules.production_llm_analysis.schemas import (
    ProductionLLMAnalysisRequest,
    ProviderAnalysisResponse,
    ProviderClaim,
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
    ) -> None:
        self._config = config
        self._http_client = http_client or UrllibHTTPClient()
        self._clock = clock

    def generate(self, request: ProductionLLMAnalysisRequest) -> ProviderAnalysisResponse:
        body = canonical_json_bytes(self._build_request_body(request))
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
                response=response,
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
        claim_schema = ProviderClaim.model_json_schema()
        if request.allowed_field_paths:
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
                "evidence_budget": request.budget_policy.limits.max_input_tokens,
                "output_reserve": request.budget_policy.limits.max_output_tokens,
                "absence_is_not_corpus_negative": True,
            },
        }
        return {
            "model": request.model,
            "temperature": 0,
            "max_tokens": request.budget_policy.limits.max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a controlled internal procurement analysis component. "
                        "Return exactly one valid JSON object matching the supplied output contract. "
                        "Use only supplied evidence fragments. Every factual claim must copy exact evidence "
                        "identities, locator and quote. Return an empty claims array when evidence is insufficient. "
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

            return ProviderAnalysisResponse(
                provider_request_id=provider_request_id,
                claims=content["claims"],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                attempt_latencies_ms=attempt_latencies_ms,
                total_latency_ms=failure_metadata["total_latency_ms"],
                retry_count=retry_count,
                raw_response_sha256=response_hash,
            )
        except InvalidProviderResponseError as exc:
            raise InvalidProviderResponseError(str(exc), **failure_metadata) from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InvalidProviderResponseError("provider_response_invalid_json", **failure_metadata) from None
        except PydanticValidationError:
            raise InvalidProviderResponseError("provider_claim_schema_invalid", **failure_metadata) from None

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

    def _total_latency_ms(self, analysis_started: float, attempt_latencies_ms: list[int]) -> int:
        return max(self._elapsed_ms(analysis_started), sum(attempt_latencies_ms))

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int(round((self._clock() - started) * 1000)))
