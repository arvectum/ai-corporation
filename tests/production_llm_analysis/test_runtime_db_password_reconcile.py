from __future__ import annotations

import json
import subprocess
from pathlib import Path

import src.modules.production_llm_analysis.runtime_db_password_reconcile as reconcile
from src.modules.production_llm_analysis.runtime_db_password_reconcile import (
    _PASSWORD_RECONCILE_SQL,
    reconcile_runtime_database_password,
)


def _completed(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _env_file(tmp_path: Path) -> Path:
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "AI_CORP_DATABASE_URL="
        "postgresql+psycopg://runtime:stale@127.0.0.1:55432/ai_corporation\n"
        "AI_CORP_LLM_PROVIDER=stub\n",
        encoding="utf-8",
    )
    return env_file


def _container_env(secret: str = "container-secret") -> str:
    return json.dumps(
        [
            "POSTGRES_USER=ai_corporation",
            f"POSTGRES_PASSWORD={secret}",
            "POSTGRES_DB=ai_corporation",
        ]
    )


def test_password_reconcile_sql_reads_secret_from_container_environment_only() -> None:
    assert "POSTGRES_PASSWORD" in _PASSWORD_RECONCILE_SQL
    assert "POSTGRES_USER" in _PASSWORD_RECONCILE_SQL
    assert "container-secret" not in _PASSWORD_RECONCILE_SQL
    assert "ALTER ROLE %I WITH PASSWORD %L" in _PASSWORD_RECONCILE_SQL


def test_dry_run_proves_local_socket_without_changing_database(tmp_path: Path) -> None:
    calls: list[dict] = []

    def runner(command, **kwargs):
        calls.append({"command": command, "kwargs": kwargs})
        if "inspect" in command:
            return _completed(stdout=_container_env())
        assert kwargs["input"] == "SELECT 1;\n"
        return _completed(stdout="1\n")

    report = reconcile_runtime_database_password(
        env_file=_env_file(tmp_path),
        backup_dir=tmp_path.parent / "external-backup",
        container="arvectum-postgres",
        docker_context="desktop-linux",
        apply=False,
        docker_runner=runner,
    )

    assert report["local_socket"]["connection_ready"] is True
    assert report["apply_requested"] is False
    assert report["password_reconcile_attempted"] is False
    assert report["password_reconciled"] is False
    assert report["database_changed"] is False
    assert report["runtime_recovery"] is None
    assert report["final_status"] == "POSTGRES_PASSWORD_RECONCILIATION_APPLY_REQUIRED"
    assert len(calls) == 2


def test_apply_reconciles_password_then_runs_safe_runtime_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "container-secret-value"
    env_file = _env_file(tmp_path)
    backup_dir = tmp_path.parent / "external-secret-backup"
    calls: list[dict] = []

    def runner(command, **kwargs):
        calls.append({"command": command, "kwargs": kwargs})
        if "inspect" in command:
            return _completed(stdout=_container_env(secret))
        if kwargs["input"] == "SELECT 1;\n":
            return _completed(stdout="1\n")
        assert kwargs["input"] == _PASSWORD_RECONCILE_SQL
        assert secret not in kwargs["input"]
        assert secret not in " ".join(command)
        return _completed(stdout="ALTER ROLE\n")

    def fake_recovery(**kwargs):
        assert kwargs["env_file"] == env_file
        assert kwargs["backup_dir"] == backup_dir
        assert kwargs["repair"] is True
        assert kwargs["docker_runner"] is runner
        return {
            "selected_candidate": "container_env",
            "secret_source_drift": True,
            "repair_requested": True,
            "repair_performed": True,
            "backup_created": True,
            "runtime_preflight": {
                "database": {
                    "connection_ready": True,
                    "schema_ready": True,
                },
                "configuration": {
                    "provider": "stub",
                    "model": None,
                    "credential_present": False,
                    "configuration_ready": False,
                },
                "candidate_count": 3,
                "eligible_run_count": 1,
                "ready_for_controlled_execution": False,
            },
            "safety": {
                "database_password_recorded": False,
                "provider_called": False,
            },
        }

    monkeypatch.setattr(reconcile, "recover_runtime_database_access", fake_recovery)

    report = reconcile_runtime_database_password(
        env_file=env_file,
        backup_dir=backup_dir,
        container="arvectum-postgres",
        docker_context="desktop-linux",
        apply=True,
        docker_runner=runner,
    )

    assert report["local_socket"]["connection_ready"] is True
    assert report["password_reconcile_attempted"] is True
    assert report["password_reconciled"] is True
    assert report["database_changed"] is True
    assert report["final_status"] == (
        "DATABASE_ACCESS_RESTORED_GATE5_CONFIGURATION_PENDING"
    )
    assert report["runtime_recovery"]["repair_performed"] is True
    assert backup_dir.is_dir()
    assert len(calls) == 3

    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert secret not in serialized
    assert "postgresql+psycopg://" not in serialized
    assert str(env_file) not in serialized
    assert str(backup_dir) not in serialized
    assert "ai_corporation" not in serialized
    assert report["safety"]["provider_called"] is False


