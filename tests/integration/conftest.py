import os
import uuid
from urllib.parse import urlsplit

import pytest

from src.shared.config.settings import invalidate_settings_cache
from src.shared.redis.client import reset_redis_runtime

_DISALLOWED_NAMESPACE_PREFIXES = ("arvectum", "production", "")
_DEFAULT_TEST_REDIS_URL = "redis://127.0.0.1:6379/1"
_DEFAULT_TEST_NAMESPACE = "test-arv007-integration"


def _test_redis_url() -> str:
    """Return only an explicitly supplied or documented test Redis URL."""
    candidate = os.environ.get("AI_CORP_REDIS_TEST_URL") or os.environ.get(
        "AI_CORP_REDIS_URL", _DEFAULT_TEST_REDIS_URL
    )
    canonical = os.environ.get("AI_CORP_REDIS_CANONICAL_URL")
    if canonical is None:
        canonical = os.environ.get("ARVECTUM_REDIS_URL")
    if canonical:
        candidate_parts = urlsplit(candidate)
        canonical_parts = urlsplit(canonical)
        candidate_endpoint = (
            candidate_parts.scheme,
            candidate_parts.hostname,
            candidate_parts.port,
            candidate_parts.path,
        )
        canonical_endpoint = (
            canonical_parts.scheme,
            canonical_parts.hostname,
            canonical_parts.port,
            canonical_parts.path,
        )
        if candidate_endpoint == canonical_endpoint:
            raise RuntimeError("test Redis endpoint must differ from canonical runtime endpoint")
    return candidate


def _test_redis_namespace() -> str:
    namespace = os.environ.get("AI_CORP_REDIS_TEST_NAMESPACE", _DEFAULT_TEST_NAMESPACE)
    if not namespace.startswith("test-"):
        raise RuntimeError("test Redis namespace must start with test-")
    return namespace


def _set_test_redis_environment(monkeypatch, *, enabled: bool) -> None:
    invalidate_settings_cache()
    if not enabled:
        for name in (
            "ARVECTUM_REDIS_URL",
            "AI_CORP_REDIS_URL",
            "ARVECTUM_REDIS_NAMESPACE",
            "AI_CORP_REDIS_NAMESPACE",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ARVECTUM_REDIS_ENABLED", "false")
        monkeypatch.setenv("AI_CORP_REDIS_ENABLED", "false")
        return

    url = _test_redis_url()
    namespace = _test_redis_namespace()
    for name in ("ARVECTUM_REDIS_URL", "AI_CORP_REDIS_URL"):
        monkeypatch.setenv(name, url)
    for name in ("ARVECTUM_REDIS_NAMESPACE", "AI_CORP_REDIS_NAMESPACE"):
        monkeypatch.setenv(name, namespace)
    monkeypatch.setenv("ARVECTUM_REDIS_ENABLED", "true")
    monkeypatch.setenv("AI_CORP_REDIS_ENABLED", "true")
    invalidate_settings_cache()


@pytest.fixture(autouse=True)
def redis_test_environment(request, monkeypatch):
    if not request.node.path.name.startswith("test_redis_"):
        yield
        return
    _set_test_redis_environment(monkeypatch, enabled=True)
    reset_redis_runtime()
    try:
        yield
    finally:
        reset_redis_runtime()
        invalidate_settings_cache()


def delete_test_namespace(client, namespace: str) -> None:
    if not namespace:
        raise ValueError("namespace must not be empty")
    normalized = namespace.lower()
    for prefix in _DISALLOWED_NAMESPACE_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + ":"):
            raise ValueError(f"Refusing to clean namespace: {namespace!r}")
    cursor = 0
    while True:
        cursor, keys = client.scan(cursor=cursor, match=f"{namespace}:*", count=100)
        if keys:
            client.delete(*keys)
        if cursor == 0:
            break


@pytest.fixture()
def test_namespace() -> str:
    ns = f"test-arv007-{uuid.uuid4().hex}"
    return ns


@pytest.fixture()
def redis_disabled(monkeypatch):
    _set_test_redis_environment(monkeypatch, enabled=False)
    try:
        yield
    finally:
        invalidate_settings_cache()


@pytest.fixture()
def redis_enabled(monkeypatch):
    _set_test_redis_environment(monkeypatch, enabled=True)
    try:
        yield
    finally:
        invalidate_settings_cache()


@pytest.fixture()
def cleanup_keys(test_namespace):
    import redis as redis_py

    r = redis_py.Redis.from_url(_test_redis_url())
    yield
    delete_test_namespace(r, test_namespace)
