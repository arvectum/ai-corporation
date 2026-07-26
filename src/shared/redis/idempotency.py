from __future__ import annotations

import secrets

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

_LUA_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


def _token() -> str:
    return secrets.token_urlsafe(32)


def claim(key: str, ttl_seconds: int | None = None) -> str | None:
    settings = get_settings()
    if not settings.arvectum_redis_enabled:
        raise RedisDisabledError("Redis is disabled")
    ttl_ms = (ttl_seconds or settings.arvectum_redis_idempotency_ttl_seconds) * 1000
    tok = _token()
    client = require_client()
    try:
        result = client.eval(_LUA_CLAIM, 1, key, tok, str(ttl_ms))
        claimed = bool(result[0])
        if claimed:
            return tok
        return None
    except Exception as exc:
        category = type(exc).__name__
        raise RedisUnavailableError(f"Redis idempotency claim failed: {category}") from exc


def release(key: str, token: str) -> bool:
    client = require_client()
    try:
        result = client.eval(_LUA_RELEASE, 1, key, token)
        return bool(result)
    except Exception as exc:
        category = type(exc).__name__
        raise RedisUnavailableError(f"Redis idempotency release failed: {category}") from exc
