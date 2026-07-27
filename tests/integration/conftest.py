import os
import uuid

import pytest

_DISALLOWED_NAMESPACE_PREFIXES = ("arvectum", "production", "")


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
    monkeypatch.setenv("AI_CORP_REDIS_ENABLED", "false")
    monkeypatch.delenv("AI_CORP_REDIS_URL", raising=False)
    yield


@pytest.fixture()
def redis_enabled(monkeypatch):
    monkeypatch.setenv("AI_CORP_REDIS_ENABLED", "true")
    monkeypatch.setenv("AI_CORP_REDIS_URL", os.environ.get("AI_CORP_REDIS_URL", "redis://127.0.0.1:6379/1"))
    yield


@pytest.fixture()
def cleanup_keys(test_namespace):
    import redis as redis_py

    r = redis_py.Redis.from_url(os.environ.get("AI_CORP_REDIS_URL", "redis://127.0.0.1:6379/1"))
    yield
    delete_test_namespace(r, test_namespace)
