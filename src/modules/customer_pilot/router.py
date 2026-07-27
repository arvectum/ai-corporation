from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.modules.customer_pilot.artifact_publisher import (
    bind_completed_analysis,
    publish_final_pdf,
)
from src.modules.customer_pilot.artifacts import (
    verified_pilot_artifact,
    verify_review_artifact_binding,
)
from src.modules.customer_pilot.binding_verifier import (
    RunSnapshotBindingError,
    verify_run_snapshot_binding,
)
from src.modules.customer_pilot.models import (
    PilotArtifact,
    PilotAuditEvent,
    PilotFeedback,
    PilotProject,
    PilotReview,
    PilotRunResult,
    ProcurementCase,
)
from src.modules.customer_registry.models import CustomerProfile
from src.shared.api.dependencies import DBSession
from src.shared.config.settings import get_settings
from src.shared.db.base import utcnow
from src.shared.redis.errors import (
    RedisAlreadyLockedError,
    RedisDisabledError,
    RedisLockTimeoutError,
    RedisUnavailableError,
)
from src.shared.redis.keys import build_lock_key
from src.shared.redis.lock import acquire as redis_acquire_lock
from src.shared.redis.lock import release as redis_release_lock
from src.tender_research.models import TenderAnalysisRun

router = APIRouter(prefix="/api/operator/pilot", tags=["customer-pilot"])
CASE_STATUSES = {
    "created",
    "collecting_documents",
    "analyzing",
    "operator_review",
    "client_ready",
    "delivered",
    "archived",
    "failed",
}
TRANSITIONS = {
    "created": {"collecting_documents", "analyzing", "archived"},
    "collecting_documents": {"analyzing", "archived", "failed"},
    "analyzing": {"operator_review", "failed"},
    "operator_review": {"client_ready", "analyzing", "archived"},
    "client_ready": {"delivered", "archived"},
    "delivered": {"archived"},
    "failed": {"analyzing", "archived"},
    "archived": set(),
}
VERDICTS = {"approved", "approved_with_notes", "needs_reanalysis", "rejected"}
FEEDBACK_CATEGORIES = {
    "missing_requirement",
    "incorrect_requirement",
    "incorrect_risk",
    "source_mismatch",
    "report_usability",
    "supplier_relevance",
    "other",
}


class ProjectIn(BaseModel):
    name: str = Field(min_length=1)


class CaseIn(BaseModel):
    procurement_number: str | None = None


class StartIn(BaseModel):
    registry_number: str | None = None


class ReviewIn(BaseModel):
    reviewer: str
    verdict: str
    checklist: dict = Field(default_factory=dict)
    internal_comment: str | None = None
    client_comment: str | None = None


class FeedbackIn(BaseModel):
    category: str
    severity: str
    expected_value: str | None = None
    observed_value: str | None = None
    comment: str | None = None


def _audit(
    session: Session,
    event: str,
    *,
    customer_id: str | None = None,
    project_id: str | None = None,
    case_id: str | None = None,
    run_id: str | None = None,
    payload: dict | None = None,
) -> None:
    # Deliberately retain only workflow metadata: never request headers/documents/secrets.
    session.add(
        PilotAuditEvent(
            customer_id=customer_id,
            project_id=project_id,
            procurement_case_id=case_id,
            run_id=run_id,
            event_type=event,
            payload=payload or {},
        )
    )


def _case(session: Session, customer_id: str, case_id: str) -> ProcurementCase:
    item = session.scalar(
        select(ProcurementCase).where(
            ProcurementCase.id == case_id, ProcurementCase.customer_id == customer_id
        )
    )
    if not item:
        raise HTTPException(404, "Procurement case not found")
    return item


def _run(
    session: Session, customer_id: str, case_id: str, run_id: str
) -> TenderAnalysisRun:
    item = session.scalar(
        select(TenderAnalysisRun).where(
            TenderAnalysisRun.id == run_id,
            TenderAnalysisRun.customer_id == customer_id,
            TenderAnalysisRun.procurement_case_id == case_id,
        )
    )
    if not item:
        raise HTTPException(404, "Analysis run not found")
    return item


