import time

import pytest

from src.shared.redis.client import reset_redis_runtime
from src.shared.redis.idempotency import claim, release

pytestmark = pytest.mark.integration


class TestIdempotencyIntegration:
    def test_first_claim_succeeds(self, test_namespace, cleanup_keys):
        reset_redis_runtime()
        key = f"{test_namespace}:idemp:first_claim"
        token = claim(key, ttl_seconds=10)
        assert isinstance(token, str)
        release(key, token)

    def test_second_claim_rejected(self, test_namespace, cleanup_keys):
        reset_redis_runtime()
        key = f"{test_namespace}:idemp:second_reject"
        token1 = claim(key, ttl_seconds=10)
        assert isinstance(token1, str)
        token2 = claim(key, ttl_seconds=10)
        assert token2 is None
        release(key, token1)

    def test_abandoned_claim_expires(self, test_namespace, cleanup_keys):
        reset_redis_runtime()
        key = f"{test_namespace}:idemp:abandoned"
        token1 = claim(key, ttl_seconds=1)
        assert isinstance(token1, str)
        time.sleep(1.5)
        token2 = claim(key, ttl_seconds=10)
        assert isinstance(token2, str)
        release(key, token2)

    def test_foreign_token_cannot_release(self, test_namespace, cleanup_keys):
        reset_redis_runtime()
        key = f"{test_namespace}:idemp:foreign_release"
        token1 = claim(key, ttl_seconds=10)
        assert isinstance(token1, str)
        assert release(key, "fake-token") is False
        token2 = claim(key, ttl_seconds=10)
        assert token2 is None
        release(key, token1)

    def test_release_after_claim(self, test_namespace, cleanup_keys):
        reset_redis_runtime()
        key = f"{test_namespace}:idemp:release"
        token1 = claim(key, ttl_seconds=10)
        assert isinstance(token1, str)
        assert release(key, token1) is True
        token2 = claim(key, ttl_seconds=10)
        assert isinstance(token2, str)
        release(key, token2)
