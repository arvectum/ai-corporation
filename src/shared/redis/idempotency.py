from __future__ import annotations

from src.shared.config.settings import get_settings
from src.shared.redis.client import require_client
from src.shared.redis.errors import RedisDisabledError, RedisUnavailableError

_LUA_CLAIM = """
local existing = redis.call('GET', KEYS[1])
if existing then
    return {0, existing}
end
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
return {1, ARGV[1]}
"""


def claim(key: str, ttl_seconds: int | None = None) -> bool:
    settings = get_settings()
    if not settings.arvectum_redis_enabled:
        raise RedisDisabledError("Redis is disabled")
    ttl_ms = (ttl_seconds or settings.arvectum_redis_idempotency_ttl_seconds) * 1000
    token = "claimed"
    client = require_client()
    try:
        result = client.eval(_LUA_CLAIM, 1, key, token, str(ttl_ms))
        return bool(result[0])
    except Exception as exc:
        raise RedisUnavailableError(f"Redis idempotency claim failed: {type(exc).__name__}") from exc


def release(key: str) -> bool:
    client = require_client()
    try:
        return bool(client.delete(key))
    except Exception as exc:
        raise RedisUnavailableError(f"Redis idempotency release failed: {type(exc).__name__}") from exc
