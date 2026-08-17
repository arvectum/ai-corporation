from __future__ import annotations

import sys
from typing import Any, Protocol

_ZERO_HASH = "0" * 64

from pydantic import ValidationError as PydanticValidationError

from src.modules.production_llm_analysis.budgets import (
    evaluate_preflight_budget,
    reconcile_runtime_budget,
)
from src.modules.production_llm_analysis.evidence import canonical_sha256
from src.modules.production_llm_analysis.grounding import validate_provider_claims
from src.modules.production_llm_analysis.schemas import (
    AnalysisStatus,
    BudgetEvaluation,
    BudgetStatus,
    EvidencePacket,
    ProductionLLMAnalysisRequest,
    ProductionLLMAnalysisResult,
    ProviderAnalysisResponse,
    SupportStatus,
)
from src.shared.llm.transport import (
    InvalidProviderResponseError,
    ProviderBudgetExceededError,
    ProviderPermanentError,
    ProviderTimeoutError,
    ProviderTransientError,
)

_SAFE_PRETRANSPORT_FAILURE_CODES = frozenset({
    "final_body_schema_missing",
    "final_body_task_invalid",
    "final_body_output_contract_missing",
    "final_body_schema_identity_mismatch",
    "final_body_schema_not_inline",
    "final_body_live_schema_mismatch",
    "final_body_max_tokens_mismatch",
    "final_body_max_claims_mismatch",
    "final_body_reference_limit_mismatch",
    "final_body_enable_thinking_not_false",
    "final_body_reasoning_format_not_none",
    "llama_schema_reference_not_local",
    "llama_schema_reference_unresolved",
    "llama_schema_reference_invalid",
    "llama_schema_reference_siblings_unsupported",
    "llama_schema_reference_cycle",
    "llama_schema_extractive_field_paths_missing",
    "llama_schema_contract_invalid",
    "llama_schema_fragment_ids_missing",
})


def _get_sanitized_pretransport_code(exc: BaseException) -> str:
    value = str(exc).strip().lower()
    if value in _SAFE_PRETRANSPORT_FAILURE_CODES:
        return value
    return "provider_call_failed"


class ProductionLLMProvider(Protocol):
    def generate(self, request: ProductionLLMAnalysisRequest) -> ProviderAnalysisResponse | dict[str, Any]: ...


def build_production_llm_request(
    *,
    evidence_packet: EvidencePacket,
    provider: str,
    provider_wire_contract_version: str = "full-v1",
    model: str,
    prompt_id: str,
    prompt_version: str,
    output_schema_id: str,
    output_schema_version: str,
    grounding_policy_version: str,
    budget_policy: Any,
    batch_plan_version: str | None = None,
    batch_plan_hash: str | None = None,
    batch_hash: str | None = None,
    batch_ordinal: int | None = None,
    batch_count: int | None = None,
    corpus_evidence_hash: str | None = None,
    map_mode: bool = False,
    max_claims: int | None = None,
    allowed_field_paths: list[str] | None = None,
    context_profile: str | None = None,
    tokenizer_identity: str | None = None,
    evidence_budget: int | None = None,
    chat_template_overhead: int | None = None,
    execution_deadline_ms: int | None = None,
) -> ProductionLLMAnalysisRequest:
    identity = {
        "customer_id": evidence_packet.customer_id,
        "project_id": evidence_packet.project_id,
        "procurement_case_id": evidence_packet.procurement_case_id,
        "run_id": evidence_packet.run_id,
        "registry_number": evidence_packet.registry_number,
        "evidence_packet_hash": evidence_packet.packet_hash,
        "provider": provider,
        "provider_wire_contract_version": provider_wire_contract_version,
        "model": model,
        "prompt_id": prompt_id,
        "prompt_version": prompt_version,
        "output_schema_id": output_schema_id,
        "output_schema_version": output_schema_version,
        "grounding_policy_version": grounding_policy_version,
        "temperature": 0.0,
        "budget_policy": budget_policy.model_dump(mode="json") if hasattr(budget_policy, "model_dump") else budget_policy,
        "batch_plan_version": batch_plan_version,
        "batch_plan_hash": batch_plan_hash,
        "batch_hash": batch_hash,
        "batch_ordinal": batch_ordinal,
        "batch_count": batch_count,
        "corpus_evidence_hash": corpus_evidence_hash,
        "map_mode": map_mode,
        "max_claims": max_claims,
        "allowed_field_paths": allowed_field_paths or [],
        "context_profile": context_profile,
        "tokenizer_identity": tokenizer_identity,
        "evidence_budget": evidence_budget,
        "chat_template_overhead": chat_template_overhead,
        "execution_deadline_ms": execution_deadline_ms,
    }
    return ProductionLLMAnalysisRequest(
        request_id=canonical_sha256(identity),
        customer_id=evidence_packet.customer_id,
        project_id=evidence_packet.project_id,
        procurement_case_id=evidence_packet.procurement_case_id,
        run_id=evidence_packet.run_id,
        registry_number=evidence_packet.registry_number,
        provider=provider,
        provider_wire_contract_version=provider_wire_contract_version,
        model=model,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        output_schema_id=output_schema_id,
        output_schema_version=output_schema_version,
        grounding_policy_version=grounding_policy_version,
        evidence_packet=evidence_packet,
        budget_policy=budget_policy,
        batch_plan_version=batch_plan_version,
        batch_plan_hash=batch_plan_hash,
        batch_hash=batch_hash,
        batch_ordinal=batch_ordinal,
        batch_count=batch_count,
        corpus_evidence_hash=corpus_evidence_hash,
        map_mode=map_mode,
        max_claims=max_claims,
        allowed_field_paths=allowed_field_paths or [],
        context_profile=context_profile,
        tokenizer_identity=tokenizer_identity,
        evidence_budget=evidence_budget,
        chat_template_overhead=chat_template_overhead,
        execution_deadline_ms=execution_deadline_ms,
    )


