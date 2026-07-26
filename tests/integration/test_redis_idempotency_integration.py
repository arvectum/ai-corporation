import os
import time
import pytest
from src.shared.redis.client import close_client
from src.shared.redis.errors import RedisDisabledError, RedisUnavailableError
from src.shared.redis.idempotency import claim, release


pytestmark = pytest.mark.integration


def _cleanup():
    close_client()
    import redis as redis_py
    r = redis_py.Redis.from_url(os.environ.get("AI_CORP_REDIS_URL", "redis://127.0.0.1:6379/1"))
    r.flushdb()


class TestIdempotencyIntegration:
    def test_first_claim_succeeds(self):
        _cleanup()
        key = "arvectum:test:idemp:first_claim"
        assert claim(key, ttl_seconds=10) is True

    def test_second_claim_rejected(self):
        _cleanup()
        key = "arvectum:test:idemp:second_reject"
        assert claim(key, ttl_seconds=10) is True
        assert claim(key, ttl_seconds=10) is False

    def test_abandoned_claim_expires(self):
        _cleanup()
        key = "arvectum:test:idemp:abandoned"
        assert claim(key, ttl_seconds=1) is True
        time.sleep(1.5)
        assert claim(key, ttl_seconds=10) is True
        release(key)

    def test_release_after_claim(self):
        _cleanup()
        key = "arvectum:test:idemp:release"
        assert claim(key, ttl_seconds=10) is True
        assert release(key) is True
        assert claim(key, ttl_seconds=10) is True
        release(key)
