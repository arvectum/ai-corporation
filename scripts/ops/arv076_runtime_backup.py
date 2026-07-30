"""Fail-closed Docker/Colima backup and isolated PostgreSQL restore tooling.

The command intentionally separates portable data backups from the mutable Colima
VM disk. A backup set is published only after every command succeeds, the source
runtime is returned to its original running state, and the final SHA-256 manifest
verifies cleanly.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import gzip
import hashlib
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = "arv-076-backup-v1"
RESTORE_SCHEMA_VERSION = "arv-076-restore-evidence-v1"
BACKUP_COMPLETE = "BACKUP_COMPLETE"
RESTORE_COMPLETE = "RESTORE_COMPLETE"
MANIFEST_NAME = "MANIFEST.json"
SHA256SUMS_NAME = "SHA256SUMS"
VERIFY_EVIDENCE_NAME = "VERIFY_EVIDENCE.json"
SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|credential|auth|dsn|database[_-]?url|redis[_-]?url|connection[_-]?string)",
    re.IGNORECASE,
)
ENV_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
CREDENTIAL_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@:]+:[^/@]+@")
BACKUP_ID = re.compile(r"^(?P<stamp>\d{8}T\d{6}Z)-(?P<suffix>[0-9a-f]{8})$")


class BackupError(RuntimeError):
    """Raised for a fail-closed backup or restore condition."""


@dataclasses.dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    elapsed_seconds: float


@dataclasses.dataclass(frozen=True)
class DockerClient:
    context: str | None = None
    env: Mapping[str, str] | None = None

    def argv(self, *args: str) -> list[str]:
        base = ["docker"]
        if self.context:
            base.extend(["--context", self.context])
        base.extend(args)
        return base

    def run(
        self,
        *args: str,
        check: bool = True,
        text: bool = True,
        stdout: int | Any = subprocess.PIPE,
        stderr: int | Any = subprocess.PIPE,
        input_data: bytes | str | None = None,
    ) -> subprocess.CompletedProcess[Any]:
        return subprocess.run(
            self.argv(*args),
            check=check,
            text=text,
            stdout=stdout,
            stderr=stderr,
            input=input_data,
            env=dict(self.env) if self.env is not None else None,
        )


@dataclasses.dataclass(frozen=True)
class PostgresSpec:
    container: str
    volume: str
    user: str
    database: str
    image: str


@dataclasses.dataclass(frozen=True)
class RestoreRuntime:
    root: Path
    colima_home: Path
    docker_config: Path
    profile: str

    @property
    def socket(self) -> Path:
        return self.colima_home / self.profile / "docker.sock"

    @property
    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["COLIMA_HOME"] = str(self.colima_home)
        env["DOCKER_CONFIG"] = str(self.docker_config)
        env["DOCKER_HOST"] = f"unix://{self.socket}"
        env["COLIMA_PROFILE"] = self.profile
        return env


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(microsecond=0)


def isoformat(value: dt.datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def ensure_command(name: str) -> None:
    if shutil.which(name) is None:
        raise BackupError(f"required command is unavailable: {name}")


def run_capture(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    check: bool = True,
) -> CommandResult:
    started = time.monotonic()
    process = subprocess.run(
        list(argv),
        check=False,
        text=True,
        capture_output=True,
        env=dict(env) if env is not None else None,
        cwd=cwd,
    )
    elapsed = time.monotonic() - started
    result = CommandResult(
        argv=tuple(argv),
        returncode=process.returncode,
        stdout=process.stdout,
        stderr=process.stderr,
        elapsed_seconds=elapsed,
    )
    if check and process.returncode != 0:
        command = shlex.join(argv)
        stderr = process.stderr.strip() or "no stderr"
        raise BackupError(f"command failed ({process.returncode}): {command}\n{stderr}")
    return result


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, mode)
    temporary.replace(path)


def atomic_write_json(path: Path, payload: Any, *, mode: int = 0o600) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        mode=mode,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def nearest_existing(path: Path) -> Path:
    candidate = path.expanduser().resolve(strict=False)
    while not candidate.exists():
        if candidate.parent == candidate:
            raise BackupError(f"no existing ancestor found for path: {path}")
        candidate = candidate.parent
    return candidate.resolve()


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def assert_independent_destination(
    backup_root: Path,
    *,
    source_colima_home: Path,
    repository_root: Path,
    require_different_device: bool,
) -> None:
    destination = backup_root.expanduser().resolve(strict=False)
    source = source_colima_home.expanduser().resolve(strict=False)
    repository = repository_root.expanduser().resolve(strict=False)

    if destination == source or is_relative_to(destination, source) or is_relative_to(source, destination):
        raise BackupError(
            "backup destination must be independent of the working Colima home: "
            f"destination={destination}, source={source}"
        )
    if destination == repository or is_relative_to(destination, repository):
        raise BackupError("backup destination must not be inside the Git repository")
    if require_different_device:
        destination_device = os.stat(nearest_existing(destination)).st_dev
        source_device = os.stat(nearest_existing(source)).st_dev
        if destination_device == source_device:
            raise BackupError(
                "backup destination is on the same filesystem/device as the working Colima home; "
                "use a second encrypted volume or explicitly pass --no-require-different-device "
                "for a non-acceptance dry run"
            )


def redact_string(value: str) -> str:
    match = ENV_ASSIGNMENT.match(value)
    if match and SENSITIVE_KEY.search(match.group(1)):
        return f"{match.group(1)}=<redacted>"
    if CREDENTIAL_URI.search(value):
        return "<redacted-uri>"
    return value


def redact(value: Any, *, key: str = "") -> Any:
    if SENSITIVE_KEY.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(item_key): redact(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [redact(item, key=key) for item in value]
    if isinstance(value, str):
        return redact_string(value)
    return value


def load_and_redact_yaml(source: Path) -> Any:
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BackupError(f"unable to read YAML config {source}: {exc}") from exc
    return redact(payload)


def safe_filename(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return normalized or "artifact"


def parse_json_lines(text: str) -> list[Any]:
    rows: list[Any] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"raw": line})
    return rows


def select_keys(rows: Sequence[Any], allowed: set[str]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            selected.append({"raw": str(row)})
            continue
        selected.append({key: row[key] for key in sorted(allowed) if key in row})
    return selected


def docker_json_lines(client: DockerClient, *args: str) -> list[Any]:
    result = client.run(*args, check=True, text=True)
    return parse_json_lines(result.stdout)


def docker_version(client: DockerClient) -> dict[str, Any]:
    result = client.run("version", "--format", "{{json .}}", check=True, text=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BackupError("docker version did not return valid JSON") from exc


def docker_info(client: DockerClient) -> dict[str, Any]:
    result = client.run("info", "--format", "{{json .}}", check=True, text=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BackupError("docker info did not return valid JSON") from exc


def inspect_container_running(client: DockerClient, container: str) -> bool:
    result = client.run("inspect", "--format", "{{.State.Running}}", container, text=True)
    return result.stdout.strip().lower() == "true"


def inspect_container_image(client: DockerClient, container: str) -> str:
    result = client.run("inspect", "--format", "{{.Config.Image}}", container, text=True)
    image = result.stdout.strip()
    if not image:
        raise BackupError(f"container has no configured image: {container}")
    return image


def wait_pg_ready(
    client: DockerClient,
    container: str,
    *,
    user: str,
    database: str,
    timeout_seconds: int = 90,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        result = client.run(
            "exec",
            container,
            "pg_isready",
            "--username",
            user,
            "--dbname",
            database,
            check=False,
            text=True,
        )
        if result.returncode == 0:
            return
        last_error = (result.stderr or result.stdout).strip()
        time.sleep(2)
    raise BackupError(f"PostgreSQL did not become ready in {timeout_seconds}s: {last_error}")


def postgres_text(
    client: DockerClient,
    spec: PostgresSpec,
    sql: str,
    *,
    container: str | None = None,
) -> str:
    target = container or spec.container
    result = client.run(
        "exec",
        target,
        "psql",
        "--username",
        spec.user,
        "--dbname",
        spec.database,
        "--no-align",
        "--tuples-only",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        sql,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def database_fingerprint(
    client: DockerClient,
    spec: PostgresSpec,
    *,
    container: str | None = None,
) -> dict[str, Any]:
    target = container or spec.container
    table_output = postgres_text(
        client,
        spec,
        """
        SELECT schemaname || E'\\t' || tablename
        FROM pg_tables
        WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY schemaname, tablename;
        """,
        container=target,
    )
    tables: list[dict[str, Any]] = []
    for line in table_output.splitlines():
        if not line.strip():
            continue
        try:
            schema, table = line.split("\t", 1)
        except ValueError as exc:
            raise BackupError(f"unexpected table-list output: {line!r}") from exc
        count = postgres_text(
            client,
            spec,
            f"SELECT count(*) FROM {quote_identifier(schema)}.{quote_identifier(table)};",
            container=target,
        )
        tables.append({"schema": schema, "table": table, "row_count": int(count)})

    extensions_text = postgres_text(
        client,
        spec,
        "SELECT extname || E'\\t' || extversion FROM pg_extension ORDER BY extname;",
        container=target,
    )
    extensions = []
    for line in extensions_text.splitlines():
        if line.strip():
            name, version = line.split("\t", 1)
            extensions.append({"name": name, "version": version})

    alembic_version: str | None = None
    if any(item["schema"] == "public" and item["table"] == "alembic_version" for item in tables):
        alembic_version = postgres_text(
            client,
            spec,
            "SELECT version_num FROM public.alembic_version ORDER BY version_num LIMIT 1;",
            container=target,
        )

    return {
        "database": spec.database,
        "user": spec.user,
        "server_version": postgres_text(client, spec, "SHOW server_version;", container=target),
        "database_size_bytes": int(
            postgres_text(
                client,
                spec,
                "SELECT pg_database_size(current_database());",
                container=target,
            )
        ),
        "alembic_version": alembic_version,
        "extensions": extensions,
        "tables": tables,
    }


def compare_fingerprints(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    differences: list[str] = []
    for key in ("database", "alembic_version", "tables"):
        if expected.get(key) != actual.get(key):
            differences.append(key)
    expected_exts = {item["name"] for item in expected.get("extensions", [])}
    actual_exts = {item["name"] for item in actual.get("extensions", [])}
    missing = expected_exts - actual_exts
    if missing:
        differences.append("extensions:" + ",".join(sorted(missing)))
    return differences


def dump_postgres(client: DockerClient, spec: PostgresSpec, destination: Path) -> float:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    started = time.monotonic()
    with temporary.open("wb") as stream:
        process = subprocess.run(
            client.argv(
                "exec",
                spec.container,
                "pg_dump",
                "--format=custom",
                "--compress=6",
                "--no-owner",
                "--no-privileges",
                "--username",
                spec.user,
                "--dbname",
                spec.database,
            ),
            check=False,
            stdout=stream,
            stderr=subprocess.PIPE,
            env=dict(client.env) if client.env is not None else None,
        )
    elapsed = time.monotonic() - started
    if process.returncode != 0:
        temporary.unlink(missing_ok=True)
        stderr = process.stderr.decode("utf-8", errors="replace").strip()
        raise BackupError(f"pg_dump failed: {stderr}")
    if temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise BackupError("pg_dump produced an empty file")
    os.chmod(temporary, 0o600)
    temporary.replace(destination)
    return elapsed


def dump_postgres_schema(client: DockerClient, spec: PostgresSpec, destination: Path) -> float:
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result = client.run(
        "exec",
        spec.container,
        "pg_dump",
        "--schema-only",
        "--no-owner",
        "--no-privileges",
        "--username",
        spec.user,
        "--dbname",
        spec.database,
        check=True,
        text=True,
    )
    elapsed = time.monotonic() - started
    atomic_write_text(destination, result.stdout)
    return elapsed


def backup_volume(
    client: DockerClient,
    *,
    volume: str,
    helper_image: str,
    destination: Path,
) -> float:
    client.run("volume", "inspect", volume, text=True)
    client.run("image", "inspect", helper_image, text=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = destination.name + ".tmp"
    temporary_path = destination.parent / temporary_name
    temporary_path.unlink(missing_ok=True)
    started = time.monotonic()
    client.run(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--mount",
        f"type=volume,src={volume},dst=/source,readonly",
        "--mount",
        f"type=bind,src={destination.parent.resolve()},dst=/backup",
        helper_image,
        "sh",
        "-ceu",
        f"cd /source && tar -czpf /backup/{shlex.quote(temporary_name)} .",
        text=True,
    )
    elapsed = time.monotonic() - started
    if not temporary_path.exists() or temporary_path.stat().st_size == 0:
        temporary_path.unlink(missing_ok=True)
        raise BackupError(f"volume archive was not created or is empty: {volume}")
    os.chmod(temporary_path, 0o600)
    temporary_path.replace(destination)
    return elapsed


def restore_volume(
    client: DockerClient,
    *,
    archive: Path,
    target_volume: str,
    helper_image: str,
) -> float:
    if not archive.is_file():
        raise BackupError(f"volume archive is missing: {archive}")
    client.run("volume", "create", target_volume, text=True)
    started = time.monotonic()
    client.run(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--mount",
        f"type=volume,src={target_volume},dst=/target",
        "--mount",
        f"type=bind,src={archive.parent.resolve()},dst=/backup,readonly",
        helper_image,
        "sh",
        "-ceu",
        f"cd /target && tar -xzpf /backup/{shlex.quote(archive.name)}",
        text=True,
    )
    return time.monotonic() - started


def save_image(client: DockerClient, image: str, destination: Path) -> float:
    client.run("image", "inspect", image, text=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    started = time.monotonic()
    save = subprocess.Popen(
        client.argv("image", "save", image),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(client.env) if client.env is not None else None,
    )
    assert save.stdout is not None
    with (
        temporary.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as compressed,
    ):
        shutil.copyfileobj(save.stdout, compressed, length=1024 * 1024)
    stderr = save.stderr.read().decode("utf-8", errors="replace") if save.stderr else ""
    returncode = save.wait()
    elapsed = time.monotonic() - started
    if returncode != 0:
        temporary.unlink(missing_ok=True)
        raise BackupError(f"docker image save failed for {image}: {stderr.strip()}")
    if not temporary.exists() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise BackupError(f"docker image save produced an empty archive: {image}")
    os.chmod(temporary, 0o600)
    temporary.replace(destination)
    return elapsed


def load_image(client: DockerClient, archive: Path) -> float:
    started = time.monotonic()
    with gzip.open(archive, "rb") as stream:
        process = subprocess.run(
            client.argv("image", "load"),
            check=False,
            stdin=stream,
            capture_output=True,
            env=dict(client.env) if client.env is not None else None,
        )
    elapsed = time.monotonic() - started
    if process.returncode != 0:
        stderr = process.stderr.decode("utf-8", errors="replace").strip()
        raise BackupError(f"docker image load failed for {archive}: {stderr}")
    return elapsed


def collect_inventory(
    client: DockerClient,
    *,
    colima_profile: str,
    colima_home: Path,
    repository_root: Path,
    compose_files: Sequence[Path],
) -> dict[str, Any]:
    colima_status = run_capture(
        ["colima", "status", "--profile", colima_profile],
        check=False,
    )
    profile_dir = colima_home.expanduser().resolve(strict=False) / colima_profile
    profile_files: list[dict[str, Any]] = []
    if profile_dir.exists():
        for path in sorted(profile_dir.rglob("*")):
            if path.is_file():
                stat = path.stat()
                profile_files.append(
                    {
                        "path": str(path.relative_to(profile_dir)),
                        "size_bytes": stat.st_size,
                        "modified_at": dt.datetime.fromtimestamp(
                            stat.st_mtime, tz=dt.UTC
                        ).isoformat().replace("+00:00", "Z"),
                    }
                )

    compose_metadata = []
    for compose_file in compose_files:
        resolved = compose_file.expanduser().resolve()
        compose_metadata.append(
            {
                "path": str(resolved.relative_to(repository_root.resolve()))
                if is_relative_to(resolved, repository_root.resolve())
                else str(resolved),
                "size_bytes": resolved.stat().st_size,
                "sha256": sha256_file(resolved),
            }
        )

    context_show = client.run("context", "show", check=True, text=True).stdout.strip()
    context_inspect = client.run("context", "inspect", check=True, text=True).stdout
    try:
        contexts = json.loads(context_inspect)
    except json.JSONDecodeError:
        contexts = [{"raw": context_inspect.strip()}]

    containers = select_keys(
        docker_json_lines(
            client,
            "ps",
            "--all",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ),
        {"ID", "Names", "Image", "State", "Status", "Ports", "Networks", "RunningFor", "CreatedAt"},
    )
    images = select_keys(
        docker_json_lines(
            client,
            "image",
            "ls",
            "--digests",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ),
        {"ID", "Repository", "Tag", "Digest", "Size", "CreatedAt", "CreatedSince"},
    )
    networks = select_keys(
        docker_json_lines(
            client,
            "network",
            "ls",
            "--no-trunc",
            "--format",
            "{{json .}}",
        ),
        {"ID", "Name", "Driver", "Scope", "IPv6", "Internal"},
    )
    volumes = select_keys(
        docker_json_lines(
            client,
            "volume",
            "ls",
            "--format",
            "{{json .}}",
        ),
        {"Name", "Driver", "Scope", "Mountpoint"},
    )

    return redact(
        {
            "schema_version": SCHEMA_VERSION,
            "captured_at": isoformat(),
            "host": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
            },
            "colima": {
                "profile": colima_profile,
                "home": str(colima_home.expanduser().resolve(strict=False)),
                "status": {
                    "returncode": colima_status.returncode,
                    "stdout": colima_status.stdout.strip(),
                    "stderr": colima_status.stderr.strip(),
                },
                "profile_files": profile_files,
            },
            "docker": {
                "version": docker_version(client),
                "info": docker_info(client),
                "context_show": context_show,
                "context_inspect": contexts,
                "containers": containers,
                "images": images,
                "networks": networks,
                "volumes": volumes,
            },
            "compose_files": compose_metadata,
        }
    )


def copy_sanitized_configs(
    destination: Path,
    *,
    colima_home: Path,
    colima_profile: str,
    compose_files: Sequence[Path],
) -> list[str]:
    written: list[str] = []
    colima_config = colima_home.expanduser().resolve(strict=False) / colima_profile / "colima.yaml"
    if colima_config.is_file():
        target = destination / "config" / "colima.yaml"
        atomic_write_text(target, yaml.safe_dump(load_and_redact_yaml(colima_config), sort_keys=False))
        written.append(str(target.relative_to(destination)))

    for index, compose_file in enumerate(compose_files, start=1):
        resolved = compose_file.expanduser().resolve()
        payload = load_and_redact_yaml(resolved)
        name = f"{index:02d}-{safe_filename(resolved.name)}"
        target = destination / "config" / "compose" / name
        atomic_write_text(target, yaml.safe_dump(payload, sort_keys=False))
        written.append(str(target.relative_to(destination)))
    return written


def build_manifest(backup_set: Path, *, backup_id: str, created_at: str) -> dict[str, Any]:
    excluded = {MANIFEST_NAME, SHA256SUMS_NAME}
    artifacts: list[dict[str, Any]] = []
    for path in sorted(backup_set.rglob("*")):
        if not path.is_file() or path.name in excluded:
            continue
        if path.is_symlink():
            raise BackupError(f"symlink is forbidden inside backup set: {path}")
        relative = path.relative_to(backup_set).as_posix()
        artifacts.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    if not artifacts:
        raise BackupError("backup set has no artifacts")
    return {
        "schema_version": SCHEMA_VERSION,
        "backup_id": backup_id,
        "created_at": created_at,
        "hash_algorithm": "sha256",
        "manifest_excludes": sorted(excluded),
        "total_size_bytes": sum(item["size_bytes"] for item in artifacts),
        "artifacts": artifacts,
    }


def write_manifest(backup_set: Path, manifest: Mapping[str, Any]) -> None:
    atomic_write_json(backup_set / MANIFEST_NAME, manifest)
    lines = [f"{item['sha256']}  {item['path']}" for item in manifest["artifacts"]]
    atomic_write_text(backup_set / SHA256SUMS_NAME, "\n".join(lines) + "\n")


def verify_backup_set(backup_set: Path, *, require_complete: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    root = backup_set.expanduser().resolve()
    manifest_path = root / MANIFEST_NAME
    sums_path = root / SHA256SUMS_NAME
    if not root.is_dir():
        raise BackupError(f"backup set is not a directory: {root}")
    if require_complete and not (root / BACKUP_COMPLETE).is_file():
        raise BackupError(f"backup set is incomplete: missing {BACKUP_COMPLETE}")
    if not manifest_path.is_file() or not sums_path.is_file():
        raise BackupError("backup set is missing manifest files")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"invalid manifest: {exc}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BackupError(f"unsupported manifest schema: {manifest.get('schema_version')}")

    listed: set[str] = set()
    failures: list[dict[str, Any]] = []
    for item in manifest.get("artifacts", []):
        relative = str(item.get("path", ""))
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            failures.append({"path": relative, "reason": "unsafe_path"})
            continue
        if relative in listed:
            failures.append({"path": relative, "reason": "duplicate_manifest_entry"})
            continue
        listed.add(relative)
        path = root / relative
        if path.is_symlink():
            failures.append({"path": relative, "reason": "symlink_forbidden"})
            continue
        if not path.is_file():
            failures.append({"path": relative, "reason": "missing"})
            continue
        size = path.stat().st_size
        digest = sha256_file(path)
        if size != item.get("size_bytes"):
            failures.append(
                {
                    "path": relative,
                    "reason": "size_mismatch",
                    "expected": item.get("size_bytes"),
                    "actual": size,
                }
            )
        if digest != item.get("sha256"):
            failures.append(
                {
                    "path": relative,
                    "reason": "sha256_mismatch",
                    "expected": item.get("sha256"),
                    "actual": digest,
                }
            )

    allowed_unlisted = {MANIFEST_NAME, SHA256SUMS_NAME}
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    extras = sorted(actual - listed - allowed_unlisted)
    for extra in extras:
        failures.append({"path": extra, "reason": "unlisted_artifact"})

    expected_sums = "\n".join(
        f"{item['sha256']}  {item['path']}" for item in manifest.get("artifacts", [])
    ) + "\n"
    actual_sums = sums_path.read_text(encoding="utf-8")
    if actual_sums != expected_sums:
        failures.append({"path": SHA256SUMS_NAME, "reason": "manifest_text_mismatch"})

    evidence = {
        "schema_version": SCHEMA_VERSION,
        "backup_id": manifest.get("backup_id"),
        "verified_at": isoformat(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "artifact_count": len(listed),
        "total_size_bytes": sum(int(item.get("size_bytes", 0)) for item in manifest.get("artifacts", [])),
        "verified": not failures,
        "failures": failures,
    }
    if failures:
        raise BackupError("backup verification failed: " + json.dumps(failures, ensure_ascii=False))
    return evidence


@contextlib.contextmanager
def stopped_containers(
    client: DockerClient,
    containers: Iterable[str],
    *,
    timeout_seconds: int,
) -> Iterable[list[str]]:
    unique = list(dict.fromkeys(item for item in containers if item))
    originally_running = [item for item in unique if inspect_container_running(client, item)]
    stopped: list[str] = []
    try:
        for container in originally_running:
            client.run("stop", "--time", str(timeout_seconds), container, text=True)
            stopped.append(container)
        yield stopped
    finally:
        failures: list[str] = []
        for container in stopped:
            result = client.run("start", container, check=False, text=True)
            if result.returncode != 0:
                failures.append(container)
        if failures:
            raise BackupError("failed to restart source containers: " + ", ".join(failures))


def parse_volume_container(items: Sequence[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise BackupError(f"expected VOLUME=CONTAINER mapping, got: {item}")
        volume, container = item.split("=", 1)
        if not volume or not container:
            raise BackupError(f"invalid VOLUME=CONTAINER mapping: {item}")
        mapping[volume] = container
    return mapping


def backup_command(args: argparse.Namespace) -> int:
    ensure_command("docker")
    ensure_command("colima")
    repository_root = Path(args.repository_root).expanduser().resolve()
    backup_root = Path(args.backup_root).expanduser().resolve(strict=False)
    colima_home = Path(args.colima_home).expanduser().resolve(strict=False)
    if not colima_home.is_dir():
        raise BackupError(f"working Colima home is missing: {colima_home}")
    profile_dir = colima_home / args.colima_profile
    if not profile_dir.is_dir():
        raise BackupError(f"working Colima profile is missing: {profile_dir}")
    compose_files = [Path(item).expanduser().resolve() for item in args.compose_file]
    for compose_file in compose_files:
        if not compose_file.is_file():
            raise BackupError(f"compose file is missing: {compose_file}")
    assert_independent_destination(
        backup_root,
        source_colima_home=colima_home,
        repository_root=repository_root,
        require_different_device=args.require_different_device,
    )
    if not args.confirm_production_downtime:
        raise BackupError(
            "cold named-volume backup requires --confirm-production-downtime; "
            "the PostgreSQL container will be stopped and restarted"
        )

    client = DockerClient(context=args.docker_context)
    if not inspect_container_running(client, args.postgres_container):
        raise BackupError(f"PostgreSQL source container is not running: {args.postgres_container}")
    postgres_image = inspect_container_image(client, args.postgres_container)
    spec = PostgresSpec(
        container=args.postgres_container,
        volume=args.postgres_volume,
        user=args.postgres_user,
        database=args.postgres_database,
        image=postgres_image,
    )
    volumes = list(dict.fromkeys([args.postgres_volume, *args.volume]))
    volume_container = parse_volume_container(args.volume_container)
    volume_container.setdefault(args.postgres_volume, args.postgres_container)
    missing_mappings = [volume for volume in volumes if volume not in volume_container]
    if missing_mappings:
        raise BackupError(
            "every cold volume needs a stop-container mapping; missing: " + ", ".join(missing_mappings)
        )

    backup_root.mkdir(parents=True, exist_ok=True)
    backup_root.chmod(0o700)
    created = utc_now()
    backup_id = created.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
    incomplete = backup_root / f".incomplete-{backup_id}"
    final = backup_root / backup_id
    if incomplete.exists() or final.exists():
        raise BackupError(f"backup set already exists: {backup_id}")
    incomplete.mkdir(mode=0o700)
    metrics: dict[str, Any] = {"started_at": isoformat(created), "steps": {}}
    backup_started = time.monotonic()

    try:
        inventory = collect_inventory(
            client,
            colima_profile=args.colima_profile,
            colima_home=colima_home,
            repository_root=repository_root,
            compose_files=compose_files,
        )
        atomic_write_json(incomplete / "metadata" / "inventory.json", inventory)
        config_files = copy_sanitized_configs(
            incomplete,
            colima_home=colima_home,
            colima_profile=args.colima_profile,
            compose_files=compose_files,
        )

        pg_dump_elapsed = dump_postgres(client, spec, incomplete / "postgres" / "postgres.dump")
        schema_elapsed = dump_postgres_schema(
            client, spec, incomplete / "postgres" / "schema.sql"
        )
        fingerprint = database_fingerprint(client, spec)
        atomic_write_json(incomplete / "postgres" / "fingerprint.json", fingerprint)
        metrics["steps"]["postgres_dump_seconds"] = round(pg_dump_elapsed, 3)
        metrics["steps"]["postgres_schema_seconds"] = round(schema_elapsed, 3)

        images_to_save = [postgres_image, args.helper_image]
        if args.save_running_images:
            for row in inventory["docker"]["containers"]:
                image = row.get("Image") if isinstance(row, Mapping) else None
                if image:
                    images_to_save.append(str(image))
        image_records: list[dict[str, Any]] = []
        for image in dict.fromkeys(images_to_save):
            name = safe_filename(image) + ".tar.gz"
            elapsed = save_image(client, image, incomplete / "images" / name)
            image_records.append({"image": image, "archive": f"images/{name}", "seconds": round(elapsed, 3)})
        atomic_write_json(incomplete / "metadata" / "image_archives.json", image_records)

        stop_targets = [volume_container[volume] for volume in volumes]
        with stopped_containers(client, stop_targets, timeout_seconds=args.stop_timeout) as stopped:
            volume_records: list[dict[str, Any]] = []
            for volume in volumes:
                archive_name = safe_filename(volume) + ".tar.gz"
                elapsed = backup_volume(
                    client,
                    volume=volume,
                    helper_image=args.helper_image,
                    destination=incomplete / "volumes" / archive_name,
                )
                volume_records.append(
                    {
                        "source_volume": volume,
                        "source_container": volume_container[volume],
                        "archive": f"volumes/{archive_name}",
                        "seconds": round(elapsed, 3),
                    }
                )
            atomic_write_json(incomplete / "metadata" / "volume_archives.json", volume_records)
            metrics["source_containers_stopped"] = stopped

        wait_pg_ready(
            client,
            spec.container,
            user=spec.user,
            database=spec.database,
            timeout_seconds=args.postgres_ready_timeout,
        )
        post_backup_fingerprint = database_fingerprint(client, spec)
        differences = compare_fingerprints(fingerprint, post_backup_fingerprint)
        if differences:
            raise BackupError(
                "source PostgreSQL fingerprint changed across cold volume backup: "
                + ", ".join(differences)
            )

        metrics["finished_at"] = isoformat()
        metrics["total_seconds"] = round(time.monotonic() - backup_started, 3)
        metrics["config_files"] = config_files
        metrics["backup_id"] = backup_id
        metrics["postgres"] = dataclasses.asdict(spec)
        metrics["postgres"]["image"] = postgres_image
        atomic_write_json(incomplete / "metadata" / "backup_metrics.json", metrics)

        preliminary = build_manifest(incomplete, backup_id=backup_id, created_at=isoformat(created))
        write_manifest(incomplete, preliminary)
        first_verify = verify_backup_set(incomplete, require_complete=False)
        atomic_write_json(incomplete / VERIFY_EVIDENCE_NAME, first_verify)
        atomic_write_text(incomplete / BACKUP_COMPLETE, isoformat() + "\n")
        final_manifest = build_manifest(incomplete, backup_id=backup_id, created_at=isoformat(created))
        write_manifest(incomplete, final_manifest)
        final_verify = verify_backup_set(incomplete, require_complete=True)
        if not final_verify["verified"]:
            raise BackupError("final verification did not pass")

        incomplete.replace(final)
        print(json.dumps({"backup_set": str(final), "verification": final_verify}, ensure_ascii=False))
        return 0
    except Exception:
        atomic_write_json(
            incomplete / "FAILED.json",
            {
                "schema_version": SCHEMA_VERSION,
                "backup_id": backup_id,
                "failed_at": isoformat(),
                "error": "backup_failed; inspect local stderr and incomplete set",
            },
        )
        raise


def verify_command(args: argparse.Namespace) -> int:
    evidence = verify_backup_set(Path(args.backup_set), require_complete=True)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0


def inventory_command(args: argparse.Namespace) -> int:
    ensure_command("docker")
    ensure_command("colima")
    repository_root = Path(args.repository_root).expanduser().resolve()
    compose_files = [Path(item).expanduser().resolve() for item in args.compose_file]
    client = DockerClient(context=args.docker_context)
    inventory = collect_inventory(
        client,
        colima_profile=args.colima_profile,
        colima_home=Path(args.colima_home),
        repository_root=repository_root,
        compose_files=compose_files,
    )
    if args.output:
        atomic_write_json(Path(args.output), inventory)
    else:
        print(json.dumps(inventory, ensure_ascii=False, indent=2))
    return 0


def start_isolated_colima(runtime: RestoreRuntime, args: argparse.Namespace) -> float:
    ensure_command("colima")
    runtime.root.mkdir(parents=True, exist_ok=False)
    runtime.colima_home.mkdir(parents=True)
    runtime.docker_config.mkdir(parents=True)
    started = time.monotonic()
    result = run_capture(
        [
            "colima",
            "start",
            "--profile",
            runtime.profile,
            "--runtime",
            "docker",
            "--cpu",
            str(args.restore_cpus),
            "--memory",
            str(args.restore_memory_gib),
            "--disk",
            str(args.restore_disk_gib),
        ],
        env=runtime.env,
        check=False,
    )
    if result.returncode != 0:
        raise BackupError(f"isolated Colima start failed: {result.stderr.strip()}")
    deadline = time.monotonic() + args.docker_ready_timeout
    client = DockerClient(env=runtime.env)
    last_error = ""
    while time.monotonic() < deadline:
        probe = client.run("version", check=False, text=True)
        if probe.returncode == 0:
            return time.monotonic() - started
        last_error = (probe.stderr or probe.stdout).strip()
        time.sleep(2)
    raise BackupError(f"isolated Docker did not become ready: {last_error}")


def remove_isolated_colima(runtime: RestoreRuntime) -> None:
    run_capture(
        ["colima", "stop", "--profile", runtime.profile],
        env=runtime.env,
        check=False,
    )
    run_capture(
        ["colima", "delete", "--force", "--profile", runtime.profile],
        env=runtime.env,
        check=False,
    )


def find_postgres_metadata(backup_set: Path) -> tuple[PostgresSpec, dict[str, Any], list[dict[str, Any]]]:
    metrics = json.loads((backup_set / "metadata" / "backup_metrics.json").read_text(encoding="utf-8"))
    postgres = metrics["postgres"]
    spec = PostgresSpec(
        container=str(postgres["container"]),
        volume=str(postgres["volume"]),
        user=str(postgres["user"]),
        database=str(postgres["database"]),
        image=str(postgres["image"]),
    )
    fingerprint = json.loads((backup_set / "postgres" / "fingerprint.json").read_text(encoding="utf-8"))
    volume_records = json.loads(
        (backup_set / "metadata" / "volume_archives.json").read_text(encoding="utf-8")
    )
    return spec, fingerprint, volume_records


def run_postgres_container(
    client: DockerClient,
    *,
    name: str,
    image: str,
    volume: str,
    user: str,
    database: str,
    password: str | None,
    publish_port: bool,
) -> None:
    args = [
        "run",
        "--detach",
        "--name",
        name,
        "--mount",
        f"type=volume,src={volume},dst=/var/lib/postgresql/data",
    ]
    if publish_port:
        args.extend(["--publish", "127.0.0.1::5432"])
    if password is not None:
        args.extend(
            [
                "--env",
                f"POSTGRES_USER={user}",
                "--env",
                f"POSTGRES_DB={database}",
                "--env",
                f"POSTGRES_PASSWORD={password}",
            ]
        )
    args.append(image)
    client.run(*args, text=True)


def mapped_port(client: DockerClient, container: str) -> int | None:
    result = client.run("port", container, "5432/tcp", check=False, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    last = result.stdout.strip().splitlines()[-1]
    try:
        return int(last.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return None


def restore_test_command(args: argparse.Namespace) -> int:
    ensure_command("docker")
    ensure_command("colima")
    backup_set = Path(args.backup_set).expanduser().resolve()
    verification = verify_backup_set(backup_set, require_complete=True)
    spec, expected_fingerprint, volume_records = find_postgres_metadata(backup_set)
    volume_record = next(
        (item for item in volume_records if item.get("source_volume") == spec.volume),
        None,
    )
    if volume_record is None:
        raise BackupError(f"PostgreSQL volume archive is absent: {spec.volume}")

    restore_root = Path(args.restore_root).expanduser().resolve(strict=False)
    runtime = RestoreRuntime(
        root=restore_root,
        colima_home=restore_root / "colima-home",
        docker_config=restore_root / "docker-config",
        profile=args.restore_profile,
    )
    evidence_path = restore_root / "RESTORE_EVIDENCE.json"
    complete_path = restore_root / RESTORE_COMPLETE
    started_at = utc_now()
    restore_started_monotonic = time.monotonic()
    steps: dict[str, Any] = {}
    created_resources: dict[str, list[str]] = {"containers": [], "volumes": []}
    client: DockerClient | None = None
    success = False

    try:
        steps["colima_start_seconds"] = round(start_isolated_colima(runtime, args), 3)
        client = DockerClient(env=runtime.env)
        steps["docker_version"] = docker_version(client)

        image_metadata = json.loads(
            (backup_set / "metadata" / "image_archives.json").read_text(encoding="utf-8")
        )
        loaded_images = []
        for item in image_metadata:
            archive = backup_set / item["archive"]
            elapsed = load_image(client, archive)
            loaded_images.append(
                {"image": item["image"], "archive": item["archive"], "seconds": round(elapsed, 3)}
            )
        steps["loaded_images"] = loaded_images
        client.run("image", "inspect", spec.image, text=True)

        helper_image = args.helper_image or spec.image
        client.run("image", "inspect", helper_image, text=True)
        suffix = secrets.token_hex(4)
        raw_volume = f"arv076_raw_{suffix}"
        raw_container = f"arv076-raw-postgres-{suffix}"
        logical_volume = f"arv076_logical_{suffix}"
        logical_container = f"arv076-logical-postgres-{suffix}"

        raw_archive = backup_set / volume_record["archive"]
        steps["raw_volume_restore_seconds"] = round(
            restore_volume(
                client,
                archive=raw_archive,
                target_volume=raw_volume,
                helper_image=helper_image,
            ),
            3,
        )
        created_resources["volumes"].append(raw_volume)
        run_postgres_container(
            client,
            name=raw_container,
            image=spec.image,
            volume=raw_volume,
            user=spec.user,
            database=spec.database,
            password=None,
            publish_port=False,
        )
        created_resources["containers"].append(raw_container)
        wait_pg_ready(
            client,
            raw_container,
            user=spec.user,
            database=spec.database,
            timeout_seconds=args.postgres_ready_timeout,
        )
        raw_fingerprint = database_fingerprint(client, spec, container=raw_container)
        raw_differences = compare_fingerprints(expected_fingerprint, raw_fingerprint)
        if raw_differences:
            raise BackupError("raw volume restore fingerprint mismatch: " + ", ".join(raw_differences))
        steps["raw_restore_fingerprint_match"] = True
        client.run("rm", "--force", raw_container, text=True)
        created_resources["containers"].remove(raw_container)

        client.run("volume", "create", logical_volume, text=True)
        created_resources["volumes"].append(logical_volume)
        password = "restore-" + secrets.token_urlsafe(24)
        run_postgres_container(
            client,
            name=logical_container,
            image=spec.image,
            volume=logical_volume,
            user=spec.user,
            database=spec.database,
            password=password,
            publish_port=args.keep_runtime,
        )
        created_resources["containers"].append(logical_container)
        wait_pg_ready(
            client,
            logical_container,
            user=spec.user,
            database=spec.database,
            timeout_seconds=args.postgres_ready_timeout,
        )
        client.run("cp", str(backup_set / "postgres" / "postgres.dump"), f"{logical_container}:/tmp/postgres.dump", text=True)
        restore_started = time.monotonic()
        client.run(
            "exec",
            logical_container,
            "pg_restore",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--username",
            spec.user,
            "--dbname",
            spec.database,
            "/tmp/postgres.dump",
            text=True,
        )
        steps["logical_restore_seconds"] = round(time.monotonic() - restore_started, 3)
        logical_fingerprint = database_fingerprint(client, spec, container=logical_container)
        logical_differences = compare_fingerprints(expected_fingerprint, logical_fingerprint)
        if logical_differences:
            raise BackupError(
                "logical restore fingerprint mismatch: " + ", ".join(logical_differences)
            )
        steps["logical_restore_fingerprint_match"] = True
        steps["pg_isready"] = True
        steps["sql_smoke"] = postgres_text(
            client,
            spec,
            "SELECT current_database() || E'\\t' || current_user || E'\\t' || count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema');",
            container=logical_container,
        )
        steps["docker_inventory"] = {
            "containers": docker_json_lines(
                client, "ps", "--all", "--no-trunc", "--format", "{{json .}}"
            ),
            "volumes": docker_json_lines(client, "volume", "ls", "--format", "{{json .}}"),
        }
        if args.keep_runtime:
            steps["kept_runtime"] = {
                "colima_home": str(runtime.colima_home),
                "docker_config": str(runtime.docker_config),
                "docker_host": runtime.env["DOCKER_HOST"],
                "profile": runtime.profile,
                "postgres_container": logical_container,
                "postgres_volume": logical_volume,
                "postgres_port": mapped_port(client, logical_container),
                "postgres_user": spec.user,
                "postgres_database": spec.database,
                "postgres_password_file": str(restore_root / "postgres-password.txt"),
            }
            atomic_write_text(restore_root / "postgres-password.txt", password + "\n")
        success = True
    finally:
        evidence = {
            "schema_version": RESTORE_SCHEMA_VERSION,
            "backup_set": str(backup_set),
            "backup_verification": verification,
            "restore_profile": runtime.profile,
            "restore_root": str(restore_root),
            "started_at": isoformat(started_at),
            "finished_at": isoformat(),
            "elapsed_seconds": round(time.monotonic() - restore_started_monotonic, 3),
            "success": success,
            "steps": steps,
            "resources": created_resources,
        }
        if restore_root.exists():
            atomic_write_json(evidence_path, evidence)
            if success:
                atomic_write_text(complete_path, isoformat() + "\n")
        if client is not None and (not args.keep_runtime or not success):
            for container in list(created_resources["containers"]):
                client.run("rm", "--force", container, check=False, text=True)
            for volume in list(created_resources["volumes"]):
                client.run("volume", "rm", volume, check=False, text=True)
        if runtime.root.exists() and (not args.keep_runtime or not success):
            remove_isolated_colima(runtime)

    if not success:
        raise BackupError("isolated restore did not complete")
    print(json.dumps({"restore_evidence": str(evidence_path), "steps": steps}, ensure_ascii=False))
    return 0


def parse_backup_timestamp(path: Path) -> dt.datetime | None:
    match = BACKUP_ID.match(path.name)
    if not match:
        return None
    return dt.datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)


def retention_keep(
    backups: Sequence[tuple[Path, dt.datetime]],
    *,
    daily: int,
    weekly: int,
    monthly: int,
) -> set[Path]:
    ordered = sorted(backups, key=lambda item: item[1], reverse=True)
    keep: set[Path] = set()

    def choose(bucket: Any, limit: int) -> None:
        seen: set[Any] = set()
        for path, timestamp in ordered:
            key = bucket(timestamp)
            if key in seen:
                continue
            seen.add(key)
            keep.add(path)
            if len(seen) >= limit:
                break

    if daily > 0:
        choose(lambda value: value.date(), daily)
    if weekly > 0:
        choose(lambda value: value.isocalendar()[:2], weekly)
    if monthly > 0:
        choose(lambda value: (value.year, value.month), monthly)
    if ordered:
        keep.add(ordered[0][0])
    return keep


def prune_command(args: argparse.Namespace) -> int:
    root = Path(args.backup_root).expanduser().resolve()
    if not root.is_dir():
        raise BackupError(f"backup root does not exist: {root}")
    backups: list[tuple[Path, dt.datetime]] = []
    for path in root.iterdir():
        if not path.is_dir() or path.name.startswith(".incomplete-"):
            continue
        timestamp = parse_backup_timestamp(path)
        if timestamp is None:
            continue
        verify_backup_set(path, require_complete=True)
        backups.append((path, timestamp))
    keep = retention_keep(
        backups,
        daily=args.keep_daily,
        weekly=args.keep_weekly,
        monthly=args.keep_monthly,
    )
    delete = [path for path, _ in backups if path not in keep]
    payload = {
        "backup_root": str(root),
        "keep": sorted(path.name for path in keep),
        "delete": sorted(path.name for path in delete),
        "apply": args.apply,
    }
    if args.apply:
        for path in delete:
            shutil.rmtree(path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def common_inventory_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository-root", default=str(Path.cwd()))
    parser.add_argument("--colima-profile", default=os.environ.get("COLIMA_PROFILE", "default"))
    parser.add_argument("--colima-home", default=os.environ.get("COLIMA_HOME", str(Path.home() / ".colima")))
    parser.add_argument("--docker-context")
    parser.add_argument(
        "--compose-file",
        action="append",
        default=[str(Path.cwd() / "docker-compose.yml")],
        help="repeatable; YAML is copied only after recursive secret redaction",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory", help="print a secret-redacted runtime inventory")
    common_inventory_arguments(inventory)
    inventory.add_argument("--output")
    inventory.set_defaults(handler=inventory_command)

    backup = subparsers.add_parser("backup", help="create and immediately verify a versioned backup set")
    common_inventory_arguments(backup)
    backup.add_argument("--backup-root", required=True)
    backup.add_argument("--postgres-container", default="arvectum-postgres")
    backup.add_argument("--postgres-volume", default="arvectum-postgres_arvectum_postgres_data")
    backup.add_argument("--postgres-user", default="ai_corporation")
    backup.add_argument("--postgres-database", default="ai_corporation")
    backup.add_argument("--volume", action="append", default=[])
    backup.add_argument(
        "--volume-container",
        action="append",
        default=[],
        help="repeatable VOLUME=CONTAINER; every cold volume must identify a container to stop",
    )
    backup.add_argument("--helper-image", default="postgres:16-alpine")
    backup.add_argument("--save-running-images", action="store_true")
    backup.add_argument("--stop-timeout", type=int, default=60)
    backup.add_argument("--postgres-ready-timeout", type=int, default=90)
    backup.add_argument("--confirm-production-downtime", action="store_true")
    backup.add_argument(
        "--require-different-device",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    backup.set_defaults(handler=backup_command)

    verify = subparsers.add_parser("verify", help="verify all SHA-256 hashes and completeness markers")
    verify.add_argument("--backup-set", required=True)
    verify.set_defaults(handler=verify_command)

    restore = subparsers.add_parser(
        "restore-test",
        help="restore raw volume and pg_dump into an isolated COLIMA_HOME",
    )
    restore.add_argument("--backup-set", required=True)
    restore.add_argument("--restore-root", required=True)
    restore.add_argument("--restore-profile", default="arv-076-restore")
    restore.add_argument("--restore-cpus", type=int, default=2)
    restore.add_argument("--restore-memory-gib", type=int, default=4)
    restore.add_argument("--restore-disk-gib", type=int, default=40)
    restore.add_argument("--docker-ready-timeout", type=int, default=180)
    restore.add_argument("--postgres-ready-timeout", type=int, default=90)
    restore.add_argument("--helper-image")
    restore.add_argument(
        "--keep-runtime",
        action="store_true",
        help="leave isolated Colima and logical PostgreSQL running for application smoke",
    )
    restore.set_defaults(handler=restore_test_command)

    prune = subparsers.add_parser("prune", help="GFS rotation; dry-run unless --apply is passed")
    prune.add_argument("--backup-root", required=True)
    prune.add_argument("--keep-daily", type=int, default=7)
    prune.add_argument("--keep-weekly", type=int, default=4)
    prune.add_argument("--keep-monthly", type=int, default=6)
    prune.add_argument("--apply", action="store_true")
    prune.set_defaults(handler=prune_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except BackupError as exc:
        print(f"ARV-076 FAIL-CLOSED: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ARV-076 interrupted; backup/restore is not accepted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