def _finish_result(**values: Any) -> ProductionLLMAnalysisResult:
    result = ProductionLLMAnalysisResult(validated_result_hash=_ZERO_HASH, **values)
    unsigned = result.model_dump(mode="json", exclude={"validated_result_hash"})
    return result.model_copy(update={"validated_result_hash": canonical_sha256(unsigned)})


def _failure_result(
    request: ProductionLLMAnalysisRequest,
    *,
    status: AnalysisStatus,
    budget: BudgetEvaluation,
    error_code: str,
    limitation: str,
    retry_count: int = 0,
    raw_response_sha256: str | None = None,
) -> ProductionLLMAnalysisResult:
    return _finish_result(
        status=status,
        canonical_input_eligible=False,
        request_id=request.request_id,
        provider=request.provider,
        provider_wire_contract_version=request.provider_wire_contract_version,
        model=request.model,
        prompt_id=request.prompt_id,
        prompt_version=request.prompt_version,
        output_schema_id=request.output_schema_id,
        output_schema_version=request.output_schema_version,
        grounding_policy_version=request.grounding_policy_version,
        evidence_packet_hash=request.evidence_packet.packet_hash,
        accepted_claims=[],
        rejected_claims=[],
        limitations=[limitation],
        budget=budget,
        retry_count=retry_count,
        sanitized_error_code=error_code,
        raw_response_sha256=raw_response_sha256,
        batch_plan_version=request.batch_plan_version,
        batch_plan_hash=request.batch_plan_hash,
        batch_hash=request.batch_hash,
        batch_ordinal=request.batch_ordinal,
        batch_count=request.batch_count,
        corpus_evidence_hash=request.corpus_evidence_hash,
        map_empty=False,
        tokenizer_identity=request.tokenizer_identity,
        evidence_budget=request.evidence_budget,
        chat_template_overhead=request.chat_template_overhead,
        execution_deadline_ms=request.execution_deadline_ms,
        context_profile=request.context_profile,
    )


def _transport_failure_values(
    error: BaseException,
    budget: BudgetEvaluation,
) -> tuple[BudgetEvaluation, int, str | None]:
    total_latency_ms = getattr(error, "total_latency_ms", None)
    if isinstance(total_latency_ms, int) and total_latency_ms >= 0:
        budget = budget.model_copy(update={"total_latency_ms": total_latency_ms})
    retry_count = getattr(error, "retry_count", 0)
    if not isinstance(retry_count, int) or retry_count < 0:
        retry_count = 0
    raw_response_sha256 = getattr(error, "raw_response_sha256", None)
    if not isinstance(raw_response_sha256, str):
        raw_response_sha256 = None
    return budget, retry_count, raw_response_sha256


