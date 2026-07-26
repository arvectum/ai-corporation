import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from fastapi.testclient import TestClient
from src.main import app


pytestmark = pytest.mark.integration


@pytest.fixture()
def client():
    from src.shared.api.dependencies import get_db_session
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from src.shared.db.base import Base
    from src.shared.db import models  # noqa: F401
    from src.shared.redis.client import reset_redis_runtime

    reset_redis_runtime()
    import redis as redis_py
    import os
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

    def test_concurrent_requests_same_idempotency_key(self, client):
        customer_id = f"cust_{uuid.uuid4().hex[:8]}"
        self._create_customer(client, customer_id)
        project_id = self._create_project(client, customer_id)
        case_id = self._create_case(client, customer_id, project_id)

        idem_key = uuid.uuid4().hex
        url = f"/api/operator/pilot/customers/{customer_id}/cases/{case_id}/runs"
        payload = {"registry_number": "TEST-CONC-001"}

        def post_request():
            resp = client.post(url, json=payload, headers={"Idempotency-Key": idem_key})
            return resp.status_code, resp.json()

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(post_request) for _ in range(3)]
            results = [f.result() for f in futures]

        statuses = [r[0] for r in results]
        data_list = [r[1] for r in results]

        assert statuses.count(201) == 1, f"Expected exactly one 201, got {statuses}"
        created = [d for d in data_list if not d.get("idempotent", False)]
        assert len(created) == 1

        ids = {d["id"] for d in data_list if "id" in d}
        assert len(ids) == 1, "All responses must reference the same run id"

    def test_idempotent_replay_when_redis_unavailable(self, client, monkeypatch):
        from src.shared.redis.client import reset_redis_runtime

        customer_id = f"cust_{uuid.uuid4().hex[:8]}"
        self._create_customer(client, customer_id)
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
        from src.shared.redis.client import reset_redis_runtime

        customer_id = f"cust_{uuid.uuid4().hex[:8]}"
        self._create_customer(client, customer_id)
        project_id = self._create_project(client, customer_id)
        case_id = self._create_case(client, customer_id, project_id)

        url = f"/api/operator/pilot/customers/{customer_id}/cases/{case_id}/runs"
        payload = {"registry_number": "TEST-004"}

        monkeypatch.setenv("AI_CORP_REDIS_ENABLED", "true")
        monkeypatch.setenv("AI_CORP_REDIS_URL", "redis://127.0.0.1:19999/0")
        reset_redis_runtime()

        resp = client.post(url, json=payload, headers={"Idempotency-Key": uuid.uuid4().hex})
        assert resp.status_code == 503
