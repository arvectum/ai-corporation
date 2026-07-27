import os
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.main import app
from src.shared.api.dependencies import get_db_session
from src.shared.db import models  # noqa: F401
from src.shared.db.base import Base
from src.shared.redis.client import reset_redis_runtime

pytestmark = pytest.mark.integration


@pytest.fixture()
def fs_db():
    db_path = f"/tmp/test_arv007_{uuid.uuid4().hex}.db"
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.commit()
    Base.metadata.create_all(engine)
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield engine, testing_session_local, db_path
    engine.dispose()
    if os.path.exists(db_path):
        os.unlink(db_path)


@pytest.fixture()
def client(fs_db):
    _, testing_session_local, _ = fs_db

    def override_get_db_session():
        session = testing_session_local()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db_session] = override_get_db_session
    reset_redis_runtime()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class TestCustomerPilotRedisIntegration:
    def _create_customer(self, client):
        payload = {"legal_name": f"Redis Test Corp {uuid.uuid4().hex[:8]}"}
        resp = client.post("/customers", json=payload)
        assert resp.status_code == 201, f"Customer creation failed: {resp.text}"
        return resp.json()["customer_id"]

    def _create_project(self, client, customer_id):
        resp = client.post(
            f"/api/operator/pilot/customers/{customer_id}/projects",
            json={"name": "Redis Test Project"},
        )
        assert resp.status_code in (200, 201), f"Project creation failed: {resp.text}"
        return resp.json()["id"]

    def _create_case(self, client, customer_id, project_id):
        resp = client.post(
            f"/api/operator/pilot/customers/{customer_id}/projects/{project_id}/cases",
            json={"procurement_number": "TEST-REDIS-001"},
        )
        assert resp.status_code in (200, 201), f"Case creation failed: {resp.text}"
        return resp.json()["id"]

    def test_concurrent_same_key_one_run_created(self, client):
        customer_id = self._create_customer(client)
        project_id = self._create_project(client, customer_id)
        case_id = self._create_case(client, customer_id, project_id)

        idem_key = uuid.uuid4().hex
        headers = {"Idempotency-Key": idem_key}
        payload = {"registry_number": "TEST-001"}
        url = f"/api/operator/pilot/customers/{customer_id}/cases/{case_id}/runs"

        resp1 = client.post(url, json=payload, headers=headers)
        assert resp1.status_code == 201
        data1 = resp1.json()
        assert data1["idempotent"] is False

        resp2 = client.post(url, json=payload, headers=headers)
        assert resp2.status_code in (200, 201)
        data2 = resp2.json()
        assert data2["idempotent"] is True
        assert data2["id"] == data1["id"]

    def test_different_keys_same_case_conflict(self, client):
        customer_id = self._create_customer(client)
        project_id = self._create_project(client, customer_id)
        case_id = self._create_case(client, customer_id, project_id)

        url = f"/api/operator/pilot/customers/{customer_id}/cases/{case_id}/runs"
        payload = {"registry_number": "TEST-002"}

        resp1 = client.post(
            url, json=payload,
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert resp1.status_code == 201

        resp2 = client.post(
            url, json=payload,
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        assert resp2.status_code == 409

    @pytest.mark.parametrize("_", range(3))
    def test_concurrent_requests_same_idempotency_key(self, _, fs_db, client):
        engine, testing_session_local, _ = fs_db
        customer_id = self._create_customer(client)
        project_id = self._create_project(client, customer_id)
        case_id = self._create_case(client, customer_id, project_id)

        idem_key = uuid.uuid4().hex
        url = f"/api/operator/pilot/customers/{customer_id}/cases/{case_id}/runs"
        payload = {"registry_number": f"TEST-CONC-{uuid.uuid4().hex[:8]}"}
        barrier = threading.Barrier(3)
        results = []
        exc_info = None

        def post_request():
            nonlocal exc_info
            try:
                with TestClient(app) as tc:
                    barrier.wait()
                    resp = tc.post(url, json=payload, headers={"Idempotency-Key": idem_key})
                    results.append((resp.status_code, resp.json()))
            except Exception as exc:  # noqa: BLE001
                exc_info = exc

        def override_get_db_session():
            sess = testing_session_local()
            try:
                yield sess
            finally:
                sess.close()

        app.dependency_overrides[get_db_session] = override_get_db_session
        try:
            threads = [threading.Thread(target=post_request) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert exc_info is None, f"Worker thread raised: {exc_info}"
            assert len(results) == 3
            statuses = [r[0] for r in results]
            data_list = [r[1] for r in results]

            # exactly one 201, rest 200 — no 409
            assert statuses.count(201) == 1, f"Expected exactly one 201, got {statuses}"
            assert statuses.count(200) == 2, f"Expected exactly two 200, got {statuses}"

            # all responses carry required fields
            for d in data_list:
                assert "id" in d, f"Response missing run id: {d}"
                assert "status" in d, f"Response missing status: {d}"
                assert "idempotent" in d, f"Response missing idempotent: {d}"

            # exactly one idempotent=false, rest idempotent=true
            idempotent_flags = [d["idempotent"] for d in data_list]
            assert idempotent_flags.count(False) == 1, f"Expected exactly one idempotent=False, got {idempotent_flags}"
            assert idempotent_flags.count(True) == 2, f"Expected exactly two idempotent=True, got {idempotent_flags}"

            # all run ids are the same
            ids = [d["id"] for d in data_list]
            assert len(set(ids)) == 1, f"All responses must reference the same run id, got {ids}"

            expected_run_id = ids[0]

            # DB confirms exactly one row
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT count(*) FROM tender_analysis_runs WHERE procurement_case_id = :cid AND idempotency_key = :ik"),
                    {"cid": case_id, "ik": idem_key},
                ).scalar()
            assert row == 1, f"Expected exactly 1 run in DB, got {row}"

            # ProcurementCase.current_run_id matches
            with engine.connect() as conn:
                current_id = conn.execute(
                    text("SELECT current_run_id FROM procurement_cases WHERE id = :cid"),
                    {"cid": case_id},
                ).scalar()
            assert current_id == expected_run_id, f"current_run_id {current_id} != run id {expected_run_id}"
        finally:
            app.dependency_overrides.clear()

    def test_idempotent_replay_when_redis_unavailable(self, client, monkeypatch):
        customer_id = self._create_customer(client)
        project_id = self._create_project(client, customer_id)
        case_id = self._create_case(client, customer_id, project_id)

        idem_key = uuid.uuid4().hex
        url = f"/api/operator/pilot/customers/{customer_id}/cases/{case_id}/runs"
        payload = {"registry_number": "TEST-003"}

        resp1 = client.post(url, json=payload, headers={"Idempotency-Key": idem_key})
        assert resp1.status_code == 201

        monkeypatch.setenv("AI_CORP_REDIS_ENABLED", "false")
        reset_redis_runtime()

        resp2 = client.post(url, json=payload, headers={"Idempotency-Key": idem_key})
        assert resp2.status_code in (200, 201)
        assert resp2.json()["idempotent"] is True

    def test_new_run_fails_closed_when_redis_unavailable(self, client, monkeypatch):
        customer_id = self._create_customer(client)
        project_id = self._create_project(client, customer_id)
        case_id = self._create_case(client, customer_id, project_id)

        url = f"/api/operator/pilot/customers/{customer_id}/cases/{case_id}/runs"
        payload = {"registry_number": "TEST-004"}

        monkeypatch.setenv("AI_CORP_REDIS_ENABLED", "true")
        monkeypatch.setenv("AI_CORP_REDIS_URL", "redis://127.0.0.1:19999/0")
        reset_redis_runtime()

        resp = client.post(url, json=payload, headers={"Idempotency-Key": uuid.uuid4().hex})
        assert resp.status_code == 503
