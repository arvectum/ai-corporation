from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.modules.customer_pilot.expert_review_models import (
    PilotExpertEscalation,
    PilotExpertEscalationEvent,
)
from src.modules.customer_pilot.models import (
    PilotAuditEvent,
    PilotFeedback,
    PilotReview,
    ProcurementCase,
)
from src.shared.db.base import utcnow
from src.tender_research.models import TenderAnalysisRun

SEVERITIES = {"sev1", "sev2", "sev3"}
TRIGGER_ROLES = {
    "missing_documentation": "procurement_expert",
    "unreadable_source": "technical_expert",
    "conflicting_documents": "procurement_expert",
    "untraceable_evidence": "quality_lead",
    "participation_decision_impact": "procurement_expert",
    "commercial_terms_outside_profile": "commercial_expert",
    "mandatory_qualification": "legal_expert",
    "unverified_economics": "commercial_expert",
    "critical_low_confidence": "quality_lead",
    "operator_system_disagreement": "quality_lead",
    "safety_or_tenant_incident": "security_expert",
    "confirmed_arvectum_error": "quality_lead",
    "other_complex_case": "procurement_expert",
}
EXPERT_ROLES = {
    "procurement_expert",
    "legal_expert",
    "commercial_expert",
    "technical_expert",
    "security_expert",
    "quality_lead",
}
INITIATOR_ROLES = {"operator", "customer_operator", "system_integrity"}
DECISIONS = {
    "CONFIRMED",
    "REJECTED",
    "NEEDS_CUSTOMER_INPUT",
    "NEEDS_NEW_RUN",
    "BLOCK_CASE",
}
CORRECTIVE_DECISIONS = {"REJECTED", "NEEDS_NEW_RUN", "BLOCK_CASE"}
FEEDBACK_CATEGORIES = {
    "missing_requirement",
    "incorrect_requirement",
    "incorrect_risk",
    "source_mismatch",
    "report_usability",
    "supplier_relevance",
    "other",
}
ACTIVE_STATUSES = {
    "awaiting_commercial_approval",
    "open",
    "assigned",
    "in_review",
    "awaiting_customer",
}
SLA_POLICY_VERSION = "arv052-business-hours-v1"
PILOT_INCLUDED_REVIEW_LIMIT = 3
PAID_REVIEW_AMOUNT_RUB = 3000
MOSCOW = ZoneInfo("Europe/Moscow")
WORKDAY_START = time(9, 0)
WORKDAY_END = time(18, 0)
SLA_BUSINESS_HOURS = {"sev1": 4, "sev2": 9, "sev3": 45}


@dataclass(slots=True)
class ExpertReviewError(Exception):
    status_code: int
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def _is_business_day(value: datetime) -> bool:
    return value.weekday() < 5


def _next_business_start(value: datetime) -> datetime:
    candidate = value.astimezone(MOSCOW)
    while True:
        if not _is_business_day(candidate):
            candidate = datetime.combine(
                (candidate + timedelta(days=1)).date(), WORKDAY_START, tzinfo=MOSCOW
            )
            continue
        if candidate.time() < WORKDAY_START:
            return datetime.combine(candidate.date(), WORKDAY_START, tzinfo=MOSCOW)
        if candidate.time() >= WORKDAY_END:
            candidate = datetime.combine(
                (candidate + timedelta(days=1)).date(), WORKDAY_START, tzinfo=MOSCOW
            )
            continue
        return candidate


def calculate_target_start(opened_at: datetime, severity: str) -> datetime:
    if severity not in SLA_BUSINESS_HOURS:
        raise ExpertReviewError(422, "UNSUPPORTED_SEVERITY", "Unsupported severity")
    remaining = SLA_BUSINESS_HOURS[severity] * 60 * 60
    cursor = _next_business_start(opened_at)
    while remaining > 0:
        end = datetime.combine(cursor.date(), WORKDAY_END, tzinfo=MOSCOW)
        available = max(0, int((end - cursor).total_seconds()))
        if remaining <= available:
            return (cursor + timedelta(seconds=remaining)).astimezone(UTC)
        remaining -= available
        cursor = _next_business_start(end + timedelta(seconds=1))
    return cursor.astimezone(UTC)


