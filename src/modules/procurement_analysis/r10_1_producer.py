"""Versioned R10.1 canonical producer beside the frozen R9 path.

This module is deliberately provider-injected.  Gate 4 proves the canonical
boundary with fake providers or mocked HTTP only; it never resolves credentials
or silently falls back to the frozen producer.
"""

from __future__ import annotations

import json
import re
import time
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
from src.modules.production_llm_analysis.batching import (
    BatchPlanningError,
    BatchPolicy,
    EvidenceBatchPlan,
    ExactRequestMeasurement,
    build_evidence_batch_plan,
    measure_openai_request_tokens,
)
from src.modules.production_llm_analysis.contracts import R10_1_CONTROLLED_MAP_CONTRACT
from src.modules.production_llm_analysis.evidence import (
    build_evidence_packet,
    canonical_sha256,
)
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
)
from src.modules.production_llm_analysis.schemas import (
    AnalysisStatus,
    BudgetPolicy,
    BudgetStatus,
    EvidenceFragmentInput,
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


_PLANNING_DIAGNOSTIC_KEYS = frozenset(
    {
        "profile",
        "provider_wire_contract_version",
        "input_fragment_wire_fields_count",
        "output_reference_wire_fields_count",
        "compact_wire_enabled",
        "completed_batch_count",
        "cursor",
        "remaining_fragment_count",
        "tokenizer_http_requests",
        "http_tokenizer_request_count",
        "exact_request_measurements",
        "exact_evidence_measurements",
        "request_tokenization_count",
        "evidence_tokenization_count",
        "candidate_evaluation_count",
        "adjustment_evaluation_count",
        "adjustment_rounds_total",
        "adjustment_rounds_max",
        "cache_hits",
        "planner_cache_hits",
        "tokenizer_cache_hits",
        "planning_duration_ms",
        "last_candidate_fragment_count",
        "last_candidate_rough_tokens",
        "last_candidate_exact_evidence_tokens",
        "last_candidate_exact_request_tokens",
        "calibration_fragment_count",
        "calibration_rough_tokens",
        "calibration_serialized_evidence_tokens",
        "calibration_full_request_tokens",
        "calibration_fixed_envelope_tokens",
        "current_fixed_envelope_tokens",
        "payload_ratio",
        "context_payload_capacity",
        "rough_batch_limit",
        "envelope_drift_max",
        "conservative_ratio",
        "packing_target_utilization",
        "calibration_raw_payload_ratio",
        "maximum_observed_payload_ratio",
        "planning_payload_ratio",
        "payload_capacity",
        "rough_capacity",
        "normal_rough_target",
        "chosen_rough_target",
        "remaining_rough_tokens",
        "remaining_slots_before",
        "required_average_rough_tokens",
        "slot_pressure_ratio",
        "globally_feasible",
        "grow_attempt_count",
        "shrink_attempt_count",
        "capacity_recalculation_count",
        "failure_reason_detail",
        "accepted_batch_count",
        "fragments_per_batch_min",
        "fragments_per_batch_max",
        "fragments_per_batch_sum",
        "rough_tokens_per_batch_min",
        "rough_tokens_per_batch_max",
        "payload_utilization_min",
        "payload_utilization_max",
        "context_utilization_min",
        "context_utilization_max",
    }
)


def sanitize_batch_planning_diagnostics(
    value: object,
) -> dict[str, int | float | bool | str]:
    """Keep only approved scalar planner aggregates for public diagnostics."""
    if not isinstance(value, dict):
        return {}
    clean: dict[str, int | float | bool | str] = {}
    for key, item in value.items():
        if key not in _PLANNING_DIAGNOSTIC_KEYS or isinstance(
            item, (dict, list, tuple)
        ):
            continue
        if isinstance(item, (int, float, bool)) or (
            isinstance(item, str)
            and len(item) <= 64
            and item.replace("_", "").isalnum()
        ):
            clean[key] = item
    return clean


class R10_1BatchPlanningRejectedError(R10_1AnalysisRejectedError):
    """Fail-closed planner rejection with allow-listed aggregate diagnostics."""

    def __init__(
        self,
        *,
        sanitized_error_code: str,
        profile: str,
        plan_version: str,
        planning_diagnostics: dict[str, int | float | bool | str],
    ):
        self.sanitized_error_code = sanitized_error_code
        self.profile = profile
        self.plan_version = plan_version
        self.planning_diagnostics = planning_diagnostics
        super().__init__(sanitized_error_code)


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
    batch_plan_hash: str | None = None
    corpus_evidence_hash: str | None = None
    batch_count: int = 1
    tokenizer_identity: str | None = None
    context_profile: str | None = None
    evidence_budget: int | None = None
    chat_template_overhead: int | None = None
    final_request_body_hashes: list[str] | None = None
    final_projected_request_tokens: list[int] | None = None
    execution_deadline_ms: int = 7_200_000


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
MAP_ALLOWED_FIELD_PATHS = tuple(
    sorted((*_REQUIREMENT_PATHS.keys(), "contract_risks", "supplier_questions"))
)


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
        if recorded_registry is not None and str(recorded_registry) != str(
            registry_number
        ):
            raise R10_1IdentityError("metadata_registry_number_mismatch")


def _evidence_packet_from_documents(
    *,
    customer_id: str,
    project_id: str,
    procurement_case_id: str,
    run_id: str,
    registry_number: str,
    documents: list[Any],
    evidence_fragments: list[EvidenceFragmentInput] | None = None,
):
    fragments: list[EvidenceFragmentInput] = []
    for ordinal, raw_fragment in enumerate(evidence_fragments or [], 1):
        fragment = (
            raw_fragment
            if isinstance(raw_fragment, EvidenceFragmentInput)
            else EvidenceFragmentInput.model_validate(raw_fragment)
        )
        # The resolver owns source ordering. Legacy in-memory callers may omit
        # it, but never get to choose another document or chunk identity.
        locator = dict(fragment.locator)
        locator.setdefault("document_order", ordinal)
        locator.setdefault("chunk_index", ordinal - 1)
        fragments.append(fragment.model_copy(update={"locator": locator}))
    if fragments:
        return build_evidence_packet(
            customer_id=customer_id,
            project_id=project_id,
            procurement_case_id=procurement_case_id,
            run_id=run_id,
            registry_number=registry_number,
            fragments=fragments,
        )
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


def build_r10_1_evidence_packet(**kwargs: Any):
    """Public storage-neutral evidence projection shared by tooling and runner."""
    return _evidence_packet_from_documents(**kwargs)


def _strings(value: Any, *, field_path: str) -> list[str]:
    values = value if isinstance(value, list) else [value]
    if not values or any(
        not isinstance(item, str) or not item.strip() for item in values
    ):
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
        required_text = (
            "clause",
            "description",
            "classification",
            "impact",
            "mitigation",
        )
        if any(
            not isinstance(item.get(key), str) or not item[key].strip()
            for key in required_text
        ):
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


def _map_supported_claims(
    result: ProductionLLMAnalysisResult,
) -> tuple[dict[str, list[str]], list[dict[str, Any]], list[dict[str, str]]]:
    if (
        result.status != AnalysisStatus.SUCCESS
        or not result.canonical_input_eligible
        or result.rejected_claims
        or not result.accepted_claims
    ):
        raise R10_1AnalysisRejectedError(
            result.sanitized_error_code or result.status.value
        )

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
            requirements[destination].extend(
                _strings(claim.value, field_path=claim.field_path)
            )
        elif claim.field_path == "contract_risks":
            # Preserve a human-readable, non-quote locator through the
            # canonical boundary.  The report must never present a risk which
            # cannot be traced back to the procurement-bound evidence packet.
            locators = [
                normalized
                for reference in claim.evidence_references
                if (
                    normalized := _risk_evidence_locator(
                        reference.document_name, reference.locator
                    )
                )
            ]
            for row in _risk_rows(claim.value):
                row["evidence_locators"] = locators
                risks.append(row)
        elif claim.field_path == "supplier_questions":
            questions.extend(_question_rows(claim.value))
        else:
            raise R10_1ClaimMappingError(f"unknown_field_path:{claim.field_path}")

    for key, values in requirements.items():
        requirements[key] = _dedupe_strings(values)
    return requirements, risks, questions


def _risk_evidence_locator(document: Any, locator: Any) -> dict[str, str] | None:
    """Project a packet locator into a safe, customer-readable reference."""
    if (
        not isinstance(document, str)
        or not document.strip()
        or "/" in document
        or "\\" in document
        or _is_technical_identifier(document)
    ):
        return None
    if not isinstance(locator, dict):
        return None
    labels = {
        "path": "путь",
        "xpath": "путь",
        "section": "раздел",
        "page": "страница",
        "paragraph": "абзац",
        "line": "строка",
        "row": "строка",
        "chunk_index": "фрагмент",
    }
    parts: list[str] = []
    for key, label in labels.items():
        value = locator.get(key)
        value_text = str(value).strip() if isinstance(value, (str, int)) else ""
        if (
            value_text
            and not _is_technical_identifier(value_text)
            and not _contains_private_path(value_text)
        ):
            parts.append(f"{label}: {value}")
    if not parts:
        return None
    return {"document": document.strip(), "locator": "; ".join(parts)}


def _is_technical_identifier(value: str) -> bool:
    normalized = value.strip()
    return bool(
        re.fullmatch(r"[0-9a-f]{64}", normalized, flags=re.IGNORECASE)
        or re.fullmatch(
            r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
            normalized,
            flags=re.IGNORECASE,
        )
    )


def _contains_private_path(value: str) -> bool:
    return (
        value.startswith(("/", "file:")) or "/Volumes/" in value or "/Users/" in value
    )


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
        "accepted_claims": [
            claim.model_dump(mode="json") for claim in result.accepted_claims
        ],
        "rejected_claim_count": len(result.rejected_claims),
        "budget": result.budget.model_dump(mode="json"),
        "retry_count": result.retry_count,
        "raw_response_sha256": result.raw_response_sha256,
        "raw_response_stored": False,
        "batch_plan_version": result.batch_plan_version
        if hasattr(result, "batch_plan_version")
        else None,
        "batch_plan_hash": result.batch_plan_hash
        if hasattr(result, "batch_plan_hash")
        else None,
        "batch_hash": result.batch_hash if hasattr(result, "batch_hash") else None,
        "batch_ordinal": result.batch_ordinal
        if hasattr(result, "batch_ordinal")
        else None,
        "batch_count": result.batch_count if hasattr(result, "batch_count") else None,
        "corpus_evidence_hash": result.corpus_evidence_hash
        if hasattr(result, "corpus_evidence_hash")
        else None,
        "tokenizer_identity": result.tokenizer_identity,
        "context_profile": result.context_profile,
        "evidence_budget": result.evidence_budget,
        "chat_template_overhead": result.chat_template_overhead,
        "final_request_body_hashes": list(result.final_request_body_hashes),
        "final_projected_request_tokens": list(result.final_projected_request_tokens),
        "execution_deadline_ms": result.execution_deadline_ms,
    }


