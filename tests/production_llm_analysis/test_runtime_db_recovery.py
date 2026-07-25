from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest
from sqlalchemy.engine import URL, make_url

import src.modules.production_llm_analysis.runtime_db_recovery as recovery
from src.modules.production_llm_analysis.runtime_db_recovery import (
    DatabaseProbe,
    _atomic_repair_env_file,
    _container_candidate_url,
    _docker_container_environment,
    recover_runtime_database_access,
)


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def test_docker_environment_is_parsed_without_logging() -> None:
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        return _completed(
            json.dumps(
                [
                    "POSTGRES_USER=arvectum",
                    "POSTGRES_PASSWORD=container-secret",
                    "POSTGRES_DB=arvectum",
                    "UNRELATED=value",
                ]
            )
        )

    environment = _docker_container_environment(
        container="arvectum-postgres",
        docker_context="desktop-linux",
        runner=runner,
    )

    assert environment["POSTGRES_PASSWORD"] == "container-secret"
    assert calls[0][:3] == ["docker", "--context", "desktop-linux"]


def test_container_candidate_preserves_network_target_and_encodes_password() -> None:
    candidate = _container_candidate_url(
        "postgresql+psycopg://old:wrong@127.0.0.1:55432/arvectum",
        {
            "POSTGRES_USER": "arvectum",
            "POSTGRES_PASSWORD": "new:p@ss/word",
            "POSTGRES_DB": "arvectum",
        },
    )

    assert candidate.drivername == "postgresql+psycopg"
    assert candidate.host == "127.0.0.1"
    assert candidate.port == 55432
    assert candidate.username == "arvectum"
    assert candidate.password == "new:p@ss/word"
    rendered = candidate.render_as_string(hide_password=False)
    assert "new%3Ap%40ss%2Fword" in rendered


def test_atomic_repair_creates_private_external_backup_and_replaces_only_database_url(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "runtime" / "repo"
    checkout.mkdir(parents=True)
    env_file = checkout / ".env.local"
    backup_dir = tmp_path / "quarantine"
    env_file.write_text(
        "AI_CORP_DATABASE_URL=postgresql+psycopg://old:wrong@127.0.0.1:55432/arvectum\n"
        "AI_CORP_LLM_PROVIDER=stub\n",
        encoding="utf-8",
    )
    candidate = URL.create(
        "postgresql+psycopg",
        username="arvectum",
        password="new-secret",
        host="127.0.0.1",
        port=55432,
        database="arvectum",
    )

    _atomic_repair_env_file(env_file, candidate, backup_dir=backup_dir)

    repaired = env_file.read_text(encoding="utf-8")
    assert "new-secret" in repaired
    assert "old:wrong" not in repaired
    assert "AI_CORP_LLM_PROVIDER=stub" in repaired
    backups = list(backup_dir.glob("repo-.env.local.backup-*"))
    assert len(backups) == 1
    assert "old:wrong" in backups[0].read_text(encoding="utf-8")
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert not list(checkout.glob("*.backup-*"))


def test_atomic_repair_rejects_backup_inside_runtime_checkout(tmp_path: Path) -> None:
    checkout = tmp_path / "runtime"
    checkout.mkdir()
    env_file = checkout / ".env.local"
    env_file.write_text("AI_CORP_LLM_PROVIDER=stub\n", encoding="utf-8")
    candidate = URL.create(
        "postgresql+psycopg",
        username="arvectum",
        password="new-secret",
        host="127.0.0.1",
        port=55432,
        database="arvectum",
    )

    with pytest.raises(ValueError, match="runtime_env_backup_must_be_outside_checkout"):
        _atomic_repair_env_file(
            env_file,
            candidate,
            backup_dir=checkout / "secret-backups",
        )


def test_recovery_repairs_only_after_container_candidate_probe_and_stays_sanitized(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AI_CORP_DATABASE_URL", raising=False)
    checkout = tmp_path / "runtime" / "repo"
    checkout.mkdir(parents=True)
    env_file = checkout / ".env.local"
    backup_dir = tmp_path / "quarantine"
    env_file.write_text(
        "AI_CORP_DATABASE_URL=postgresql+psycopg://arvectum:wrong@127.0.0.1:55432/arvectum\n"
        "AI_CORP_LLM_PROVIDER=stub\n",
        encoding="utf-8",
    )

    def docker_runner(command, **kwargs):
        return _completed(
            json.dumps(
                [
                    "POSTGRES_USER=arvectum",
                    "POSTGRES_PASSWORD=container-secret",
                    "POSTGRES_DB=arvectum",
                ]
            )
        )

    def probe(database_url):
        url = make_url(database_url)
        if url.password == "container-secret":
            return DatabaseProbe(True, None, ("096_add_r8_canonical_snapshot_binding",))
        return DatabaseProbe(False, "database_authentication_failed", ())

    def fake_preflight(settings, *, limit):
        assert limit == 30
        assert make_url(settings.database_url).password == "container-secret"
        return {
            "database": {
                "connection_ready": True,
                "schema_ready": True,
            },
            "ready_for_controlled_execution": False,
        }

    monkeypatch.setattr(
        recovery,
        "collect_runtime_controlled_provider_preflight",
        fake_preflight,
    )

    report = recover_runtime_database_access(
        env_file=env_file,
        container="arvectum-postgres",
        docker_context="desktop-linux",
        repair=True,
        backup_dir=backup_dir,
        docker_runner=docker_runner,
        probe=probe,
    )

    assert report["selected_candidate"] == "container_env"
    assert report["secret_source_drift"] is True
    assert report["repair_performed"] is True
    assert report["backup_created"] is True
    assert report["runtime_preflight"]["database"]["connection_ready"] is True
    serialized = json.dumps(report, sort_keys=True)
    assert "container-secret" not in serialized
    assert "wrong" not in serialized
    assert str(env_file) not in serialized
    assert str(backup_dir) not in serialized
    assert report["safety"]["provider_called"] is False
