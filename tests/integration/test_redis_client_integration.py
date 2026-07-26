import os
import pytest
from src.shared.redis.client import get_client, close_client, ping, health_snapshot, require_client
from src.shared.redis.errors import RedisDisabledError, RedisUnavailableError


pytestmark = pytest.mark.integration


def _cleanup():
    close_client()


class TestClientIntegration:
    def test_lazy_connection_when_disabled(self):
        _cleanup()
        os.environ["AI_CORP_REDIS_ENABLED"] = "false"
        from src.shared.config.settings import get_settings
        from src.shared.redis.client import _client_instance, _client_disabled
        _client_instance = None
        _client_disabled = False
        client = get_client()
        assert client is None
        os.environ["AI_CORP_REDIS_ENABLED"] = "true"

    def test_ping_healthy(self):
        _cleanup()
        result = ping()
        assert result["enabled"] is True
        assert result["status"] == "healthy"
        assert result["latency_ms"] is not None

    def test_ping_disabled(self):
        _cleanup()
        try:
            os.environ["AI_CORP_REDIS_ENABLED"] = "false"
            from src.shared.config.settings import get_settings
            from src.shared.redis.client import _client_instance, _client_disabled
            _client_instance = None
            _client_disabled = False
            result = ping()
            assert result["enabled"] is False
            assert result["status"] == "disabled"
        finally:
            os.environ["AI_CORP_REDIS_ENABLED"] = "true"

    def test_health_snapshot(self):
        _cleanup()
        snap = health_snapshot()
        assert "enabled" in snap
        assert "status" in snap

    def test_close_and_repeated_close_idempotent(self):
        _cleanup()
        client = get_client()
        assert client is not None
        close_client()
        close_client()

    def test_sanitized_diagnostics(self):
        _cleanup()
        snap = health_snapshot()
        assert "redis://" not in str(snap)
        assert "password" not in str(snap).lower()
        assert isinstance(snap["latency_ms"], (float, int)) or snap["latency_ms"] is None

    def test_require_client_raises_when_disabled(self):
        _cleanup()
        try:
            os.environ["AI_CORP_REDIS_ENABLED"] = "false"
            from src.shared.config.settings import get_settings
            from src.shared.redis.client import _client_instance, _client_disabled
            _client_instance = None
            _client_disabled = False
            with pytest.raises(RedisDisabledError):
                require_client()
        finally:
            os.environ["AI_CORP_REDIS_ENABLED"] = "true"