def _event_digest(
    *,
    escalation_id: str,
    sequence_no: int,
    event_type: str,
    actor: str,
    actor_role: str,
    payload: dict,
    previous_event_hash: str | None,
) -> str:
    canonical = json.dumps(
        {
            "escalation_id": escalation_id,
            "sequence_no": sequence_no,
            "event_type": event_type,
            "actor": actor,
            "actor_role": actor_role,
            "payload": payload,
            "previous_event_hash": previous_event_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _append_event(
    session: Session,
    escalation: PilotExpertEscalation,
    *,
    event_type: str,
    actor: str,
    actor_role: str,
    payload: dict | None = None,
) -> PilotExpertEscalationEvent:
    event_payload = payload or {}
    sequence_no = (
        session.scalar(
            select(func.max(PilotExpertEscalationEvent.sequence_no)).where(
                PilotExpertEscalationEvent.escalation_id == escalation.id
            )
        )
        or 0
    ) + 1
    previous = escalation.latest_event_hash
    event_hash = _event_digest(
        escalation_id=escalation.id,
        sequence_no=sequence_no,
        event_type=event_type,
        actor=actor,
        actor_role=actor_role,
        payload=event_payload,
        previous_event_hash=previous,
    )
    event = PilotExpertEscalationEvent(
        escalation_id=escalation.id,
        sequence_no=sequence_no,
        customer_id=escalation.customer_id,
        project_id=escalation.project_id,
        procurement_case_id=escalation.procurement_case_id,
        run_id=escalation.run_id,
        event_type=event_type,
        actor=actor,
        actor_role=actor_role,
        payload=event_payload,
        previous_event_hash=previous,
        event_hash=event_hash,
    )
    session.add(event)
    escalation.latest_event_hash = event_hash
    escalation.updated_at = utcnow()
    return event


def _audit(
    session: Session,
    escalation: PilotExpertEscalation,
    event_type: str,
    payload: dict | None = None,
) -> None:
    session.add(
        PilotAuditEvent(
            customer_id=escalation.customer_id,
            project_id=escalation.project_id,
            procurement_case_id=escalation.procurement_case_id,
            run_id=escalation.run_id,
            event_type=event_type,
            payload={"escalation_id": escalation.id, **(payload or {})},
        )
    )


def _case_and_run(
    session: Session, customer_id: str, case_id: str, run_id: str
) -> tuple[ProcurementCase, TenderAnalysisRun]:
    case = session.scalar(
        select(ProcurementCase)
        .where(
            ProcurementCase.id == case_id,
            ProcurementCase.customer_id == customer_id,
        )
        .with_for_update()
    )
    if not case:
        raise ExpertReviewError(404, "CASE_NOT_FOUND", "Procurement case not found")
    run = session.scalar(
        select(TenderAnalysisRun).where(
            TenderAnalysisRun.id == run_id,
            TenderAnalysisRun.customer_id == customer_id,
            TenderAnalysisRun.procurement_case_id == case_id,
        )
    )
    if not run:
        raise ExpertReviewError(404, "RUN_NOT_FOUND", "Analysis run not found")
    if case.current_run_id != run.id:
        raise ExpertReviewError(409, "RUN_NOT_CURRENT", "Analysis run is no longer current")
    return case, run


def _scoped_escalation(
    session: Session,
    customer_id: str,
    case_id: str,
    escalation_id: str,
    *,
    lock: bool = False,
) -> PilotExpertEscalation:
    statement = select(PilotExpertEscalation).where(
        PilotExpertEscalation.id == escalation_id,
        PilotExpertEscalation.customer_id == customer_id,
        PilotExpertEscalation.procurement_case_id == case_id,
    )
    if lock:
        statement = statement.with_for_update()
    escalation = session.scalar(statement)
    if not escalation:
        raise ExpertReviewError(404, "ESCALATION_NOT_FOUND", "Escalation not found")
    return escalation


def _included_review_count(session: Session, project_id: str) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(PilotExpertEscalation)
            .where(
                PilotExpertEscalation.project_id == project_id,
                PilotExpertEscalation.severity.in_({"sev1", "sev2"}),
                PilotExpertEscalation.billing_mode == "included_pilot",
            )
        )
        or 0
    )


