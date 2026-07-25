from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.engine import make_url

from src.shared.config.settings import Settings

_POSTGRES_PORT_KEY = "5432/tcp"
_REQUIRED_POSTGRES_ENV_KEYS = frozenset(
    {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"}
)


@dataclass(frozen=True)
class HostListenerProbe:
    open: bool
    error_code: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"open": self.open, "error_code": self.error_code}


def _probe_host_listener(host: str, port: int, *, timeout: float = 1.0) -> HostListenerProbe:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return HostListenerProbe(True, None)
    except ConnectionRefusedError:
        return HostListenerProbe(False, "connection_refused")
    except TimeoutError:
        return HostListenerProbe(False, "connection_timeout")
    except OSError:
        return HostListenerProbe(False, "connection_failed")


def _docker_container_ids(
    *,
    docker_context: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[str]:
    completed = runner(
        [
            "docker",
            "--context",
            docker_context,
            "ps",
            "-aq",
            "--no-trunc",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _docker_inspect_containers(
    container_ids: list[str],
    *,
    docker_context: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[dict[str, Any]]:
    if not container_ids:
        return []
    completed = runner(
        ["docker", "--context", docker_context, "inspect", *container_ids],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, list):
        raise ValueError("docker_inspect_payload_invalid")
    return [item for item in payload if isinstance(item, dict)]


def _environment_key_set(raw_environment: Any) -> set[str]:
    if not isinstance(raw_environment, list):
        return set()
    keys: set[str] = set()
    for item in raw_environment:
        if isinstance(item, str) and "=" in item:
            keys.add(item.split("=", 1)[0])
    return keys


def _published_bindings(raw_ports: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_ports, dict):
        return []
    bindings: list[dict[str, Any]] = []
    for container_port, raw_bindings in raw_ports.items():
        if not isinstance(container_port, str) or not isinstance(raw_bindings, list):
            continue
        for raw_binding in raw_bindings:
            if not isinstance(raw_binding, dict):
                continue
            host_port = raw_binding.get("HostPort")
            host_ip = raw_binding.get("HostIp")
            if not isinstance(host_port, str) or not host_port.isdigit():
                continue
            parsed_port = int(host_port)
            if not 1 <= parsed_port <= 65535:
                continue
            bindings.append(
                {
                    "container_port": container_port,
                    "host_ip": host_ip if isinstance(host_ip, str) else None,
                    "host_port": parsed_port,
                }
            )
    return sorted(
        bindings,
        key=lambda item: (
            item["host_port"],
            item["container_port"],
            item["host_ip"] or "",
        ),
    )


def _container_summary(container: dict[str, Any], *, runtime_port: int) -> dict[str, Any]:
    config = container.get("Config") if isinstance(container.get("Config"), dict) else {}
    state = container.get("State") if isinstance(container.get("State"), dict) else {}
    network = (
        container.get("NetworkSettings")
        if isinstance(container.get("NetworkSettings"), dict)
        else {}
    )
    labels = config.get("Labels") if isinstance(config.get("Labels"), dict) else {}
    health = state.get("Health") if isinstance(state.get("Health"), dict) else {}
    environment_keys = _environment_key_set(config.get("Env"))
    bindings = _published_bindings(network.get("Ports"))
    exposed_ports = config.get("ExposedPorts")
    exposed_port_keys = set(exposed_ports) if isinstance(exposed_ports, dict) else set()
    raw_name = container.get("Name")
    name = raw_name.lstrip("/") if isinstance(raw_name, str) else None
    image = config.get("Image") if isinstance(config.get("Image"), str) else None
    image_lower = image.lower() if image else ""
    postgres_like = bool(
        _POSTGRES_PORT_KEY in exposed_port_keys
        or _REQUIRED_POSTGRES_ENV_KEYS.issubset(environment_keys)
        or "postgres" in image_lower
    )
    return {
        "name": name,
        "image": image,
        "status": state.get("Status") if isinstance(state.get("Status"), str) else None,
        "health": health.get("Status") if isinstance(health.get("Status"), str) else None,
        "compose_project": labels.get("com.docker.compose.project"),
        "compose_service": labels.get("com.docker.compose.service"),
        "postgres_like": postgres_like,
        "postgres_env_keys_present": _REQUIRED_POSTGRES_ENV_KEYS.issubset(
            environment_keys
        ),
        "exposes_postgres_port": _POSTGRES_PORT_KEY in exposed_port_keys,
        "published_bindings": bindings,
        "runtime_port_match": any(
            binding["host_port"] == runtime_port for binding in bindings
        ),
    }


def _resolution_code(
    *,
    listener_open: bool,
    target: dict[str, Any] | None,
    port_owners: list[dict[str, Any]],
) -> str:
    if not listener_open:
        return "RUNTIME_ENDPOINT_CLOSED"
    if len(port_owners) > 1:
        return "AMBIGUOUS_DOCKER_PORT_OWNERSHIP"
    if len(port_owners) == 1:
        owner = port_owners[0]
        if target is not None and owner.get("name") == target.get("name"):
            return "TARGET_CONTAINER_PUBLISHED_ENDPOINT_IDENTIFIED"
        return "RUNTIME_ENDPOINT_OWNED_BY_DIFFERENT_CONTAINER"
    if target is not None and not target.get("published_bindings"):
        return "TARGET_CONTAINER_NETWORK_ONLY_RUNTIME_ENDPOINT_UNIDENTIFIED"
    return "RUNTIME_ENDPOINT_NON_DOCKER_OR_UNIDENTIFIED"


def collect_runtime_database_topology(
    *,
    env_file: Path,
    target_container: str,
    docker_context: str,
    docker_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    listener_probe: Callable[[str, int], HostListenerProbe] = _probe_host_listener,
) -> dict[str, Any]:
    if not env_file.is_file() or env_file.is_symlink():
        raise ValueError("runtime_env_file_not_regular")
    settings = Settings(_env_file=env_file, _env_file_encoding="utf-8")
    runtime_url = make_url(settings.database_url)
    if runtime_url.get_backend_name() != "postgresql":
        raise ValueError("runtime_database_not_postgresql")
    if not runtime_url.host or not runtime_url.port:
        raise ValueError("runtime_database_network_target_incomplete")

    container_ids = _docker_container_ids(
        docker_context=docker_context,
        runner=docker_runner,
    )
    inspected = _docker_inspect_containers(
        container_ids,
        docker_context=docker_context,
        runner=docker_runner,
    )
    summaries = sorted(
        (
            _container_summary(container, runtime_port=runtime_url.port)
            for container in inspected
        ),
        key=lambda item: item.get("name") or "",
    )
    postgres_candidates = [item for item in summaries if item["postgres_like"]]
    target = next(
        (item for item in summaries if item.get("name") == target_container),
        None,
    )
    port_owners = [item for item in summaries if item["runtime_port_match"]]
    listener = listener_probe(runtime_url.host, runtime_url.port)
    resolution = _resolution_code(
        listener_open=listener.open,
        target=target,
        port_owners=port_owners,
    )

    return {
        "topology_version": "r10.1-runtime-db-topology-v1",
        "runtime_target": {
            "dialect": runtime_url.get_backend_name(),
            "host": runtime_url.host,
            "port": runtime_url.port,
            "database_name": runtime_url.database,
            "username_recorded": False,
            "password_recorded": False,
            "database_url_recorded": False,
        },
        "host_listener": listener.as_dict(),
        "target_container": target,
        "postgres_candidate_count": len(postgres_candidates),
        "postgres_candidates": postgres_candidates,
        "runtime_port_owner_count": len(port_owners),
        "runtime_port_owners": port_owners,
        "resolution_code": resolution,
        "mutation_performed": False,
        "provider_called": False,
        "safety": {
            "container_environment_values_recorded": False,
            "database_username_recorded": False,
            "database_password_recorded": False,
            "database_url_recorded": False,
            "env_file_path_recorded": False,
            "document_text_recorded": False,
        },
    }
