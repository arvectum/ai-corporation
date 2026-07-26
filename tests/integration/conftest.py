import os

import pytest


@pytest.fixture(scope="session")
def redis_disabled():
    os.environ["AI_CORP_REDIS_ENABLED"] = "false"
    yield
    os.environ.pop("AI_CORP_REDIS_ENABLED", None)


@pytest.fixture(scope="session")
def redis_enabled():
    os.environ["AI_CORP_REDIS_ENABLED"] = "true"
    os.environ["AI_CORP_REDIS_URL"] = os.environ.get(
        "AI_CORP_REDIS_URL", "redis://127.0.0.1:6379/1"
    )
    yield
    os.environ.pop("AI_CORP_REDIS_ENABLED", None)
    os.environ.pop("AI_CORP_REDIS_URL", None)


@pytest.fixture()
def cleanup_keys():
    import redis as redis_py
    r = redis_py.Redis.from_url(
        os.environ.get("AI_CORP_REDIS_URL", "redis://127.0.0.1:6379/1")
    )
    yield
    r.flushdb()
