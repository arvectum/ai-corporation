from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from src.modules.customer_pilot.expert_review_models import PilotExpertEscalation
from src.modules.customer_pilot.expert_review_service import (
    ExpertReviewError,
    assign_expert,
    create_escalation,
    decide_commercial_approval,
    get_escalation,
    get_event_chain,
    list_escalations,
    record_expert_decision,
    resume_after_customer_input,
    start_expert_review,
)
from src.shared.api.dependencies import DBSession

router = APIRouter(prefix="/api/operator/pilot", tags=["customer-pilot-expert-review"])


class EscalationCreateIn(BaseModel):
    severity: Literal["sev1", "sev2", "sev3"]
    trigger_code: str = Field(min_length=1, max_length=64)
    initiated_by: str = Field(min_length=1, max_length=256)
    initiator_role: Literal["operator", "customer_operator", "system_integrity"]
    disputed_finding: str = Field(min_length=1, max_length=10000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    potential_consequence: str = Field(min_length=1, max_length=10000)
    requested_decision: str = Field(min_length=1, max_length=4000)
    client_comment: str | None = Field(default=None, max_length=4000)


class CommercialDecisionIn(BaseModel):
    actor: str = Field(min_length=1, max_length=256)
    actor_role: str = Field(min_length=1, max_length=48)
    decision: Literal["approve", "waive"]


class ExpertAssignmentIn(BaseModel):
    actor: str = Field(min_length=1, max_length=256)
    actor_role: str = Field(min_length=1, max_length=48)
    expert: str = Field(min_length=1, max_length=256)
    expert_role: str = Field(min_length=1, max_length=48)


class ActorIn(BaseModel):
    actor: str = Field(min_length=1, max_length=256)
    actor_role: str = Field(min_length=1, max_length=48)


class CustomerInputIn(ActorIn):
    customer_input_summary: str = Field(min_length=1, max_length=10000)


class ExpertDecisionIn(ActorIn):
    decision_code: Literal[
        "CONFIRMED",
        "REJECTED",
        "NEEDS_CUSTOMER_INPUT",
        "NEEDS_NEW_RUN",
        "BLOCK_CASE",
    ]
    rationale: str = Field(min_length=1, max_length=10000)
    client_comment: str | None = Field(default=None, max_length=4000)
    feedback_category: str | None = Field(default=None, max_length=64)
    expected_value: str | None = Field(default=None, max_length=10000)
    observed_value: str | None = Field(default=None, max_length=10000)


def _raise_http(exc: ExpertReviewError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    ) from exc


def _serialize(item: PilotExpertEscalation) -> dict:
    return {
        "id": item.id,
        "customer_id": item.customer_id,
        "project_id": item.project_id,
        "procurement_case_id": item.procurement_case_id,
        "run_id": item.run_id,
        "severity": item.severity,
        "trigger_code": item.trigger_code,
        "status": item.status,
        "initiated_by": item.initiated_by,
        "initiator_role": item.initiator_role,
        "disputed_finding": item.disputed_finding,
        "evidence_refs": item.evidence_refs,
        "potential_consequence": item.potential_consequence,
        "requested_decision": item.requested_decision,
        "required_expert_role": item.required_expert_role,
        "assigned_expert": item.assigned_expert,
        "assigned_expert_role": item.assigned_expert_role,
        "billing_mode": item.billing_mode,
        "billing_status": item.billing_status,
        "billable_amount_rub": item.billable_amount_rub,
        "sla_policy_version": item.sla_policy_version,
        "target_start_at": item.target_start_at,
        "started_at": item.started_at,
        "decision_code": item.decision_code,
        "decision_rationale": item.decision_rationale,
        "requires_new_run": item.requires_new_run,
        "client_comment": item.client_comment,
        "feedback_id": item.feedback_id,
        "latest_event_hash": item.latest_event_hash,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "resolved_at": item.resolved_at,
        "immutable_at": item.immutable_at,
    }


@router.post(
    "/customers/{customer_id}/cases/{case_id}/runs/{run_id}/expert-escalations",
    status_code=status.HTTP_201_CREATED,
)
def create_expert_escalation(
    customer_id: str,
    case_id: str,
    run_id: str,
    payload: EscalationCreateIn,
    session: DBSession,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1),
):
    try:
        item, created = create_escalation(
            session,
            customer_id=customer_id,
            case_id=case_id,
            run_id=run_id,
            idempotency_key=idempotency_key,
            **payload.model_dump(),
        )
    except ExpertReviewError as exc:
        _raise_http(exc)
    return {**_serialize(item), "idempotent": not created}


