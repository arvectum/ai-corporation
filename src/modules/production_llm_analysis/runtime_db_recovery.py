from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from src.modules.production_llm_analysis.runtime_preflight import (
    _database_error_code,
    collect_runtime_controlled_provider_preflight,
)
from src.shared.config.settings import Settings

_DATABASE_ENV_KEY = "AI_CORP_DATABASE_URL"
_REQUIRED_CONTAINER_KEYS = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
_POSTGRES_CONTAINER_PORT = "5432/tcp"


@dataclass(frozen=True)
class DatabaseProbe:
    connection_ready: bool
    error_code: str | None
    alembic_revisions: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "connection_ready": self.connection_ready,
            "error_code": self.error_code,
            "alembic_revisions": list(self.alembic_revisions),
        }


def _probe_database(database_url: str | URL) -> DatabaseProbe:
    url = make_url(database_url)
    connect_args: dict[str, Any] = {}
    if url.get_backend_name() == "postgresql":
        connect_args["connect_timeout"] = 5
    engine = create_engine(
        url,
        future=True,
        connect_args=connect_args,
        pool_pre_ping=True,
    )
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            revisions: tuple[str, ...] = ()
            table_exists = bool(
                connection.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM information_schema.tables "
                        "WHERE table_schema = 'public' "
                        "AND table_name = 'alembic_version'"
                        ")"
                    )
                ).scalar()
            )
            if table_exists:
                revisions = tuple(
                    sorted(
                        str(value)
                        for value in connection.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalars()
                    )
                )
            return DatabaseProbe(True, None, revisions)
    except SQLAlchemyError as exc:
        return DatabaseProbe(False, _database_error_code(exc), ())
    finally:
        engine.dispose()


