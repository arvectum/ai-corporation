from __future__ import annotations

from typing import Any, Protocol

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

_ZERO_HASH = "0" * 64


class ProductionLLMProvider(Protocol):
    def generate(self, request: ProductionLLMAnalysisRequest) -> ProviderAnalysisResponse | dict[str, Any]: ...


def build_production_llm_request(
    *,
    evidence_packet: EvidencePacket,
    provider: str,
    model: str,
    prompt_id: str,
    prompt_version: str,
    output_schema_id: str,
    output_schema_version: str,
    grounding_policy_version: str,
    budget_policy: Any,
) -> ProductionLLMAnalysisRequest:
    identity = {
        "customer_id": evidence_packet.customer_id,
        "project_id": evidence_packet.project_id,
        "procurement_case_id": evidence_packet.procurement_case_id,
        "run_id": evidence_packet.run_id,
        "registry_number": evidence_packet.registry_number,
        "evidence_packet_hash": evidence_packet.packet_hash,
        "provider": provider,
        "model": model,
        "prompt_id": prompt_id,
        "prompt_version": prompt_version,
        "output_schema_id": output_schema_id,
        "output_schema_version": output_schema_version,
        "grounding_policy_version": grounding_policy_version,
        "temperature": 0.0,
        "budget_policy": budget_policy.model_dump(mode="json") if hasattr(budget_policy, "model_dump") else budget_policy,
    }
    return ProductionLLMAnalysisRequest(
        request_id=canonical_sha256(identity),
        customer_id=evidence_packet.customer_id,
        project_id=evidence_packet.project_id,
        procurement_case_id=evidence_packet.procurement_case_id,
        run_id=evidence_packet.run_id,
        registry_number=evidence_packet.registry_number,
        provider=provider,
        model=model,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        output_schema_id=output_schema_id,
        output_schema_version=output_schema_version,
        grounding_policy_version=grounding_policy_version,
        evidence_packet=evidence_packet,
        budget_policy=budget_policy,
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
) -> ProductionLLMAnalysisResult:
    return _finish_result(
        status=status,
        canonical_input_eligible=False,
        request_id=request.request_id,
        provider=request.provider,
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
        sanitized_error_code=error_code,
    )


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
    except ProviderBudgetExceededError:
        return _failure_result(
            request,
            status=AnalysisStatus.BUDGET_EXCEEDED,
            budget=preflight.model_copy(
                update={
                    "status": BudgetStatus.EXCEEDED,
                    "reasons": [*preflight.reasons, "provider_runtime_budget_exceeded"],
                }
            ),
            error_code="provider_runtime_budget_exceeded",
            limitation="Provider execution stopped before another attempt could exceed the configured budget.",
        )
    except InvalidProviderResponseError:
        return _failure_result(
            request,
            status=AnalysisStatus.INVALID_RESPONSE,
            budget=preflight,
            error_code="provider_response_invalid",
            limitation="Provider response did not satisfy the versioned output schema.",
        )
    except ProviderTimeoutError:
        return _failure_result(
            request,
            status=AnalysisStatus.TIMEOUT,
            budget=preflight,
            error_code="provider_timeout",
            limitation="Provider timed out; no generated claim was accepted.",
        )
    except ProviderPermanentError:
        return _failure_result(
            request,
            status=AnalysisStatus.PROVIDER_UNAVAILABLE,
            budget=preflight,
            error_code="provider_request_rejected",
            limitation="Provider rejected the request; no generated claim was accepted.",
        )
    except ProviderTransientError:
        return _failure_result(
            request,
            status=AnalysisStatus.PROVIDER_UNAVAILABLE,
            budget=preflight,
            error_code="provider_transient_failure",
            limitation="Provider remained unavailable after bounded retries; no generated claim was accepted.",
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
    except Exception:
        return _failure_result(
            request,
            status=AnalysisStatus.PROVIDER_UNAVAILABLE,
            budget=preflight,
            error_code="provider_call_failed",
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

    runtime_budget = reconcile_runtime_budget(request, response, preflight)
    grounded = validate_provider_claims(request.evidence_packet, response.claims)
    accepted = [claim for claim in grounded if claim.support_status == SupportStatus.SUPPORTED]
    rejected = [claim for claim in grounded if claim.support_status != SupportStatus.SUPPORTED]

    limitations: list[str] = []
    status = AnalysisStatus.SUCCESS
    if runtime_budget.status == BudgetStatus.EXCEEDED:
        status = AnalysisStatus.BUDGET_EXCEEDED
        limitations.append("Runtime token, latency, retry or cost budget was exceeded.")
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
    )
