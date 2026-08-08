"""Run one approved customer procurement twice through the R10.1 provider path.

The API key is read only through the existing Settings secret boundary. The
command writes customer canonical files locally and a separate sanitized,
quote-free manifest suitable for review. It never publishes into the general
customer workflow and never accepts a credential as a command-line argument.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from src.modules.customer_pilot.input_resolver import (
    resolve_customer_run_inputs_for_analysis_run,
)
from src.modules.customer_pilot.models import ProcurementCase
from src.modules.procurement_analysis.r10_1_producer import (
    R10_1CanonicalProductionError,
    build_r10_1_batch_plan,
    build_r10_1_evidence_packet,
)
from src.modules.production_llm_analysis.batching import (
    BatchPolicy,
    tokenizer_from_environment,
)
from src.modules.production_llm_analysis.contracts import R10_1_CONTROLLED_MAP_CONTRACT
from src.modules.production_llm_analysis.controlled_evidence import (
    ControlledEvidenceError,
    load_approved_provider_policy,
    run_controlled_provider_evidence,
)
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
    OpenAICompatibleTransportConfig,
)
from src.shared.config.settings import get_settings
from src.shared.db.session import SessionLocal
from src.tender_research.config import load_config
from src.tender_research.models import ProcurementTender, TenderAnalysisRun


class ControlledRunnerConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedControlledEvidence:
    """Immutable pre-transport state shared by preflight and execution."""

    run: TenderAnalysisRun
    inputs: Any
    metadata: dict[str, Any]
    packet_hash: str
    batch_plan_hash: str
    transport_identity_hash: str
    output_root: Path
    token_counter: Any


_FAILURE_CODE_PATTERN = re.compile(r"^[a-z0-9_.:-]{1,160}$")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-registry-number", required=True)
    parser.add_argument("--approved-policy", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    return parser.parse_args()


def _provider_secret_boundary(provider_name: str) -> tuple[str, str]:
    settings = get_settings()
    configured = (settings.llm_provider or "").strip().lower()
    if configured != provider_name:
        raise ControlledRunnerConfigurationError("configured_provider_not_approved")
    if provider_name in {"openai", "openai_compatible"}:
        base_url, api_key = settings.openai_base_url, settings.openai_api_key
    elif provider_name == "cloudru":
        base_url, api_key = settings.cloudru_base_url, settings.cloudru_api_key
    else:
        raise ControlledRunnerConfigurationError(
            "provider_not_supported_by_gate5_runner"
        )
    if not api_key:
        raise ControlledRunnerConfigurationError("provider_credential_missing")
    return base_url, api_key


def _safe_segment(value: str) -> str:
    segment = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return segment[:80] or "model"


def _sanitized_producer_failure(exc: R10_1CanonicalProductionError) -> str:
    """Return only repository-owned diagnostic codes, never raw exception text."""

    code = str(exc).strip().lower()
    if _FAILURE_CODE_PATTERN.fullmatch(code):
        return code
    return "canonical_production_failed"


def _controlled_failure_message(exc: R10_1CanonicalProductionError) -> str:
    return f"controlled_provider_evidence_rejected:{_sanitized_producer_failure(exc)}"


def prepare_controlled_provider_evidence(
    *, session, run: TenderAnalysisRun, case: ProcurementCase, policy, base_url: str,
    api_key: str, output_root: Path, token_counter: Any,
) -> PreparedControlledEvidence:
    """Build all deterministic evidence state without creating a provider transport."""
    if run.status != "completed":
        raise ControlledRunnerConfigurationError("analysis_run_not_completed")
    inputs = resolve_customer_run_inputs_for_analysis_run(session, run)
    binding = json.loads(run.metadata_json or "{}")
    tender_id = binding.get("arv001_tender_id") if isinstance(binding, dict) else None
    tender = session.scalar(select(ProcurementTender).where(ProcurementTender.id == tender_id))
    if not tender:
        raise ControlledRunnerConfigurationError("analysis_run_intake_binding_missing")
    metadata = _metadata(run=run, case=case, tender=tender, documents=inputs.documents, warnings=inputs.warnings, limitations=inputs.limitations)
    if output_root.exists():
        raise ControlledRunnerConfigurationError("output_root_already_exists")
    if not bool(getattr(token_counter, "persistent", False)):
        raise ControlledRunnerConfigurationError("exact_persistent_tokenizer_not_configured")
    evidence_chunks = [chunk for document in inputs.documents for chunk in (document.evidence_chunks or [])]
    packet = build_r10_1_evidence_packet(
        customer_id=str(run.customer_id), project_id=str(run.project_id), procurement_case_id=str(run.procurement_case_id),
        run_id=str(run.id), registry_number=run.registry_number, documents=inputs.documents, evidence_fragments=evidence_chunks or None,
    )
    batch_policy = BatchPolicy.approved_32k(tokenizer_identity=str(token_counter.identity), measured_overhead=0)
    plan = build_r10_1_batch_plan(
        packet=packet, customer_id=str(run.customer_id), project_id=str(run.project_id), procurement_case_id=str(run.procurement_case_id),
        registry_number=run.registry_number, run_id=str(run.id), documents=inputs.documents, provider_name=policy.provider,
        model=policy.model, budget_policy=policy.budget, token_counter=token_counter, batch_policy=batch_policy,
        prompt_id=R10_1_CONTROLLED_MAP_CONTRACT.prompt_id, prompt_version=R10_1_CONTROLLED_MAP_CONTRACT.prompt_version,
        output_schema_id=R10_1_CONTROLLED_MAP_CONTRACT.output_schema_id, output_schema_version=R10_1_CONTROLLED_MAP_CONTRACT.output_schema_version,
        grounding_policy_version=R10_1_CONTROLLED_MAP_CONTRACT.grounding_policy_version, controlled=True,
    )
    transport_identity_hash = __import__("hashlib").sha256(f"{base_url}\0{policy.provider}\0{policy.model}".encode()).hexdigest()
    # Validate the config boundary but do not retain or invoke a transport.
    OpenAICompatibleTransportConfig(base_url=base_url, api_key=api_key)
    return PreparedControlledEvidence(run, inputs, metadata, packet.packet_hash, plan.plan_hash, transport_identity_hash, output_root, token_counter)


def _document_file_descriptor(document: Any) -> dict[str, Any]:
    """Project a resolved document into the canonical output-builder contract.

    The controlled runner previously emitted only ``{"name": ...}``, while the
    shared finalization step requires ``display_name``, ``extension`` and
    ``size_bytes``.  Keep the projection metadata-only: no document text or raw
    bytes are copied into runner metadata or the sanitized manifest.
    """

    display_name = str(getattr(document, "display_name", "") or "Документ")
    extension = str(getattr(document, "extension", "") or "").strip().lower()
    if not extension:
        extension = Path(display_name).suffix.lower()
    raw_content = getattr(document, "raw_content", None)
    size_bytes = (
        len(raw_content)
        if isinstance(raw_content, (bytes, bytearray, memoryview))
        else 0
    )
    role = str(getattr(document, "role", "") or "supporting")
    source = str(getattr(document, "source", "") or "customer_run")
    extracted = bool(getattr(document, "extracted_text_available", False))
    warnings = [str(item) for item in (getattr(document, "warnings", None) or [])]

    return {
        "name": display_name,
        "display_name": display_name,
        "original_name": display_name,
        "stored_name": display_name,
        "extension": extension,
        "size_bytes": size_bytes,
        "content_type": "application/octet-stream",
        "file_id": str(getattr(document, "file_id", "") or ""),
        "role": role,
        "role_hint": role,
        "document_kind": role,
        "source": source,
        "source_type": source,
        "extracted_text_available": extracted,
        "text_extraction_status": "extracted" if extracted else "empty",
        "warnings": warnings,
    }


def _metadata(
    *,
    run: TenderAnalysisRun,
    case: ProcurementCase,
    tender: ProcurementTender | None,
    documents: list,
    warnings: list[str],
    limitations: list[str],
) -> dict:
    return {
        "customer_id": str(run.customer_id),
        "project_id": str(run.project_id),
        "run_id": str(run.id),
        "procurement_id": run.registry_number,
        "tender_title": tender.title if tender else f"Закупка {run.registry_number}",
        "tender_category": (
            tender.law_type if tender and tender.law_type else "Закупка"
        ),
        "customer_name": (
            tender.customer_name
            if tender and tender.customer_name
            else str(run.customer_id)
        ),
        "customer_inn": tender.customer_inn if tender else None,
        "customer_kpp": tender.customer_kpp if tender else None,
        "publication_date": (
            tender.publication_date.isoformat()
            if tender and tender.publication_date
            else None
        ),
        "deadline": (
            tender.application_deadline.isoformat()
            if tender and tender.application_deadline
            else None
        ),
        "status": "analyzing",
        "warnings": list(warnings),
        "limitations": list(limitations),
        "files": [_document_file_descriptor(document) for document in documents],
        "procurement": {
            "registry_number": run.registry_number,
            "case_id": str(case.id),
            "customer_name": tender.customer_name if tender else None,
            "customer_inn": tender.customer_inn if tender else None,
            "customer_kpp": tender.customer_kpp if tender else None,
            "initial_price": tender.nmck_amount if tender else None,
            "publication_date": (
                tender.publication_date.isoformat()
                if tender and tender.publication_date
                else None
            ),
            "deadline": (
                tender.application_deadline.isoformat()
                if tender and tender.application_deadline
                else None
            ),
        },
    }


def main() -> int:
    args = _arguments()
    try:
        # Install ARV-001 live runtime adapters (sentinels, non-reasoning, and verification)
        from src.modules.production_llm_analysis.llama_schema_constraint import (
            install_llama_schema_constraint,
            enable_live_boundary_verification,
        )
        install_llama_schema_constraint()
        enable_live_boundary_verification()

        policy = load_approved_provider_policy(args.approved_policy)

        settings = get_settings()
        if not settings.llm_model or settings.llm_model != policy.model:
            raise ControlledRunnerConfigurationError("configured_model_not_approved")
        base_url, api_key = _provider_secret_boundary(policy.provider)

        with SessionLocal() as session:
            run = session.scalar(
                select(TenderAnalysisRun).where(TenderAnalysisRun.id == args.run_id)
            )
            if not run:
                raise ControlledRunnerConfigurationError("analysis_run_not_found")
            if run.registry_number != args.expected_registry_number:
                raise ControlledRunnerConfigurationError("registry_number_not_approved")
            if not all((run.customer_id, run.project_id, run.procurement_case_id)):
                raise ControlledRunnerConfigurationError(
                    "analysis_run_is_not_customer_owned"
                )
            case = session.scalar(
                select(ProcurementCase).where(
                    ProcurementCase.id == run.procurement_case_id,
                    ProcurementCase.customer_id == run.customer_id,
                    ProcurementCase.project_id == run.project_id,
                )
            )
            if not case:
                raise ControlledRunnerConfigurationError(
                    "procurement_case_identity_mismatch"
                )
            if (
                case.procurement_number
                and case.procurement_number != run.registry_number
            ):
                raise ControlledRunnerConfigurationError(
                    "procurement_case_registry_mismatch"
                )

        output_root = args.output_root or (
            Path(load_config().data_dir)
            / "r10-1-controlled-evidence"
            / f"{run.id}-{policy.provider}-{_safe_segment(policy.model)}"
        )

        def provider_factory():
            return OpenAICompatibleProductionLLMProvider(
                OpenAICompatibleTransportConfig(
                    base_url=base_url,
                    api_key=api_key,
                )
            )

        token_counter = tokenizer_from_environment()
        prepared = prepare_controlled_provider_evidence(
            session=session, run=run, case=case, policy=policy, base_url=base_url,
            api_key=api_key, output_root=output_root, token_counter=token_counter,
        )
        if args.preflight_only:
            print(
                json.dumps(
                    {
                        "status": "controlled_preflight_complete",
                        "evidence_packet_hash": prepared.packet_hash,
                        "batch_plan_hash": prepared.batch_plan_hash,
                        "ready_for_transport": True,
                        "controlled_preflight_invocations": 1,
                        "controlled_provider_invocations": 0,
                        "provider_generation_calls": 0,
                    },
                    sort_keys=True,
                )
            )
            return 0
        bundle = run_controlled_provider_evidence(
            output_root=prepared.output_root,
            customer_id=str(run.customer_id),
            project_id=str(run.project_id),
            procurement_case_id=str(run.procurement_case_id),
            registry_number=run.registry_number,
            run_id=str(run.id),
            metadata=prepared.metadata,
            documents=prepared.inputs.documents,
            evidence_chunks=[
                chunk
                for document in prepared.inputs.documents
                for chunk in (document.evidence_chunks or [])
            ],
            token_counter=prepared.token_counter,
            controlled=True,
            provider_factory=provider_factory,
            policy=policy,
        )
        print(
            json.dumps(
                {
                    "status": "controlled_evidence_complete",
                    "manifest_path": str(bundle.manifest_path),
                    "manifest_hash": bundle.manifest["manifest_hash"],
                    "request_id": bundle.manifest["stable_identity"]["request_id"],
                    "evidence_packet_hash": bundle.manifest["stable_identity"][
                        "evidence_packet_hash"
                    ],
                    "provider": policy.provider,
                    "model": policy.model,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (ControlledEvidenceError, ControlledRunnerConfigurationError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except R10_1CanonicalProductionError as exc:
        print(_controlled_failure_message(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - terminal boundary remains sanitized.
        name = type(exc).__name__
        if name == "HTTPException":
            code = getattr(exc, "status_code", None)
            if isinstance(code, int) and 400 <= code <= 599:
                name = f"HTTPException_{code}"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name):
            name = "Exception"
        print(f"controlled_provider_evidence_failed:{name}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