def _docker_container_environment(
    *,
    container: str,
    docker_context: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    completed = runner(
        [
            "docker",
            "--context",
            docker_context,
            "inspect",
            "--format",
            "{{json .Config.Env}}",
            container,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    values = json.loads(completed.stdout)
    if not isinstance(values, list):
        raise ValueError("docker_environment_invalid")
    environment: dict[str, str] = {}
    for item in values:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, value = item.split("=", 1)
        environment[key] = value
    if any(not environment.get(key) for key in _REQUIRED_CONTAINER_KEYS):
        raise ValueError("docker_postgres_environment_incomplete")
    return environment


def _docker_published_postgres_endpoint(
    *,
    container: str,
    docker_context: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str, int]:
    completed = runner(
        [
            "docker",
            "--context",
            docker_context,
            "inspect",
            "--format",
            '{{json (index .NetworkSettings.Ports "5432/tcp")}}',
            container,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    bindings = json.loads(completed.stdout)
    if not isinstance(bindings, list) or not bindings:
        raise ValueError("docker_postgres_port_not_published")

    ports: set[int] = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        raw_port = binding.get("HostPort")
        if isinstance(raw_port, str) and raw_port.isdigit():
            port = int(raw_port)
            if 1 <= port <= 65535:
                ports.add(port)
    if len(ports) != 1:
        raise ValueError("docker_postgres_published_port_ambiguous")
    return "127.0.0.1", ports.pop()


def _container_candidate_url(
    runtime_database_url: str,
    container_environment: dict[str, str],
) -> URL:
    runtime_url = make_url(runtime_database_url)
    if runtime_url.get_backend_name() != "postgresql":
        raise ValueError("runtime_database_not_postgresql")
    if not runtime_url.host or not runtime_url.port:
        raise ValueError("runtime_database_network_target_incomplete")
    return URL.create(
        drivername=runtime_url.drivername,
        username=container_environment["POSTGRES_USER"],
        password=container_environment["POSTGRES_PASSWORD"],
        host=runtime_url.host,
        port=runtime_url.port,
        database=container_environment["POSTGRES_DB"],
        query=runtime_url.query,
    )


def _published_container_candidate_url(
    runtime_database_url: str,
    container_environment: dict[str, str],
    *,
    host: str,
    port: int,
) -> URL:
    runtime_url = make_url(runtime_database_url)
    if runtime_url.get_backend_name() != "postgresql":
        raise ValueError("runtime_database_not_postgresql")
    return URL.create(
        drivername=runtime_url.drivername,
        username=container_environment["POSTGRES_USER"],
        password=container_environment["POSTGRES_PASSWORD"],
        host=host,
        port=port,
        database=container_environment["POSTGRES_DB"],
        query=runtime_url.query,
    )


def _replace_database_url_line(text_value: str, database_url: str) -> str:
    replacement = f"{_DATABASE_ENV_KEY}={database_url}"
    lines = text_value.splitlines()
    found = False
    output: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        prefix = "export " if stripped.startswith("export ") else ""
        candidate = stripped[len(prefix) :]
        if candidate.startswith(f"{_DATABASE_ENV_KEY}="):
            if not found:
                indentation = line[: len(line) - len(stripped)]
                output.append(f"{indentation}{prefix}{replacement}")
                found = True
            continue
        output.append(line)
    if not found:
        output.append(replacement)
    suffix = "\n" if text_value.endswith("\n") or not text_value else ""
    return "\n".join(output) + suffix


def _prepare_backup_directory(env_file: Path, backup_dir: Path) -> Path:
    env_parent = env_file.parent.resolve()
    raw_backup = backup_dir.expanduser()
    if raw_backup.exists() and raw_backup.is_symlink():
        raise ValueError("runtime_env_backup_directory_symlink")
    raw_backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved_backup = raw_backup.resolve()
    if resolved_backup == env_parent or env_parent in resolved_backup.parents:
        raise ValueError("runtime_env_backup_must_be_outside_checkout")
    os.chmod(resolved_backup, stat.S_IRWXU)
    return resolved_backup


def _atomic_repair_env_file(
    env_file: Path,
    database_url: URL,
    *,
    backup_dir: Path,
) -> None:
    if not env_file.is_file() or env_file.is_symlink():
        raise ValueError("runtime_env_file_not_regular")
    backup_root = _prepare_backup_directory(env_file, backup_dir)
    original = env_file.read_text(encoding="utf-8")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_name = f"{env_file.parent.name}-{env_file.name}.backup-{timestamp}"
    backup = backup_root / backup_name
    if backup.exists():
        raise ValueError("runtime_env_backup_collision")
    backup.write_text(original, encoding="utf-8")
    os.chmod(backup, stat.S_IRUSR | stat.S_IWUSR)

    rendered = database_url.render_as_string(hide_password=False)
    repaired = _replace_database_url_line(original, rendered)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{env_file.name}.",
        dir=str(env_file.parent),
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(repaired)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary_path, env_file)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def recover_runtime_database_access(
    *,
    env_file: Path,
    container: str,
    docker_context: str,
    repair: bool,
    backup_dir: Path | None = None,
    limit: int = 30,
    docker_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    probe: Callable[[str | URL], DatabaseProbe] = _probe_database,
) -> dict[str, Any]:
    if limit < 1 or limit > 100:
        raise ValueError("preflight_limit_out_of_range")
    if not env_file.is_file() or env_file.is_symlink():
        raise ValueError("runtime_env_file_not_regular")
    if repair and backup_dir is None:
        raise ValueError("runtime_env_backup_directory_required")

    runtime_settings = Settings(_env_file=env_file, _env_file_encoding="utf-8")
    runtime_url = make_url(runtime_settings.database_url)
    runtime_probe = probe(runtime_settings.database_url)

    report: dict[str, Any] = {
        "recovery_version": "r10.1-runtime-db-recovery-v2",
        "runtime_candidate": runtime_probe.as_dict(),
        "container_candidate": {
            "available": False,
            "connection_ready": False,
            "error_code": None,
            "alembic_revisions": [],
        },
        "published_endpoint_candidate": {
            "available": False,
            "host": None,
            "port": None,
            "connection_ready": False,
            "error_code": None,
            "alembic_revisions": [],
        },
        "selected_candidate": "runtime_env" if runtime_probe.connection_ready else "none",
        "secret_source_drift": False,
        "endpoint_source_drift": False,
        "repair_requested": repair,
        "repair_performed": False,
        "backup_created": False,
        "runtime_preflight": None,
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

    selected_settings = runtime_settings
    selected_url: URL | None = None
    if not runtime_probe.connection_ready:
        container_environment = _docker_container_environment(
            container=container,
            docker_context=docker_context,
            runner=docker_runner,
        )
        container_url = _container_candidate_url(
            runtime_settings.database_url,
            container_environment,
        )
        container_probe = probe(container_url)
        report["container_candidate"] = {
            "available": True,
            **container_probe.as_dict(),
        }
        if container_probe.connection_ready:
            report["selected_candidate"] = "container_env"
            report["secret_source_drift"] = True
            selected_url = container_url
        else:
            try:
                published_host, published_port = _docker_published_postgres_endpoint(
                    container=container,
                    docker_context=docker_context,
                    runner=docker_runner,
                )
                published_url = _published_container_candidate_url(
                    runtime_settings.database_url,
                    container_environment,
                    host=published_host,
                    port=published_port,
                )
                published_probe = probe(published_url)
                report["published_endpoint_candidate"] = {
                    "available": True,
                    "host": published_host,
                    "port": published_port,
                    **published_probe.as_dict(),
                }
                if published_probe.connection_ready:
                    report["selected_candidate"] = "container_published_endpoint"
                    report["secret_source_drift"] = True
                    report["endpoint_source_drift"] = (
                        runtime_url.host != published_host
                        or runtime_url.port != published_port
                    )
                    selected_url = published_url
            except (subprocess.SubprocessError, ValueError):
                report["published_endpoint_candidate"]["error_code"] = (
                    "docker_postgres_published_endpoint_unavailable"
                )

        if selected_url is not None:
            selected_settings = Settings(
                _env_file=env_file,
                _env_file_encoding="utf-8",
                database_url=selected_url.render_as_string(hide_password=False),
            )
            if repair:
                assert backup_dir is not None
                _atomic_repair_env_file(
                    env_file,
                    selected_url,
                    backup_dir=backup_dir,
                )
                report["repair_performed"] = True
                report["backup_created"] = True
                selected_settings = Settings(
                    _env_file=env_file,
                    _env_file_encoding="utf-8",
                )

    if report["selected_candidate"] != "none":
        report["runtime_preflight"] = collect_runtime_controlled_provider_preflight(
            selected_settings,
            limit=limit,
        )
    return report