def _transition(case: ProcurementCase, target: str) -> None:
    if target not in CASE_STATUSES or target not in TRANSITIONS.get(case.status, set()):
        raise HTTPException(
            409, f"Invalid lifecycle transition: {case.status} -> {target}"
        )
    case.status = target
    case.updated_at = utcnow()


_IDEM_RESULT_TTL = 3600  # 1 hour, matches idempotency_key lifetime


def _lookup_idem_result(settings, idempotency_key: str) -> dict | None:
    if not settings.arvectum_redis_enabled:
        return None
    try:
        from src.shared.redis.client import require_client
        rc = require_client()
        cached = rc.get(f"idem_result:{idempotency_key}")
        if cached:
            return json.loads(cached)
    except Exception:  # noqa: BLE001, S110
        pass
    return None


def _store_idem_result(settings, idempotency_key: str, data: dict) -> None:
    if not settings.arvectum_redis_enabled:
        return
    try:
        from src.shared.redis.client import require_client
        rc = require_client()
        rc.setex(f"idem_result:{idempotency_key}", _IDEM_RESULT_TTL, json.dumps(data))
    except Exception:  # noqa: BLE001, S110
        pass


@router.post("/customers/{customer_id}/projects", status_code=status.HTTP_201_CREATED)
def create_project(customer_id: str, payload: ProjectIn, session: DBSession):
    if not session.scalar(
        select(CustomerProfile).where(CustomerProfile.customer_id == customer_id)
    ):
        raise HTTPException(404, "Customer not found")
    slug = re.sub(r"[^a-z0-9]+", "-", payload.name.lower()).strip("-")[:48] or "project"
    project = PilotProject(
        customer_id=customer_id,
        name=payload.name.strip(),
        internal_slug=f"{slug}-{uuid4().hex[:10]}",
    )
    session.add(project)
    session.flush()
    _audit(session, "project_created", customer_id=customer_id, project_id=project.id)
    session.commit()
    return {
        "id": project.id,
        "customer_id": customer_id,
        "name": project.name,
        "internal_slug": project.internal_slug,
    }


@router.post(
    "/customers/{customer_id}/projects/{project_id}/cases",
    status_code=status.HTTP_201_CREATED,
)
def create_case(customer_id: str, project_id: str, payload: CaseIn, session: DBSession):
    if not session.scalar(
        select(PilotProject).where(
            PilotProject.id == project_id, PilotProject.customer_id == customer_id
        )
    ):
        raise HTTPException(404, "Project not found")
    case = ProcurementCase(
        customer_id=customer_id,
        project_id=project_id,
        procurement_number=payload.procurement_number,
        artifact_key=f"c_{uuid4().hex}",
    )
    session.add(case)
    session.flush()
    _audit(
        session,
        "case_created",
        customer_id=customer_id,
        project_id=project_id,
        case_id=case.id,
    )
    session.commit()
    return {
        "id": case.id,
        "customer_id": customer_id,
        "project_id": project_id,
        "status": case.status,
        "artifact_key": case.artifact_key,
    }


