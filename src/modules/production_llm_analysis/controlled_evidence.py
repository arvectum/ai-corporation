from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.modules.procurement_analysis.r10_1_producer import (
    R10_1CanonicalProduction,
    produce_r10_1_canonical_analysis,
)
from src.modules.production_llm_analysis.evidence import canonical_sha256
from src.modules.production_llm_analysis.schemas import BudgetPolicy
from src.modules.production_llm_analysis.service import ProductionLLMProvider


MANIFEST_VERSION = "r10.1-controlled-provider-evidence-v1"


class ControlledEvidenceError(RuntimeError):
    """Base fail-closed error for the controlled Gate 5 evidence run."""


class ControlledEvidenceConflictError(ControlledEvidenceError):
    """The target exists or repeated executions produced conflicting semantics."""


class ApprovedControlledProviderPolicy(BaseModel):
    """Versioned non-secret provider, pricing and budget approval."""

    model_config = ConfigDict(extra="forbid")

    policy_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    budget: BudgetPolicy


@dataclass(frozen=True)
class ControlledEvidenceBundle:
    manifest: dict[str, Any]
    manifest_path: Path
    first: R10_1CanonicalProduction
    second: R10_1CanonicalProduction


def load_approved_provider_policy(path: Path) -> ApprovedControlledProviderPolicy:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ControlledEvidenceError("approved_provider_policy_unreadable") from exc
    except json.JSONDecodeError as exc:
        raise ControlledEvidenceError("approved_provider_policy_invalid_json") from exc
    try:
        return ApprovedControlledProviderPolicy.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise ControlledEvidenceError("approved_provider_policy_invalid") from exc


def _reference_summary(reference: Any) -> dict[str, Any]:
    return {
        "procurement_case_id": reference.procurement_case_id,
        "registry_number": reference.registry_number,
        "fragment_id": reference.fragment_id,
        "document_id": reference.document_id,
        "document_name": reference.document_name,
        "chunk_id": reference.chunk_id,
        "locator": reference.locator,
        "quote_sha256": reference.quote_sha256,
    }


def _claim_summary(claim: Any) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "field_path": claim.field_path,
        "support_status": claim.support_status.value,
        "provider_confidence": claim.provider_confidence,
        "validated_confidence": claim.validated_confidence,
        "confidence_basis": claim.confidence_basis.value,
        "evidence_references": [
            _reference_summary(reference) for reference in claim.evidence_references
        ],
        "validation_errors": list(claim.validation_errors),
        "limitations": list(claim.limitations),
    }


def _claim_semantics(claim: Any) -> dict[str, Any]:
    """Return grounded content while excluding provider-owned confidence metadata."""

    return {
        "claim_id": claim.claim_id,
        "field_path": claim.field_path,
        "value": claim.value,
        "support_status": claim.support_status.value,
        "validated_confidence": claim.validated_confidence,
        "confidence_basis": claim.confidence_basis.value,
        "evidence_references": [
            reference.model_dump(mode="json") for reference in claim.evidence_references
        ],
        "validation_errors": list(claim.validation_errors),
        "limitations": list(claim.limitations),
    }


def _grounded_claims_hash(production: R10_1CanonicalProduction) -> str:
    result = production.llm_result
    return canonical_sha256(
        {
            "accepted_claims": [
                _claim_semantics(claim) for claim in result.accepted_claims
            ],
            "rejected_claims": [
                _claim_semantics(claim) for claim in result.rejected_claims
            ],
        }
    )


def _stable_semantic_identity(production: R10_1CanonicalProduction) -> dict[str, Any]:
    """Identity that must remain stable despite request IDs, timing and usage variance."""

    result = production.llm_result
    return {
        "request_id": result.request_id,
        "evidence_packet_hash": result.evidence_packet_hash,
        "grounded_claims_hash": _grounded_claims_hash(production),
        "source_analysis_run_id": production.source_analysis_run_id,
        "source_graph_hash": production.source_graph_hash,
        "production_model_hash": production.production_model_hash,
    }


def _publication_summary(production: R10_1CanonicalProduction) -> dict[str, Any]:
    return {
        **_stable_semantic_identity(production),
        "validated_result_hash": production.llm_result.validated_result_hash,
        "report_model_hash": production.report_model_hash,
        "requirements_file_sha256": production.persisted.requirements_file_sha256,
        "canonical_report_file_sha256": (
            production.persisted.canonical_report_file_sha256
        ),
    }


def _execution_summary(production: R10_1CanonicalProduction) -> dict[str, Any]:
    result = production.llm_result
    return {
        "status": result.status.value,
        "canonical_input_eligible": result.canonical_input_eligible,
        "provider_request_id": result.provider_request_id,
        "validated_result_hash": result.validated_result_hash,
        "accepted_claim_count": len(result.accepted_claims),
        "rejected_claim_count": len(result.rejected_claims),
        "accepted_claims": [_claim_summary(claim) for claim in result.accepted_claims],
        "rejected_claims": [_claim_summary(claim) for claim in result.rejected_claims],
        "limitations": list(result.limitations),
        "budget": result.budget.model_dump(mode="json"),
        "retry_count": result.retry_count,
        "sanitized_error_code": result.sanitized_error_code,
        "raw_response_sha256": result.raw_response_sha256,
        "raw_response_stored": False,
        "publication": _publication_summary(production),
    }


