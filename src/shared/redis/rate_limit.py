from __future__ import annotations

from src.shared.config.settings import get_settings
from src.shared.redis.client import require_client
from src.shared.redis.errors import RedisDisabledError, RedisUnavailableError

_LUA_FIXED_WINDOW = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local now = redis.call('TIME')
local window_start = now[1] - (now[1] % window_seconds)
local window_key = key .. ':' .. tostring(window_start)
local current = redis.call('INCR', window_key)
if current == 1 then
    redis.call('EXPIRE', window_key, window_seconds * 2)
end
local remaining = math.max(0, limit - current)
local reset_at = window_start + window_seconds
local retry_after = 0
if current > limit then
    retry_after = reset_at - now[1]
end
return {tostring(current <= limit), tostring(current), tostring(remaining), tostring(retry_after), tostring(reset_at)}
"""


def check(key: str, limit: int | None = None, window_seconds: int | None = None) -> dict:
    settings = get_settings()
    if not settings.arvectum_redis_enabled:
        raise RedisDisabledError("Redis is disabled")
    lim = limit or settings.arvectum_redis_rate_limit_default_limit
    win = window_seconds or settings.arvectum_redis_rate_limit_window_seconds
    client = require_client()
    try:
        result = client.eval(_LUA_FIXED_WINDOW, 1, key, str(lim), str(win))
        allowed = result[0] == "1"
        remaining = int(result[2])
        retry_after = int(result[3])
        reset_at = int(result[4])
        return {
            "allowed": allowed,
            "limit": lim,
            "remaining": remaining,
            "retry_after_seconds": max(0, retry_after),
            "reset_after_seconds": max(0, reset_at - int(__import__("time").time())),
        }
    except Exception as exc:
        raise RedisUnavailableError(f"Redis rate limit check failed: {type(exc).__name__}") from exc