@router.post("/customers/{customer_id}/cases/{case_id}/runs")
def start_run(
        customer_id: str,
        case_id: str,
        payload: StartIn,
        session: DBSession,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        response: Response = None,
    ):
    case = _case(session, customer_id, case_id)

    existing = session.scalar(
        select(TenderAnalysisRun).where(
            TenderAnalysisRun.procurement_case_id == case_id,
            TenderAnalysisRun.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return {
            "id": existing.id,
            "status": existing.status,
            "idempotent": True,
            "artifact_key": existing.artifact_key,
        }

    settings = get_settings()
    lock_token: str | None = None
    if settings.arvectum_redis_enabled:
        lock_key = build_lock_key(
            namespace=settings.arvectum_redis_namespace,
            environment="production",
            customer=customer_id,
            project=case.project_id,
            case=case_id,
            operation="start_run",
            idempotency_key_raw=idempotency_key,
        )
        try:
            lock_token = redis_acquire_lock(lock_key, wait_timeout_seconds=5)
        except (RedisDisabledError, RedisUnavailableError):
            raise HTTPException(
                status_code=503,
                detail={"code": "redis_unavailable", "message": "Run coordination unavailable"},
            )
        except (RedisAlreadyLockedError, RedisLockTimeoutError):
            idem_result = _lookup_idem_result(settings, idempotency_key)
            if idem_result:
                idem_result["idempotent"] = True
                return idem_result
            existing = session.scalar(
                select(TenderAnalysisRun).where(
                    TenderAnalysisRun.procurement_case_id == case_id,
                    TenderAnalysisRun.idempotency_key == idempotency_key,
                )
            )
            if existing:
                return {
                    "id": existing.id,
                    "status": existing.status,
                    "idempotent": True,
                    "artifact_key": existing.artifact_key,
                }
            raise HTTPException(
                status_code=503,
                detail={"code": "run_coordination_timeout", "message": "Run coordination timed out"},
            )

    try:
        session.rollback()
        # Check Redis for an idempotency result cached by the thread that
        # created the run.  This avoids SQLite WAL-mode snapshot isolation
        # issues where a fresh read transaction on a different connection
        # may not see data committed by another thread.
        if lock_token is not None and settings.arvectum_redis_enabled:
            idem_result = _lookup_idem_result(settings, idempotency_key)
            if idem_result:
                idem_result["idempotent"] = True
                return idem_result

        existing = session.scalar(
            select(TenderAnalysisRun).where(
                TenderAnalysisRun.procurement_case_id == case_id,
                TenderAnalysisRun.idempotency_key == idempotency_key,
            )
        )
        if existing:
            return {
                "id": existing.id,
                "status": existing.status,
                "idempotent": True,
                "artifact_key": existing.artifact_key,
            }

        session.rollback()
        startable = {"created", "collecting_documents", "failed", "operator_review"}
        claimed = session.execute(
            update(ProcurementCase)
            .where(
                ProcurementCase.id == case_id,
                ProcurementCase.customer_id == customer_id,
                ProcurementCase.status.in_(startable),
            )
            .values(status="analyzing", updated_at=utcnow())
        ).rowcount
        if claimed != 1:
            session.rollback()
            idem_result = _lookup_idem_result(settings, idempotency_key)
            if idem_result:
                idem_result["idempotent"] = True
                return idem_result
            existing = session.scalar(
                select(TenderAnalysisRun).where(
                    TenderAnalysisRun.procurement_case_id == case_id,
                    TenderAnalysisRun.idempotency_key == idempotency_key,
                )
            )
            if existing:
                return {
                    "id": existing.id,
                    "status": existing.status,
                    "idempotent": True,
                    "artifact_key": existing.artifact_key,
                }
            raise HTTPException(409, "A run is already active or case cannot be started")

        run = TenderAnalysisRun(
            registry_number=payload.registry_number
            or case.procurement_number
            or "manual-documents",
            status="analyzing",
            customer_id=customer_id,
            project_id=case.project_id,
            procurement_case_id=case_id,
            idempotency_key=idempotency_key,
            artifact_key=f"r_{uuid4().hex}",
            source="customer_pilot",
        )
        try:
            session.add(run)
            session.flush()
            case.current_run_id = run.id
        except IntegrityError:
            session.rollback()
            idem_result = _lookup_idem_result(settings, idempotency_key)
            if idem_result:
                idem_result["idempotent"] = True
                return idem_result
            existing = session.scalar(
                select(TenderAnalysisRun).where(
                    TenderAnalysisRun.procurement_case_id == case_id,
                    TenderAnalysisRun.idempotency_key == idempotency_key,
                )
            )
            if existing:
                return {
                    "id": existing.id,
                    "status": existing.status,
                    "idempotent": True,
                    "artifact_key": existing.artifact_key,
                }
            raise HTTPException(409, "Concurrent analysis start was rejected")

        _audit(
            session,
            "analysis_started",
            customer_id=customer_id,
            project_id=case.project_id,
            case_id=case_id,
            run_id=run.id,
        )
        session.commit()
        _store_idem_result(
            settings,
            idempotency_key,
            {"id": run.id, "status": run.status, "idempotent": False, "artifact_key": run.artifact_key},
        )
        if response is not None:
            response.status_code = 201
        return {"id": run.id, "status": run.status, "idempotent": False, "artifact_key": run.artifact_key}
    finally:
        if lock_token is not None:
            try:
                redis_release_lock(lock_key, lock_token)
            except Exception:  # noqa: BLE001, S110
                pass


@router.post("/customers/{customer_id}/cases/{case_id}/runs/{run_id}/complete")
def complete_run(
    customer_id: str,
    case_id: str,
    run_id: str,
    session: DBSession,
):
    case = _case(session, customer_id, case_id)
    run = _run(session, customer_id, case_id, run_id)
    if case.current_run_id != run.id:
        raise HTTPException(409, "Analysis run is no longer current")
    existing = session.scalar(
        select(PilotRunResult).where(PilotRunResult.run_id == run.id)
    )
    if run.status == "completed" and existing and existing.is_verified_snapshot_binding:
        try:
            verify_run_snapshot_binding(run=run, case=case, binding=existing)
        except RunSnapshotBindingError as exc:
            _audit(
                session,
                "analysis_snapshot_invalid",
                customer_id=customer_id,
                project_id=case.project_id,
                case_id=case_id,
                run_id=run_id,
            )
            session.commit()
            raise HTTPException(409, "Existing canonical snapshot is invalid") from exc
        return {
            "id": run.id,
            "status": run.status,
            "case_status": case.status,
            "run_result_id": existing.id,
            "idempotent": True,
        }
    if run.status not in {"analyzing", "completed"}:
        raise HTTPException(409, "Run is not active")
    try:
        binding = bind_completed_analysis(session, run, case)
    except HTTPException:
        _audit(
            session,
            "analysis_failed",
            customer_id=customer_id,
            project_id=case.project_id,
            case_id=case_id,
            run_id=run_id,
            payload={"reason": "canonical_result_unavailable"},
        )
        session.commit()
        raise
    run.status = "completed"
    case.status = "operator_review"
    case.updated_at = utcnow()
    _audit(
        session,
        "analysis_completed",
        customer_id=customer_id,
        project_id=case.project_id,
        case_id=case_id,
        run_id=run_id,
    )
    session.commit()
    return {
        "id": run.id,
        "status": run.status,
        "case_status": case.status,
        "run_result_id": binding.id,
        "idempotent": False,
    }


def _artifact(session: Session, run: TenderAnalysisRun) -> PilotArtifact:
    item = session.scalar(
        select(PilotArtifact).where(
            PilotArtifact.run_id == run.id, PilotArtifact.artifact_type == "final_pdf"
        )
    )
    if not item:
        raise HTTPException(404, "Final artifact not found")
    return item


@router.post(
    "/customers/{customer_id}/cases/{case_id}/runs/{run_id}/artifacts/final-pdf",
    status_code=status.HTTP_201_CREATED,
)
def publish_pdf(customer_id: str, case_id: str, run_id: str, session: DBSession):
    case = _case(session, customer_id, case_id)
    run = _run(session, customer_id, case_id, run_id)
    if case.current_run_id != run.id:
        raise HTTPException(409, "Analysis run is no longer current")
    artifact, created = publish_final_pdf(session, run, case)
    if created:
        _audit(
            session,
            "artifact_exported",
            customer_id=customer_id,
            project_id=case.project_id,
            case_id=case_id,
            run_id=run_id,
            payload={"artifact_key": artifact.artifact_key},
        )
    session.commit()
    return _artifact_response(artifact)


def _artifact_response(artifact: PilotArtifact) -> dict:
    return {
        "id": artifact.id,
        "artifact_type": artifact.artifact_type,
        "artifact_key": artifact.artifact_key,
        "report_model_hash": artifact.report_model_hash,
        "renderer_version": artifact.renderer_version,
        "pdf_sha256": artifact.pdf_sha256,
        "byte_size": artifact.byte_size,
        "created_at": artifact.created_at,
        "immutable_at": artifact.immutable_at,
        "status": artifact.status,
    }


@router.get("/customers/{customer_id}/cases/{case_id}/runs/{run_id}/artifacts")
def list_artifacts(customer_id: str, case_id: str, run_id: str, session: DBSession):
    _case(session, customer_id, case_id)
    run = _run(session, customer_id, case_id, run_id)
    return [
        _artifact_response(item)
        for item in session.scalars(
            select(PilotArtifact).where(
                PilotArtifact.customer_id == customer_id,
                PilotArtifact.procurement_case_id == case_id,
                PilotArtifact.run_id == run.id,
            )
        ).all()
    ]


@router.get(
    "/customers/{customer_id}/cases/{case_id}/runs/{run_id}/artifacts/final-pdf"
)
def download_pdf(customer_id: str, case_id: str, run_id: str, session: DBSession):
    case = _case(session, customer_id, case_id)
    run = _run(session, customer_id, case_id, run_id)
    artifact = _artifact(session, run)
    result = session.scalar(
        select(PilotRunResult).where(PilotRunResult.id == artifact.run_result_id)
    )
    if not result:
        raise HTTPException(409, "Final artifact binding is missing")
    verified = verified_pilot_artifact(run, case, result, artifact)
    return Response(
        content=verified.pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.artifact_key}.pdf"'
        },
    )