def build_sanitized_controlled_evidence_manifest(
    *,
    policy: ApprovedControlledProviderPolicy,
    productions: Sequence[R10_1CanonicalProduction],
) -> dict[str, Any]:
    if len(productions) != 2:
        raise ControlledEvidenceError("controlled_evidence_requires_two_executions")
    first, second = productions
    first_result = first.llm_result
    second_result = second.llm_result
    if first_result.provider != policy.provider or second_result.provider != policy.provider:
        raise ControlledEvidenceError("controlled_evidence_provider_policy_mismatch")
    if first_result.model != policy.model or second_result.model != policy.model:
        raise ControlledEvidenceError("controlled_evidence_model_policy_mismatch")

    first_identity = _stable_semantic_identity(first)
    second_identity = _stable_semantic_identity(second)
    if first_identity != second_identity:
        raise ControlledEvidenceConflictError(
            "controlled_evidence_repeat_identity_mismatch"
        )

    stable = {
        "provider": policy.provider,
        "model": policy.model,
        "approval_policy_version": policy.policy_version,
        "pricing_table_version": policy.budget.pricing.pricing_table_version,
        "currency": policy.budget.pricing.currency,
        "prompt_id": first_result.prompt_id,
        "prompt_version": first_result.prompt_version,
        "output_schema_id": first_result.output_schema_id,
        "output_schema_version": first_result.output_schema_version,
        "grounding_policy_version": first_result.grounding_policy_version,
        **first_identity,
    }
    payload = {
        "manifest_version": MANIFEST_VERSION,
        "stable_identity": stable,
        "repeat_count": 2,
        "repeat_identity_verified": True,
        "executions": [_execution_summary(first), _execution_summary(second)],
        "safety": {
            "credential_source": "existing_settings_secret_boundary",
            "credential_value_recorded": False,
            "raw_tender_text_recorded": False,
            "raw_provider_body_recorded": False,
            "raw_response_stored": False,
            "evidence_quotes_recorded": False,
            "local_paths_recorded": False,
        },
    }
    return {**payload, "manifest_hash": canonical_sha256(payload)}


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _relocate_production(
    production: R10_1CanonicalProduction,
    destination: Path,
) -> R10_1CanonicalProduction:
    persisted = replace(
        production.persisted,
        requirements_path=destination / "requirements.json",
        canonical_report_path=destination / "canonical_report.json",
        report_json_path=destination / "report.json",
        report_html_path=destination / "report.html",
        steps_path=destination / "steps.json",
    )
    return replace(production, persisted=persisted)


def run_controlled_provider_evidence(
    *,
    output_root: Path,
    customer_id: str,
    project_id: str,
    procurement_case_id: str,
    registry_number: str,
    run_id: str,
    metadata: dict[str, Any],
    documents: list[Any],
    provider_factory: Callable[[], ProductionLLMProvider],
    policy: ApprovedControlledProviderPolicy,
) -> ControlledEvidenceBundle:
    """Execute the same approved input twice and publish only matching evidence.

    Canonical files remain local inside the controlled output root. The manifest
    is quote-free and credential-free so it can be reviewed independently from
    customer documents. Volatile provider request IDs, latency, usage and raw
    response hashes are retained per execution but are not treated as stable.
    """

    target = output_root.resolve()
    if target.exists():
        raise ControlledEvidenceConflictError("controlled_evidence_target_exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.partial.{uuid4().hex}"
    if stage.exists():
        raise ControlledEvidenceConflictError("controlled_evidence_stage_exists")
    stage.mkdir(mode=0o750)

    try:
        productions: list[R10_1CanonicalProduction] = []
        for index in (1, 2):
            production = produce_r10_1_canonical_analysis(
                customer_id=customer_id,
                project_id=project_id,
                procurement_case_id=procurement_case_id,
                registry_number=registry_number,
                run_id=run_id,
                output_dir=stage / f"execution-{index}",
                metadata=metadata,
                documents=documents,
                provider=provider_factory(),
                budget_policy=policy.budget,
                provider_name=policy.provider,
                model=policy.model,
            )
            productions.append(production)

        manifest = build_sanitized_controlled_evidence_manifest(
            policy=policy,
            productions=productions,
        )
        manifest_path = stage / "controlled-evidence.manifest.json"
        _write_manifest(manifest_path, manifest)
        bundle = ControlledEvidenceBundle(
            manifest=manifest,
            manifest_path=target / manifest_path.name,
            first=_relocate_production(productions[0], target / "execution-1"),
            second=_relocate_production(productions[1], target / "execution-2"),
        )
        os.replace(stage, target)
        return bundle
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
