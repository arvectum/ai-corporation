import os
import time

import pytest

from src.shared.redis.client import reset_redis_runtime
from src.shared.redis.idempotency import claim, release

pytestmark = pytest.mark.integration


def _cleanup():
    import redis as redis_py
    r = redis_py.Redis.from_url(os.environ.get("AI_CORP_REDIS_URL", "redis://127.0.0.1:6379/1"))
    r.flushdb()
    reset_redis_runtime()


class TestIdempotencyIntegration:
    def test_first_claim_succeeds(self):
        _cleanup()
        key = "arvectum:test:idemp:first_claim"
        token = claim(key, ttl_seconds=10)
        assert isinstance(token, str)
        release(key, token)

    def test_second_claim_rejected(self):
        _cleanup()
        key = "arvectum:test:idemp:second_reject"
        token1 = claim(key, ttl_seconds=10)
        assert isinstance(token1, str)
        token2 = claim(key, ttl_seconds=10)
        assert token2 is None
        release(key, token1)

    def test_abandoned_claim_expires(self):
        _cleanup()
        key = "arvectum:test:idemp:abandoned"
        token1 = claim(key, ttl_seconds=1)
        assert isinstance(token1, str)
        time.sleep(1.5)
        token2 = claim(key, ttl_seconds=10)
        assert isinstance(token2, str)
        release(key, token2)

    def test_foreign_token_cannot_release(self):
        _cleanup()
        key = "arvectum:test:idemp:foreign_release"
        token1 = claim(key, ttl_seconds=10)
        assert isinstance(token1, str)
        assert release(key, "fake-token") is False
        token2 = claim(key, ttl_seconds=10)
        assert token2 is None
        release(key, token1)

    def test_release_after_claim(self):
        _cleanup()
        key = "arvectum:test:idemp:release"
        token1 = claim(key, ttl_seconds=10)
        assert isinstance(token1, str)
        assert release(key, token1) is True
        token2 = claim(key, ttl_seconds=10)
        assert isinstance(token2, str)
        release(key, token2)
