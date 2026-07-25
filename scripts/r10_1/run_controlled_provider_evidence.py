#!/usr/bin/env python3
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
from pathlib import Path

from sqlalchemy import select

from src.modules.customer_pilot.models import ProcurementCase
from src.modules.procurement_analysis.customer_inputs import resolve_customer_run_inputs
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


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
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
        "customer_id": run.customer_id,
        "project_id": run.project_id,
        "run_id": run.id,
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
        "files": [{"name": document.display_name} for document in documents],
        "procurement": {
            "registry_number": run.registry_number,
            "case_id": case.id,
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
            if case.procurement_number and case.procurement_number != run.registry_number:
                raise ControlledRunnerConfigurationError(
                    "procurement_case_registry_mismatch"
                )

            inputs = resolve_customer_run_inputs(session, run.registry_number)
            tender = session.scalar(
                select(ProcurementTender)
                .where(
                    (ProcurementTender.registry_number == run.registry_number)
                    | (ProcurementTender.purchase_number == run.registry_number)
                )
                .order_by(
                    ProcurementTender.updated_at.desc(),
                    ProcurementTender.external_id.desc(),
                    ProcurementTender.id.desc(),
                )
            )
            metadata = _metadata(
                run=run,
                case=case,
                tender=tender,
                documents=inputs.documents,
                warnings=inputs.warnings,
                limitations=inputs.limitations,
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

        bundle = run_controlled_provider_evidence(
            output_root=output_root,
            customer_id=str(run.customer_id),
            project_id=str(run.project_id),
            procurement_case_id=str(run.procurement_case_id),
            registry_number=run.registry_number,
            run_id=run.id,
            metadata=metadata,
            documents=inputs.documents,
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
    except Exception:
        print("controlled_provider_evidence_failed", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
