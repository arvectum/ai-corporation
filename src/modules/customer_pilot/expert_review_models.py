from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.db.base import Base, UUIDPrimaryKeyMixin, utcnow


class PilotExpertEscalation(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "pilot_expert_escalations"

    customer_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("customer_profiles.customer_id"), nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pilot_projects.id"), nullable=False
    )
    procurement_case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("procurement_cases.id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tender_analysis_runs.id"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    trigger_code: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    initiated_by: Mapped[str] = mapped_column(String(256), nullable=False)
    initiator_role: Mapped[str] = mapped_column(String(48), nullable=False)
    disputed_finding: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    potential_consequence: Mapped[str] = mapped_column(Text, nullable=False)
    requested_decision: Mapped[str] = mapped_column(Text, nullable=False)
    required_expert_role: Mapped[str] = mapped_column(String(48), nullable=False)
    assigned_expert: Mapped[str | None] = mapped_column(String(256), nullable=True)
    assigned_expert_role: Mapped[str | None] = mapped_column(String(48), nullable=True)
    billing_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    billing_status: Mapped[str] = mapped_column(String(32), nullable=False)
    billable_amount_rub: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sla_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    target_start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    decision_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_new_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    client_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    feedback_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("pilot_feedback.id"), nullable=True
    )
    latest_event_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    immutable_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_pilot_expert_escalation_idempotency",
        ),
        Index(
            "ix_pilot_expert_escalations_customer_case",
            "customer_id",
            "procurement_case_id",
        ),
        Index(
            "ix_pilot_expert_escalations_project_billing",
            "project_id",
            "billing_mode",
        ),
        Index(
            "ix_pilot_expert_escalations_status_target",
            "status",
            "target_start_at",
        ),
    )


class PilotExpertEscalationEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "pilot_expert_escalation_events"

    escalation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("pilot_expert_escalations.id"), nullable=False
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    procurement_case_id: Mapped[str] = mapped_column(String(36), nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    previous_event_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "escalation_id",
            "sequence_no",
            name="uq_pilot_expert_escalation_event_sequence",
        ),
        Index(
            "ix_pilot_expert_escalation_events_scope",
            "customer_id",
            "procurement_case_id",
            "created_at",
        ),
    )
