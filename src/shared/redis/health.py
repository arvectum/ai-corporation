from __future__ import annotations

from src.shared.redis.client import health_snapshot


def redis_section() -> dict:
    return health_snapshot()