@router.post("/customers/{customer_id}/cases/{case_id}/runs/{run_id}/review")
def review_run(
    customer_id: str, case_id: str, run_id: str, payload: ReviewIn, session: DBSession
):
    case = _case(session, customer_id, case_id)
    run = _run(session, customer_id, case_id, run_id)
    if case.current_run_id != run.id:
        raise HTTPException(409, "Analysis run is no longer current")
    if payload.verdict not in VERDICTS or case.status != "operator_review":
        raise HTTPException(409, "Review is not permitted")
    if session.scalar(select(PilotReview).where(PilotReview.run_id == run_id)):
        raise HTTPException(409, "Review is immutable; create a new run for re-review")
    artifact = None
    result = None
    if payload.verdict in {"approved", "approved_with_notes"}:
        artifact = session.scalar(
            select(PilotArtifact).where(
                PilotArtifact.run_id == run.id,
                PilotArtifact.artifact_type == "final_pdf",
            )
        )
        if not artifact:
            # A missing expected artifact can result from a tampered run/artifact
            # binding; keep the review endpoint on the same fail-closed trust path.
            raise HTTPException(409, "Final artifact trust binding is invalid")
        result = session.scalar(
            select(PilotRunResult).where(PilotRunResult.id == artifact.run_result_id)
        )
        if not result or not result.is_verified_snapshot_binding:
            raise HTTPException(409, "Canonical result binding is required")
        verified_pilot_artifact(run, case, result, artifact)
    review = PilotReview(
        customer_id=customer_id,
        project_id=case.project_id,
        procurement_case_id=case_id,
        run_id=run_id,
        reviewer=payload.reviewer,
        verdict=payload.verdict,
        checklist=payload.checklist,
        internal_comment=payload.internal_comment,
        client_comment=payload.client_comment,
        source_graph_hash=result.source_graph_hash if result else None,
        artifact_id=artifact.id if artifact else None,
        artifact_key=artifact.artifact_key if artifact else None,
        pdf_sha256=artifact.pdf_sha256 if artifact else None,
        renderer_version=artifact.renderer_version if artifact else None,
        report_model_hash=artifact.report_model_hash if artifact else None,
        artifact_hashes={"pdf": artifact.pdf_sha256} if artifact else {},
        immutable_at=utcnow()
        if payload.verdict in {"approved", "approved_with_notes"}
        else None,
    )
    session.add(review)
    _audit(
        session,
        "review_updated",
        customer_id=customer_id,
        project_id=case.project_id,
        case_id=case_id,
        run_id=run_id,
        payload={"verdict": payload.verdict},
    )
    session.commit()
    return {
        "id": review.id,
        "verdict": review.verdict,
        "immutable": review.immutable_at is not None,
    }


