from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from src.modules.production_llm_analysis.controlled_preflight import (
    collect_controlled_provider_preflight,
    resolve_provider_preflight,
)
from src.shared.config.settings import Settings

REQUIRED_ALEMBIC_HEAD = "096_add_r8_canonical_snapshot_binding"
REQUIRED_RUN_COLUMNS = frozenset(
    {
        "customer_id",
        "project_id",
        "procurement_case_id",
        "idempotency_key",
        "artifact_key",
    }
)


def _sanitized_database_target(database_url: str) -> dict[str, Any]:
    url = make_url(database_url)
    backend = url.get_backend_name()
    if backend == "sqlite":
        raw_database = url.database or ""
        database_name = (
            raw_database
            if raw_database in {"", ":memory:"}
            else Path(raw_database).name
        )
        return {
            "dialect": backend,
            "host": None,
            "port": None,
            "database_name": database_name or None,
            "local_path_recorded": False,
            "username_recorded": False,
            "password_recorded": False,
        }
    return {
        "dialect": backend,
        "host": url.host,
        "port": url.port,
        "database_name": url.database,
        "local_path_recorded": False,
        "username_recorded": False,
        "password_recorded": False,
    }


def _database_error_code(exc: BaseException) -> str:
    message = str(exc).lower()
    if "password authentication failed" in message or "authentication failed" in message:
        return "database_authentication_failed"
    if "connection refused" in message:
        return "database_connection_refused"
    if "could not translate host name" in message or "name or service not known" in message:
        return "database_host_resolution_failed"
    if "timeout" in message or "timed out" in message:
        return "database_connection_timeout"
    if "does not exist" in message and "database" in message:
        return "database_missing"
    return "database_connection_failed"


def collect_database_preflight(
    engine: Engine,
    *,
    database_url: str,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        **_sanitized_database_target(database_url),
        "connection_ready": False,
        "error_code": None,
        "alembic_revisions": [],
        "required_alembic_head": REQUIRED_ALEMBIC_HEAD,
        "alembic_head_ready": False,
        "tender_analysis_runs_present": False,
        "tender_analysis_run_columns": [],
        "required_columns": sorted(REQUIRED_RUN_COLUMNS),
        "required_columns_present": False,
        "schema_ready": False,
        "reason_codes": [],
    }
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            revisions: list[str] = []
            if "alembic_version" in tables:
                revisions = sorted(
                    str(value)
                    for value in connection.execute(
                        text("SELECT version_num FROM alembic_version")
                    ).scalars()
                )

            run_table_present = "tender_analysis_runs" in tables
            run_columns = (
                sorted(
                    item["name"]
                    for item in inspector.get_columns("tender_analysis_runs")
                )
                if run_table_present
                else []
            )
            columns_ready = REQUIRED_RUN_COLUMNS.issubset(run_columns)
            head_ready = revisions == [REQUIRED_ALEMBIC_HEAD]

            reasons: list[str] = []
            if not revisions:
                reasons.append("alembic_revision_missing")
            elif not head_ready:
                reasons.append("alembic_head_mismatch")
            if not run_table_present:
                reasons.append("tender_analysis_runs_missing")
            elif not columns_ready:
                reasons.append("required_run_columns_missing")

            report.update(
                {
                    "connection_ready": True,
                    "alembic_revisions": revisions,
                    "alembic_head_ready": head_ready,
                    "tender_analysis_runs_present": run_table_present,
                    "tender_analysis_run_columns": run_columns,
                    "required_columns_present": columns_ready,
                    "schema_ready": not reasons,
                    "reason_codes": sorted(reasons),
                }
            )
    except SQLAlchemyError as exc:
        report["error_code"] = _database_error_code(exc)
        report["reason_codes"] = [report["error_code"]]
    return report


def collect_runtime_controlled_provider_preflight(
    settings: Settings,
    *,
    limit: int = 30,
) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise ValueError("preflight_limit_out_of_range")

    database_backend = make_url(settings.database_url).get_backend_name()
    connect_args: dict[str, Any] = {}
    if database_backend == "sqlite":
        connect_args["check_same_thread"] = False
    elif database_backend == "postgresql":
        connect_args["connect_timeout"] = 5
    engine = create_engine(
        settings.database_url,
        future=True,
        connect_args=connect_args,
        pool_pre_ping=True,
    )
    try:
        database = collect_database_preflight(
            engine,
            database_url=settings.database_url,
        )
        provider = resolve_provider_preflight(settings)
        base_report: dict[str, Any] = {
            "preflight_version": "r10.1-controlled-provider-preflight-v2",
            "database": database,
            "configuration": provider.as_dict(),
            "eligible_run_count": 0,
            "candidate_count": 0,
            "ready_for_controlled_execution": False,
            "candidates": [],
            "safety": {
                "credential_value_recorded": False,
                "database_password_recorded": False,
                "database_url_recorded": False,
                "raw_tender_text_recorded": False,
                "raw_provider_body_recorded": False,
                "local_paths_recorded": False,
            },
        }
        if not database["schema_ready"]:
            return base_report

        session_factory = sessionmaker(
            bind=engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        with session_factory() as session:
            report = collect_controlled_provider_preflight(
                session,
                settings,
                limit=limit,
            )
        report["preflight_version"] = base_report["preflight_version"]
        report["database"] = database
        report["safety"] = base_report["safety"]
        report["ready_for_controlled_execution"] = bool(
            database["schema_ready"]
            and report["ready_for_controlled_execution"]
        )
        return report
    finally:
        engine.dispose()