def run_production_llm_analysis(
    request: ProductionLLMAnalysisRequest,
    provider: ProductionLLMProvider,
) -> ProductionLLMAnalysisResult:
    preflight = evaluate_preflight_budget(request)
    if preflight.status == BudgetStatus.EXCEEDED:
        return _failure_result(
            request,
            status=AnalysisStatus.BUDGET_EXCEEDED,
            budget=preflight,
            error_code="budget_preflight_exceeded",
            limitation="Provider was not invoked because the configured request budget was exceeded.",
        )

    try:
        raw_response = provider.generate(request)
    except ProviderBudgetExceededError as exc:
        failure_budget, retry_count, response_hash = _transport_failure_values(exc, preflight)
        return _failure_result(
            request,
            status=AnalysisStatus.BUDGET_EXCEEDED,
            budget=failure_budget.model_copy(
                update={
                    "status": BudgetStatus.EXCEEDED,
                    "reasons": [*failure_budget.reasons, "provider_runtime_budget_exceeded"],
                }
            ),
            error_code="provider_runtime_budget_exceeded",
            limitation="Provider execution stopped before another attempt could exceed the configured budget.",
            retry_count=retry_count,
            raw_response_sha256=response_hash,
        )
    except InvalidProviderResponseError as exc:
        failure_budget, retry_count, response_hash = _transport_failure_values(exc, preflight)
        code = str(exc).strip()
        if code == "provider_response_truncated":
            return _failure_result(
                request,
                status=AnalysisStatus.INVALID_RESPONSE,
                budget=failure_budget,
                error_code="provider_response_truncated",
                limitation="Provider response was truncated at the output limit; no partial claim was accepted.",
                retry_count=retry_count,
                raw_response_sha256=response_hash,
            )
        return _failure_result(
            request,
            status=AnalysisStatus.INVALID_RESPONSE,
            budget=failure_budget,
            error_code="provider_response_invalid",
            limitation="Provider response did not satisfy the versioned output schema.",
            retry_count=retry_count,
            raw_response_sha256=response_hash,
        )
    except ProviderTimeoutError as exc:
        failure_budget, retry_count, response_hash = _transport_failure_values(exc, preflight)
        return _failure_result(
            request,
            status=AnalysisStatus.TIMEOUT,
            budget=failure_budget,
            error_code="provider_timeout",
            limitation="Provider timed out; no generated claim was accepted.",
            retry_count=retry_count,
            raw_response_sha256=response_hash,
        )
    except ProviderPermanentError as exc:
        failure_budget, retry_count, response_hash = _transport_failure_values(exc, preflight)
        return _failure_result(
            request,
            status=AnalysisStatus.PROVIDER_UNAVAILABLE,
            budget=failure_budget,
            error_code="provider_request_rejected",
            limitation="Provider rejected the request; no generated claim was accepted.",
            retry_count=retry_count,
            raw_response_sha256=response_hash,
        )
    except ProviderTransientError as exc:
        failure_budget, retry_count, response_hash = _transport_failure_values(exc, preflight)
        return _failure_result(
            request,
            status=AnalysisStatus.PROVIDER_UNAVAILABLE,
            budget=failure_budget,
            error_code="provider_transient_failure",
            limitation="Provider remained unavailable after bounded retries; no generated claim was accepted.",
            retry_count=retry_count,
            raw_response_sha256=response_hash,
        )
    except TimeoutError:
        return _failure_result(
            request,
            status=AnalysisStatus.TIMEOUT,
            budget=preflight,
            error_code="provider_timeout",
            limitation="Provider timed out; no generated claim was accepted.",
        )
    except (ConnectionError, OSError):
        return _failure_result(
            request,
            status=AnalysisStatus.PROVIDER_UNAVAILABLE,
            budget=preflight,
            error_code="provider_unavailable",
            limitation="Provider was unavailable; no stub or positive fallback was used.",
        )
    except Exception:  # noqa: BLE001 - transport boundary must sanitize unknown failures.
        return _failure_result(
            request,
            status=AnalysisStatus.PROVIDER_UNAVAILABLE,
            budget=preflight,
            error_code=_get_sanitized_pretransport_code(sys.exc_info()[1]),
            limitation="Provider call failed with a sanitized error; no generated claim was accepted.",
        )

    try:
        response = (
            raw_response
            if isinstance(raw_response, ProviderAnalysisResponse)
            else ProviderAnalysisResponse.model_validate(raw_response)
        )
    except (PydanticValidationError, TypeError, ValueError):
        return _failure_result(
            request,
            status=AnalysisStatus.INVALID_RESPONSE,
            budget=preflight,
            error_code="provider_response_invalid",
            limitation="Provider response did not satisfy the versioned output schema.",
        )

    if request.max_claims is not None and len(response.claims) > request.max_claims:
        return _failure_result(
            request,
            status=AnalysisStatus.INVALID_RESPONSE,
            budget=preflight,
            error_code="evidence_batch_output_budget_exceeded",
            limitation="Provider returned more claims than the approved batch contract allows.",
        )
    if request.allowed_field_paths and any(
        claim.field_path not in request.allowed_field_paths for claim in response.claims
    ):
        return _failure_result(
            request,
            status=AnalysisStatus.VALIDATION_FAILED,
            budget=preflight,
            error_code="evidence_batch_grounding_failed",
            limitation="Provider returned a field path outside the approved map contract.",
        )

    runtime_budget = reconcile_runtime_budget(request, response, preflight)
    grounded = validate_provider_claims(request.evidence_packet, response.claims)
    accepted = [claim for claim in grounded if claim.support_status == SupportStatus.SUPPORTED]
    rejected = [claim for claim in grounded if claim.support_status != SupportStatus.SUPPORTED]

    limitations: list[str] = []
    status = AnalysisStatus.SUCCESS
    if runtime_budget.status == BudgetStatus.EXCEEDED:
        status = AnalysisStatus.BUDGET_EXCEEDED
        limitations.append("Runtime token, latency, retry or cost budget was exceeded.")
    elif not grounded and request.map_mode:
        status = AnalysisStatus.SUCCESS
        limitations.append("map_batch_empty; absence in this batch is not a corpus-wide negative conclusion.")
    elif not grounded:
        status = AnalysisStatus.INSUFFICIENT_EVIDENCE
        limitations.append("Provider returned no claims.")
    elif rejected:
        if all(claim.support_status == SupportStatus.INSUFFICIENT_EVIDENCE for claim in rejected) and not accepted:
            status = AnalysisStatus.INSUFFICIENT_EVIDENCE
        else:
            status = AnalysisStatus.VALIDATION_FAILED
        limitations.append("One or more provider claims failed deterministic grounding validation.")

    canonical_input_eligible = status == AnalysisStatus.SUCCESS and bool(accepted) and not rejected
    error_code = None
    if status == AnalysisStatus.BUDGET_EXCEEDED:
        error_code = "runtime_budget_exceeded"
    elif status == AnalysisStatus.INSUFFICIENT_EVIDENCE:
        error_code = "insufficient_evidence"
    elif status == AnalysisStatus.VALIDATION_FAILED:
        error_code = "grounding_validation_failed"

    return _finish_result(
        status=status,
        canonical_input_eligible=canonical_input_eligible,
        request_id=request.request_id,
        provider=request.provider,
        provider_wire_contract_version=request.provider_wire_contract_version,
        model=request.model,
        provider_request_id=response.provider_request_id,
        prompt_id=request.prompt_id,
        prompt_version=request.prompt_version,
        output_schema_id=request.output_schema_id,
        output_schema_version=request.output_schema_version,
        grounding_policy_version=request.grounding_policy_version,
        evidence_packet_hash=request.evidence_packet.packet_hash,
        accepted_claims=accepted,
        rejected_claims=rejected,
        limitations=limitations,
        budget=runtime_budget,
        retry_count=response.retry_count,
        sanitized_error_code=error_code,
        raw_response_sha256=response.raw_response_sha256,
        batch_plan_version=request.batch_plan_version,
        batch_plan_hash=request.batch_plan_hash,
        batch_hash=request.batch_hash,
        batch_ordinal=request.batch_ordinal,
        batch_count=request.batch_count,
        corpus_evidence_hash=request.corpus_evidence_hash,
        map_empty=request.map_mode and not grounded,
        tokenizer_identity=request.tokenizer_identity,
        evidence_budget=request.evidence_budget,
        chat_template_overhead=request.chat_template_overhead,
        execution_deadline_ms=request.execution_deadline_ms,
        context_profile=request.context_profile,
    )