@router.post("/customers/{customer_id}/cases/{case_id}/client-ready")
def mark_client_ready(customer_id: str, case_id: str, session: DBSession):
    case = _case(session, customer_id, case_id)
    approved = session.scalar(
        select(PilotReview).where(
            PilotReview.procurement_case_id == case_id,
            PilotReview.run_id == case.current_run_id,
            PilotReview.verdict.in_({"approved", "approved_with_notes"}),
        )
    )
    if not approved:
        raise HTTPException(409, "Completed approved operator review is required")
    if approved.run_id != case.current_run_id:
        raise HTTPException(409, "Approved review is not for the current run")
    run = _run(session, customer_id, case_id, approved.run_id)
    artifact = _artifact(session, run)
    result = session.scalar(
        select(PilotRunResult).where(PilotRunResult.id == artifact.run_result_id)
    )
    if not result or not result.is_verified_snapshot_binding:
        raise HTTPException(409, "Canonical result binding is required")
    verified = verified_pilot_artifact(run, case, result, artifact)
    verify_review_artifact_binding(
        review=approved,
        run=run,
        case=case,
        result=result,
        artifact=artifact,
        verified_artifact=verified,
    )
    _transition(case, "client_ready")
    _audit(
        session,
        "client_ready",
        customer_id=customer_id,
        project_id=case.project_id,
        case_id=case_id,
    )
    session.commit()
    return {"status": case.status}


