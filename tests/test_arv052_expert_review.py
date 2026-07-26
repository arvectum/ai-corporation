from sqlalchemy import select

from src.modules.customer_pilot.expert_review_models import (
    PilotExpertEscalationEvent,
)
from src.modules.customer_pilot.models import PilotFeedback, PilotProject, ProcurementCase
from src.modules.customer_registry.models import CustomerProfile
from src.tender_research.models import TenderAnalysisRun


def _scope(session, customer_id: str = "CUST-A") -> tuple[ProcurementCase, TenderAnalysisRun]:
    customer = CustomerProfile(
        customer_id=customer_id,
        legal_name=customer_id,
        customer_status="prospect",
    )
    project = PilotProject(
        customer_id=customer_id,
        name="Expert review pilot",
        internal_slug=f"expert-{customer_id.lower()}",
    )
    session.add_all([customer, project])
    session.flush()
    case = ProcurementCase(
        customer_id=customer_id,
        project_id=project.id,
        procurement_number="0379100000726000101",
        status="operator_review",
        artifact_key=f"case-{customer_id.lower()}",
    )
    session.add(case)
    session.flush()
    run = TenderAnalysisRun(
        registry_number=case.procurement_number,
        status="completed",
        customer_id=customer_id,
        project_id=project.id,
        procurement_case_id=case.id,
        idempotency_key="seed",
        artifact_key=f"run-{customer_id.lower()}",
        source="customer_pilot",
    )
    session.add(run)
    session.flush()
    case.current_run_id = run.id
    session.commit()
    return case, run


def _create(client, case: ProcurementCase, run: TenderAnalysisRun, key: str, **overrides):
    payload = {
        "severity": "sev2",
        "trigger_code": "missing_documentation",
        "initiated_by": "operator@example.test",
        "initiator_role": "operator",
        "disputed_finding": "A material requirement cannot be confirmed.",
        "evidence_refs": ["doc-1#page=4"],
        "potential_consequence": "The participation decision may be wrong.",
        "requested_decision": "Confirm the requirement or require a new run.",
    }
    payload.update(overrides)
    return client.post(
        f"/api/operator/pilot/customers/{case.customer_id}/cases/{case.id}/runs/{run.id}/expert-escalations",
        json=payload,
        headers={"Idempotency-Key": key},
    )


def _assign_start(client, case: ProcurementCase, escalation_id: str):
    base = (
        f"/api/operator/pilot/customers/{case.customer_id}/cases/{case.id}"
        f"/expert-escalations/{escalation_id}"
    )
    assigned = client.post(
        f"{base}/assign",
        json={
            "actor": "lead@example.test",
            "actor_role": "quality_lead",
            "expert": "expert@example.test",
            "expert_role": "procurement_expert",
        },
    )
    assert assigned.status_code == 200, assigned.text
    started = client.post(
        f"{base}/start",
        json={"actor": "expert@example.test", "actor_role": "procurement_expert"},
    )
    assert started.status_code == 200, started.text
    return base


def _resolve_confirmed(client, case: ProcurementCase, escalation_id: str):
    base = _assign_start(client, case, escalation_id)
    response = client.post(
        f"{base}/decision",
        json={
            "actor": "expert@example.test",
            "actor_role": "procurement_expert",
            "decision_code": "CONFIRMED",
            "rationale": "The source confirms the requirement.",
        },
    )
    assert response.status_code == 200, response.text
    return response


def test_sev1_safety_escalation_blocks_delivery_and_is_idempotent(client, session):
    case, run = _scope(session)
    first = _create(
        client,
        case,
        run,
        "safety-1",
        severity="sev1",
        trigger_code="safety_or_tenant_incident",
    )
    duplicate = _create(
        client,
        case,
        run,
        "safety-1",
        severity="sev1",
        trigger_code="safety_or_tenant_incident",
    )
    assert first.status_code == duplicate.status_code == 201
    assert duplicate.json()["idempotent"] is True
    assert first.json()["id"] == duplicate.json()["id"]
    assert first.json()["billing_mode"] == "safety_no_charge"
    assert first.json()["billable_amount_rub"] == 0
    session.refresh(case)
    assert case.status == "expert_review"
    assert (
        client.post(
            f"/api/operator/pilot/customers/{case.customer_id}/cases/{case.id}/client-ready"
        ).status_code
        == 409
    )
    events = client.get(
        f"/api/operator/pilot/customers/{case.customer_id}/cases/{case.id}"
        f"/expert-escalations/{first.json()['id']}/events"
    )
    assert events.status_code == 200
    assert events.json()["chain_valid"] is True
    assert events.json()["events"][0]["event_type"] == "escalation_created"


