from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

from sqlalchemy.engine import make_url

import src.modules.production_llm_analysis.runtime_db_recovery as recovery
from src.modules.production_llm_analysis.runtime_db_recovery import (
    DatabaseProbe,
    _docker_published_postgres_endpoint,
    _published_container_candidate_url,
    recover_runtime_database_access,
)


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def test_docker_published_endpoint_uses_single_host_port() -> None:
    commands: list[list[str]] = []

    def runner(command, **kwargs):
        commands.append(command)
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        return _completed(
            json.dumps(
                [
                    {"HostIp": "0.0.0.0", "HostPort": "55432"},
                    {"HostIp": "::", "HostPort": "55432"},
                ]
            )
        )

    endpoint = _docker_published_postgres_endpoint(
        container="arvectum-postgres",
        docker_context="desktop-linux",
        runner=runner,
    )

    assert endpoint == ("127.0.0.1", 55432)
    assert commands[0][:3] == ["docker", "--context", "desktop-linux"]
    assert "POSTGRES_PASSWORD" not in " ".join(commands[0])


def test_published_candidate_replaces_endpoint_and_encodes_password() -> None:
    candidate = _published_container_candidate_url(
        "postgresql+psycopg://old:wrong@127.0.0.1:6432/old_db",
        {
            "POSTGRES_USER": "arvectum",
            "POSTGRES_PASSWORD": "new:p@ss/word",
            "POSTGRES_DB": "arvectum",
        },
        host="127.0.0.1",
        port=55432,
    )

    assert candidate.drivername == "postgresql+psycopg"
    assert candidate.host == "127.0.0.1"
    assert candidate.port == 55432
    assert candidate.username == "arvectum"
    assert candidate.password == "new:p@ss/word"
    assert candidate.database == "arvectum"
    assert "new%3Ap%40ss%2Fword" in candidate.render_as_string(hide_password=False)


def test_recovery_falls_back_to_published_endpoint_and_repairs_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("AI_CORP_DATABASE_URL", raising=False)
    checkout = tmp_path / "runtime" / "repo"
    checkout.mkdir(parents=True)
    env_file = checkout / ".env.local"
    backup_dir = tmp_path / "quarantine"
    env_file.write_text(
        "AI_CORP_DATABASE_URL="
        "postgresql+psycopg://runtime:wrong@127.0.0.1:6432/arvectum\n"
        "AI_CORP_LLM_PROVIDER=stub\n",
        encoding="utf-8",
    )

    inspect_calls = 0

    def docker_runner(command, **kwargs):
        nonlocal inspect_calls
        inspect_calls += 1
        rendered = " ".join(command)
        if ".Config.Env" in rendered:
            return _completed(
                json.dumps(
                    [
                        "POSTGRES_USER=arvectum",
                        "POSTGRES_PASSWORD=container-secret",
                        "POSTGRES_DB=arvectum",
                    ]
                )
            )
        assert ".NetworkSettings.Ports" in rendered
        return _completed(
            json.dumps(
                [{"HostIp": "0.0.0.0", "HostPort": "55432"}]
            )
        )

    def probe(database_url):
        url = make_url(database_url)
        if (
            url.username == "arvectum"
            and url.password == "container-secret"
            and url.host == "127.0.0.1"
            and url.port == 55432
            and url.database == "arvectum"
        ):
            return DatabaseProbe(
                True,
                None,
                ("096_add_r8_canonical_snapshot_binding",),
            )
        return DatabaseProbe(False, "database_authentication_failed", ())

    def fake_preflight(settings, *, limit):
        assert limit == 30
        url = make_url(settings.database_url)
        assert url.password == "container-secret"
        assert url.port == 55432
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

    assert inspect_calls == 2
    assert report["runtime_candidate"]["connection_ready"] is False
    assert report["container_candidate"]["connection_ready"] is False
    assert report["published_endpoint_candidate"] == {
        "available": True,
        "host": "127.0.0.1",
        "port": 55432,
        "connection_ready": True,
        "error_code": None,
        "alembic_revisions": ["096_add_r8_canonical_snapshot_binding"],
    }
    assert report["selected_candidate"] == "container_published_endpoint"
    assert report["secret_source_drift"] is True
    assert report["endpoint_source_drift"] is True
    assert report["repair_performed"] is True
    assert report["backup_created"] is True
    assert report["runtime_preflight"]["database"]["connection_ready"] is True

    repaired = env_file.read_text(encoding="utf-8")
    repaired_url = make_url(
        next(
            line.split("=", 1)[1]
            for line in repaired.splitlines()
            if line.startswith("AI_CORP_DATABASE_URL=")
        )
    )
    assert repaired_url.username == "arvectum"
    assert repaired_url.password == "container-secret"
    assert repaired_url.host == "127.0.0.1"
    assert repaired_url.port == 55432
    assert repaired_url.database == "arvectum"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700

    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert "container-secret" not in serialized
    assert "runtime:wrong" not in serialized
    assert str(env_file) not in serialized
    assert str(backup_dir) not in serialized
    assert report["safety"]["provider_called"] is False


def test_unpublished_endpoint_stays_fail_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AI_CORP_DATABASE_URL", raising=False)
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "AI_CORP_DATABASE_URL="
        "postgresql+psycopg://runtime:wrong@127.0.0.1:6432/arvectum\n",
        encoding="utf-8",
    )

    def docker_runner(command, **kwargs):
        rendered = " ".join(command)
        if ".Config.Env" in rendered:
            return _completed(
                json.dumps(
                    [
                        "POSTGRES_USER=arvectum",
                        "POSTGRES_PASSWORD=container-secret",
                        "POSTGRES_DB=arvectum",
                    ]
                )
            )
        return _completed("null")

    report = recover_runtime_database_access(
        env_file=env_file,
        container="arvectum-postgres",
        docker_context="desktop-linux",
        repair=False,
        docker_runner=docker_runner,
        probe=lambda database_url: DatabaseProbe(
            False,
            "database_authentication_failed",
            (),
        ),
    )

    assert report["selected_candidate"] == "none"
    assert report["repair_performed"] is False
    assert report["backup_created"] is False
    assert report["runtime_preflight"] is None
    assert report["published_endpoint_candidate"]["available"] is False
    assert report["published_endpoint_candidate"]["error_code"] == (
        "docker_postgres_published_endpoint_unavailable"
    )
