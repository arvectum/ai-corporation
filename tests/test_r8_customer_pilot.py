from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.modules.customer_pilot import artifact_publisher, artifacts, canonical_snapshot
from src.modules.customer_pilot.models import (
    PilotProject,
    PilotRunResult,
    ProcurementCase,
)
from src.modules.customer_pilot.router import StartIn, start_run
from src.modules.customer_registry.models import CustomerProfile
from src.shared.api.middleware import _is_protected_path
from src.shared.config.settings import Settings
from src.shared.db.base import Base
from src.tender_research.models import (
    ProcurementDocumentChunk,
    ProcurementTender,
    ProcurementTenderDocument,
    TenderAnalysisRun,
)


def _customer(session, customer_id: str) -> None:
    session.add(
        CustomerProfile(
            customer_id=customer_id, legal_name=customer_id, customer_status="prospect"
        )
    )
    session.commit()


def _case(client, customer_id: str, name: str = "Pilot") -> dict:
    project = client.post(
        f"/api/operator/pilot/customers/{customer_id}/projects", json={"name": name}
    ).json()
    response = client.post(
        f"/api/operator/pilot/customers/{customer_id}/projects/{project['id']}/cases",
        json={"procurement_number": "0379100000726000101"},
    )
    assert response.status_code == 201
    return response.json()


def test_two_customers_are_isolated_and_same_procurement_has_distinct_keys(
    client, session
):
    _customer(session, "CUST-A")
    _customer(session, "CUST-B")
    case_a, case_b = _case(client, "CUST-A"), _case(client, "CUST-B")
    assert case_a["id"] != case_b["id"]
    assert case_a["artifact_key"] != case_b["artifact_key"]
    assert (
        client.get(
            f"/api/operator/pilot/customers/CUST-A/cases/{case_b['id']}"
        ).status_code
        == 404
    )


def _trusted_analysis(monkeypatch, root, session):
    config = lambda: type("Config", (), {"data_dir": str(root)})()
    monkeypatch.setattr(artifact_publisher, "load_config", config)
    monkeypatch.setattr(artifacts, "load_config", config)
    monkeypatch.setattr(canonical_snapshot, "load_config", config)
    tender = ProcurementTender(
        source="fixture",
        external_id="r8-real-input",
        registry_number="0379100000726000101",
        title="Поставка кабельной продукции",
    )
    session.add(tender)
    session.flush()
    document = ProcurementTenderDocument(
        tender_id=tender.id,
        file_name="Техническое задание.txt",
        download_status="downloaded",
        text_extraction_status="completed",
        sha256="a" * 64,
    )
    session.add(document)
    session.flush()
    session.add(
        ProcurementDocumentChunk(
            tender_id=tender.id,
            document_id=document.id,
            chunk_index=0,
            text="Поставка кабеля ВВГнг 3х2.5. Количество 100 метров. Срок поставки 10 дней.",
            text_hash="b" * 64,
            char_start=0,
            char_end=78,
            token_estimate=20,
            source_file_name=document.file_name,
        )
    )
    session.commit()


def test_idempotent_start_review_feedback_and_delivery_contract(
    client, session, tmp_path, monkeypatch
):
    _customer(session, "CUST-A")
    _trusted_analysis(monkeypatch, tmp_path, session)
    case = _case(client, "CUST-A")
    url = f"/api/operator/pilot/customers/CUST-A/cases/{case['id']}/runs"
    first = client.post(url, json={}, headers={"Idempotency-Key": "one"})
    again = client.post(url, json={}, headers={"Idempotency-Key": "one"})
    assert first.status_code == 201 and again.status_code == 200
    assert (
        again.json()["idempotent"] is True and first.json()["id"] == again.json()["id"]
    )
    run_id = first.json()["id"]
    assert (
        client.post(
            f"/api/operator/pilot/customers/CUST-A/cases/{case['id']}/runs/{run_id}/complete"
        ).status_code
        == 200
    )
    review = client.post(
        f"/api/operator/pilot/customers/CUST-A/cases/{case['id']}/runs/{run_id}/review",
        json={"reviewer": "operator", "verdict": "approved"},
    )
    assert review.status_code == 409
    published = client.post(
        f"/api/operator/pilot/customers/CUST-A/cases/{case['id']}/runs/{run_id}/artifacts/final-pdf"
    )
    assert published.status_code == 201
    review = client.post(
        f"/api/operator/pilot/customers/CUST-A/cases/{case['id']}/runs/{run_id}/review",
        json={"reviewer": "operator", "verdict": "approved"},
    )
    assert review.status_code == 200 and review.json()["immutable"] is True
    feedback = client.post(
        f"/api/operator/pilot/customers/CUST-A/cases/{case['id']}/feedback?run_id={run_id}",
        json={"category": "report_usability", "severity": "low", "comment": "ok"},
    )
    assert feedback.status_code == 201
    assert (
        client.post(
            f"/api/operator/pilot/customers/CUST-A/cases/{case['id']}/client-ready"
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/operator/pilot/customers/CUST-A/cases/{case['id']}/delivered"
        ).status_code
        == 200
    )


