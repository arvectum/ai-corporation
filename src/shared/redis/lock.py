from __future__ import annotations

import secrets
import time

from src.shared.config.settings import get_settings
from src.shared.redis.client import require_client
from src.shared.redis.errors import (
    RedisAlreadyLockedError,
    RedisDisabledError,
    RedisLockTimeoutError,
    RedisUnavailableError,
    redis_error_category,
)

_LUA_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


def _token() -> str:
    return secrets.token_urlsafe(32)


def acquire(
    key: str,
    *,
    ttl_seconds: int | None = None,
    wait_timeout_seconds: float | None = None,
) -> str:
    settings = get_settings()
    if not settings.arvectum_redis_enabled:
        raise RedisDisabledError("Redis is disabled")
    ttl_ms = (ttl_seconds or settings.arvectum_redis_default_lock_ttl_seconds) * 1000
    tok = _token()
    deadline = (time.monotonic() + wait_timeout_seconds) if wait_timeout_seconds else None
    while True:
        client = require_client()
        try:
            acquired = client.set(key, tok, nx=True, px=ttl_ms)
        except Exception:  # noqa: BLE001
            if deadline is not None and time.monotonic() >= deadline:
                raise RedisLockTimeoutError("Lock acquire timed out")
            time.sleep(0.05)
            continue
        if acquired:
            return tok
        if deadline is not None and time.monotonic() >= deadline:
            raise RedisLockTimeoutError("Lock acquire timed out")
        if deadline is not None:
            time.sleep(0.05)
        else:
            raise RedisAlreadyLockedError("Resource is already locked")


def release(key: str, token: str) -> bool:
    client = require_client()
    try:
        result = client.eval(_LUA_RELEASE, 1, key, token)
        return bool(result)
    except Exception as exc:  # noqa: BLE001
        category = redis_error_category(exc)
        raise RedisUnavailableError(f"Redis lock release unavailable: category={category}") from None
