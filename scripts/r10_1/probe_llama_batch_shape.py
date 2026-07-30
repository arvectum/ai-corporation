#!/usr/bin/env python3
"""One-call token-shaped compatibility probe for the local llama.cpp Gate 5 path.

The probe is database-free and customer-data-free. It uses the approved exact
persistent tokenizer to shape deterministic synthetic evidence to the same 32K
request boundary as the controlled batch planner, disables template-level
thinking, and makes at most one provider call.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from src.modules.production_llm_analysis.batching import (
    BatchPolicy,
    ExactRequestMeasurement,
    measure_openai_request_tokens,
    tokenizer_from_environment,
)
from src.modules.production_llm_analysis.contracts import (
    R10_1_CONTROLLED_MAP_CONTRACT,
)
from src.modules.production_llm_analysis.controlled_evidence import (
    load_approved_provider_policy,
)
from src.modules.production_llm_analysis.evidence import build_evidence_packet
from src.modules.production_llm_analysis.llama_reasoning_control import (
    install_llama_non_reasoning_mode,
)
from src.modules.production_llm_analysis.llama_schema_constraint import (
    install_llama_schema_constraint,
)
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
    OpenAICompatibleTransportConfig,
)
from src.modules.production_llm_analysis.schemas import EvidenceFragmentInput
from src.modules.production_llm_analysis.service import (
    build_production_llm_request,
)
from src.shared.config.settings import get_settings
from src.shared.llm.transport import (
    HTTPRequest,
    HTTPResponse,
    InvalidProviderResponseError,
    ProviderBudgetExceededError,
    ProviderPermanentError,
    ProviderTimeoutError,
    ProviderTransientError,
    UrllibHTTPClient,
)

_FRAGMENT_COUNT = 96
_MIN_BATCH_SHAPE_FRAGMENTS = 8
_ALLOWED_FIELD_PATHS = [
    "requirements.document_requirements",
    "requirements.evaluation_criteria",
    "requirements.qualification_requirements",
    "requirements.technical_requirements",
]
_SAFE_CODE_PATTERN = re.compile(r"^[a-z0-9_.-]{1,80}$")
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


class _RecordingHTTPClient:
    """Record only sanitized response metadata for the single probe call."""

    def __init__(self) -> None:
        self._delegate = UrllibHTTPClient()
        self.last_status_code: int | None = None
        self.last_error_type: str | None = None

    def send(self, request: HTTPRequest) -> HTTPResponse:
        response = self._delegate.send(request)
        self.last_status_code = response.status_code
        if not 200 <= response.status_code < 300:
            self.last_error_type = _sanitized_server_error_type(response.body)
        return response


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--approved-policy", required=True, type=Path)
    return parser.parse_args()


def _single_attempt_budget(policy):
    limits = policy.budget.limits.model_copy(update={"max_retries": 0})
    return policy.budget.model_copy(update={"limits": limits})


def _provider_boundary(policy):
    settings = get_settings()
    configured_provider = (settings.llm_provider or "").strip().lower()
    if configured_provider != policy.provider:
        raise RuntimeError("configured_provider_not_approved")
    if settings.llm_model != policy.model:
        raise RuntimeError("configured_model_not_approved")
    if policy.provider not in {"openai", "openai_compatible"}:
        raise RuntimeError("provider_not_supported_by_probe")
    if not settings.openai_api_key:
        raise RuntimeError("provider_credential_missing")
    return settings.openai_base_url, settings.openai_api_key


def _synthetic_text(index: int) -> str:
    prefix = f"Synthetic technical requirement block {index:03d}. "
    sentence = (
        "The supplied cable shall use copper conductors, rated insulation, "
        "factory quality control, traceable marking, and documented acceptance. "
    )
    return prefix + (sentence * 9) + f"Unique marker ARV-BATCH-{index:03d}."


def _build_batch_shaped_request(
    policy,
    *,
    fragment_count: int = _FRAGMENT_COUNT,
    tokenizer_identity: str = "synthetic-batch-probe",
):
    if not 1 <= fragment_count <= _FRAGMENT_COUNT:
        raise ValueError("batch_probe_fragment_count_invalid")
    fragments = [
        EvidenceFragmentInput(
            document_id=f"synthetic-document-{index // 24:02d}",
            document_name=f"synthetic-{index // 24:02d}.txt",
            chunk_id=f"synthetic-chunk-{index:03d}",
            locator={"document_order": index // 24, "chunk_index": index},
            text=_synthetic_text(index),
        )
        for index in range(fragment_count)
    ]
    packet = build_evidence_packet(
        customer_id="synthetic-customer",
        project_id="synthetic-project",
        procurement_case_id="synthetic-case",
        run_id="synthetic-run",
        registry_number="synthetic-registry",
        fragments=fragments,
    )
    contract = R10_1_CONTROLLED_MAP_CONTRACT
    return build_production_llm_request(
        evidence_packet=packet,
        provider=policy.provider,
        provider_wire_contract_version=contract.provider_wire_contract_version,
        model=policy.model,
        prompt_id=contract.prompt_id,
        prompt_version=contract.prompt_version,
        output_schema_id=contract.output_schema_id,
        output_schema_version=contract.output_schema_version,
        grounding_policy_version=contract.grounding_policy_version,
        budget_policy=_single_attempt_budget(policy),
        batch_plan_version=contract.plan_version,
        batch_plan_hash="1" * 64,
        batch_hash="2" * 64,
        batch_ordinal=1,
        batch_count=17,
        corpus_evidence_hash="3" * 64,
        map_mode=True,
        max_claims=3,
        allowed_field_paths=_ALLOWED_FIELD_PATHS,
        context_profile="32k",
        tokenizer_identity=tokenizer_identity,
        evidence_budget=24488,
        chat_template_overhead=32,
        execution_deadline_ms=600000,
    )


def _measurement_fits(
    measurement: ExactRequestMeasurement,
    batch_policy: BatchPolicy,
) -> bool:
    return (
        measurement.serialized_evidence_tokens <= batch_policy.evidence_budget
        and measurement.full_request_tokens
        + batch_policy.output_reserve
        + batch_policy.safety_margin
        <= batch_policy.context_window
    )


def _shape_request(policy, provider, tokenizer):
    if not bool(getattr(tokenizer, "persistent", False)):
        raise RuntimeError("batch_probe_exact_persistent_tokenizer_required")
    tokenizer_identity = str(getattr(tokenizer, "identity", "")).strip()
    if not tokenizer_identity:
        raise RuntimeError("batch_probe_tokenizer_identity_missing")

    batch_policy = BatchPolicy.approved_32k(tokenizer_identity=tokenizer_identity)
    batch_policy.validate(policy.budget, controlled=True)
    best: tuple[Any, ExactRequestMeasurement, int] | None = None
    lower, upper = 1, _FRAGMENT_COUNT
    while lower <= upper:
        fragment_count = (lower + upper) // 2
        request = _build_batch_shaped_request(
            policy,
            fragment_count=fragment_count,
            tokenizer_identity=tokenizer_identity,
        )
        body = provider._build_request_body(request)
        measurement = measure_openai_request_tokens(
            body,
            tokenizer=tokenizer,
            chat_template_overhead=batch_policy.chat_template_overhead,
        )
        if _measurement_fits(measurement, batch_policy):
            best = request, measurement, fragment_count
            lower = fragment_count + 1
        else:
            upper = fragment_count - 1

    if best is None:
        raise RuntimeError("batch_probe_no_fitting_request")
    if best[2] < _MIN_BATCH_SHAPE_FRAGMENTS:
        raise RuntimeError("batch_probe_shape_too_small")
    return (*best, batch_policy)


def _sanitized_invalid_response_code(exc: InvalidProviderResponseError) -> str:
    candidate = str(exc).strip().lower()
    if candidate in _SAFE_INVALID_RESPONSE_CODES:
        return candidate
    return "provider_response_invalid"


def _sanitized_server_error_type(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode("utf-8"))
        error = payload.get("error") if isinstance(payload, dict) else None
        candidate = error.get("type") if isinstance(error, dict) else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(candidate, str):
        return None
    candidate = candidate.strip().lower()
    return candidate if _SAFE_CODE_PATTERN.fullmatch(candidate) else None


def _sanitized_http_code(status_code: int | None) -> str:
    if isinstance(status_code, int) and 400 <= status_code <= 599:
        return f"provider_http_{status_code}"
    return "provider_request_rejected"


def _validate_response(request, response) -> tuple[int, int]:
    if not response.claims:
        raise RuntimeError("batch_probe_empty_claims")
    if len(response.claims) > 3:
        raise RuntimeError("batch_probe_claim_count_invalid")

    fragments = {
        fragment.fragment_id: fragment
        for fragment in request.evidence_packet.fragments
    }
    reference_count = 0
    for claim in response.claims:
        if claim.field_path not in _ALLOWED_FIELD_PATHS:
            raise RuntimeError("batch_probe_field_path_invalid")
        if len(claim.evidence_references) != 1:
            raise RuntimeError("batch_probe_reference_count_invalid")
        reference = claim.evidence_references[0]
        fragment = fragments.get(reference.fragment_id)
        if fragment is None:
            raise RuntimeError("batch_probe_fragment_invalid")
        if claim.value != fragment.text or reference.quote != fragment.text:
            raise RuntimeError("batch_probe_server_grounding_invalid")
        reference_count += 1
    return len(response.claims), reference_count


def main() -> int:
    args = _arguments()
    recorder = _RecordingHTTPClient()
    provider_call_count = 0
    shaped: tuple[Any, ExactRequestMeasurement, int, BatchPolicy] | None = None
    tokenizer_start = 0
    try:
        policy = load_approved_provider_policy(args.approved_policy)
        base_url, api_key = _provider_boundary(policy)
        install_llama_schema_constraint()
        install_llama_non_reasoning_mode()
        tokenizer = tokenizer_from_environment()
        tokenizer_start = int(getattr(tokenizer, "invocations", 0))
        provider = OpenAICompatibleProductionLLMProvider(
            OpenAICompatibleTransportConfig(base_url=base_url, api_key=api_key),
            http_client=recorder,
        )
        shaped = _shape_request(policy, provider, tokenizer)
        request, measurement, fragment_count, batch_policy = shaped
        request_body = provider._build_request_body(request)
        thinking_enabled = request_body.get("chat_template_kwargs", {}).get(
            "enable_thinking"
        )
        if thinking_enabled is not False:
            raise RuntimeError("batch_probe_thinking_not_disabled")

        provider_call_count = 1
        response = provider.generate(request)
        claim_count, reference_count = _validate_response(request, response)
        tokenizer_calls = int(getattr(tokenizer, "invocations", 0)) - tokenizer_start
        context_headroom = (
            batch_policy.context_window
            - batch_policy.output_reserve
            - batch_policy.safety_margin
            - measurement.full_request_tokens
        )
        print(
            json.dumps(
                {
                    "status": "batch_shaped_compact_contract_passed",
                    "provider_call_count": provider_call_count,
                    "claim_count": claim_count,
                    "reference_count": reference_count,
                    "retry_count": response.retry_count,
                    "server_owned_grounding": True,
                    "reasoning_enabled": False,
                    "synthetic_fragment_count": fragment_count,
                    "exact_request_tokens": measurement.full_request_tokens,
                    "serialized_evidence_tokens": measurement.serialized_evidence_tokens,
                    "context_headroom_tokens": context_headroom,
                    "tokenizer_call_count": tokenizer_calls,
                    "model": policy.model,
                },
                sort_keys=True,
            )
        )
        return 0
    except InvalidProviderResponseError as exc:
        print(
            json.dumps(
                {
                    "status": "invalid_provider_response",
                    "code": _sanitized_invalid_response_code(exc),
                    "provider_call_count": provider_call_count,
                    "reasoning_enabled": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except ProviderPermanentError:
        payload = {
            "status": "provider_request_rejected",
            "code": _sanitized_http_code(recorder.last_status_code),
            "provider_call_count": provider_call_count,
            "reasoning_enabled": False,
        }
        if recorder.last_error_type is not None:
            payload["server_error_type"] = recorder.last_error_type
        if shaped is not None:
            _, measurement, fragment_count, batch_policy = shaped
            payload.update(
                {
                    "synthetic_fragment_count": fragment_count,
                    "exact_request_tokens": measurement.full_request_tokens,
                    "context_headroom_tokens": (
                        batch_policy.context_window
                        - batch_policy.output_reserve
                        - batch_policy.safety_margin
                        - measurement.full_request_tokens
                    ),
                }
            )
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 3
    except ProviderTimeoutError:
        print("provider_timeout", file=sys.stderr)
        return 3
    except ProviderTransientError:
        print("provider_transient_failure", file=sys.stderr)
        return 3
    except ProviderBudgetExceededError:
        print("provider_budget_exceeded", file=sys.stderr)
        return 3
    except Exception as exc:
        code = str(exc).strip().lower()
        if not code.replace("_", "").isalnum() or len(code) > 120:
            code = "batch_probe_failed"
        print(code or "batch_probe_failed", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
