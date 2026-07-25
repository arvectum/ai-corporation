from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from src.modules.production_llm_analysis.runtime_db_recovery import (
    _docker_container_environment,
    _prepare_backup_directory,
    recover_runtime_database_access,
)

_REQUIRED_CONTAINER_KEYS = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")

_LOCAL_SOCKET_COMMAND = """
exec psql \
  --no-psqlrc \
  --quiet \
  --tuples-only \
  --no-align \
  --set ON_ERROR_STOP=1 \
  --host /var/run/postgresql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB"
""".strip()

_LOCAL_SOCKET_PROBE_SQL = "SELECT 1;\n"

_PASSWORD_RECONCILE_SQL = """\
\\set QUIET 1
\\getenv reconcile_user POSTGRES_USER
\\getenv reconcile_password POSTGRES_PASSWORD
SELECT format(
  'ALTER ROLE %I WITH PASSWORD %L',
  :'reconcile_user',
  :'reconcile_password'
) \\gexec
"""


def _docker_psql(
    *,
    container: str,
    docker_context: str,
    sql: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(
        [
            "docker",
            "--context",
            docker_context,
            "exec",
            "-i",
            container,
            "sh",
            "-ceu",
            _LOCAL_SOCKET_COMMAND,
        ],
        input=sql,
        capture_output=True,
        text=True,
        check=False,
    )


def _socket_probe(
    *,
    container: str,
    docker_context: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[bool, str | None]:
    completed = _docker_psql(
        container=container,
        docker_context=docker_context,
        sql=_LOCAL_SOCKET_PROBE_SQL,
        runner=runner,
    )
    if completed.returncode != 0:
        return False, "container_local_socket_authentication_failed"
    if completed.stdout.strip() != "1":
        return False, "container_local_socket_probe_invalid"
    return True, None


def reconcile_runtime_database_password(
    *,
    env_file: Path,
    backup_dir: Path,
    container: str,
    docker_context: str,
    apply: bool,
    limit: int = 30,
    docker_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise ValueError("preflight_limit_out_of_range")
    if not env_file.is_file() or env_file.is_symlink():
        raise ValueError("runtime_env_file_not_regular")

    container_environment = _docker_container_environment(
        container=container,
        docker_context=docker_context,
        runner=docker_runner,
    )
    if any(not container_environment.get(key) for key in _REQUIRED_CONTAINER_KEYS):
        raise ValueError("docker_postgres_environment_incomplete")

    socket_ready, socket_error = _socket_probe(
        container=container,
        docker_context=docker_context,
        runner=docker_runner,
    )

    report: dict[str, Any] = {
        "reconcile_version": "r10.1-runtime-db-password-reconcile-v1",
        "local_socket": {
            "connection_ready": socket_ready,
            "error_code": socket_error,
        },
        "apply_requested": apply,
        "password_reconcile_attempted": False,
        "password_reconciled": False,
        "database_changed": False,
        "runtime_recovery": None,
        "final_status": "LOCAL_SOCKET_RECOVERY_UNAVAILABLE",
        "safety": {
            "database_url_recorded": False,
            "database_username_recorded": False,
            "database_password_recorded": False,
            "container_environment_recorded": False,
            "env_file_path_recorded": False,
            "backup_path_recorded": False,
            "provider_called": False,
        },
    }

    if not socket_ready:
        return report

    if not apply:
        report["final_status"] = "POSTGRES_PASSWORD_RECONCILIATION_APPLY_REQUIRED"
        return report

    _prepare_backup_directory(env_file, backup_dir)
    report["password_reconcile_attempted"] = True
    completed = _docker_psql(
        container=container,
        docker_context=docker_context,
        sql=_PASSWORD_RECONCILE_SQL,
        runner=docker_runner,
    )
    if completed.returncode != 0:
        report["final_status"] = "POSTGRES_PASSWORD_RECONCILIATION_FAILED"
        return report

    report["password_reconciled"] = True
    report["database_changed"] = True

    try:
        recovery = recover_runtime_database_access(
            env_file=env_file,
            container=container,
            docker_context=docker_context,
            repair=True,
            backup_dir=backup_dir,
            limit=limit,
            docker_runner=docker_runner,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        report["final_status"] = "PASSWORD_RECONCILED_RUNTIME_RECOVERY_FAILED"
        return report

    report["runtime_recovery"] = recovery
    runtime_preflight = recovery.get("runtime_preflight")
    if recovery.get("selected_candidate") == "none":
        report["final_status"] = "PASSWORD_RECONCILED_RUNTIME_ACCESS_NOT_RESTORED"
    elif not isinstance(runtime_preflight, dict):
        report["final_status"] = "DATABASE_ACCESS_RESTORED_PREFLIGHT_NOT_RUN"
    elif not (runtime_preflight.get("database") or {}).get("connection_ready"):
        report["final_status"] = "DATABASE_ACCESS_RESTORED_PREFLIGHT_DATABASE_FAILED"
    elif not (runtime_preflight.get("database") or {}).get("schema_ready"):
        report["final_status"] = "DATABASE_ACCESS_RESTORED_SCHEMA_MIGRATION_REQUIRED"
    elif runtime_preflight.get("ready_for_controlled_execution"):
        report["final_status"] = "GATE5_PREFLIGHT_READY"
    else:
        report["final_status"] = "DATABASE_ACCESS_RESTORED_GATE5_CONFIGURATION_PENDING"
    return report


def serialized_report_contains_secret(report: dict[str, Any], secret_values: tuple[str, ...]) -> bool:
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    return any(value and value in serialized for value in secret_values)
