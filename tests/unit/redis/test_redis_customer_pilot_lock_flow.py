from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from sqlalchemy import text

from src.shared.redis.errors import RedisLockTimeoutError, RedisUnavailableError


class TestStartRunLockFlow:
    def test_first_acquire_creates_run(self, client, session):
        customer_id = "cust_lock_test"
        session.execute(
            text(
                "INSERT INTO customer_profiles (id, customer_id, legal_name, customer_status, created_at, updated_at) VALUES (:id, :cid, :name, 'prospect', :now, :now)"
            ),
            {"id": uuid.uuid4().hex, "cid": customer_id, "name": customer_id, "now": datetime.now(UTC)},
        )
        session.commit()

        project = client.post(
            f"/api/operator/pilot/customers/{customer_id}/projects",
            json={"name": "Lock Test"},
        ).json()
        case = client.post(
            f"/api/operator/pilot/customers/{customer_id}/projects/{project['id']}/cases",
            json={"procurement_number": "LOCK-TEST-001"},
        ).json()

        idem_key = uuid.uuid4().hex
        resp = client.post(
            f"/api/operator/pilot/customers/{customer_id}/cases/{case['id']}/runs",
            json={"registry_number": "RUN-UNIT-001"},
            headers={"Idempotency-Key": idem_key},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["idempotent"] is False
        assert data["status"] == "analyzing"
        assert "id" in data

    def test_existing_pg_run_returns_idempotent_before_redis(self, client, session):
        customer_id = "cust_lock_replay"
        session.execute(
            text(
                "INSERT INTO customer_profiles (id, customer_id, legal_name, customer_status, created_at, updated_at) VALUES (:id, :cid, :name, 'prospect', :now, :now)"
            ),
            {"id": uuid.uuid4().hex, "cid": customer_id, "name": customer_id, "now": datetime.now(UTC)},
        )
        session.commit()

        project = client.post(
            f"/api/operator/pilot/customers/{customer_id}/projects",
            json={"name": "Replay Test"},
        ).json()
        case = client.post(
            f"/api/operator/pilot/customers/{customer_id}/projects/{project['id']}/cases",
            json={"procurement_number": "LOCK-TEST-002"},
        ).json()

        idem_key = uuid.uuid4().hex
        first = client.post(
            f"/api/operator/pilot/customers/{customer_id}/cases/{case['id']}/runs",
            json={"registry_number": "RUN-UNIT-002"},
            headers={"Idempotency-Key": idem_key},
        )
        assert first.status_code == 201

        with patch("src.modules.customer_pilot.router.redis_acquire_lock") as mock_lock:
            second = client.post(
                f"/api/operator/pilot/customers/{customer_id}/cases/{case['id']}/runs",
                json={"registry_number": "RUN-UNIT-002"},
                headers={"Idempotency-Key": idem_key},
            )
            mock_lock.assert_not_called()

        assert second.status_code == 200
        assert second.json()["idempotent"] is True
        assert second.json()["id"] == first.json()["id"]

    def test_lock_timeout_with_pg_run_returns_idempotent(self, client, session):
        customer_id = "cust_timeout_replay"
        session.execute(
            text(
                "INSERT INTO customer_profiles (id, customer_id, legal_name, customer_status, created_at, updated_at) VALUES (:id, :cid, :name, 'prospect', :now, :now)"
            ),
            {"id": uuid.uuid4().hex, "cid": customer_id, "name": customer_id, "now": datetime.now(UTC)},
        )
        session.commit()

        project = client.post(
            f"/api/operator/pilot/customers/{customer_id}/projects",
            json={"name": "Timeout Replay"},
        ).json()
        case = client.post(
            f"/api/operator/pilot/customers/{customer_id}/projects/{project['id']}/cases",
            json={"procurement_number": "LOCK-TEST-003"},
        ).json()

        idem_key = uuid.uuid4().hex
        first = client.post(
            f"/api/operator/pilot/customers/{customer_id}/cases/{case['id']}/runs",
            json={"registry_number": "RUN-UNIT-003"},
            headers={"Idempotency-Key": idem_key},
        )
        assert first.status_code == 201

        with patch("src.modules.customer_pilot.router.redis_acquire_lock") as mock_lock:
            mock_lock.side_effect = RedisLockTimeoutError("timed out")
            second = client.post(
                f"/api/operator/pilot/customers/{customer_id}/cases/{case['id']}/runs",
                json={"registry_number": "RUN-UNIT-003"},
                headers={"Idempotency-Key": idem_key},
            )

        assert second.status_code == 200
        assert second.json()["idempotent"] is True
        assert second.json()["id"] == first.json()["id"]

    def test_lock_timeout_no_pg_run_returns_timeout_error(self, client, session):
        customer_id = "cust_timeout_fail"
        session.execute(
            text(
                "INSERT INTO customer_profiles (id, customer_id, legal_name, customer_status, created_at, updated_at) VALUES (:id, :cid, :name, 'prospect', :now, :now)"
            ),
            {"id": uuid.uuid4().hex, "cid": customer_id, "name": customer_id, "now": datetime.now(UTC)},
        )
        session.commit()

        project = client.post(
            f"/api/operator/pilot/customers/{customer_id}/projects",
            json={"name": "Timeout Fail"},
        ).json()
        case = client.post(
            f"/api/operator/pilot/customers/{customer_id}/projects/{project['id']}/cases",
            json={"procurement_number": "LOCK-TEST-004"},
        ).json()

        mock_settings = MagicMock(arvectum_redis_enabled=True, arvectum_redis_namespace="test")
        with patch("src.modules.customer_pilot.router.get_settings", return_value=mock_settings), \
             patch("src.modules.customer_pilot.router.redis_acquire_lock") as mock_lock:
            mock_lock.side_effect = RedisLockTimeoutError("timed out")
            resp = client.post(
                f"/api/operator/pilot/customers/{customer_id}/cases/{case['id']}/runs",
                json={"registry_number": "RUN-UNIT-004"},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            )

        assert resp.status_code == 503
        body = resp.json()
        assert body["detail"]["code"] == "run_coordination_timeout"

    def test_redis_unavailable_returns_503(self, client, session):
        customer_id = "cust_unavail"
        session.execute(
            text(
                "INSERT INTO customer_profiles (id, customer_id, legal_name, customer_status, created_at, updated_at) VALUES (:id, :cid, :name, 'prospect', :now, :now)"
            ),
            {"id": uuid.uuid4().hex, "cid": customer_id, "name": customer_id, "now": datetime.now(UTC)},
        )
        session.commit()

        project = client.post(
            f"/api/operator/pilot/customers/{customer_id}/projects",
            json={"name": "Unavail"},
        ).json()
        case = client.post(
            f"/api/operator/pilot/customers/{customer_id}/projects/{project['id']}/cases",
            json={"procurement_number": "LOCK-TEST-005"},
        ).json()

        mock_settings = MagicMock(arvectum_redis_enabled=True, arvectum_redis_namespace="test")
        with patch("src.modules.customer_pilot.router.get_settings", return_value=mock_settings), \
             patch("src.modules.customer_pilot.router.redis_acquire_lock") as mock_lock:
            mock_lock.side_effect = RedisUnavailableError("Redis down")
            resp = client.post(
                f"/api/operator/pilot/customers/{customer_id}/cases/{case['id']}/runs",
                json={"registry_number": "RUN-UNIT-005"},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            )

        assert resp.status_code == 503
        body = resp.json()
        assert body["detail"]["code"] == "redis_unavailable"
