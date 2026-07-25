from __future__ import annotations

import json
import subprocess
from pathlib import Path

from src.modules.production_llm_analysis.runtime_db_topology import (
    HostListenerProbe,
    collect_runtime_database_topology,
)


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _env_file(tmp_path: Path, *, port: int = 55432) -> Path:
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "AI_CORP_DATABASE_URL="
        f"postgresql+psycopg://secret-user:secret-password@127.0.0.1:{port}/arvectum\n",
        encoding="utf-8",
    )
    return env_file


def _container(
    *,
    name: str,
    image: str,
    bindings: list[dict] | None,
    environment: list[str] | None = None,
    status: str = "running",
    health: str | None = "healthy",
    project: str | None = None,
    service: str | None = None,
) -> dict:
    labels: dict[str, str] = {}
    if project is not None:
        labels["com.docker.compose.project"] = project
    if service is not None:
        labels["com.docker.compose.service"] = service
    state: dict = {"Status": status}
    if health is not None:
        state["Health"] = {"Status": health}
    return {
        "Name": f"/{name}",
        "Config": {
            "Image": image,
            "Env": environment or [],
            "Labels": labels,
            "ExposedPorts": {"5432/tcp": {}} if "postgres" in image else {},
        },
        "State": state,
        "NetworkSettings": {"Ports": {"5432/tcp": bindings}},
    }


def _runner_for(containers: list[dict]):
    calls: list[list[str]] = []

    def runner(command, **kwargs):
        calls.append(command)
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        if "ps" in command:
            return _completed("id-1\nid-2\n")
        assert "inspect" in command
        return _completed(json.dumps(containers))

    return runner, calls


def test_topology_identifies_different_container_owning_runtime_port(
    tmp_path: Path,
) -> None:
    target = _container(
        name="arvectum-postgres",
        image="postgres:16-alpine",
        bindings=None,
        environment=[
            "POSTGRES_USER=arvectum",
            "POSTGRES_PASSWORD=must-not-leak",
            "POSTGRES_DB=arvectum",
        ],
        project="legacy-runtime",
        service="postgres",
    )
    owner = _container(
        name="other-postgres",
        image="postgres:16-alpine",
        bindings=[{"HostIp": "127.0.0.1", "HostPort": "55432"}],
        environment=[
            "POSTGRES_USER=other",
            "POSTGRES_PASSWORD=other-secret",
            "POSTGRES_DB=other",
        ],
        project="other-stack",
        service="db",
    )
    runner, calls = _runner_for([target, owner])

    report = collect_runtime_database_topology(
        env_file=_env_file(tmp_path),
        target_container="arvectum-postgres",
        docker_context="desktop-linux",
        docker_runner=runner,
        listener_probe=lambda host, port: HostListenerProbe(True, None),
    )

    assert report["runtime_port_owner_count"] == 1
    assert report["runtime_port_owners"][0]["name"] == "other-postgres"
    assert report["target_container"]["published_bindings"] == []
    assert report["resolution_code"] == (
        "RUNTIME_ENDPOINT_OWNED_BY_DIFFERENT_CONTAINER"
    )
    assert len(calls) == 2

    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert "must-not-leak" not in serialized
    assert "other-secret" not in serialized
    assert "secret-user" not in serialized
    assert "secret-password" not in serialized
    assert str(tmp_path) not in serialized
    assert report["mutation_performed"] is False
    assert report["provider_called"] is False


def test_network_only_target_with_open_unowned_listener_is_explicit(
    tmp_path: Path,
) -> None:
    target = _container(
        name="arvectum-postgres",
        image="postgres:16-alpine",
        bindings=None,
        environment=[
            "POSTGRES_USER=arvectum",
            "POSTGRES_PASSWORD=must-not-leak",
            "POSTGRES_DB=arvectum",
        ],
    )
    runner, _ = _runner_for([target])

    report = collect_runtime_database_topology(
        env_file=_env_file(tmp_path),
        target_container="arvectum-postgres",
        docker_context="desktop-linux",
        docker_runner=runner,
        listener_probe=lambda host, port: HostListenerProbe(True, None),
    )

    assert report["runtime_port_owner_count"] == 0
    assert report["target_container"]["published_bindings"] == []
    assert report["resolution_code"] == (
        "TARGET_CONTAINER_NETWORK_ONLY_RUNTIME_ENDPOINT_UNIDENTIFIED"
    )


def test_target_container_published_endpoint_is_identified(tmp_path: Path) -> None:
    target = _container(
        name="arvectum-postgres",
        image="postgres:16-alpine",
        bindings=[
            {"HostIp": "0.0.0.0", "HostPort": "55432"},
            {"HostIp": "::", "HostPort": "55432"},
        ],
        environment=[
            "POSTGRES_USER=arvectum",
            "POSTGRES_PASSWORD=must-not-leak",
            "POSTGRES_DB=arvectum",
        ],
    )
    runner, _ = _runner_for([target])

    report = collect_runtime_database_topology(
        env_file=_env_file(tmp_path),
        target_container="arvectum-postgres",
        docker_context="desktop-linux",
        docker_runner=runner,
        listener_probe=lambda host, port: HostListenerProbe(True, None),
    )

    assert report["runtime_port_owner_count"] == 1
    assert report["resolution_code"] == (
        "TARGET_CONTAINER_PUBLISHED_ENDPOINT_IDENTIFIED"
    )
    assert len(report["target_container"]["published_bindings"]) == 2


def test_closed_listener_wins_over_container_metadata(tmp_path: Path) -> None:
    target = _container(
        name="arvectum-postgres",
        image="postgres:16-alpine",
        bindings=None,
    )
    runner, _ = _runner_for([target])

    report = collect_runtime_database_topology(
        env_file=_env_file(tmp_path),
        target_container="arvectum-postgres",
        docker_context="desktop-linux",
        docker_runner=runner,
        listener_probe=lambda host, port: HostListenerProbe(
            False,
            "connection_refused",
        ),
    )

    assert report["host_listener"] == {
        "open": False,
        "error_code": "connection_refused",
    }
    assert report["resolution_code"] == "RUNTIME_ENDPOINT_CLOSED"


def test_non_postgres_containers_are_not_reported_as_candidates(tmp_path: Path) -> None:
    target = _container(
        name="arvectum-postgres",
        image="postgres:16-alpine",
        bindings=None,
    )
    web = _container(
        name="arvectum-web",
        image="python:3.12",
        bindings=None,
        environment=["API_TOKEN=must-not-leak"],
        health=None,
    )
    runner, _ = _runner_for([target, web])

    report = collect_runtime_database_topology(
        env_file=_env_file(tmp_path),
        target_container="arvectum-postgres",
        docker_context="desktop-linux",
        docker_runner=runner,
        listener_probe=lambda host, port: HostListenerProbe(True, None),
    )

    assert report["postgres_candidate_count"] == 1
    assert [item["name"] for item in report["postgres_candidates"]] == [
        "arvectum-postgres"
    ]
    assert "must-not-leak" not in json.dumps(report, sort_keys=True)
