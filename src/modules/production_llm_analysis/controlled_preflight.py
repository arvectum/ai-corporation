from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.modules.customer_pilot.models import ProcurementCase
from src.shared.config.settings import Settings
from src.tender_research.models import (
    ProcurementDocumentChunk,
    ProcurementTender,
    ProcurementTenderDocument,
    TenderAnalysisRun,
)


SUPPORTED_PROVIDER_ALIASES = frozenset({"openai", "openai_compatible", "cloudru"})
_USABLE_DOWNLOAD_STATUSES = ("downloaded", "completed", "ready")


@dataclass(frozen=True)
class ProviderPreflight:
    provider: str
    model: str | None
    provider_supported: bool
    credential_present: bool
    endpoint_host: str | None
    configuration_ready: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "provider_supported": self.provider_supported,
            "credential_present": self.credential_present,
            "endpoint_host": self.endpoint_host,
            "configuration_ready": self.configuration_ready,
        }


@dataclass(frozen=True)
class EligibleRunPreflight:
    run_id: str
    registry_number: str
    run_status: str
    customer_id: str
    project_id: str
    procurement_case_id: str
    case_status: str | None
    document_count: int
    extracted_document_count: int
    chunk_count: int
    token_estimate: int
    created_at: str
    eligible_for_gate5: bool
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "registry_number": self.registry_number,
            "run_status": self.run_status,
            "customer_id": self.customer_id,
            "project_id": self.project_id,
            "procurement_case_id": self.procurement_case_id,
            "case_status": self.case_status,
            "document_count": self.document_count,
            "extracted_document_count": self.extracted_document_count,
            "chunk_count": self.chunk_count,
            "token_estimate": self.token_estimate,
            "created_at": self.created_at,
            "eligible_for_gate5": self.eligible_for_gate5,
            "reason_codes": list(self.reason_codes),
        }


