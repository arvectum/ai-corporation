import os

import pytest


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
def cleanup_keys():
    import redis as redis_py

    r = redis_py.Redis.from_url(os.environ.get("AI_CORP_REDIS_URL", "redis://127.0.0.1:6379/1"))
    yield
    r.flushdb()
