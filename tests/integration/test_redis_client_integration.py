import pytest

from src.shared.redis.client import (
    close_client,
    get_client,
    health_snapshot,
    ping,
    require_client,
    reset_redis_runtime,
)
from src.shared.redis.errors import RedisDisabledError

pytestmark = pytest.mark.integration


class TestClientIntegration:
    def test_lazy_connection_when_disabled(self, monkeypatch):
        reset_redis_runtime()
        monkeypatch.setenv("ARVECTUM_REDIS_ENABLED", "false")
        monkeypatch.setenv("AI_CORP_REDIS_ENABLED", "false")
        reset_redis_runtime()
        client = get_client()
        assert client is None

    def test_ping_healthy(self):
        reset_redis_runtime()
        result = ping()
        assert result["enabled"] is True
        assert result["status"] == "healthy"
        assert result["latency_ms"] is not None

    def test_ping_disabled(self, monkeypatch):
        reset_redis_runtime()
        monkeypatch.setenv("ARVECTUM_REDIS_ENABLED", "false")
        monkeypatch.setenv("AI_CORP_REDIS_ENABLED", "false")
        reset_redis_runtime()
        result = ping()
        assert result["enabled"] is False
        assert result["status"] == "disabled"

    def test_health_snapshot(self):
        reset_redis_runtime()
        snap = health_snapshot()
        assert "enabled" in snap
        assert "status" in snap

    def test_close_and_repeated_close_idempotent(self):
        reset_redis_runtime()
        client = get_client()
        assert client is not None
        close_client()
        close_client()

    def test_sanitized_diagnostics(self):
        reset_redis_runtime()
        snap = health_snapshot()
        assert "redis://" not in str(snap)
        assert "password" not in str(snap).lower()
        assert isinstance(snap["latency_ms"], (float, int)) or snap["latency_ms"] is None

    def test_require_client_raises_when_disabled(self, monkeypatch):
        reset_redis_runtime()
        monkeypatch.setenv("ARVECTUM_REDIS_ENABLED", "false")
        monkeypatch.setenv("AI_CORP_REDIS_ENABLED", "false")
        reset_redis_runtime()
        with pytest.raises(RedisDisabledError):
            require_client()

