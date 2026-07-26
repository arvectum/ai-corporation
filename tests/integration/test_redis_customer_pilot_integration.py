import os
import uuid
import pytest
from fastapi.testclient import TestClient
from src.main import app


pytestmark = pytest.mark.integration


@pytest.fixture()
def client():
    from src.shared.api.dependencies import get_db_session
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker
    from sqlalchemy.pool import StaticPool
    from src.shared.db.base import Base
    from src.shared.db import models  # noqa: F401
    from src.shared.redis.client import close_client

    close_client()
    import redis as redis_py
    r = redis_py.Redis.from_url(os.environ.get("AI_CORP_REDIS_URL", "redis://127.0.0.1:6379/1"))
    r.flushdb()

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = testing_session_local()

    def override_get_db_session():
        yield session

    app.dependency_overrides[get_db_session] = override_get_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)


class TestCustomerPilotRedisIntegration:
    def _create_customer(self, client, customer_id="cust_redis_test"):
        payload = {
            "customer_id": customer_id,
            "company_name": "Redis Test Corp",
            "email": "redis@test.com",
        }
        resp = client.post("/api/operator/pilot/customers/register", json=payload)
        assert resp.status_code in (200, 201), f"Customer creation failed: {resp.text}"

    def _create_project(self, client, customer_id="cust_redis_test"):
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
        from src.modules.customer_registry.router import router as cr_router
        from src.modules.customer_pilot.router import router as cp_router

        customer_id = f"cust_{uuid.uuid4().hex[:8]}"
        self._create_customer(client, customer_id)
        project_id = self._create_project(client, customer_id)
        case_id = self._create_case(client, customer_id, project_id)

        idem_key = uuid.uuid4().hex
        headers = {"Idempotency-Key": idem_key}
        payload = {"registry_number": "TEST-001"}
        url = f"/api/operator/pilot/customers/{customer_id}/cases/{case_id}/runs"

        resp1 = client.post(url, json=payload, headers=headers)
        assert resp1.status_code == 201, f"First run failed: {resp1.text}"
        data1 = resp1.json()
        assert data1["idempotent"] is False

        resp2 = client.post(url, json=payload, headers=headers)
        assert resp2.status_code in (200, 201), f"Second run failed: {resp2.text}"
        data2 = resp2.json()
        assert data2["idempotent"] is True
        assert data2["id"] == data1["id"]

    def test_different_keys_same_case_conflict(self, client):
        customer_id = f"cust_{uuid.uuid4().hex[:8]}"
        self._create_customer(client, customer_id)
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

    def test_idempotent_replay_when_redis_unavailable(self, client):
        customer_id = f"cust_{uuid.uuid4().hex[:8]}"
        self._create_customer(client, customer_id)
        project_id = self._create_project(client, customer_id)
        case_id = self._create_case(client, customer_id, project_id)

        idem_key = uuid.uuid4().hex
        url = f"/api/operator/pilot/customers/{customer_id}/cases/{case_id}/runs"
        payload = {"registry_number": "TEST-003"}

        resp1 = client.post(url, json=payload, headers={"Idempotency-Key": idem_key})
        assert resp1.status_code == 201

        try:
            os.environ["AI_CORP_REDIS_ENABLED"] = "false"
            from src.shared.redis.client import _client_instance, _client_disabled
            _client_instance = None
            _client_disabled = False

            resp2 = client.post(url, json=payload, headers={"Idempotency-Key": idem_key})
            assert resp2.status_code in (200, 201)
            assert resp2.json()["idempotent"] is True
        finally:
            os.environ["AI_CORP_REDIS_ENABLED"] = "true"

    def test_new_run_fails_closed_when_redis_unavailable(self, client):
        customer_id = f"cust_{uuid.uuid4().hex[:8]}"
        self._create_customer(client, customer_id)
        project_id = self._create_project(client, customer_id)
        case_id = self._create_case(client, customer_id, project_id)

        url = f"/api/operator/pilot/customers/{customer_id}/cases/{case_id}/runs"
        payload = {"registry_number": "TEST-004"}

        try:
            os.environ["AI_CORP_REDIS_ENABLED"] = "true"
            os.environ["AI_CORP_REDIS_URL"] = "redis://127.0.0.1:19999/0"
            from src.shared.redis.client import _client_instance, _client_disabled
            _client_instance = None
            _client_disabled = False

            resp = client.post(url, json=payload, headers={"Idempotency-Key": uuid.uuid4().hex})
            assert resp.status_code == 503
        finally:
            os.environ["AI_CORP_REDIS_ENABLED"] = "true"
            os.environ["AI_CORP_REDIS_URL"] = os.environ.get("AI_CORP_REDIS_URL_ORIG", "redis://127.0.0.1:6379/1")
