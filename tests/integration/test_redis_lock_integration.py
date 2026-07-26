import os
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from src.shared.redis.client import close_client
from src.shared.redis.errors import RedisAlreadyLockedError, RedisLockTimeoutError
from src.shared.redis.lock import acquire, release


pytestmark = pytest.mark.integration


def _cleanup():
    close_client()
    import redis as redis_py
    r = redis_py.Redis.from_url(os.environ.get("AI_CORP_REDIS_URL", "redis://127.0.0.1:6379/1"))
    r.flushdb()


class TestLockIntegration:
    def test_acquire_and_release(self):
        _cleanup()
        key = "arvectum:test:lock:test_acquire_release"
        token = acquire(key, ttl_seconds=10)
        assert token
        assert release(key, token) is True

    def test_second_acquire_rejected(self):
        _cleanup()
        key = "arvectum:test:lock:test_second_reject"
        token = acquire(key, ttl_seconds=10)
        assert token
        with pytest.raises(RedisAlreadyLockedError):
            acquire(key, ttl_seconds=10, wait_timeout_seconds=None)

    def test_foreign_token_release_rejected(self):
        _cleanup()
        key = "arvectum:test:lock:test_foreign_release"
        token1 = acquire(key, ttl_seconds=10)
        assert token1
        result = release(key, "fake-token")
        assert result is False

    def test_expired_owner_does_not_delete_new_lock(self):
        _cleanup()
        key = "arvectum:test:lock:test_expired_owner"
        token1 = acquire(key, ttl_seconds=1)
        assert token1
        time.sleep(1.5)
        token2 = acquire(key, ttl_seconds=10)
        assert token2
        result = release(key, token1)
        assert result is False
        release(key, token2)

    def test_ttl_expiry(self):
        _cleanup()
        key = "arvectum:test:lock:test_ttl_expiry"
        token = acquire(key, ttl_seconds=1)
        assert token
        time.sleep(1.5)
        token2 = acquire(key, ttl_seconds=10)
        assert token2
        release(key, token2)

    def test_wait_timeout_raises(self):
        _cleanup()
        key = "arvectum:test:lock:test_wait_timeout"
        token1 = acquire(key, ttl_seconds=5)
        assert token1
        with pytest.raises(RedisLockTimeoutError):
            acquire(key, ttl_seconds=5, wait_timeout_seconds=0.1)
        release(key, token1)

    def test_concurrent_lock_one_winner(self):
        _cleanup()
        key = "arvectum:test:lock:concurrent"

        def try_acquire():
            try:
                return acquire(key, ttl_seconds=5, wait_timeout_seconds=2)
            except (RedisAlreadyLockedError, RedisLockTimeoutError):
                return None

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(try_acquire) for _ in range(5)]
            tokens = [f.result() for f in futures]
        tokens = [t for t in tokens if t is not None]
        assert len(tokens) == 1, f"Expected 1 winner, got {len(tokens)}"
        if tokens:
            release(key, tokens[0])