def test_local_socket_failure_stops_before_password_change(tmp_path: Path) -> None:
    calls: list[dict] = []

    def runner(command, **kwargs):
        calls.append({"command": command, "kwargs": kwargs})
        if "inspect" in command:
            return _completed(stdout=_container_env())
        return _completed(returncode=2, stderr="authentication failed")

    report = reconcile_runtime_database_password(
        env_file=_env_file(tmp_path),
        backup_dir=tmp_path.parent / "external-backup",
        container="arvectum-postgres",
        docker_context="desktop-linux",
        apply=True,
        docker_runner=runner,
    )

    assert report["local_socket"] == {
        "connection_ready": False,
        "error_code": "container_local_socket_authentication_failed",
    }
    assert report["password_reconcile_attempted"] is False
    assert report["database_changed"] is False
    assert report["final_status"] == "LOCAL_SOCKET_RECOVERY_UNAVAILABLE"
    assert len(calls) == 2


def test_failed_alter_role_is_fail_closed_and_does_not_run_recovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    recovery_called = False

    def runner(command, **kwargs):
        if "inspect" in command:
            return _completed(stdout=_container_env())
        if kwargs["input"] == "SELECT 1;\n":
            return _completed(stdout="1\n")
        return _completed(returncode=3, stderr="alter failed")

    def fake_recovery(**kwargs):
        nonlocal recovery_called
        recovery_called = True
        return {}

    monkeypatch.setattr(reconcile, "recover_runtime_database_access", fake_recovery)

    report = reconcile_runtime_database_password(
        env_file=_env_file(tmp_path),
        backup_dir=tmp_path.parent / "external-backup",
        container="arvectum-postgres",
        docker_context="desktop-linux",
        apply=True,
        docker_runner=runner,
    )

    assert report["password_reconcile_attempted"] is True
    assert report["password_reconciled"] is False
    assert report["database_changed"] is False
    assert report["runtime_recovery"] is None
    assert report["final_status"] == "POSTGRES_PASSWORD_RECONCILIATION_FAILED"
    assert recovery_called is False


def test_ready_preflight_is_reported_after_successful_reconciliation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def runner(command, **kwargs):
        if "inspect" in command:
            return _completed(stdout=_container_env())
        if kwargs["input"] == "SELECT 1;\n":
            return _completed(stdout="1\n")
        return _completed(stdout="ALTER ROLE\n")

    monkeypatch.setattr(
        reconcile,
        "recover_runtime_database_access",
        lambda **kwargs: {
            "selected_candidate": "container_env",
            "runtime_preflight": {
                "database": {
                    "connection_ready": True,
                    "schema_ready": True,
                },
                "ready_for_controlled_execution": True,
            },
        },
    )

    report = reconcile_runtime_database_password(
        env_file=_env_file(tmp_path),
        backup_dir=tmp_path.parent / "external-backup",
        container="arvectum-postgres",
        docker_context="desktop-linux",
        apply=True,
        docker_runner=runner,
    )

    assert report["final_status"] == "GATE5_PREFLIGHT_READY"