@router.post("/customers/{customer_id}/cases/{case_id}/delivered")
def delivered(customer_id: str, case_id: str, session: DBSession):
    case = _case(session, customer_id, case_id)
    review = session.scalar(
        select(PilotReview).where(
            PilotReview.procurement_case_id == case_id,
            PilotReview.run_id == case.current_run_id,
            PilotReview.verdict.in_({"approved", "approved_with_notes"}),
            PilotReview.immutable_at.is_not(None),
            PilotReview.artifact_id.is_not(None),
        )
    )
    if not review or not review.artifact_id:
        raise HTTPException(409, "Immutable final PDF is required")
    if review.run_id != case.current_run_id:
        raise HTTPException(409, "Approved review is not for the current run")
    run = _run(session, customer_id, case_id, review.run_id)
    artifact = _artifact(session, run)
    result = session.scalar(
        select(PilotRunResult).where(PilotRunResult.id == artifact.run_result_id)
    )
    if not result or not result.is_verified_snapshot_binding:
        raise HTTPException(409, "Canonical result binding is required")
    verified = verified_pilot_artifact(run, case, result, artifact)
    verify_review_artifact_binding(
        review=review,
        run=run,
        case=case,
        result=result,
        artifact=artifact,
        verified_artifact=verified,
    )
    _transition(case, "delivered")
    _audit(
        session,
        "delivered",
        customer_id=customer_id,
        project_id=case.project_id,
        case_id=case_id,
    )
    session.commit()
    return {"status": case.status}


