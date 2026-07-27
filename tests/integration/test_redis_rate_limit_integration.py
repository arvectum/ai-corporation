from concurrent.futures import ThreadPoolExecutor

import pytest

from src.shared.redis.client import reset_redis_runtime
from src.shared.redis.rate_limit import check

pytestmark = pytest.mark.integration


class TestRateLimitIntegration:
    def test_allowed_within_limit(self, test_namespace, cleanup_keys):
        reset_redis_runtime()
        key = f"{test_namespace}:ratelimit:allowed"
        result = check(key, limit=5, window_seconds=60)
        assert result["allowed"] is True
        assert result["limit"] == 5
        assert result["remaining"] >= 4
        assert result["retry_after_seconds"] == 0

    def test_blocked_when_exceeded(self, test_namespace, cleanup_keys):
        reset_redis_runtime()
        key = f"{test_namespace}:ratelimit:blocked"
        for _ in range(3):
            check(key, limit=3, window_seconds=60)
        result = check(key, limit=3, window_seconds=60)
        assert result["allowed"] is False
        assert result["remaining"] == 0

    def test_deterministic_retry_after(self, test_namespace, cleanup_keys):
        reset_redis_runtime()
        key = f"{test_namespace}:ratelimit:retry_after"
        for _ in range(2):
            check(key, limit=2, window_seconds=60)
        result = check(key, limit=2, window_seconds=60)
        assert result["allowed"] is False
        assert result["retry_after_seconds"] > 0

    def test_sequential_increments(self, test_namespace, cleanup_keys):
        reset_redis_runtime()
        key = f"{test_namespace}:ratelimit:sequential"
        for i in range(5):
            result = check(key, limit=10, window_seconds=60)
            assert result["allowed"] is True
            assert result["remaining"] == 10 - (i + 1)

    def test_concurrent_increments(self, test_namespace, cleanup_keys):
        reset_redis_runtime()
        key = f"{test_namespace}:ratelimit:concurrent"

        def inc():
            return check(key, limit=10, window_seconds=60)

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(inc) for _ in range(5)]
            results = [f.result() for f in futures]
        assert all(r["allowed"] for r in results)
        final = check(key, limit=10, window_seconds=60)
        assert final["remaining"] <= 5

    def test_tenant_isolation(self, test_namespace, cleanup_keys):
        reset_redis_runtime()
        key_a = f"{test_namespace}:ratelimit:tenant_a"
        key_b = f"{test_namespace}:ratelimit:tenant_b"
        for _ in range(10):
            check(key_a, limit=5, window_seconds=60)
        result_a = check(key_a, limit=5, window_seconds=60)
        assert result_a["allowed"] is False
        result_b = check(key_b, limit=5, window_seconds=60)
        assert result_b["allowed"] is True