def _endpoint_host(raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed.hostname


def resolve_provider_preflight(settings: Settings) -> ProviderPreflight:
    provider = (settings.llm_provider or "").strip().lower()
    model = settings.llm_model.strip() if settings.llm_model else None
    provider_supported = provider in SUPPORTED_PROVIDER_ALIASES

    if provider in {"openai", "openai_compatible"}:
        credential_present = bool(settings.openai_api_key)
        endpoint_host = _endpoint_host(settings.openai_base_url)
    elif provider == "cloudru":
        credential_present = bool(settings.cloudru_api_key)
        endpoint_host = _endpoint_host(settings.cloudru_base_url)
    else:
        credential_present = False
        endpoint_host = None

    configuration_ready = bool(
        provider_supported and model and credential_present and endpoint_host
    )
    return ProviderPreflight(
        provider=provider,
        model=model,
        provider_supported=provider_supported,
        credential_present=credential_present,
        endpoint_host=endpoint_host,
        configuration_ready=configuration_ready,
    )


def _resolve_tender(session: Session, registry_number: str) -> ProcurementTender | None:
    return session.scalar(
        select(ProcurementTender)
        .where(
            or_(
                ProcurementTender.registry_number == registry_number,
                ProcurementTender.purchase_number == registry_number,
            )
        )
        .order_by(
            ProcurementTender.updated_at.desc(),
            ProcurementTender.external_id.desc(),
            ProcurementTender.id.desc(),
        )
    )


def _document_metrics(
    session: Session, tender: ProcurementTender | None
) -> tuple[int, int, int, int]:
    if tender is None:
        return 0, 0, 0, 0

    document_ids = session.scalars(
        select(ProcurementTenderDocument.id).where(
            ProcurementTenderDocument.tender_id == tender.id,
            ProcurementTenderDocument.download_status.in_(_USABLE_DOWNLOAD_STATUSES),
        )
    ).all()
    if not document_ids:
        return 0, 0, 0, 0

    document_count = len(document_ids)
    extracted_document_count = int(
        session.scalar(
            select(func.count(func.distinct(ProcurementDocumentChunk.document_id))).where(
                ProcurementDocumentChunk.document_id.in_(document_ids),
                func.length(func.trim(ProcurementDocumentChunk.text)) > 0,
            )
        )
        or 0
    )
    chunk_count = int(
        session.scalar(
            select(func.count(ProcurementDocumentChunk.id)).where(
                ProcurementDocumentChunk.document_id.in_(document_ids),
                func.length(func.trim(ProcurementDocumentChunk.text)) > 0,
            )
        )
        or 0
    )
    token_estimate = int(
        session.scalar(
            select(func.coalesce(func.sum(ProcurementDocumentChunk.token_estimate), 0)).where(
                ProcurementDocumentChunk.document_id.in_(document_ids),
                func.length(func.trim(ProcurementDocumentChunk.text)) > 0,
            )
        )
        or 0
    )
    return document_count, extracted_document_count, chunk_count, token_estimate


def collect_controlled_provider_preflight(
    session: Session,
    settings: Settings,
    *,
    limit: int = 30,
) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise ValueError("preflight_limit_out_of_range")

    provider = resolve_provider_preflight(settings)
    runs = session.scalars(
        select(TenderAnalysisRun)
        .where(
            TenderAnalysisRun.customer_id.is_not(None),
            TenderAnalysisRun.project_id.is_not(None),
            TenderAnalysisRun.procurement_case_id.is_not(None),
        )
        .order_by(TenderAnalysisRun.created_at.desc(), TenderAnalysisRun.id.desc())
        .limit(limit)
    ).all()

    candidates: list[EligibleRunPreflight] = []
    for run in runs:
        reason_codes: list[str] = []
        case = session.scalar(
            select(ProcurementCase).where(
                ProcurementCase.id == run.procurement_case_id,
                ProcurementCase.customer_id == run.customer_id,
                ProcurementCase.project_id == run.project_id,
            )
        )
        if case is None:
            reason_codes.append("procurement_case_identity_mismatch")
        elif case.procurement_number and case.procurement_number != run.registry_number:
            reason_codes.append("procurement_case_registry_mismatch")

        tender = _resolve_tender(session, run.registry_number)
        if tender is None:
            reason_codes.append("persisted_procurement_intake_missing")

        (
            document_count,
            extracted_document_count,
            chunk_count,
            token_estimate,
        ) = _document_metrics(session, tender)
        if document_count == 0:
            reason_codes.append("usable_documents_missing")
        elif extracted_document_count == 0 or chunk_count == 0:
            reason_codes.append("extracted_chunks_missing")

        candidates.append(
            EligibleRunPreflight(
                run_id=run.id,
                registry_number=run.registry_number,
                run_status=run.status,
                customer_id=str(run.customer_id),
                project_id=str(run.project_id),
                procurement_case_id=str(run.procurement_case_id),
                case_status=case.status if case else None,
                document_count=document_count,
                extracted_document_count=extracted_document_count,
                chunk_count=chunk_count,
                token_estimate=token_estimate,
                created_at=run.created_at.isoformat(),
                eligible_for_gate5=not reason_codes,
                reason_codes=tuple(sorted(set(reason_codes))),
            )
        )

    eligible_count = sum(candidate.eligible_for_gate5 for candidate in candidates)
    report = {
        "preflight_version": "r10.1-controlled-provider-preflight-v1",
        "configuration": provider.as_dict(),
        "eligible_run_count": eligible_count,
        "candidate_count": len(candidates),
        "ready_for_controlled_execution": bool(
            provider.configuration_ready and eligible_count > 0
        ),
        "candidates": [candidate.as_dict() for candidate in candidates],
        "safety": {
            "credential_value_recorded": False,
            "raw_tender_text_recorded": False,
            "raw_provider_body_recorded": False,
            "local_paths_recorded": False,
        },
    }
    return report
