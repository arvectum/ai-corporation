import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from pytest_optional_profiles import (
    OPTIONAL_PROFILE_MARKERS,
    infer_optional_test_markers,
    profile_skip_reason,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

_original_database_url = os.environ.get("AI_CORP_DATABASE_URL")
if _original_database_url:
    os.environ.setdefault("AI_CORP_ORIGINAL_DATABASE_URL", _original_database_url)
# PostgreSQL acceptance tests intentionally exercise the application sessions
# configured by the runner.  The normal suite remains hermetic SQLite.
if os.environ.get("RUN_R8_POSTGRES_INTEGRATION") != "1":
    os.environ["AI_CORP_DATABASE_URL"] = "sqlite+pysqlite:///:memory:"

from src.main import app
from src.shared.api.dependencies import get_db_session
from src.shared.db.base import Base
from src.shared.db import models  # noqa: F401


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "storage_gate_enforced: do NOT patch check_ingestion_allowed — test exercises real storage gate")


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ai-corporation test profiles")
    group.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that are skipped in the default offline profile.",
    )
    group.addoption(
        "--run-postgres",
        action="store_true",
        default=False,
        help="Run PostgreSQL/pgvector tests that are skipped by default.",
    )
    group.addoption(
        "--run-network",
        action="store_true",
        default=False,
        help="Run tests that require live network access.",
    )
    group.addoption(
        "--run-llama-cpp",
        action="store_true",
        default=False,
        help="Run llama.cpp-specific tests that are skipped by default.",
    )
    group.addoption(
        "--run-live-smoke",
        action="store_true",
        default=False,
        help="Run live smoke tests for the Mac mini/runtime contour.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    enabled_profiles = {
        "integration": config.getoption("--run-integration"),
        "postgres": config.getoption("--run-postgres"),
        "network": config.getoption("--run-network"),
        "llama_cpp": config.getoption("--run-llama-cpp"),
        "live_smoke": config.getoption("--run-live-smoke"),
    }
    for item in items:
        explicit_markers = {marker.name for marker in item.iter_markers()}
        optional_markers = infer_optional_test_markers(item.nodeid, explicit_markers)
        for marker_name in OPTIONAL_PROFILE_MARKERS:
            if marker_name in optional_markers:
                item.add_marker(getattr(pytest.mark, marker_name))

        skip_reason = profile_skip_reason(optional_markers, enabled_profiles)
        if skip_reason:
            item.add_marker(pytest.mark.skip(reason=skip_reason))


@pytest.fixture()
def session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    with testing_session_local() as db:
        yield db
    Base.metadata.drop_all(engine)


@pytest.fixture()
def client(session: Session) -> Generator[TestClient, None, None]:
    def override_get_db_session() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_db_session] = override_get_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _allow_ingestion_by_default(request: pytest.FixtureRequest):
    """Allow ingestion in all tests by default.
    Tests with @pytest.mark.storage_gate_enforced bypass this fixture
    so the real storage gate is exercised.
    """
    if request.node.get_closest_marker("storage_gate_enforced"):
        yield
        return
    import unittest.mock
    with unittest.mock.patch("src.tender_research.rag.prepare_service.check_ingestion_allowed", return_value=None):
        with unittest.mock.patch("src.tender_research.rag.job_runner.check_ingestion_allowed", return_value=None):
            with unittest.mock.patch("src.tender_research.api.check_ingestion_allowed", return_value=None):
                yield
