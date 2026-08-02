"""Supported application workflow used by the ARV-001 one-shot runner."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from scripts.arv001.complete_corpus_contract import (
    DEFAULT_CUSTOMER_NAME,
    DEFAULT_MODEL,
    DEFAULT_PROJECT_NAME,
    DEFAULT_PROVIDER,
    DEFAULT_REGISTRY_NUMBER,
    AcceptanceBlocked,
    PreparedDocument,
    corpus_hash,
    sha256_file,
)


def static_contract_preflight() -> dict[str, Any]:
    from src.modules.customer_pilot.artifact_publisher import bind_completed_analysis
    from src.modules.customer_pilot.binding_verifier import verify_run_snapshot_binding
    from src.modules.customer_pilot.router import (
        CaseIn,
        ProjectIn,
        StartIn,
        complete_run,
        create_case,
        create_project,
        start_run,
    )
    from src.modules.customer_registry.schemas import CreateCustomerRequest
    from src.modules.customer_registry.service import create_customer
    from src.modules.procurement_source_graph import provenance_records, serialize_graph
    from src.shared.enums import CustomerStatus
    from src.tender_research.repository import TenderRepository

    mismatches: list[str] = []
    try:
        request = CreateCustomerRequest(
            legal_name=DEFAULT_CUSTOMER_NAME,
            customer_status=CustomerStatus.PROSPECT,
        )
        if request.customer_status.value != "PROSPECT":
            mismatches.append("CustomerStatus_PROSPECT_value_mismatch")
    except Exception:
        mismatches.append("CreateCustomerRequest_PROSPECT_invalid")
    if set(StartIn.model_fields) != {"registry_number"}:
        mismatches.append("StartIn_contract_changed")
    try:
        StartIn(registry_number=DEFAULT_REGISTRY_NUMBER)
        ProjectIn(name=DEFAULT_PROJECT_NAME)
        CaseIn(procurement_number=DEFAULT_REGISTRY_NUMBER)
    except Exception:
        mismatches.append("customer_pilot_payload_validation_failed")
    for name in ("upsert_tender", "upsert_document", "upsert_document_chunk"):
        if not callable(getattr(TenderRepository, name, None)):
            mismatches.append(f"TenderRepository_{name}_missing")
    entry_points = {
        "create_customer": create_customer,
        "create_project": create_project,
        "create_case": create_case,
        "start_run": start_run,
        "complete_run": complete_run,
        "bind_completed_analysis": bind_completed_analysis,
        "verify_run_snapshot_binding": verify_run_snapshot_binding,
        "serialize_graph": serialize_graph,
        "provenance_records": provenance_records,
    }
    for name, value in entry_points.items():
        if not callable(value):
            mismatches.append(f"entry_point_missing:{name}")
    if mismatches:
        raise AcceptanceBlocked(
            "orchestration_contract_mismatch:" + ",".join(sorted(mismatches))
        )
    return {
        "schema_mismatches": [],
        "customer_status": "PROSPECT",
        "start_run_fields": ["registry_number"],
        "analysis_mode_db_field_required": False,
        "source_graph_entry_points": ["serialize_graph", "provenance_records"],
    }


def provider_preflight(policy_path, expected_sha: str) -> dict[str, Any]:
    import httpx

    from src.modules.production_llm_analysis.batching import tokenizer_from_environment
    from src.modules.production_llm_analysis.controlled_evidence import (
        load_approved_provider_policy,
    )
    from src.shared.config.settings import get_settings

    actual_sha = sha256_file(policy_path)
    if actual_sha != expected_sha:
        raise AcceptanceBlocked("approved_policy_sha_mismatch")
    policy = load_approved_provider_policy(policy_path)
    settings = get_settings()
    if (policy.provider, policy.model) != (DEFAULT_PROVIDER, DEFAULT_MODEL):
        raise AcceptanceBlocked("approved_policy_identity_mismatch")
    if (settings.llm_provider, settings.llm_model) != (policy.provider, policy.model):
        raise AcceptanceBlocked("configured_provider_model_mismatch")
    if settings.llm_max_retries != 0 or not settings.openai_api_key:
        raise AcceptanceBlocked("provider_secret_or_retry_boundary_invalid")
    parsed = urlsplit(settings.openai_base_url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise AcceptanceBlocked("provider_not_loopback")
    try:
        response = httpx.get(
            settings.openai_base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json().get("data", [])
    except Exception as exc:
        raise AcceptanceBlocked("loopback_models_probe_failed") from exc
    if [str(item.get("id")) for item in data if isinstance(item, dict)] != [
        policy.model
    ]:
        raise AcceptanceBlocked("loopback_model_alias_mismatch")
    tokenizer = tokenizer_from_environment()
    if not getattr(tokenizer, "persistent", False) or not getattr(
        tokenizer, "identity", None
    ):
        raise AcceptanceBlocked("exact_persistent_tokenizer_missing")
    if policy.budget.limits.max_output_tokens != 4096:
        raise AcceptanceBlocked("approved_output_budget_mismatch")
    return {
        "provider": policy.provider,
        "model": policy.model,
        "policy_sha256": actual_sha,
        "max_output_tokens": 4096,
        "max_retries": 0,
        "loopback_only": True,
        "credential_recorded": False,
        "tokenizer_identity": str(tokenizer.identity),
    }


def database_preflight() -> dict[str, Any]:
    from sqlalchemy import func, select

    from src.modules.customer_pilot.models import PilotProject, PilotRunResult, ProcurementCase
    from src.modules.customer_registry.models import CustomerProfile
    from src.modules.production_llm_analysis.runtime_preflight import collect_database_preflight
    from src.shared.config.settings import get_settings
    from src.shared.db.session import SessionLocal, engine
    from src.tender_research.models import (
        ProcurementDocumentChunk,
        ProcurementTender,
        ProcurementTenderDocument,
        TenderAnalysisRun,
    )

    report = collect_database_preflight(
        engine, database_url=get_settings().database_url
    )
    if report.get("dialect") != "sqlite" or not report.get("schema_ready"):
        raise AcceptanceBlocked("local_test_database_schema_not_ready")
    models = (
        CustomerProfile,
        PilotProject,
        ProcurementCase,
        TenderAnalysisRun,
        ProcurementTender,
        ProcurementTenderDocument,
        ProcurementDocumentChunk,
        PilotRunResult,
    )
    with SessionLocal() as session:
        counts = {
            model.__tablename__: int(
                session.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in models
        }
    if any(counts.values()):
        raise AcceptanceBlocked("local_test_database_not_empty")
    return {
        "dialect": "sqlite",
        "schema_ready": True,
        "alembic_revisions": report.get("alembic_revisions", []),
        "application_counts_before": counts,
        "database_url_recorded": False,
    }


def _field(mapping: Any, *names: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    normalized = {
        re.sub(r"[^a-zа-я0-9]", "", name.lower()) for name in names
    }
    for key, value in mapping.items():
        if re.sub(r"[^a-zа-я0-9]", "", str(key).lower()) in normalized:
            return (
                value.get("value")
                if isinstance(value, dict) and "value" in value
                else value
            )
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = re.sub(r"[^0-9,.-]", "", str(value or "")).replace(",", ".")
    try:
        return float(text) if text else None
    except ValueError:
        return None


def _date(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            pass
    return None


def _tender_payload(
    metadata: dict[str, Any],
    parse_summary: dict[str, Any],
    registry_number: str,
    corpus_sha: str,
    logical_documents: list[dict[str, Any]],
) -> dict[str, Any]:
    procurement = (
        metadata.get("procurement")
        if isinstance(metadata.get("procurement"), dict)
        else {}
    )
    fields = (
        parse_summary.get("fields")
        if isinstance(parse_summary.get("fields"), dict)
        else {}
    )
    title = (
        metadata.get("tender_title")
        or procurement.get("title")
        or _field(fields, "procurement_title", "title", "subject", "наименование")
        or f"Закупка {registry_number}"
    )
    customer = (
        metadata.get("customer_name")
        or procurement.get("customer_name")
        or _field(fields, "customer_name", "заказчик")
    )
    nmck = (
        procurement.get("initial_price")
        or metadata.get("initial_price")
        or _field(fields, "nmck", "нмцк")
    )
    return {
        "source": "eis",
        "external_id": f"{registry_number}:arv001:{corpus_sha[:16]}",
        "registry_number": registry_number,
        "purchase_number": registry_number,
        "law_type": metadata.get("law") or "44-ФЗ",
        "title": str(title),
        "customer_name": str(customer) if customer else None,
        "nmck_amount": _number(nmck),
        "currency": "RUB",
        "publication_date": _date(
            metadata.get("publication_date") or procurement.get("publication_date")
        ),
        "application_deadline": _date(
            metadata.get("deadline") or procurement.get("deadline")
        ),
        "status": "published",
        "content_hash": corpus_sha,
        "raw_payload": {
            "arv001_complete_corpus": True,
            "corpus_sha256": corpus_sha,
            "logical_documents": logical_documents,
        },
    }


def create_application_data(
    *,
    customer_name: str,
    project_name: str,
    registry_number: str,
    corpus_sha: str,
    metadata: dict[str, Any],
    parse_summary: dict[str, Any],
    logical_documents: list[dict[str, Any]],
    documents: list[PreparedDocument],
) -> dict[str, Any]:
    from fastapi import Response
    from sqlalchemy import func, select

    from src.modules.customer_pilot.binding_verifier import verify_run_snapshot_binding
    from src.modules.customer_pilot.models import PilotRunResult, ProcurementCase
    from src.modules.customer_pilot.router import (
        CaseIn,
        ProjectIn,
        StartIn,
        complete_run,
        create_case,
        create_project,
        start_run,
    )
    from src.modules.customer_registry.schemas import CreateCustomerRequest
    from src.modules.customer_registry.service import create_customer
    from src.shared.db.session import SessionLocal
    from src.shared.enums import CustomerStatus
    from src.tender_research.models import (
        ProcurementDocumentChunk,
        ProcurementTenderDocument,
        TenderAnalysisRun,
    )
    from src.tender_research.repository import TenderRepository

    with SessionLocal() as session:
        profile, duplicate = create_customer(
            session,
            CreateCustomerRequest(
                legal_name=customer_name,
                customer_status=CustomerStatus.PROSPECT,
            ),
        )
        if duplicate:
            raise AcceptanceBlocked("internal_customer_not_new")
        project = create_project(
            profile.customer_id, ProjectIn(name=project_name), session
        )
        case = create_case(
            profile.customer_id,
            project["id"],
            CaseIn(procurement_number=registry_number),
            session,
        )
        repository = TenderRepository(session)
        tender = repository.upsert_tender(
            _tender_payload(
                metadata,
                parse_summary,
                registry_number,
                corpus_sha,
                logical_documents,
            )
        )
        for document in documents:
            row = repository.upsert_document(
                {
                    "tender_id": tender.id,
                    "source_document_id": document.sha256,
                    "file_name": document.original_name,
                    "file_url": document.source_url,
                    "local_path": str(document.path),
                    "content_type": document.content_type,
                    "size_bytes": document.size_bytes,
                    "sha256": document.sha256,
                    "download_status": "downloaded",
                    "text_extraction_status": "extracted",
                    "extracted_text_chars": len(document.text),
                    "raw_meta": {
                        "document_kind": document.document_kind,
                        "source_type": document.source_type,
                        "corpus_sha256": corpus_sha,
                        "corpus_descriptor": document.corpus_descriptor,
                    },
                }
            )
            for chunk in document.chunks:
                repository.upsert_document_chunk(
                    {
                        "tender_id": tender.id,
                        "document_id": row.id,
                        "chunk_index": chunk.index,
                        "text": chunk.text,
                        "text_hash": chunk.text_hash,
                        "char_start": chunk.char_start,
                        "char_end": chunk.char_end,
                        "token_estimate": chunk.token_estimate,
                        "source_file_name": document.original_name,
                        "raw_meta": {"document_kind": document.document_kind},
                    }
                )
        session.commit()
        response = Response()
        run_data = start_run(
            profile.customer_id,
            case["id"],
            StartIn(registry_number=registry_number),
            session,
            idempotency_key=f"arv001-complete-{uuid4().hex}",
            response=response,
        )
        run_id = str(run_data["id"])
        complete_run(profile.customer_id, case["id"], run_id, session)
        run = session.scalar(
            select(TenderAnalysisRun).where(TenderAnalysisRun.id == run_id)
        )
        case_row = session.scalar(
            select(ProcurementCase).where(ProcurementCase.id == case["id"])
        )
        binding = session.scalar(
            select(PilotRunResult).where(PilotRunResult.run_id == run_id)
        )
        if not run or not case_row or not binding:
            raise AcceptanceBlocked("application_binding_missing_after_completion")
        verified = verify_run_snapshot_binding(
            run=run, case=case_row, binding=binding
        )
        persisted = session.scalars(
            select(ProcurementTenderDocument).where(
                ProcurementTenderDocument.tender_id == tender.id
            )
        ).all()
        descriptors = [
            dict((item.raw_meta or {}).get("corpus_descriptor") or {})
            for item in persisted
        ]
        if len(persisted) != 10 or corpus_hash(descriptors) != corpus_sha:
            raise AcceptanceBlocked("persisted_corpus_verification_failed")
        chunk_count = int(
            session.scalar(
                select(func.count()).select_from(ProcurementDocumentChunk).where(
                    ProcurementDocumentChunk.tender_id == tender.id
                )
            )
            or 0
        )
        extracted = int(
            session.scalar(
                select(
                    func.count(func.distinct(ProcurementDocumentChunk.document_id))
                ).where(ProcurementDocumentChunk.tender_id == tender.id)
            )
            or 0
        )
        if chunk_count <= 0 or extracted != 10:
            raise AcceptanceBlocked("persisted_chunk_coverage_failed")
        return {
            "customer_id": str(profile.customer_id),
            "project_id": str(project["id"]),
            "case_id": str(case["id"]),
            "run_id": run_id,
            "tender_id": str(tender.id),
            "run_status": str(run.status),
            "case_status": str(case_row.status),
            "document_count": 10,
            "extracted_document_count": extracted,
            "chunk_count": chunk_count,
            "corpus_sha256": corpus_sha,
            "source_graph_hash": str(binding.source_graph_hash),
            "snapshot_verified": True,
            "snapshot_report_bytes": len(verified.canonical_report_bytes),
        }


def post_persistence_preflight(run_id: str) -> dict[str, Any]:
    from src.modules.production_llm_analysis.runtime_preflight import (
        collect_runtime_controlled_provider_preflight,
    )
    from src.shared.config.settings import get_settings

    report = collect_runtime_controlled_provider_preflight(
        get_settings(), limit=100
    )
    candidate = next(
        (
            item
            for item in report.get("candidates", [])
            if str(item.get("run_id")) == run_id
        ),
        None,
    )
    if not candidate:
        raise AcceptanceBlocked("new_run_missing_from_gate5_preflight")
    if not report.get("ready_for_controlled_execution"):
        raise AcceptanceBlocked("gate5_not_ready")
    if not candidate.get("eligible_for_gate5") or candidate.get("reason_codes"):
        raise AcceptanceBlocked("new_run_not_eligible_for_gate5")
    if (
        int(candidate.get("document_count") or 0) != 10
        or int(candidate.get("extracted_document_count") or 0) != 10
        or int(candidate.get("chunk_count") or 0) <= 0
        or int(candidate.get("token_estimate") or 0) <= 0
    ):
        raise AcceptanceBlocked("gate5_complete_corpus_metrics_invalid")
    return {
        "preflight_version": report.get("preflight_version"),
        "ready_for_controlled_execution": True,
        "configuration": report.get("configuration"),
        "database": report.get("database"),
        "candidate": candidate,
        "safety": report.get("safety"),
    }