@router.post("/customers/{customer_id}/cases/{case_id}/archive")
def archive_case(customer_id: str, case_id: str, session: DBSession):
    case = _case(session, customer_id, case_id)
    _transition(case, "archived")
    _audit(
        session,
        "case_archived",
        customer_id=customer_id,
        project_id=case.project_id,
        case_id=case_id,
    )
    session.commit()
    return {"status": case.status}


@router.post(
    "/customers/{customer_id}/cases/{case_id}/feedback",
    status_code=status.HTTP_201_CREATED,
)
def feedback(
    customer_id: str, case_id: str, run_id: str, payload: FeedbackIn, session: DBSession
):
    case = _case(session, customer_id, case_id)
    _run(session, customer_id, case_id, run_id)
    if payload.category not in FEEDBACK_CATEGORIES:
        raise HTTPException(422, "Unsupported feedback category")
    item = PilotFeedback(
        customer_id=customer_id,
        project_id=case.project_id,
        procurement_case_id=case_id,
        run_id=run_id,
        **payload.model_dump(),
    )
    session.add(item)
    _audit(
        session,
        "feedback_created",
        customer_id=customer_id,
        project_id=case.project_id,
        case_id=case_id,
        run_id=run_id,
    )
    session.commit()
    return {"id": item.id, "category": item.category}


@router.get("/customers/{customer_id}/cases/{case_id}/feedback")
def list_feedback(customer_id: str, case_id: str, session: DBSession):
    _case(session, customer_id, case_id)
    return [
        {
            "id": item.id,
            "run_id": item.run_id,
            "category": item.category,
            "severity": item.severity,
            "comment": item.comment,
            "created_at": item.created_at,
        }
        for item in session.scalars(
            select(PilotFeedback)
            .where(
                PilotFeedback.customer_id == customer_id,
                PilotFeedback.procurement_case_id == case_id,
            )
            .order_by(PilotFeedback.created_at.desc())
        )
    ]


@router.get("/customers/{customer_id}/cases/{case_id}")
def get_case(customer_id: str, case_id: str, session: DBSession):
    case = _case(session, customer_id, case_id)
    runs = session.scalars(
        select(TenderAnalysisRun).where(
            TenderAnalysisRun.customer_id == customer_id,
            TenderAnalysisRun.procurement_case_id == case_id,
        )
    ).all()
    return {
        "id": case.id,
        "customer_id": customer_id,
        "project_id": case.project_id,
        "status": case.status,
        "artifact_key": case.artifact_key,
        "runs": [
            {"id": r.id, "status": r.status, "artifact_key": r.artifact_key}
            for r in runs
        ],
    }


@router.get("/customers/{customer_id}/cases")
def list_cases(customer_id: str, session: DBSession):
    return [
        {
            "id": item.id,
            "project_id": item.project_id,
            "status": item.status,
            "procurement_number": item.procurement_number,
            "artifact_key": item.artifact_key,
        }
        for item in session.scalars(
            select(ProcurementCase)
            .where(ProcurementCase.customer_id == customer_id)
            .order_by(ProcurementCase.created_at.desc())
        )
    ]


@router.get("/summary")
def summary(session: DBSession):
    return {
        "active_runs": session.scalar(
            select(func.count())
            .select_from(TenderAnalysisRun)
            .where(TenderAnalysisRun.status == "analyzing")
        )
        or 0,
        "failed_runs": session.scalar(
            select(func.count())
            .select_from(TenderAnalysisRun)
            .where(TenderAnalysisRun.status == "failed")
        )
        or 0,
        "cases_awaiting_review": session.scalar(
            select(func.count())
            .select_from(ProcurementCase)
            .where(ProcurementCase.status == "operator_review")
        )
        or 0,
        "cases_ready_for_client": session.scalar(
            select(func.count())
            .select_from(ProcurementCase)
            .where(ProcurementCase.status == "client_ready")
        )
        or 0,
        "last_backup_at": None,
        "timestamp": datetime.now(UTC).isoformat(),
    }