def test_fourth_blocking_review_requires_paid_addon_approval(client, session):
    case, run = _scope(session)
    for index in range(3):
        created = _create(client, case, run, f"included-{index}")
        assert created.status_code == 201
        assert created.json()["billing_mode"] == "included_pilot"
        _resolve_confirmed(client, case, created.json()["id"])
        session.refresh(case)
        assert case.status == "operator_review"

    fourth = _create(client, case, run, "paid-4")
    assert fourth.status_code == 201
    assert fourth.json()["status"] == "awaiting_commercial_approval"
    assert fourth.json()["billing_mode"] == "paid_addon"
    assert fourth.json()["billing_status"] == "pending_approval"
    assert fourth.json()["billable_amount_rub"] == 3000
    base = (
        f"/api/operator/pilot/customers/{case.customer_id}/cases/{case.id}"
        f"/expert-escalations/{fourth.json()['id']}"
    )
    blocked_assignment = client.post(
        f"{base}/assign",
        json={
            "actor": "lead@example.test",
            "actor_role": "quality_lead",
            "expert": "expert@example.test",
            "expert_role": "procurement_expert",
        },
    )
    assert blocked_assignment.status_code == 409
    approved = client.post(
        f"{base}/commercial-decision",
        json={
            "actor": "commercial@example.test",
            "actor_role": "commercial_manager",
            "decision": "approve",
        },
    )
    assert approved.status_code == 200
    assert approved.json()["billing_status"] == "approved"
    assert (
        client.post(
            f"{base}/assign",
            json={
                "actor": "lead@example.test",
                "actor_role": "quality_lead",
                "expert": "expert@example.test",
                "expert_role": "procurement_expert",
            },
        ).status_code
        == 200
    )


def test_needs_new_run_creates_feedback_and_reopens_analysis(client, session):
    case, run = _scope(session)
    created = _create(client, case, run, "new-run")
    base = _assign_start(client, case, created.json()["id"])
    decision = client.post(
        f"{base}/decision",
        json={
            "actor": "expert@example.test",
            "actor_role": "procurement_expert",
            "decision_code": "NEEDS_NEW_RUN",
            "rationale": "The risk was derived from a superseded document.",
            "feedback_category": "incorrect_risk",
            "expected_value": "Use the amended contract draft.",
            "observed_value": "The initial contract draft was used.",
        },
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "resolved"
    assert decision.json()["requires_new_run"] is True
    assert decision.json()["immutable_at"] is not None
    feedback = session.scalar(
        select(PilotFeedback).where(PilotFeedback.id == decision.json()["feedback_id"])
    )
    assert feedback and feedback.category == "incorrect_risk"
    session.refresh(case)
    assert case.status == "operator_review"
    rerun = client.post(
        f"/api/operator/pilot/customers/{case.customer_id}/cases/{case.id}/runs",
        json={},
        headers={"Idempotency-Key": "rerun-after-expert"},
    )
    assert rerun.status_code == 201
    assert rerun.json()["id"] != run.id


def test_customer_input_round_trip_and_event_tamper_detection(client, session):
    case, run = _scope(session)
    created = _create(client, case, run, "customer-input")
    base = _assign_start(client, case, created.json()["id"])
    waiting = client.post(
        f"{base}/decision",
        json={
            "actor": "expert@example.test",
            "actor_role": "procurement_expert",
            "decision_code": "NEEDS_CUSTOMER_INPUT",
            "rationale": "The delivery address is missing.",
            "client_comment": "Please confirm the delivery address.",
        },
    )
    assert waiting.status_code == 200
    assert waiting.json()["status"] == "awaiting_customer"
    resumed = client.post(
        f"{base}/customer-input",
        json={
            "actor": "operator@example.test",
            "actor_role": "operator",
            "customer_input_summary": "Customer confirmed the address in writing.",
        },
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "assigned"
    assert (
        client.post(
            f"{base}/start",
            json={
                "actor": "expert@example.test",
                "actor_role": "procurement_expert",
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"{base}/decision",
            json={
                "actor": "expert@example.test",
                "actor_role": "procurement_expert",
                "decision_code": "CONFIRMED",
                "rationale": "Customer input resolves the ambiguity.",
            },
        ).status_code
        == 200
    )
    chain = client.get(f"{base}/events")
    assert chain.json()["chain_valid"] is True
    first_event = session.scalar(
        select(PilotExpertEscalationEvent)
        .where(PilotExpertEscalationEvent.escalation_id == created.json()["id"])
        .order_by(PilotExpertEscalationEvent.sequence_no)
    )
    first_event.payload = {"tampered": True}
    session.commit()
    assert client.get(f"{base}/events").json()["chain_valid"] is False


def test_sev3_is_nonblocking_and_cross_tenant_scope_is_hidden(client, session):
    case_a, run_a = _scope(session, "CUST-A")
    case_b, _ = _scope(session, "CUST-B")
    created = _create(client, case_a, run_a, "sev3", severity="sev3")
    assert created.status_code == 201
    session.refresh(case_a)
    assert case_a.status == "operator_review"
    foreign = client.get(
        f"/api/operator/pilot/customers/{case_b.customer_id}/cases/{case_b.id}"
        f"/expert-escalations/{created.json()['id']}"
    )
    assert foreign.status_code == 404
    base = _assign_start(client, case_a, created.json()["id"])
    blocked = client.post(
        f"{base}/decision",
        json={
            "actor": "expert@example.test",
            "actor_role": "procurement_expert",
            "decision_code": "BLOCK_CASE",
            "rationale": "This must first be promoted to a blocking severity.",
            "feedback_category": "other",
        },
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "SEVERITY_PROMOTION_REQUIRED"
