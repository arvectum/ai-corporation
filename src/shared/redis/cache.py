from __future__ import annotations

import json
import logging

from src.shared.config.settings import get_settings
from src.shared.redis.client import get_client

logger = logging.getLogger(__name__)

_FORBIDDEN_KEYS_PREFIXES = ("secret", "password", "token", "key", "credential", "auth")


def _check_payload(value: object) -> None:
    if isinstance(value, str):
        lower = value.lower()
        for prefix in _FORBIDDEN_KEYS_PREFIXES:
            if lower.startswith(prefix):
                raise ValueError(f"Payload looks like a secret (prefix '{prefix}')")


def _serialize(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def get(key: str) -> object | None:
    client = get_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Corrupt cache value for key %s", key)
        try:
            client.delete(key)
        except Exception:
            pass
        return None
    except Exception:
        logger.exception("Cache get error")
        return None


def set(key: str, value: object, ttl_seconds: int | None = None) -> None:
    settings = get_settings()
    _check_payload(value)
    client = get_client()
    if client is None:
        return
    ttl = ttl_seconds or settings.arvectum_redis_default_cache_ttl_seconds
    try:
        raw = _serialize(value)
        client.set(key, raw, ex=ttl)
    except Exception:
        logger.exception("Cache set error")


def delete(key: str) -> None:
    client = get_client()
    if client is None:
        return
    try:
        client.delete(key)
    except Exception:
        logger.exception("Cache delete error")
