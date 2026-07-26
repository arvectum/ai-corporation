import os
import pytest
from src.shared.redis.client import close_client
from src.shared.redis.rate_limit import check


pytestmark = pytest.mark.integration


def _cleanup():
    close_client()
    import redis as redis_py
    r = redis_py.Redis.from_url(os.environ.get("AI_CORP_REDIS_URL", "redis://127.0.0.1:6379/1"))
    r.flushdb()


class TestRateLimitIntegration:
    def test_allowed_within_limit(self):
        _cleanup()
        key = "arvectum:test:ratelimit:allowed"
        result = check(key, limit=5, window_seconds=60)
        assert result["allowed"] is True
        assert result["limit"] == 5
        assert result["remaining"] >= 4
        assert result["retry_after_seconds"] == 0

    def test_blocked_when_exceeded(self):
        _cleanup()
        key = "arvectum:test:ratelimit:blocked"
        for _ in range(3):
            check(key, limit=3, window_seconds=60)
        result = check(key, limit=3, window_seconds=60)
        assert result["allowed"] is False
        assert result["remaining"] == 0

    def test_deterministic_retry_after(self):
        _cleanup()
        key = "arvectum:test:ratelimit:retry_after"
        for _ in range(2):
            check(key, limit=2, window_seconds=60)
        result = check(key, limit=2, window_seconds=60)
        assert result["allowed"] is False
        assert result["retry_after_seconds"] > 0

    def test_concurrent_rate_increments(self):
        _cleanup()
        key = "arvectum:test:ratelimit:concurrent"
        for i in range(5):
            result = check(key, limit=10, window_seconds=60)
            assert result["allowed"] is True
            assert result["remaining"] == 10 - (i + 1)

    def test_tenant_isolation(self):
        _cleanup()
        key_a = "arvectum:test:ratelimit:tenant_a"
        key_b = "arvectum:test:ratelimit:tenant_b"
        for _ in range(10):
            check(key_a, limit=5, window_seconds=60)
        result_a = check(key_a, limit=5, window_seconds=60)
        assert result_a["allowed"] is False
        result_b = check(key_b, limit=5, window_seconds=60)
        assert result_b["allowed"] is True
