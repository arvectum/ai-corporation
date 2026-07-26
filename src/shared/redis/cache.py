from __future__ import annotations

import json
import logging

from src.shared.config.settings import get_settings
from src.shared.redis.client import get_client

logger = logging.getLogger(__name__)

_FORBIDDEN_PREFIXES = ("secret", "password", "token", "key", "credential", "auth")


def _normalize(text: str) -> str:
    return text.lower().replace("-", "").replace("_", "")


def _check_payload(value: object) -> None:
    if isinstance(value, str):
        normalized = _normalize(value)
        for prefix in _FORBIDDEN_PREFIXES:
            if normalized.startswith(prefix):
                raise ValueError("Payload contains a forbidden secret-like value")
    elif isinstance(value, dict):
        for k, v in value.items():
            normalized_key = _normalize(str(k))
            for prefix in _FORBIDDEN_PREFIXES:
                if normalized_key.startswith(prefix):
                    raise ValueError("Payload contains a forbidden secret-like key")
            _check_payload(v)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _check_payload(item)


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
        logger.warning("Cache corrupt value", extra={"operation": "get"})
        try:
            client.delete(key)
        except Exception:  # noqa: BLE001, S110
            pass
        return None
    except Exception:  # noqa: BLE001
        logger.warning("Cache get error", extra={"operation": "get"})
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
    except Exception:  # noqa: BLE001
        logger.warning("Cache set error", extra={"operation": "set"})


def delete(key: str) -> None:
    client = get_client()
    if client is None:
        return
    try:
        client.delete(key)
    except Exception:  # noqa: BLE001
        logger.warning("Cache delete error", extra={"operation": "delete"})