def _merge_batch_results(
    *,
    results: list[ProductionLLMAnalysisResult],
    corpus_packet_hash: str,
    plan: EvidenceBatchPlan,
) -> ProductionLLMAnalysisResult:
    """Merge map outputs without provider-order or hash-order ambiguity."""
    if not results:
        raise R10_1AnalysisRejectedError("no_batch_results")
    if any(result.status != AnalysisStatus.SUCCESS for result in results):
        raise R10_1AnalysisRejectedError("evidence_batch_provider_failed")

    def semantic(claim: Any) -> dict[str, Any]:
        references = [
            {
                "fragment_id": ref.fragment_id,
                "quote_sha256": ref.quote_sha256,
                "locator_hash": canonical_sha256(ref.locator),
            }
            for ref in claim.evidence_references
        ]
        references.sort(key=canonical_sha256)
        return {
            "field_path": claim.field_path,
            "value": claim.value,
            "support_status": claim.support_status.value,
            "evidence_references": references,
        }

    all_claims = [
        claim
        for result in results
        for claim in (*result.accepted_claims, *result.rejected_claims)
    ]
    by_claim_id: dict[str, dict[str, Any]] = {}
    for claim in all_claims:
        item = semantic(claim)
        prior = by_claim_id.get(claim.claim_id)
        if prior is not None and prior["semantic"] != item:
            raise R10_1ClaimMappingError("evidence_aggregation_claim_id_conflict")
        by_claim_id[claim.claim_id] = {"semantic": item, "claim": claim}
    unique_by_semantic: dict[str, dict[str, Any]] = {}
    for item in by_claim_id.values():
        key = canonical_sha256(item["semantic"])
        claim = item["claim"]
        if key not in unique_by_semantic:
            unique_by_semantic[key] = {"claim": claim, "claim_ids": [claim.claim_id]}
        else:
            unique_by_semantic[key]["claim_ids"].append(claim.claim_id)
    merged_claims = []
    for item in unique_by_semantic.values():
        claim = item["claim"]
        canonical_id = (
            min(item["claim_ids"]) if len(item["claim_ids"]) > 1 else claim.claim_id
        )
        merged_claims.append(claim.model_copy(update={"claim_id": canonical_id}))
    merged_claims.sort(key=lambda claim: (claim.field_path, claim.claim_id))
    accepted = [
        claim for claim in merged_claims if claim.support_status.value == "supported"
    ]
    rejected = [
        claim for claim in merged_claims if claim.support_status.value != "supported"
    ]
    supported_values: dict[str, set[str]] = {}
    for claim in accepted:
        supported_values.setdefault(claim.field_path, set()).add(
            canonical_sha256(claim.value)
        )
    conflicts = [field for field, values in supported_values.items() if len(values) > 1]
    first = results[0]
    estimated_input = sum(result.budget.estimated_input_tokens for result in results)
    estimated_output = sum(result.budget.estimated_output_tokens for result in results)
    actual_inputs = [result.budget.actual_input_tokens for result in results]
    actual_outputs = [result.budget.actual_output_tokens for result in results]
    actual_input = (
        sum(value for value in actual_inputs if value is not None)
        if all(value is not None for value in actual_inputs)
        else None
    )
    actual_output = (
        sum(value for value in actual_outputs if value is not None)
        if all(value is not None for value in actual_outputs)
        else None
    )
    first_budget = first.budget
    aggregate_budget = first_budget.model_copy(
        update={
            "status": BudgetStatus.WITHIN_BUDGET,
            "estimated_input_tokens": estimated_input,
            "estimated_output_tokens": estimated_output,
            "actual_input_tokens": actual_input,
            "actual_output_tokens": actual_output,
            "estimated_cost": sum(
                result.budget.estimated_cost or 0 for result in results
            ),
            "actual_or_reconciled_cost": sum(
                result.budget.actual_or_reconciled_cost or 0 for result in results
            ),
            "total_latency_ms": sum(
                result.budget.total_latency_ms or 0 for result in results
            ),
            "reasons": sorted(
                {reason for result in results for reason in result.budget.reasons}
            ),
        }
    )
    result_hashes = [
        canonical_sha256(
            {
                "status": result.status.value,
                "accepted": [semantic(claim) for claim in result.accepted_claims],
                "rejected": [semantic(claim) for claim in result.rejected_claims],
            }
        )
        for result in results
    ]
    merged = first.model_copy(
        update={
            "request_id": canonical_sha256(
                {
                    "plan_hash": plan.plan_hash,
                    "corpus_evidence_hash": plan.corpus_evidence_hash,
                    "batch_hashes": list(plan.ordered_batch_hashes),
                }
            ),
            "evidence_packet_hash": corpus_packet_hash,
            "accepted_claims": accepted,
            "rejected_claims": rejected,
            "limitations": sorted(
                {limitation for result in results for limitation in result.limitations}
                | ({"cross_batch_supported_value_conflict"} if conflicts else set())
            ),
            "budget": aggregate_budget,
            "provider_request_id": None,
            "raw_response_sha256": None,
            "retry_count": sum(result.retry_count for result in results),
            "batch_plan_version": plan.plan_version,
            "batch_plan_hash": plan.plan_hash,
            "batch_count": len(plan.batches),
            "corpus_evidence_hash": plan.corpus_evidence_hash,
            "batch_hashes": list(plan.ordered_batch_hashes),
            "batch_result_hashes": result_hashes,
            "provider_call_count": len(results),
            "empty_batch_count": sum(result.map_empty for result in results),
            "provider_request_ids": [
                request_id
                for result in results
                for request_id in (
                    [result.provider_request_id] if result.provider_request_id else []
                )
            ],
            "map_empty": not accepted and not rejected,
            "status": AnalysisStatus.INSUFFICIENT_EVIDENCE
            if not accepted and not rejected
            else AnalysisStatus.SUCCESS,
            "canonical_input_eligible": bool(accepted) and not rejected,
            "sanitized_error_code": "insufficient_evidence"
            if not accepted and not rejected
            else None,
            "tokenizer_identity": plan.tokenizer_identity,
            "context_profile": plan.policy.profile,
            "evidence_budget": plan.policy.evidence_budget,
            "chat_template_overhead": plan.policy.chat_template_overhead,
        }
    )
    return merged.model_copy(
        update={
            "validated_result_hash": canonical_sha256(
                merged.model_dump(mode="json", exclude={"validated_result_hash"})
            )
        }
    )