def test_cross_customer_run_and_feedback_are_not_disclosed(client, session):
    _customer(session, "CUST-A")
    _customer(session, "CUST-B")
    case_a, case_b = _case(client, "CUST-A"), _case(client, "CUST-B")
    run = client.post(
        f"/api/operator/pilot/customers/CUST-A/cases/{case_a['id']}/runs",
        json={},
        headers={"Idempotency-Key": "A"},
    ).json()
    response = client.post(
        f"/api/operator/pilot/customers/CUST-B/cases/{case_b['id']}/runs/{run['id']}/complete"
    )
    assert response.status_code == 404


def test_completed_customers_share_no_snapshot_or_binding_namespace(
    client, session, tmp_path, monkeypatch
):
    _customer(session, "CUST-A")
    _customer(session, "CUST-B")
    _trusted_analysis(monkeypatch, tmp_path, session)
    case_a, case_b = _case(client, "CUST-A"), _case(client, "CUST-B")
    run_a = client.post(
        f"/api/operator/pilot/customers/CUST-A/cases/{case_a['id']}/runs",
        json={},
        headers={"Idempotency-Key": "complete-a"},
    ).json()["id"]
    run_b = client.post(
        f"/api/operator/pilot/customers/CUST-B/cases/{case_b['id']}/runs",
        json={},
        headers={"Idempotency-Key": "complete-b"},
    ).json()["id"]
    complete_a = client.post(
        f"/api/operator/pilot/customers/CUST-A/cases/{case_a['id']}/runs/{run_a}/complete"
    )
    complete_b = client.post(
        f"/api/operator/pilot/customers/CUST-B/cases/{case_b['id']}/runs/{run_b}/complete"
    )
    assert complete_a.status_code == complete_b.status_code == 200
    assert (
        client.post(
            f"/api/operator/pilot/customers/CUST-A/cases/{case_a['id']}/runs/{run_a}/complete"
        ).json()["idempotent"]
        is True
    )
    results = session.execute(select(PilotRunResult)).scalars().all()
    by_run = {item.run_id: item for item in results}
    left, right = by_run[run_a], by_run[run_b]
    assert left.source_analysis_run_id != right.source_analysis_run_id
    assert left.canonical_report_storage_key != right.canonical_report_storage_key
    assert left.binding_manifest_storage_key != right.binding_manifest_storage_key
    assert left.is_verified_snapshot_binding and right.is_verified_snapshot_binding


def test_concurrent_different_keys_create_only_one_active_run(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'concurrent.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        customer = CustomerProfile(
            customer_id="CUST-RACE", legal_name="Race", customer_status="prospect"
        )
        project = PilotProject(
            customer_id=customer.customer_id, name="Race", internal_slug="race"
        )
        session.add_all([customer, project])
        session.flush()
        case = ProcurementCase(
            customer_id=customer.customer_id,
            project_id=project.id,
            procurement_number="0379100000726000101",
            artifact_key="c_race",
        )
        session.add(case)
        session.commit()
        case_id = case.id

    def attempt(key):
        with factory() as session:
            try:
                return start_run("CUST-RACE", case_id, StartIn(), session, key)
            except Exception as exc:
                return getattr(exc, "status_code", 500)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["key-a", "key-b"]))
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert 409 in outcomes
    with factory() as session:
        runs = session.scalars(
            select(TenderAnalysisRun).where(
                TenderAnalysisRun.procurement_case_id == case_id
            )
        ).all()
        assert len(runs) == 1 and runs[0].status == "analyzing"


def test_customer_pilot_router_is_inside_existing_operator_auth_boundary():
    assert _is_protected_path("/api/operator/pilot/summary", ("/api",), ("/health",))
    assert "/api" in Settings().pilot_auth_protected_prefixes.split(",")
    assert "/customers" in Settings().pilot_auth_protected_prefixes.split(",")
