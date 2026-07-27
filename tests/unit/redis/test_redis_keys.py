import hashlib

import pytest

from src.shared.redis.keys import build_idempotency_key, build_key, build_lock_key


class TestKeyBuilder:
    def test_basic_key_structure(self):
        key = build_key("arvectum", "test", "lock", tenant="t1", customer="c1", project="p1", case="case1")
        assert key == "arvectum:test:lock:t1:c1:p1:case1"

    def test_sha256_for_user_controlled(self):
        raw = "user-idempotency-key-123"
        key = build_key("arvectum", "test", "lock", customer="c1", user_controlled=raw)
        expected_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert key == f"arvectum:test:lock:c1:{expected_hash}"

    def test_raw_idempotency_key_not_in_key(self):
        raw = "my-secret-idempotency-key"
        key = build_idempotency_key("arvectum", "test", "c1", "p1", "case1", raw)
        assert "my-secret" not in key
        assert raw not in key
        assert ":" in key

    def test_lock_key_includes_operation(self):
        customer = "cust_abc"
        case = "case_123"
        raw_key = "key-xyz"
        key = build_lock_key("arvectum", "prod", customer, "proj_1", case, "start_run", raw_key)
        assert "arvectum:prod:lock:" in key
        assert customer in key
        assert case in key
        assert "start_run" in key
        assert raw_key not in key

    def test_reject_empty_tenant(self):
        with pytest.raises(ValueError, match="tenant must be non-empty"):
            build_key("arvectum", "test", "lock", tenant="")

    def test_deterministic(self):
        kwargs = {"namespace": "ns", "environment": "env", "component": "c", "customer": "cust", "project": "proj"}
        assert build_key(**kwargs) == build_key(**kwargs)

    def test_cross_tenant_isolation(self):
        key_a = build_key("arvectum", "test", "lock", tenant="tenant_a", customer="c1")
        key_b = build_key("arvectum", "test", "lock", tenant="tenant_b", customer="c1")
        assert key_a != key_b