def build_r10_1_batch_plan(
    *,
    packet: Any,
    customer_id: str,
    project_id: str,
    procurement_case_id: str,
    registry_number: str,
    run_id: str,
    documents: list[Any],
    provider_name: str,
    model: str,
    budget_policy: BudgetPolicy,
    token_counter: Any,
    batch_policy: BatchPolicy,
    prompt_id: str,
    prompt_version: str,
    output_schema_id: str,
    output_schema_version: str,
    grounding_policy_version: str,
    controlled: bool,
) -> EvidenceBatchPlan:
    """Build the sole product plan used by both producer and offline verifier."""
    if controlled and (
        (
            prompt_id,
            prompt_version,
            output_schema_id,
            output_schema_version,
            grounding_policy_version,
            batch_policy.provider_wire_contract_version,
            batch_policy.plan_version,
        )
        != (
            R10_1_CONTROLLED_MAP_CONTRACT.prompt_id,
            R10_1_CONTROLLED_MAP_CONTRACT.prompt_version,
            R10_1_CONTROLLED_MAP_CONTRACT.output_schema_id,
            R10_1_CONTROLLED_MAP_CONTRACT.output_schema_version,
            R10_1_CONTROLLED_MAP_CONTRACT.grounding_policy_version,
            R10_1_CONTROLLED_MAP_CONTRACT.provider_wire_contract_version,
            R10_1_CONTROLLED_MAP_CONTRACT.plan_version,
        )
    ):
        raise R10_1BatchPlanningRejectedError(
            sanitized_error_code="r10_1_controlled_map_contract_mismatch",
            profile=batch_policy.profile,
            plan_version=batch_policy.plan_version,
            planning_diagnostics={},
        )
    plan_fragments = [
        EvidenceFragmentInput(
            document_id=fragment.document_id,
            document_name=fragment.document_name,
            chunk_id=fragment.chunk_id,
            locator=fragment.locator,
            text=fragment.text,
        )
        for fragment in packet.fragments
    ]
    from src.modules.production_llm_analysis.openai_compatible import (
        OpenAICompatibleProductionLLMProvider,
    )

    def measure_request(
        candidate: list[EvidenceFragmentInput],
    ) -> ExactRequestMeasurement:
        candidate_packet = _evidence_packet_from_documents(
            customer_id=customer_id,
            project_id=project_id,
            procurement_case_id=procurement_case_id,
            run_id=run_id,
            registry_number=registry_number,
            documents=documents,
            evidence_fragments=candidate,
        )
        content_hash = canonical_sha256(
            {
                "fragment_ids": [
                    fragment.fragment_id for fragment in candidate_packet.fragments
                ]
            }
        )
        request = build_production_llm_request(
            evidence_packet=candidate_packet,
            provider=provider_name,
            provider_wire_contract_version=batch_policy.provider_wire_contract_version,
            model=model,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            output_schema_id=output_schema_id,
            output_schema_version=output_schema_version,
            grounding_policy_version=grounding_policy_version,
            budget_policy=budget_policy,
            batch_plan_version=batch_policy.plan_version,
            batch_plan_hash="0" * 64,
            batch_hash=content_hash,
            batch_ordinal=1,
            batch_count=1,
            corpus_evidence_hash=packet.packet_hash,
            map_mode=True,
            max_claims=batch_policy.max_claims,
            allowed_field_paths=list(MAP_ALLOWED_FIELD_PATHS) if controlled else [],
            context_profile=batch_policy.profile,
            tokenizer_identity=batch_policy.tokenizer_identity,
            evidence_budget=batch_policy.evidence_budget,
            chat_template_overhead=batch_policy.chat_template_overhead,
            execution_deadline_ms=batch_policy.execution_deadline_ms,
        )
        adapter = OpenAICompatibleProductionLLMProvider.__new__(
            OpenAICompatibleProductionLLMProvider
        )
        body = adapter._build_request_body(request)
        return measure_openai_request_tokens(
            body,
            tokenizer=token_counter,
            chat_template_overhead=batch_policy.chat_template_overhead,
        )

    try:
        return build_evidence_batch_plan(
            plan_fragments,
            tokenizer=token_counter,
            policy=batch_policy,
            request_measure=measure_request,
            request_measurement_identity={
                "provider": provider_name,
                "model": model,
                "prompt_id": prompt_id,
                "prompt_version": prompt_version,
                "output_schema_id": output_schema_id,
                "output_schema_version": output_schema_version,
            },
            budget_policy=budget_policy,
            controlled=controlled,
        )
    except BatchPlanningError as exc:
        raise R10_1BatchPlanningRejectedError(
            sanitized_error_code=exc.code,
            profile=batch_policy.profile,
            plan_version=batch_policy.plan_version,
            planning_diagnostics=sanitize_batch_planning_diagnostics(
                getattr(exc, "diagnostics", {})
            ),
        ) from exc


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
    prompt_id: str = R10_1_CONTROLLED_MAP_CONTRACT.prompt_id,
    prompt_version: str = R10_1_CONTROLLED_MAP_CONTRACT.prompt_version,
    output_schema_id: str = R10_1_CONTROLLED_MAP_CONTRACT.output_schema_id,
    output_schema_version: str = R10_1_CONTROLLED_MAP_CONTRACT.output_schema_version,
    grounding_policy_version: str = R10_1_CONTROLLED_MAP_CONTRACT.grounding_policy_version,
    source_analysis_run_id: str | None = None,
    evidence_chunks: list[dict[str, Any]] | None = None,
    token_counter: Any | None = None,
    batch_policy: BatchPolicy | None = None,
    controlled: bool = False,
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
    fragments = [
        EvidenceFragmentInput.model_validate(item) for item in (evidence_chunks or [])
    ]
    if not fragments:
        for document in documents:
            fragments.extend(
                EvidenceFragmentInput.model_validate(item)
                for item in (getattr(document, "evidence_chunks", None) or [])
            )
    packet = _evidence_packet_from_documents(
        customer_id=customer_id,
        project_id=project_id,
        procurement_case_id=procurement_case_id,
        run_id=run_id,
        registry_number=registry_number,
        documents=documents,
        evidence_fragments=fragments or None,
    )
    # Tests may inject a deterministic counter. Production must inject the
    # approved llama.cpp/Gemma tokenizer; the fallback is only for legacy small
    # offline callers and is never sufficient for a controlled live run.
    counter = token_counter or (lambda text: max(1, len(text.encode("utf-8")) // 4))
    if batch_policy is None:
        tokenizer_identity = str(getattr(counter, "identity", "offline-estimated"))
        batch_policy = (
            BatchPolicy.approved_32k(
                tokenizer_identity=tokenizer_identity, measured_overhead=0
            )
            if controlled
            else BatchPolicy(tokenizer_identity=tokenizer_identity)
        )
    plan = build_r10_1_batch_plan(
        packet=packet,
        customer_id=customer_id,
        project_id=project_id,
        procurement_case_id=procurement_case_id,
        registry_number=registry_number,
        run_id=run_id,
        documents=documents,
        provider_name=provider_name,
        model=model,
        budget_policy=budget_policy,
        token_counter=counter,
        batch_policy=batch_policy,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        output_schema_id=output_schema_id,
        output_schema_version=output_schema_version,
        grounding_policy_version=grounding_policy_version,
        controlled=controlled,
    )
    started = time.monotonic()
    from src.modules.production_llm_analysis.openai_compatible import (
        OpenAICompatibleProductionLLMProvider,
    )

    batch_results: list[ProductionLLMAnalysisResult] = []
    final_request_body_hashes: list[str] = []
    final_projected_tokens: list[int] = []
    for batch in plan.batches:
        if (time.monotonic() - started) * 1000 >= batch_policy.execution_deadline_ms:
            raise R10_1AnalysisRejectedError("evidence_batch_execution_timeout")
        batch_packet = _evidence_packet_from_documents(
            customer_id=customer_id,
            project_id=project_id,
            procurement_case_id=procurement_case_id,
            run_id=run_id,
            registry_number=registry_number,
            documents=documents,
            evidence_fragments=list(batch.fragments),
        )
        request = build_production_llm_request(
            evidence_packet=batch_packet,
            provider=provider_name,
            provider_wire_contract_version=batch_policy.provider_wire_contract_version,
            model=model,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            output_schema_id=output_schema_id,
            output_schema_version=output_schema_version,
            grounding_policy_version=grounding_policy_version,
            budget_policy=budget_policy,
            batch_plan_version=plan.plan_version,
            batch_plan_hash=plan.plan_hash,
            batch_hash=batch.batch_hash,
            batch_ordinal=batch.batch_ordinal,
            batch_count=len(plan.batches),
            corpus_evidence_hash=plan.corpus_evidence_hash,
            map_mode=True,
            max_claims=batch_policy.max_claims,
            allowed_field_paths=list(MAP_ALLOWED_FIELD_PATHS) if controlled else [],
            context_profile=batch_policy.profile,
            tokenizer_identity=plan.tokenizer_identity,
            evidence_budget=batch_policy.evidence_budget,
            chat_template_overhead=batch_policy.chat_template_overhead,
            execution_deadline_ms=batch_policy.execution_deadline_ms,
        )
        provider_adapter = OpenAICompatibleProductionLLMProvider.__new__(
            OpenAICompatibleProductionLLMProvider
        )
        final_body = provider_adapter._build_request_body(request)

        final_measurement = measure_openai_request_tokens(
            final_body,
            tokenizer=counter,
            chat_template_overhead=batch_policy.chat_template_overhead,
        )
        if (
            final_measurement.serialized_evidence_tokens > batch_policy.evidence_budget
            or final_measurement.full_request_tokens
            + batch_policy.output_reserve
            + batch_policy.safety_margin
            > batch_policy.context_window
        ):
            raise R10_1AnalysisRejectedError(
                "evidence_batch_final_request_budget_exceeded"
            )
        final_request_body_hashes.append(final_measurement.request_body_hash)
        final_projected_tokens.append(final_measurement.full_request_tokens)
        result = run_production_llm_analysis(request, provider)
        if result.status != AnalysisStatus.SUCCESS:
            sanitized = result.sanitized_error_code or ""
            if sanitized == "provider_response_truncated":
                raise R10_1AnalysisRejectedError("evidence_batch_output_truncated")
            error_code = (result.sanitized_error_code if not controlled else None) or {
                AnalysisStatus.TIMEOUT: "evidence_batch_execution_timeout",
                AnalysisStatus.PROVIDER_UNAVAILABLE: (
                    result.sanitized_error_code
                    if controlled
                    and result.sanitized_error_code
                    in {
                        "provider_request_rejected",
                        "provider_transient_failure",
                        "provider_unavailable",
                        "provider_call_failed",
                        "provider_timeout",
                        "provider_response_invalid",
                        "provider_response_truncated",
                        "provider_runtime_budget_exceeded",
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
                    }
                    else "evidence_batch_provider_failed"
                ),
                AnalysisStatus.INVALID_RESPONSE: "evidence_batch_invalid_response",
                AnalysisStatus.BUDGET_EXCEEDED: "evidence_aggregation_budget_exceeded",
                AnalysisStatus.VALIDATION_FAILED: "evidence_batch_grounding_failed",
                AnalysisStatus.INSUFFICIENT_EVIDENCE: "evidence_batch_grounding_failed",
            }.get(result.status, "evidence_batch_provider_failed")
            raise R10_1AnalysisRejectedError(error_code)
        batch_results.append(result)
    result = _merge_batch_results(
        results=batch_results, corpus_packet_hash=packet.packet_hash, plan=plan
    )
    result = result.model_copy(
        update={
            "final_request_body_hashes": final_request_body_hashes,
            "final_projected_request_tokens": final_projected_tokens,
            "validated_result_hash": canonical_sha256(
                result.model_dump(mode="json", exclude={"validated_result_hash"})
            ),
        }
    )
    aggregate_budget = result.budget
    if (
        len(plan.batches) > batch_policy.max_provider_calls
        or aggregate_budget.estimated_input_tokens > batch_policy.max_total_input_tokens
        or aggregate_budget.actual_input_tokens is not None
        and aggregate_budget.actual_input_tokens > batch_policy.max_total_input_tokens
        or aggregate_budget.estimated_output_tokens
        > batch_policy.max_total_output_tokens
        or aggregate_budget.actual_output_tokens is not None
        and aggregate_budget.actual_output_tokens > batch_policy.max_total_output_tokens
        or result.retry_count > batch_policy.max_total_retries
        or (aggregate_budget.actual_or_reconciled_cost or 0)
        > batch_policy.max_total_cost
        or (aggregate_budget.total_latency_ms or 0) > batch_policy.execution_deadline_ms
    ):
        raise R10_1AnalysisRejectedError("evidence_aggregation_budget_exceeded")
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
            # customer_id is a tenant identity, never a customer-facing
            # procurement attribute.  A document-derived value is applied
            # below when available; otherwise the report model renders an
            # explicit "not extracted" state.
            "customer_name": owned.get("customer_name"),
            "status": "completed",
            "warnings": list(owned.get("warnings") or []),
            "limitations": [*list(owned.get("limitations") or []), *result.limitations],
            "files": list(owned.get("files") or []),
            "mode": "production_llm_r10_1",
            "analysis_mode": "production_llm_r10_1",
            "ai_runtime_provenance": _runtime_provenance(result),
            "procurement": {
                **(
                    owned.get("procurement")
                    if isinstance(owned.get("procurement"), dict)
                    else {}
                ),
                "registry_number": registry_number,
                "case_id": procurement_case_id,
            },
        }
    )
    from src.modules.tender_operator_agent_demo.upload_service import (
        _enrich_procurement_metadata_from_documents,
    )

    notice_text = "\n".join(
        document.text or "" for document in documents if document.role == "notice"
    )
    combined_text = "\n".join(document.text or "" for document in documents)
    owned = _enrich_procurement_metadata_from_documents(
        owned,
        documents=documents,
        combined_text=combined_text,
        notice_text=notice_text,
        technical_spec_text="\n".join(
            document.text or ""
            for document in documents
            if document.role == "technical_spec"
        ),
        contract_draft_text="\n".join(
            document.text or ""
            for document in documents
            if document.role == "contract_draft"
        ),
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
    recommendation = str(
        outputs.get("final_recommendation", {}).get("recommendation") or ""
    ).upper()
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
    canonical_model = persisted_requirements["preliminary_analysis"][
        "canonical_procurement_model"
    ]
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
        batch_plan_hash=plan.plan_hash,
        corpus_evidence_hash=plan.corpus_evidence_hash,
        batch_count=len(plan.batches),
        tokenizer_identity=plan.tokenizer_identity,
        context_profile=plan.policy.profile,
        evidence_budget=plan.policy.evidence_budget,
        chat_template_overhead=plan.policy.chat_template_overhead,
        final_request_body_hashes=final_request_body_hashes,
        final_projected_request_tokens=final_projected_tokens,
        execution_deadline_ms=plan.policy.execution_deadline_ms,
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
        raise R10_1CanonicalProductionError(
            "unsupported_canonical_analysis_mode"
        ) from exc

    if resolved_mode == CanonicalAnalysisMode.FROZEN_R9:
        if any(
            value is not None
            for value in (provider, budget_policy, provider_name, model)
        ):
            raise R10_1CanonicalProductionError(
                "frozen_r9_rejects_provider_configuration"
            )
        return produce_frozen_canonical_analysis(
            registry_number=registry_number,
            run_id=run_id,
            output_dir=output_dir,
            metadata=metadata,
            documents=documents,
            source_analysis_run_id=source_analysis_run_id,
        )

    if not all(
        (
            customer_id,
            project_id,
            procurement_case_id,
            provider,
            budget_policy,
            provider_name,
            model,
        )
    ):
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