def _billing_for_new_escalation(
    session: Session, project_id: str, severity: str, trigger_code: str
) -> tuple[str, str, int, str]:
    if trigger_code in {"safety_or_tenant_incident", "confirmed_arvectum_error"}:
        return "safety_no_charge", "not_chargeable", 0, "open"
    if severity == "sev3":
        return "included_pilot", "included", 0, "open"
    if _included_review_count(session, project_id) < PILOT_INCLUDED_REVIEW_LIMIT:
        return "included_pilot", "included", 0, "open"
    return (
        "paid_addon",
        "pending_approval",
        PAID_REVIEW_AMOUNT_RUB,
        "awaiting_commercial_approval",
    )


def create_escalation(
    session: Session,
    *,
    customer_id: str,
    case_id: str,
    run_id: str,
    idempotency_key: str,
    severity: str,
    trigger_code: str,
    initiated_by: str,
    initiator_role: str,
    disputed_finding: str,
    evidence_refs: list[str],
    potential_consequence: str,
    requested_decision: str,
    client_comment: str | None,
) -> tuple[PilotExpertEscalation, bool]:
    existing = session.scalar(
        select(PilotExpertEscalation).where(
            PilotExpertEscalation.customer_id == customer_id,
            PilotExpertEscalation.idempotency_key == idempotency_key,
        )
    )
    if existing:
        if existing.procurement_case_id != case_id or existing.run_id != run_id:
            raise ExpertReviewError(
                409,
                "IDEMPOTENCY_SCOPE_CONFLICT",
                "Idempotency key is already used for another scope",
            )
        return existing, False
    if severity not in SEVERITIES:
        raise ExpertReviewError(422, "UNSUPPORTED_SEVERITY", "Unsupported severity")
    if trigger_code not in TRIGGER_ROLES:
        raise ExpertReviewError(422, "UNSUPPORTED_TRIGGER", "Unsupported trigger code")
    if initiator_role not in INITIATOR_ROLES:
        raise ExpertReviewError(
            422, "UNSUPPORTED_INITIATOR_ROLE", "Unsupported initiator role"
        )
    case, run = _case_and_run(session, customer_id, case_id, run_id)
    if case.status != "operator_review" or run.status != "completed":
        raise ExpertReviewError(
            409,
            "ESCALATION_NOT_PERMITTED",
            "Escalation requires the current completed run in operator review",
        )
    if session.scalar(select(PilotReview).where(PilotReview.run_id == run_id)):
        raise ExpertReviewError(
            409,
            "REVIEW_ALREADY_IMMUTABLE",
            "Create a new run instead of escalating an immutable operator review",
        )
    billing_mode, billing_status, amount, workflow_status = _billing_for_new_escalation(
        session, case.project_id, severity, trigger_code
    )
    opened_at = utcnow()
    escalation = PilotExpertEscalation(
        customer_id=customer_id,
        project_id=case.project_id,
        procurement_case_id=case_id,
        run_id=run_id,
        idempotency_key=idempotency_key,
        severity=severity,
        trigger_code=trigger_code,
        status=workflow_status,
        initiated_by=initiated_by.strip(),
        initiator_role=initiator_role,
        disputed_finding=disputed_finding.strip(),
        evidence_refs=evidence_refs,
        potential_consequence=potential_consequence.strip(),
        requested_decision=requested_decision.strip(),
        required_expert_role=TRIGGER_ROLES[trigger_code],
        billing_mode=billing_mode,
        billing_status=billing_status,
        billable_amount_rub=amount,
        sla_policy_version=SLA_POLICY_VERSION,
        target_start_at=calculate_target_start(opened_at, severity),
        client_comment=client_comment,
        created_at=opened_at,
        updated_at=opened_at,
    )
    session.add(escalation)
    session.flush()
    if severity in {"sev1", "sev2"}:
        case.status = "expert_review"
        case.updated_at = opened_at
    _append_event(
        session,
        escalation,
        event_type="escalation_created",
        actor=initiated_by,
        actor_role=initiator_role,
        payload={
            "severity": severity,
            "trigger_code": trigger_code,
            "required_expert_role": escalation.required_expert_role,
            "billing_mode": billing_mode,
            "billing_status": billing_status,
            "billable_amount_rub": amount,
            "target_start_at": escalation.target_start_at.isoformat(),
        },
    )
    _audit(
        session,
        escalation,
        "expert_escalation_created",
        {"severity": severity, "trigger_code": trigger_code},
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        duplicate = session.scalar(
            select(PilotExpertEscalation).where(
                PilotExpertEscalation.customer_id == customer_id,
                PilotExpertEscalation.idempotency_key == idempotency_key,
            )
        )
        if (
            duplicate
            and duplicate.procurement_case_id == case_id
            and duplicate.run_id == run_id
        ):
            return duplicate, False
        raise ExpertReviewError(
            409, "CONCURRENT_ESCALATION_CONFLICT", "Concurrent escalation was rejected"
        ) from exc
    return escalation, True


def decide_commercial_approval(
    session: Session,
    *,
    customer_id: str,
    case_id: str,
    escalation_id: str,
    actor: str,
    actor_role: str,
    decision: str,
) -> PilotExpertEscalation:
    escalation = _scoped_escalation(
        session, customer_id, case_id, escalation_id, lock=True
    )
    if escalation.status != "awaiting_commercial_approval":
        raise ExpertReviewError(
            409,
            "COMMERCIAL_DECISION_NOT_PERMITTED",
            "Escalation is not awaiting commercial approval",
        )
    if decision == "approve":
        escalation.billing_status = "approved"
    elif decision == "waive":
        escalation.billing_mode = "waived"
        escalation.billing_status = "waived"
        escalation.billable_amount_rub = 0
    else:
        raise ExpertReviewError(
            422, "UNSUPPORTED_COMMERCIAL_DECISION", "Unsupported commercial decision"
        )
    escalation.status = "open"
    _append_event(
        session,
        escalation,
        event_type="commercial_decision_recorded",
        actor=actor,
        actor_role=actor_role,
        payload={
            "decision": decision,
            "billing_mode": escalation.billing_mode,
            "billing_status": escalation.billing_status,
            "billable_amount_rub": escalation.billable_amount_rub,
        },
    )
    _audit(session, escalation, "expert_escalation_commercial_decision")
    session.commit()
    return escalation


def assign_expert(
    session: Session,
    *,
    customer_id: str,
    case_id: str,
    escalation_id: str,
    actor: str,
    actor_role: str,
    expert: str,
    expert_role: str,
) -> PilotExpertEscalation:
    escalation = _scoped_escalation(
        session, customer_id, case_id, escalation_id, lock=True
    )
    if escalation.status not in {"open", "assigned"}:
        raise ExpertReviewError(
            409, "ASSIGNMENT_NOT_PERMITTED", "Escalation cannot be assigned now"
        )
    if escalation.billing_status == "pending_approval":
        raise ExpertReviewError(
            409,
            "COMMERCIAL_APPROVAL_REQUIRED",
            "Paid expert review requires explicit commercial approval or waiver",
        )
    if expert_role not in EXPERT_ROLES:
        raise ExpertReviewError(422, "UNSUPPORTED_EXPERT_ROLE", "Unsupported expert role")
    if expert_role not in {escalation.required_expert_role, "quality_lead"}:
        raise ExpertReviewError(
            409,
            "EXPERT_ROLE_MISMATCH",
            f"Required expert role is {escalation.required_expert_role}",
        )
    escalation.assigned_expert = expert.strip()
    escalation.assigned_expert_role = expert_role
    escalation.status = "assigned"
    _append_event(
        session,
        escalation,
        event_type="expert_assigned",
        actor=actor,
        actor_role=actor_role,
        payload={"expert": escalation.assigned_expert, "expert_role": expert_role},
    )
    _audit(session, escalation, "expert_escalation_assigned")
    session.commit()
    return escalation


def start_expert_review(
    session: Session,
    *,
    customer_id: str,
    case_id: str,
    escalation_id: str,
    actor: str,
    actor_role: str,
) -> PilotExpertEscalation:
    escalation = _scoped_escalation(
        session, customer_id, case_id, escalation_id, lock=True
    )
    if escalation.status != "assigned" or not escalation.assigned_expert:
        raise ExpertReviewError(
            409, "START_NOT_PERMITTED", "An assigned expert is required"
        )
    escalation.status = "in_review"
    escalation.started_at = escalation.started_at or utcnow()
    _append_event(
        session,
        escalation,
        event_type="expert_review_started",
        actor=actor,
        actor_role=actor_role,
        payload={"started_at": escalation.started_at.isoformat()},
    )
    _audit(session, escalation, "expert_escalation_started")
    session.commit()
    return escalation


def resume_after_customer_input(
    session: Session,
    *,
    customer_id: str,
    case_id: str,
    escalation_id: str,
    actor: str,
    actor_role: str,
    customer_input_summary: str,
) -> PilotExpertEscalation:
    escalation = _scoped_escalation(
        session, customer_id, case_id, escalation_id, lock=True
    )
    if escalation.status != "awaiting_customer":
        raise ExpertReviewError(
            409, "RESUME_NOT_PERMITTED", "Escalation is not awaiting customer input"
        )
    escalation.status = "assigned"
    _append_event(
        session,
        escalation,
        event_type="customer_input_received",
        actor=actor,
        actor_role=actor_role,
        payload={"summary": customer_input_summary.strip()},
    )
    _audit(session, escalation, "expert_escalation_customer_input_received")
    session.commit()
    return escalation


def _release_case_if_unblocked(
    session: Session, escalation: PilotExpertEscalation
) -> None:
    session.flush()
    blocking = session.scalar(
        select(func.count())
        .select_from(PilotExpertEscalation)
        .where(
            PilotExpertEscalation.procurement_case_id
            == escalation.procurement_case_id,
            PilotExpertEscalation.id != escalation.id,
            PilotExpertEscalation.severity.in_({"sev1", "sev2"}),
            PilotExpertEscalation.status.in_(ACTIVE_STATUSES),
        )
    )
    if blocking:
        return
    case = session.scalar(
        select(ProcurementCase)
        .where(
            ProcurementCase.id == escalation.procurement_case_id,
            ProcurementCase.customer_id == escalation.customer_id,
        )
        .with_for_update()
    )
    if case and case.status == "expert_review":
        case.status = "operator_review"
        case.updated_at = utcnow()


def record_expert_decision(
    session: Session,
    *,
    customer_id: str,
    case_id: str,
    escalation_id: str,
    actor: str,
    actor_role: str,
    decision_code: str,
    rationale: str,
    client_comment: str | None,
    feedback_category: str | None,
    expected_value: str | None,
    observed_value: str | None,
) -> PilotExpertEscalation:
    escalation = _scoped_escalation(
        session, customer_id, case_id, escalation_id, lock=True
    )
    if escalation.status != "in_review":
        raise ExpertReviewError(
            409, "DECISION_NOT_PERMITTED", "Expert review must be in progress"
        )
    if decision_code not in DECISIONS:
        raise ExpertReviewError(422, "UNSUPPORTED_DECISION", "Unsupported decision code")
    if escalation.severity == "sev3" and decision_code in {
        "NEEDS_NEW_RUN",
        "BLOCK_CASE",
    }:
        raise ExpertReviewError(
            409,
            "SEVERITY_PROMOTION_REQUIRED",
            "Promote the escalation to Sev-1 or Sev-2 before blocking or requiring a new run",
        )
    if (
        decision_code in CORRECTIVE_DECISIONS
        and feedback_category not in FEEDBACK_CATEGORIES
    ):
        raise ExpertReviewError(
            422,
            "FEEDBACK_REQUIRED",
            "A supported feedback category is required for a corrective decision",
        )
    if decision_code == "NEEDS_CUSTOMER_INPUT":
        escalation.status = "awaiting_customer"
        escalation.decision_code = decision_code
        escalation.decision_rationale = rationale.strip()
        escalation.client_comment = client_comment
        _append_event(
            session,
            escalation,
            event_type="customer_input_requested",
            actor=actor,
            actor_role=actor_role,
            payload={"decision_code": decision_code, "client_comment": client_comment},
        )
        _audit(session, escalation, "expert_escalation_customer_input_requested")
        session.commit()
        return escalation

    now = utcnow()
    feedback = None
    if decision_code in CORRECTIVE_DECISIONS:
        feedback = PilotFeedback(
            customer_id=escalation.customer_id,
            project_id=escalation.project_id,
            procurement_case_id=escalation.procurement_case_id,
            run_id=escalation.run_id,
            category=feedback_category,
            severity={"sev1": "critical", "sev2": "high", "sev3": "low"}[
                escalation.severity
            ],
            expected_value=expected_value,
            observed_value=observed_value,
            comment=rationale.strip(),
        )
        session.add(feedback)
        session.flush()
        escalation.feedback_id = feedback.id
    escalation.status = "blocked" if decision_code == "BLOCK_CASE" else "resolved"
    escalation.decision_code = decision_code
    escalation.decision_rationale = rationale.strip()
    escalation.requires_new_run = decision_code == "NEEDS_NEW_RUN"
    escalation.client_comment = client_comment
    escalation.resolved_at = now
    escalation.immutable_at = now
    _append_event(
        session,
        escalation,
        event_type="expert_decision_recorded",
        actor=actor,
        actor_role=actor_role,
        payload={
            "decision_code": decision_code,
            "feedback_id": feedback.id if feedback else None,
            "requires_new_run": escalation.requires_new_run,
            "client_comment": client_comment,
        },
    )
    _audit(
        session,
        escalation,
        "expert_escalation_resolved",
        {"decision_code": decision_code, "feedback_id": escalation.feedback_id},
    )
    if decision_code == "BLOCK_CASE":
        case = session.scalar(
            select(ProcurementCase)
            .where(
                ProcurementCase.id == escalation.procurement_case_id,
                ProcurementCase.customer_id == escalation.customer_id,
            )
            .with_for_update()
        )
        if case:
            case.status = "archived"
            case.updated_at = now
    else:
        _release_case_if_unblocked(session, escalation)
    session.commit()
    return escalation


def list_escalations(
    session: Session, *, customer_id: str, case_id: str
) -> list[PilotExpertEscalation]:
    case = session.scalar(
        select(ProcurementCase).where(
            ProcurementCase.id == case_id,
            ProcurementCase.customer_id == customer_id,
        )
    )
    if not case:
        raise ExpertReviewError(404, "CASE_NOT_FOUND", "Procurement case not found")
    return list(
        session.scalars(
            select(PilotExpertEscalation)
            .where(
                PilotExpertEscalation.customer_id == customer_id,
                PilotExpertEscalation.procurement_case_id == case_id,
            )
            .order_by(PilotExpertEscalation.created_at.desc())
        )
    )


def get_escalation(
    session: Session, *, customer_id: str, case_id: str, escalation_id: str
) -> PilotExpertEscalation:
    return _scoped_escalation(session, customer_id, case_id, escalation_id)


def get_event_chain(
    session: Session, *, customer_id: str, case_id: str, escalation_id: str
) -> tuple[list[PilotExpertEscalationEvent], bool]:
    escalation = _scoped_escalation(session, customer_id, case_id, escalation_id)
    events = list(
        session.scalars(
            select(PilotExpertEscalationEvent)
            .where(PilotExpertEscalationEvent.escalation_id == escalation.id)
            .order_by(PilotExpertEscalationEvent.sequence_no)
        )
    )
    previous = None
    valid = bool(events)
    for expected_sequence, event in enumerate(events, start=1):
        expected_hash = _event_digest(
            escalation_id=event.escalation_id,
            sequence_no=event.sequence_no,
            event_type=event.event_type,
            actor=event.actor,
            actor_role=event.actor_role,
            payload=event.payload,
            previous_event_hash=event.previous_event_hash,
        )
        if (
            event.sequence_no != expected_sequence
            or event.previous_event_hash != previous
            or event.event_hash != expected_hash
        ):
            valid = False
        previous = event.event_hash
    if previous != escalation.latest_event_hash:
        valid = False
    return events, valid
