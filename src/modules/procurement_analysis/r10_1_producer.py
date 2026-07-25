"""Versioned R10.1 canonical producer beside the frozen R9 path.

This module is deliberately provider-injected.  Gate 4 proves the canonical
boundary with fake providers or mocked HTTP only; it never resolves credentials
or silently falls back to the frozen producer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from src.modules.procurement_analysis.canonical_persistence import (
    PersistedCanonicalOutputs,
    persist_canonical_outputs,
    verify_persisted_canonical_outputs,
)
from src.modules.procurement_analysis.frozen_producer import (
    FrozenCanonicalProduction,
    produce_frozen_canonical_analysis,
)
from src.modules.production_llm_analysis.evidence import build_evidence_packet
from src.modules.production_llm_analysis.schemas import (
    AnalysisStatus,
    BudgetPolicy,
    EvidenceFragmentInput,
    GroundedClaim,
    ProductionLLMAnalysisResult,
    SupportStatus,
)
from src.modules.production_llm_analysis.service import (
    ProductionLLMProvider,
    build_production_llm_request,
    run_production_llm_analysis,
)


class CanonicalAnalysisMode(StrEnum):
    FROZEN_R9 = "frozen_r9"
    PRODUCTION_LLM_R10_1 = "production_llm_r10_1"


class R10_1CanonicalProductionError(RuntimeError):
    """Base fail-closed error for the versioned R10.1 producer."""


class R10_1IdentityError(R10_1CanonicalProductionError):
    """Server-owned run, tenant or procurement identities do not align."""


class R10_1AnalysisRejectedError(R10_1CanonicalProductionError):
    """The provider result is not eligible for canonical publication."""


class R10_1ClaimMappingError(R10_1CanonicalProductionError):
    """A grounded claim cannot be mapped through the explicit allow-list."""


@dataclass(frozen=True)
class R10_1CanonicalProduction:
    registry_number: str
    source_analysis_run_id: str
    persisted: PersistedCanonicalOutputs
    requirements: dict[str, Any]
    canonical_model: dict[str, Any]
    source_graph: dict[str, Any]
    source_graph_hash: str
    production_model_hash: str
    report_model_hash: str
    llm_result: ProductionLLMAnalysisResult


_REQUIREMENT_PATHS = {
    "requirements.technical_requirements": "technical_requirements",
    "requirements.document_requirements": "document_requirements",
    "requirements.qualification_requirements": "qualification_requirements",
    "requirements.evaluation_criteria": "evaluation_criteria",
}
_ALLOWED_RISK_CLASSIFICATIONS = {
    "market_standard_harsh_term",
    "commercially_material_risk",
    "deal_breaker_candidate",
}
_POSITIVE_RECOMMENDATIONS = {
    "GO",
    "GO_WITH_CONDITIONS",
    "PARTICIPATE",
    "PARTICIPATE_CONDITIONALLY",
    "READY",
    "APPROVED",
}


def _owned_identity(
    *,
    metadata: dict[str, Any],
    customer_id: str,
    project_id: str,
    procurement_case_id: str,
    run_id: str,
    registry_number: str,
) -> None:
    expected = {
        "customer_id": customer_id,
        "project_id": project_id,
        "run_id": run_id,
        "procurement_id": registry_number,
    }
    for key, value in expected.items():
        recorded = metadata.get(key)
        if recorded is not None and str(recorded) != str(value):
            raise R10_1IdentityError(f"metadata_{key}_mismatch")
    procurement = metadata.get("procurement")
    if procurement is not None and not isinstance(procurement, dict):
        raise R10_1IdentityError("metadata_procurement_invalid")
    if isinstance(procurement, dict):
        recorded_case = procurement.get("case_id")
        recorded_registry = procurement.get("registry_number")
        if recorded_case is not None and str(recorded_case) != str(procurement_case_id):
            raise R10_1IdentityError("metadata_procurement_case_id_mismatch")
        if recorded_registry is not None and str(recorded_registry) != str(registry_number):
            raise R10_1IdentityError("metadata_registry_number_mismatch")


def _evidence_packet_from_documents(
    *,
    customer_id: str,
    project_id: str,
    procurement_case_id: str,
    run_id: str,
    registry_number: str,
    documents: list[Any],
):
    fragments: list[EvidenceFragmentInput] = []
    seen_document_ids: set[str] = set()
    for document in sorted(
        documents,
        key=lambda item: (
            str(getattr(item, "file_id", "")),
            str(getattr(item, "display_name", "")),
        ),
    ):
        text = str(getattr(document, "text", "") or "").strip()
        if not text:
            continue
        document_id = str(getattr(document, "file_id", "") or "").strip()
        document_name = str(getattr(document, "display_name", "") or "").strip()
        if not document_id or not document_name:
            raise R10_1IdentityError("document_identity_incomplete")
        if document_id in seen_document_ids:
            raise R10_1IdentityError("duplicate_document_identity")
        seen_document_ids.add(document_id)
        role = str(getattr(document, "role", "supporting") or "supporting")
        fragments.append(
            EvidenceFragmentInput(
                document_id=document_id,
                document_name=document_name,
                chunk_id=f"{document_id}:fulltext:v1",
                locator={"role": role, "segment": 0},
                text=text,
            )
        )
    if not fragments:
        raise R10_1AnalysisRejectedError("insufficient_evidence")
    return build_evidence_packet(
        customer_id=customer_id,
        project_id=project_id,
        procurement_case_id=procurement_case_id,
        run_id=run_id,
        registry_number=registry_number,
        fragments=fragments,
    )


def _strings(value: Any, *, field_path: str) -> list[str]:
    values = value if isinstance(value, list) else [value]
    if not values or any(not isinstance(item, str) or not item.strip() for item in values):
        raise R10_1ClaimMappingError(f"invalid_string_claim:{field_path}")
    return [item.strip() for item in values]


def _risk_rows(value: Any) -> list[dict[str, Any]]:
    values = value if isinstance(value, list) else [value]
    rows: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            raise R10_1ClaimMappingError("invalid_contract_risk_claim")
        allowed = {
            "clause",
            "description",
            "classification",
            "impact",
            "mitigation",
            "operator_decision_required",
        }
        if set(item) - allowed:
            raise R10_1ClaimMappingError("unknown_contract_risk_field")
        required_text = ("clause", "description", "classification", "impact", "mitigation")
        if any(not isinstance(item.get(key), str) or not item[key].strip() for key in required_text):
            raise R10_1ClaimMappingError("incomplete_contract_risk_claim")
        if item["classification"] not in _ALLOWED_RISK_CLASSIFICATIONS:
            raise R10_1ClaimMappingError("invalid_contract_risk_classification")
        review_required = item.get("operator_decision_required", True)
        if not isinstance(review_required, bool):
            raise R10_1ClaimMappingError("invalid_contract_risk_review_flag")
        rows.append(
            {
                "clause": item["clause"].strip(),
                "description": item["description"].strip(),
                "classification": item["classification"],
                "impact": item["impact"].strip(),
                "mitigation": item["mitigation"].strip(),
                "operator_decision_required": review_required,
            }
        )
    return rows


def _question_rows(value: Any) -> list[dict[str, str]]:
    values = value if isinstance(value, list) else [value]
    rows: list[dict[str, str]] = []
    for item in values:
        if not isinstance(item, dict) or set(item) != {"question", "category"}:
            raise R10_1ClaimMappingError("invalid_supplier_question_claim")
        question = item.get("question")
        category = item.get("category")
        if not isinstance(question, str) or not question.strip():
            raise R10_1ClaimMappingError("invalid_supplier_question_text")
        if not isinstance(category, str) or not category.strip():
            raise R10_1ClaimMappingError("invalid_supplier_question_category")
        rows.append({"question": question.strip(), "category": category.strip()})
    return rows


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _map_supported_claims(result: ProductionLLMAnalysisResult) -> tuple[dict[str, list[str]], list[dict[str, Any]], list[dict[str, str]]]:
    if (
        result.status != AnalysisStatus.SUCCESS
        or not result.canonical_input_eligible
        or result.rejected_claims
        or not result.accepted_claims
    ):
        raise R10_1AnalysisRejectedError(result.sanitized_error_code or result.status.value)

    requirements: dict[str, list[str]] = {
        "technical_requirements": [],
        "document_requirements": [],
        "qualification_requirements": [],
        "evaluation_criteria": [],
    }
    risks: list[dict[str, Any]] = []
    questions: list[dict[str, str]] = []
    seen_claim_ids: set[str] = set()

    for claim in result.accepted_claims:
        if claim.claim_id in seen_claim_ids:
            raise R10_1ClaimMappingError("duplicate_accepted_claim_id")
        seen_claim_ids.add(claim.claim_id)
        if claim.support_status != SupportStatus.SUPPORTED:
            raise R10_1ClaimMappingError("non_supported_claim_in_accepted_set")
        if not claim.evidence_references or claim.validated_confidence is None:
            raise R10_1ClaimMappingError("accepted_claim_grounding_incomplete")

        destination = _REQUIREMENT_PATHS.get(claim.field_path)
        if destination:
            requirements[destination].extend(_strings(claim.value, field_path=claim.field_path))
        elif claim.field_path == "contract_risks":
            risks.extend(_risk_rows(claim.value))
        elif claim.field_path == "supplier_questions":
            questions.extend(_question_rows(claim.value))
        else:
            raise R10_1ClaimMappingError(f"unknown_field_path:{claim.field_path}")

    for key, values in requirements.items():
        requirements[key] = _dedupe_strings(values)
    return requirements, risks, questions


def _runtime_provenance(result: ProductionLLMAnalysisResult) -> dict[str, Any]:
    return {
        "producer": "production_llm_r10_1",
        "request_id": result.request_id,
        "provider": result.provider,
        "model": result.model,
        "provider_request_id": result.provider_request_id,
        "prompt_id": result.prompt_id,
        "prompt_version": result.prompt_version,
        "output_schema_id": result.output_schema_id,
        "output_schema_version": result.output_schema_version,
        "grounding_policy_version": result.grounding_policy_version,
        "evidence_packet_hash": result.evidence_packet_hash,
        "validated_result_hash": result.validated_result_hash,
        "accepted_claims": [claim.model_dump(mode="json") for claim in result.accepted_claims],
        "rejected_claim_count": len(result.rejected_claims),
        "budget": result.budget.model_dump(mode="json"),
        "retry_count": result.retry_count,
        "raw_response_sha256": result.raw_response_sha256,
        "raw_response_stored": False,
    }


def produce_r10_1_canonical_analysis(
    *,
    customer_id: str,
    project_id: str,
    procurement_case_id: str,
    registry_number: str,
    run_id: str,
    output_dir: Path,
    metadata: dict[str, Any],
    documents: list[Any],
    provider: ProductionLLMProvider,
    budget_policy: BudgetPolicy,
    provider_name: str,
    model: str,
    prompt_id: str = "procurement-analysis",
    prompt_version: str = "r10.1-v1",
    output_schema_id: str = "production-llm-analysis",
    output_schema_version: str = "v1",
    grounding_policy_version: str = "grounding-v1",
    source_analysis_run_id: str | None = None,
) -> R10_1CanonicalProduction:
    """Produce verified canonical bytes from fully grounded, allow-listed claims."""
    _owned_identity(
        metadata=metadata,
        customer_id=customer_id,
        project_id=project_id,
        procurement_case_id=procurement_case_id,
        run_id=run_id,
        registry_number=registry_number,
    )
    packet = _evidence_packet_from_documents(
        customer_id=customer_id,
        project_id=project_id,
        procurement_case_id=procurement_case_id,
        run_id=run_id,
        registry_number=registry_number,
        documents=documents,
    )
    request = build_production_llm_request(
        evidence_packet=packet,
        provider=provider_name,
        model=model,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        output_schema_id=output_schema_id,
        output_schema_version=output_schema_version,
        grounding_policy_version=grounding_policy_version,
        budget_policy=budget_policy,
    )
    result = run_production_llm_analysis(request, provider)
    requirements, risks, questions = _map_supported_claims(result)

    from src.modules.tender_operator_agent_demo.upload_service import (
        _build_final_recommendation,
        _build_output_payloads,
        _build_steps_from_outputs,
        _render_canonical_report_html,
        _safe_datetime,
    )

    owned = dict(metadata)
    owned.update(
        {
            "customer_id": customer_id,
            "project_id": project_id,
            "run_id": run_id,
            "procurement_id": registry_number,
            "tender_title": owned.get("tender_title") or f"Закупка {registry_number}",
            "tender_category": owned.get("tender_category") or "Закупка",
            "customer_name": owned.get("customer_name") or customer_id,
            "status": "completed",
            "warnings": list(owned.get("warnings") or []),
            "limitations": [*list(owned.get("limitations") or []), *result.limitations],
            "files": list(owned.get("files") or []),
            "mode": "production_llm_r10_1",
            "analysis_mode": "production_llm_r10_1",
            "ai_runtime_provenance": _runtime_provenance(result),
            "procurement": {
                **(owned.get("procurement") if isinstance(owned.get("procurement"), dict) else {}),
                "registry_number": registry_number,
                "case_id": procurement_case_id,
            },
        }
    )
    outputs = _build_output_payloads(
        metadata=owned,
        documents=documents,
        analysis_mode="production_llm_r10_1",
        requirements=requirements,
        calibrated_risks=risks,
        supplier_questions=questions,
        tkp_comparison=None,
        economics=None,
        bid_decision=None,
        core_complete=False,
        quote_inputs_present=False,
    )
    recommendation = str(outputs.get("final_recommendation", {}).get("recommendation") or "").upper()
    if recommendation in _POSITIVE_RECOMMENDATIONS:
        raise R10_1ClaimMappingError("positive_recommendation_prohibited")
    steps = _build_steps_from_outputs(owned, outputs)
    _build_final_recommendation(outputs)
    persisted_files = persist_canonical_outputs(
        output_dir=output_dir,
        run_id=run_id,
        metadata=owned,
        outputs=outputs,
        steps=steps,
        render_html=_render_canonical_report_html,
        now_factory=_safe_datetime,
    )
    verified = verify_persisted_canonical_outputs(
        output_dir=output_dir,
        expected_outputs=outputs,
        expected_canonical_report=persisted_files.canonical_report,
    )
    persisted_requirements = json.loads(verified.requirements_bytes)
    canonical_model = persisted_requirements["preliminary_analysis"]["canonical_procurement_model"]
    stable_source_run_id = source_analysis_run_id or str(
        uuid5(NAMESPACE_URL, f"arvectum:r10.1-source:{run_id}:{result.request_id}")
    )
    return R10_1CanonicalProduction(
        registry_number=registry_number,
        source_analysis_run_id=stable_source_run_id,
        persisted=verified,
        requirements=persisted_requirements,
        canonical_model=canonical_model,
        source_graph=verified.source_graph,
        source_graph_hash=verified.source_graph_hash,
        production_model_hash=verified.production_model_hash,
        report_model_hash=verified.report_model_hash,
        llm_result=result,
    )


def produce_canonical_analysis(
    *,
    mode: CanonicalAnalysisMode | str,
    registry_number: str,
    run_id: str,
    output_dir: Path,
    metadata: dict[str, Any],
    documents: list[Any],
    source_analysis_run_id: str | None = None,
    customer_id: str | None = None,
    project_id: str | None = None,
    procurement_case_id: str | None = None,
    provider: ProductionLLMProvider | None = None,
    budget_policy: BudgetPolicy | None = None,
    provider_name: str | None = None,
    model: str | None = None,
) -> FrozenCanonicalProduction | R10_1CanonicalProduction:
    """Select one producer explicitly; never fall back between modes."""
    try:
        resolved_mode = CanonicalAnalysisMode(mode)
    except ValueError as exc:
        raise R10_1CanonicalProductionError("unsupported_canonical_analysis_mode") from exc

    if resolved_mode == CanonicalAnalysisMode.FROZEN_R9:
        if any(value is not None for value in (provider, budget_policy, provider_name, model)):
            raise R10_1CanonicalProductionError("frozen_r9_rejects_provider_configuration")
        return produce_frozen_canonical_analysis(
            registry_number=registry_number,
            run_id=run_id,
            output_dir=output_dir,
            metadata=metadata,
            documents=documents,
            source_analysis_run_id=source_analysis_run_id,
        )

    if not all((customer_id, project_id, procurement_case_id, provider, budget_policy, provider_name, model)):
        raise R10_1CanonicalProductionError("r10_1_provider_configuration_incomplete")
    return produce_r10_1_canonical_analysis(
        customer_id=str(customer_id),
        project_id=str(project_id),
        procurement_case_id=str(procurement_case_id),
        registry_number=registry_number,
        run_id=run_id,
        output_dir=output_dir,
        metadata=metadata,
        documents=documents,
        provider=provider,
        budget_policy=budget_policy,
        provider_name=str(provider_name),
        model=str(model),
        source_analysis_run_id=source_analysis_run_id,
    )
