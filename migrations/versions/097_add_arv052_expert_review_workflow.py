"""add ARV-052 productized expert review workflow

Revision ID: 097_add_arv052_expert_review
Revises: 096_add_r8_canonical_snapshot_binding
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "097_add_arv052_expert_review"
down_revision: str | Sequence[str] | None = "096_add_r8_canonical_snapshot_binding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("pilot_expert_escalations"):
        op.create_table(
            "pilot_expert_escalations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "customer_id",
                sa.String(64),
                sa.ForeignKey("customer_profiles.customer_id"),
                nullable=False,
            ),
            sa.Column(
                "project_id",
                sa.String(36),
                sa.ForeignKey("pilot_projects.id"),
                nullable=False,
            ),
            sa.Column(
                "procurement_case_id",
                sa.String(36),
                sa.ForeignKey("procurement_cases.id"),
                nullable=False,
            ),
            sa.Column(
                "run_id",
                sa.String(36),
                sa.ForeignKey("tender_analysis_runs.id"),
                nullable=False,
            ),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("severity", sa.String(16), nullable=False),
            sa.Column("trigger_code", sa.String(64), nullable=False),
            sa.Column("status", sa.String(40), nullable=False),
            sa.Column("initiated_by", sa.String(256), nullable=False),
            sa.Column("initiator_role", sa.String(48), nullable=False),
            sa.Column("disputed_finding", sa.Text(), nullable=False),
            sa.Column("evidence_refs", sa.JSON(), nullable=False),
            sa.Column("potential_consequence", sa.Text(), nullable=False),
            sa.Column("requested_decision", sa.Text(), nullable=False),
            sa.Column("required_expert_role", sa.String(48), nullable=False),
            sa.Column("assigned_expert", sa.String(256)),
            sa.Column("assigned_expert_role", sa.String(48)),
            sa.Column("billing_mode", sa.String(32), nullable=False),
            sa.Column("billing_status", sa.String(32), nullable=False),
            sa.Column("billable_amount_rub", sa.Integer(), nullable=False),
            sa.Column("sla_policy_version", sa.String(64), nullable=False),
            sa.Column("target_start_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("decision_code", sa.String(40)),
            sa.Column("decision_rationale", sa.Text()),
            sa.Column("requires_new_run", sa.Boolean(), nullable=False),
            sa.Column("client_comment", sa.Text()),
            sa.Column(
                "feedback_id",
                sa.String(36),
                sa.ForeignKey("pilot_feedback.id"),
            ),
            sa.Column("latest_event_hash", sa.String(64)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True)),
            sa.Column("immutable_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "customer_id",
                "idempotency_key",
                name="uq_pilot_expert_escalation_idempotency",
            ),
        )
        op.create_index(
            "ix_pilot_expert_escalations_customer_case",
            "pilot_expert_escalations",
            ["customer_id", "procurement_case_id"],
        )
        op.create_index(
            "ix_pilot_expert_escalations_project_billing",
            "pilot_expert_escalations",
            ["project_id", "billing_mode"],
        )
        op.create_index(
            "ix_pilot_expert_escalations_status_target",
            "pilot_expert_escalations",
            ["status", "target_start_at"],
        )
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("pilot_expert_escalation_events"):
        op.create_table(
            "pilot_expert_escalation_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "escalation_id",
                sa.String(36),
                sa.ForeignKey("pilot_expert_escalations.id"),
                nullable=False,
            ),
            sa.Column("sequence_no", sa.Integer(), nullable=False),
            sa.Column("customer_id", sa.String(64), nullable=False),
            sa.Column("project_id", sa.String(36), nullable=False),
            sa.Column("procurement_case_id", sa.String(36), nullable=False),
            sa.Column("run_id", sa.String(36), nullable=False),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column("actor", sa.String(256), nullable=False),
            sa.Column("actor_role", sa.String(48), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("previous_event_hash", sa.String(64)),
            sa.Column("event_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "escalation_id",
                "sequence_no",
                name="uq_pilot_expert_escalation_event_sequence",
            ),
        )
        op.create_index(
            "ix_pilot_expert_escalation_events_scope",
            "pilot_expert_escalation_events",
            ["customer_id", "procurement_case_id", "created_at"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("pilot_expert_escalation_events"):
        op.drop_table("pilot_expert_escalation_events")
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("pilot_expert_escalations"):
        op.drop_table("pilot_expert_escalations")
