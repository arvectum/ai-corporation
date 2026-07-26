import os
import time

import pytest

from src.shared.redis.cache import delete, get, set
from src.shared.redis.client import close_client, reset_redis_runtime

pytestmark = pytest.mark.integration


def _cleanup():
    import redis as redis_py
    r = redis_py.Redis.from_url(os.environ.get("AI_CORP_REDIS_URL", "redis://127.0.0.1:6379/1"))
    r.flushdb()
    reset_redis_runtime()


class TestCacheIntegration:
    def test_set_and_get(self):
        _cleanup()
        key = "arvectum:test:cache:set_get"
        set(key, {"name": "test", "value": 42})
        val = get(key)
        assert val == {"name": "test", "value": 42}

    def test_ttl_expiry(self):
        _cleanup()
        key = "arvectum:test:cache:ttl"
        set(key, "short-lived", ttl_seconds=1)
        assert get(key) == "short-lived"
        time.sleep(1.5)
        assert get(key) is None

    def test_cache_miss(self):
        _cleanup()
        assert get("arvectum:test:cache:nonexistent") is None

    def test_delete(self):
        _cleanup()
        key = "arvectum:test:cache:delete"
        set(key, "to-delete")
        assert get(key) == "to-delete"
        delete(key)
        assert get(key) is None

    def test_deterministic_json_serialization(self):
        _cleanup()
        key = "arvectum:test:cache:json_deterministic"
        set(key, {"b": 2, "a": 1, "c": [3, 1, 2]})
        val = get(key)
        assert val == {"a": 1, "b": 2, "c": [3, 1, 2]}

    def test_secret_like_payload_rejected(self):
        _cleanup()
        key = "arvectum:test:cache:secret_reject"
        with pytest.raises(ValueError, match="secret"):
            set(key, "secret-api-key-12345")

    def test_secret_like_key_rejected_in_nested_dict(self):
        _cleanup()
        key = "arvectum:test:cache:secret_nested"
        with pytest.raises(ValueError, match="secret"):
            set(key, {"data": {"secret_token": "abc"}})

    def test_secret_like_value_rejected_in_list(self):
        _cleanup()
        key = "arvectum:test:cache:secret_list"
        with pytest.raises(ValueError, match="secret"):
            set(key, ["safe", "password123"])

    def test_corrupt_value_handled_as_miss(self):
        _cleanup()
        import redis as redis_py
        r = redis_py.Redis.from_url(os.environ.get("AI_CORP_REDIS_URL", "redis://127.0.0.1:6379/1"))
        key = "arvectum:test:cache:corrupt"
        r.set(key, "not-json")
        assert get(key) is None

    def test_cache_outage_fail_open(self, monkeypatch):
        _cleanup()
        close_client()
        monkeypatch.setenv("AI_CORP_REDIS_URL", "redis://127.0.0.1:19999/0")
        reset_redis_runtime()
        val = get("some_key")
        assert val is None
        reset_redis_runtime()
