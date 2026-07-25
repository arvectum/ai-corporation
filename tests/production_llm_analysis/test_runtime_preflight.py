from __future__ import annotations

import json

from sqlalchemy import create_engine, text

from src.modules.production_llm_analysis.runtime_preflight import (
    REQUIRED_ALEMBIC_HEAD,
    _database_error_code,
    collect_database_preflight,
    collect_runtime_controlled_provider_preflight,
)
from src.shared.config.settings import Settings


def _sqlite_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'private-runtime.db'}"


def test_database_target_excludes_password_username_and_local_path(tmp_path) -> None:
    database_url = _sqlite_url(tmp_path)
    engine = create_engine(database_url, future=True)
    try:
        report = collect_database_preflight(engine, database_url=database_url)
    finally:
        engine.dispose()

    assert report["dialect"] == "sqlite"
    assert report["database_name"] == "private-runtime.db"
    assert report["local_path_recorded"] is False
    assert report["username_recorded"] is False
    assert report["password_recorded"] is False
    serialized = json.dumps(report, sort_keys=True)
    assert str(tmp_path) not in serialized


def test_database_schema_requires_exact_head_and_r8_run_columns(tmp_path) -> None:
    database_url = _sqlite_url(tmp_path)
    engine = create_engine(database_url, future=True)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(255))"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": REQUIRED_ALEMBIC_HEAD},
        )
        connection.execute(
            text(
                """
                CREATE TABLE tender_analysis_runs (
                    id VARCHAR(36) PRIMARY KEY,
                    customer_id VARCHAR(64),
                    project_id VARCHAR(36),
                    procurement_case_id VARCHAR(36),
                    idempotency_key VARCHAR(128),
                    artifact_key VARCHAR(96)
                )
                """
            )
        )
    try:
        report = collect_database_preflight(engine, database_url=database_url)
    finally:
        engine.dispose()

    assert report["connection_ready"] is True
    assert report["alembic_revisions"] == [REQUIRED_ALEMBIC_HEAD]
    assert report["required_columns_present"] is True
    assert report["schema_ready"] is True
    assert report["reason_codes"] == []


def test_database_authentication_error_is_reduced_to_stable_code() -> None:
    error = RuntimeError(
        "password authentication failed for user secret-user password=secret-value"
    )
    assert _database_error_code(error) == "database_authentication_failed"


def test_runtime_preflight_fails_closed_before_orm_on_outdated_schema(tmp_path) -> None:
    database_url = _sqlite_url(tmp_path)
    settings = Settings(
        database_url=database_url,
        llm_provider="openai_compatible",
        llm_model="approved-model",
        openai_api_key="must-not-leak",
        openai_base_url="https://api.example.test/v1?token=must-not-leak",
    )

    report = collect_runtime_controlled_provider_preflight(settings)

    assert report["database"]["connection_ready"] is True
    assert report["database"]["schema_ready"] is False
    assert report["ready_for_controlled_execution"] is False
    assert report["candidates"] == []
    assert report["safety"]["database_url_recorded"] is False
    assert report["safety"]["database_password_recorded"] is False
    serialized = json.dumps(report, sort_keys=True)
    assert "must-not-leak" not in serialized
    assert str(tmp_path) not in serialized