@router.get("/customers/{customer_id}/cases/{case_id}/expert-escalations")
def get_expert_escalations(customer_id: str, case_id: str, session: DBSession):
    try:
        items = list_escalations(session, customer_id=customer_id, case_id=case_id)
    except ExpertReviewError as exc:
        _raise_http(exc)
    return [_serialize(item) for item in items]


@router.get(
    "/customers/{customer_id}/cases/{case_id}/expert-escalations/{escalation_id}"
)
def get_expert_escalation(
    customer_id: str, case_id: str, escalation_id: str, session: DBSession
):
    try:
        item = get_escalation(
            session,
            customer_id=customer_id,
            case_id=case_id,
            escalation_id=escalation_id,
        )
    except ExpertReviewError as exc:
        _raise_http(exc)
    return _serialize(item)


@router.post(
    "/customers/{customer_id}/cases/{case_id}/expert-escalations/{escalation_id}/commercial-decision"
)
def record_commercial_decision(
    customer_id: str,
    case_id: str,
    escalation_id: str,
    payload: CommercialDecisionIn,
    session: DBSession,
):
    try:
        item = decide_commercial_approval(
            session,
            customer_id=customer_id,
            case_id=case_id,
            escalation_id=escalation_id,
            **payload.model_dump(),
        )
    except ExpertReviewError as exc:
        _raise_http(exc)
    return _serialize(item)


@router.post(
    "/customers/{customer_id}/cases/{case_id}/expert-escalations/{escalation_id}/assign"
)
def assign_escalation_expert(
    customer_id: str,
    case_id: str,
    escalation_id: str,
    payload: ExpertAssignmentIn,
    session: DBSession,
):
    try:
        item = assign_expert(
            session,
            customer_id=customer_id,
            case_id=case_id,
            escalation_id=escalation_id,
            **payload.model_dump(),
        )
    except ExpertReviewError as exc:
        _raise_http(exc)
    return _serialize(item)


@router.post(
    "/customers/{customer_id}/cases/{case_id}/expert-escalations/{escalation_id}/start"
)
def start_escalation_review(
    customer_id: str,
    case_id: str,
    escalation_id: str,
    payload: ActorIn,
    session: DBSession,
):
    try:
        item = start_expert_review(
            session,
            customer_id=customer_id,
            case_id=case_id,
            escalation_id=escalation_id,
            **payload.model_dump(),
        )
    except ExpertReviewError as exc:
        _raise_http(exc)
    return _serialize(item)


@router.post(
    "/customers/{customer_id}/cases/{case_id}/expert-escalations/{escalation_id}/customer-input"
)
def resume_escalation_after_customer_input(
    customer_id: str,
    case_id: str,
    escalation_id: str,
    payload: CustomerInputIn,
    session: DBSession,
):
    try:
        item = resume_after_customer_input(
            session,
            customer_id=customer_id,
            case_id=case_id,
            escalation_id=escalation_id,
            **payload.model_dump(),
        )
    except ExpertReviewError as exc:
        _raise_http(exc)
    return _serialize(item)


@router.post(
    "/customers/{customer_id}/cases/{case_id}/expert-escalations/{escalation_id}/decision"
)
def decide_escalation(
    customer_id: str,
    case_id: str,
    escalation_id: str,
    payload: ExpertDecisionIn,
    session: DBSession,
):
    try:
        item = record_expert_decision(
            session,
            customer_id=customer_id,
            case_id=case_id,
            escalation_id=escalation_id,
            **payload.model_dump(),
        )
    except ExpertReviewError as exc:
        _raise_http(exc)
    return _serialize(item)


@router.get(
    "/customers/{customer_id}/cases/{case_id}/expert-escalations/{escalation_id}/events"
)
def get_escalation_events(
    customer_id: str, case_id: str, escalation_id: str, session: DBSession
):
    try:
        events, chain_valid = get_event_chain(
            session,
            customer_id=customer_id,
            case_id=case_id,
            escalation_id=escalation_id,
        )
    except ExpertReviewError as exc:
        _raise_http(exc)
    return {
        "escalation_id": escalation_id,
        "chain_valid": chain_valid,
        "events": [
            {
                "id": event.id,
                "sequence_no": event.sequence_no,
                "event_type": event.event_type,
                "actor": event.actor,
                "actor_role": event.actor_role,
                "payload": event.payload,
                "previous_event_hash": event.previous_event_hash,
                "event_hash": event.event_hash,
                "created_at": event.created_at,
            }
            for event in events
        ],
    }
