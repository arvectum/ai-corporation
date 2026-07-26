from __future__ import annotations

import logging
import time
from threading import Lock

import redis as redis_py

from src.shared.config.settings import get_settings
from src.shared.redis.errors import RedisDisabledError, RedisUnavailableError

logger = logging.getLogger(__name__)

_client_instance: redis_py.Redis | None = None
_client_lock: Lock = Lock()
_client_disabled: bool = False


def _build_client() -> redis_py.Redis | None:
    settings = get_settings()
    if not settings.arvectum_redis_enabled:
        return None
    url = settings.arvectum_redis_url
    if not url:
        logger.error("REDIS_ENABLED=true but REDIS_URL is not set")
        return None
    pool = redis_py.ConnectionPool.from_url(
        url,
        max_connections=settings.arvectum_redis_max_connections,
        socket_connect_timeout=settings.arvectum_redis_connect_timeout_seconds,
        socket_timeout=settings.arvectum_redis_operation_timeout_seconds,
        decode_responses=True,
    )
    return redis_py.Redis.from_pool(pool)


def get_client() -> redis_py.Redis | None:
    global _client_instance, _client_disabled
    if _client_disabled:
        return None
    if _client_instance is not None:
        return _client_instance
    with _client_lock:
        if _client_instance is not None:
            return _client_instance
        client = _build_client()
        if client is None:
            _client_disabled = True
            return None
        _client_instance = client
        logger.info("Redis client initialized")
        return _client_instance


def close_client() -> None:
    global _client_instance, _client_disabled
    with _client_lock:
        if _client_instance is not None:
            try:
                _client_instance.close()
            except Exception:
                logger.exception("Error closing Redis client")
            _client_instance = None
        _client_disabled = False


def ping() -> dict:
    enabled = get_settings().arvectum_redis_enabled
    if not enabled:
        return {"enabled": False, "status": "disabled", "latency_ms": None, "error_category": None}
    client = get_client()
    if client is None:
        return {"enabled": True, "status": "unavailable", "latency_ms": None, "error_category": "no_client"}
    try:
        start = time.monotonic()
        client.ping()
        elapsed_ms = round((time.monotonic() - start) * 1000, 1)
        return {"enabled": True, "status": "healthy", "latency_ms": elapsed_ms, "error_category": None}
    except redis_py.RedisError as exc:
        category = type(exc).__name__
        logger.warning("Redis ping failed: %s", exc)
        return {"enabled": True, "status": "unavailable", "latency_ms": None, "error_category": category}


def health_snapshot() -> dict:
    return ping()


def require_client() -> redis_py.Redis:
    client = get_client()
    if client is None:
        settings = get_settings()
        if not settings.arvectum_redis_enabled:
            raise RedisDisabledError("Redis is disabled")
        raise RedisUnavailableError("Redis is unavailable")
    return client
