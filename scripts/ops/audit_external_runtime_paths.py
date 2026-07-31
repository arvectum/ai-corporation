"""Read-only validation for the external Arvectum runtime root.

The default command is explicitly filesystem-only.  A full audit must opt in
to either a sanitized ``--inventory-json`` adapter or ``--live-runtime``.
Neither mode contacts Ollama, LM Studio, or another model/provider endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import stat as stat_module
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REASON_EXTERNAL_ROOT_NOT_CONFIGURED = "external_root_not_configured"
REASON_EXTERNAL_ROOT_MISSING = "external_root_missing"
REASON_EXTERNAL_ROOT_SYMLINK = "external_root_symlink"
REASON_EXTERNAL_ROOT_NOT_DIRECTORY = "external_root_not_directory"
REASON_EXTERNAL_ROOT_NOT_WRITABLE = "external_root_not_writable"
REASON_EXTERNAL_ROOT_SAME_FILESYSTEM = "external_root_same_filesystem"
REASON_INTERNAL_ROOT_NOT_CONFIGURED = "internal_root_not_configured"
REASON_INTERNAL_ROOT_MISSING = "internal_root_missing"
REASON_INTERNAL_ROOT_NOT_DIRECTORY = "internal_root_not_directory"
REASON_REQUIRED_SUBROOT_MISSING = "required_subroot_missing"
REASON_REQUIRED_SUBROOT_SYMLINK = "required_subroot_symlink"
REASON_DOCKER_CONTEXT_UNKNOWN = "docker_context_unknown"
REASON_DOCKER_CONTEXT_UNAVAILABLE = "docker_context_unavailable"
REASON_POSTGRES_MISSING = "postgres_missing"
REASON_POSTGRES_DUPLICATE = "postgres_duplicate"
REASON_REDIS_MISSING = "redis_missing"
REASON_REDIS_DUPLICATE = "redis_duplicate"
REASON_INVENTORY_JSON_INVALID = "inventory_json_invalid"
REASON_LIVE_DOCKER_UNAVAILABLE = "live_docker_unavailable"

PUBLIC_LABELS = {
    "internal_root": "<internal-runtime-root>",
    "external_root": "<external-runtime-root>",
}


@dataclass(frozen=True)
class RuntimePolicy:
    external_root: Path | None
    internal_root: Path | None
    required_subroots: tuple[str, ...] = (
        "data",
        "artifacts",
        "eis-archives",
        "company-agent-runs",
        "backups",
        "models",
        "infrastructure",
        "temporary",
    )
    docker_context: str | None = None
    docker_context_allowlist: tuple[str, ...] = ()
    require_separate_filesystem: bool = True
    required_ollama: bool = False
    required_lmstudio: bool = False


@dataclass
class RuntimeInventory:
    docker_contexts: set[str] = field(default_factory=set)
    active_docker_context: str | None = None
    postgres_instances: int | None = None
    redis_instances: int | None = None
    ollama_available: bool | None = None
    lmstudio_available: bool | None = None
    reason_codes: set[str] = field(default_factory=set)


def _device(path: Path, stat_fn: Callable[[str], os.stat_result]) -> int:
    return stat_fn(os.fspath(path)).st_dev


def _safe_mode(path: Path, stat_fn: Callable[[str], os.stat_result]) -> bool:
    try:
        mode = stat_fn(os.fspath(path)).st_mode
    except OSError:
        return False
    return bool(mode & (stat_module.S_IWUSR | stat_module.S_IWGRP | stat_module.S_IWOTH))


def validate_filesystem(
    policy: RuntimePolicy,
    *,
    stat_fn: Callable[[str], os.stat_result] = os.stat,
    require_separate_filesystem: bool = True,
) -> list[str]:
    """Return stable reason codes without creating or modifying filesystem state."""

    root = policy.external_root
    if root is None:
        return [REASON_EXTERNAL_ROOT_NOT_CONFIGURED]
    if root.is_symlink():
        return [REASON_EXTERNAL_ROOT_SYMLINK]
    if not root.exists():
        return [REASON_EXTERNAL_ROOT_MISSING]
    if not root.is_dir():
        return [REASON_EXTERNAL_ROOT_NOT_DIRECTORY]
    if policy.internal_root is None:
        return [REASON_INTERNAL_ROOT_NOT_CONFIGURED]
    if not policy.internal_root.exists():
        return [REASON_INTERNAL_ROOT_MISSING]
    if not policy.internal_root.is_dir():
        return [REASON_INTERNAL_ROOT_NOT_DIRECTORY]
    if not _safe_mode(root, stat_fn) or not os.access(root, os.W_OK):
        return [REASON_EXTERNAL_ROOT_NOT_WRITABLE]
    if require_separate_filesystem and _device(root, stat_fn) == _device(policy.internal_root, stat_fn):
        return [REASON_EXTERNAL_ROOT_SAME_FILESYSTEM]

    reasons: list[str] = []
    for name in policy.required_subroots:
        subroot = root / name
        if subroot.is_symlink():
            reasons.append(f"{REASON_REQUIRED_SUBROOT_SYMLINK}:{name}")
        elif not subroot.is_dir():
            reasons.append(f"{REASON_REQUIRED_SUBROOT_MISSING}:{name}")
    return reasons


def _instance_reason(count: int | None, missing: str, duplicate: str) -> str | None:
    if count is None:
        return None
    if count == 0:
        return missing
    if count > 1:
        return duplicate
    return None


def validate_inventory(policy: RuntimePolicy, inventory: RuntimeInventory) -> list[str]:
    reasons: list[str] = sorted(inventory.reason_codes)
    if policy.docker_context and policy.docker_context not in inventory.docker_contexts:
        reasons.append(REASON_DOCKER_CONTEXT_UNKNOWN)
    if policy.docker_context and inventory.active_docker_context != policy.docker_context:
        reasons.append(REASON_DOCKER_CONTEXT_UNKNOWN)
    for reason in (
        _instance_reason(inventory.postgres_instances, REASON_POSTGRES_MISSING, REASON_POSTGRES_DUPLICATE),
        _instance_reason(inventory.redis_instances, REASON_REDIS_MISSING, REASON_REDIS_DUPLICATE),
    ):
        if reason:
            reasons.append(reason)
    if policy.required_ollama and inventory.ollama_available is not True:
        reasons.append("required_ollama_unavailable")
    if policy.required_lmstudio and inventory.lmstudio_available is not True:
        reasons.append("required_lmstudio_unavailable")
    return reasons


def sanitize_result(
    policy: RuntimePolicy,
    reasons: list[str],
    inventory: RuntimeInventory,
    *,
    mode: str = "filesystem-only",
    inventory_source: str = "not-checked",
) -> dict[str, Any]:
    def _optional_status(value: bool | None) -> str:
        if value is None:
            return "not_checked"
        return "available" if value else "unavailable"

    return {
        "status": "ok" if not reasons else "failed",
        "mode": mode,
        "inventory_source": inventory_source,
        "reason_codes": sorted(set(reasons)),
        "paths": PUBLIC_LABELS,
        "required_subroots": list(policy.required_subroots),
        "docker_context": policy.docker_context or "<unspecified>",
        "inventory": {
            "active_docker_context": inventory.active_docker_context or "<unspecified>",
            "postgres_instances": inventory.postgres_instances,
            "redis_instances": inventory.redis_instances,
            "ollama": _optional_status(inventory.ollama_available),
            "lmstudio": _optional_status(inventory.lmstudio_available),
        },
        "read_only": True,
    }


def _policy_from_environment() -> RuntimePolicy:
    raw_external = os.environ.get("ARVECTUM_STORAGE_ROOT") or os.environ.get(
        "AI_CORP_ARVECTUM_STORAGE_ROOT"
    )
    raw_internal = os.environ.get("ARVECTUM_INTERNAL_RUNTIME_ROOT")
    allowlist = tuple(
        item.strip()
        for item in os.environ.get("ARVECTUM_DOCKER_CONTEXTS", "").split(",")
        if item.strip()
    )
    return RuntimePolicy(
        external_root=Path(raw_external).expanduser() if raw_external else None,
        internal_root=Path(raw_internal).expanduser() if raw_internal else None,
        docker_context=os.environ.get("ARVECTUM_DOCKER_CONTEXT"),
        docker_context_allowlist=allowlist,
        require_separate_filesystem=os.environ.get(
            "ARVECTUM_REQUIRE_SEPARATE_FILESYSTEM", "true"
        ).lower()
        == "true",
        required_ollama=os.environ.get("ARVECTUM_OLLAMA_REQUIRED", "false").lower() == "true",
        required_lmstudio=os.environ.get("ARVECTUM_LMSTUDIO_REQUIRED", "false").lower() == "true",
    )


def _inventory_from_json(path: Path) -> RuntimeInventory:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError
        contexts = payload.get("docker_contexts", [])
        if not isinstance(contexts, list) or not all(isinstance(item, str) for item in contexts):
            raise TypeError
        counts = (payload.get("postgres_instances"), payload.get("redis_instances"))
        if not all(isinstance(item, int) and item >= 0 for item in counts):
            raise TypeError
        return RuntimeInventory(
            docker_contexts=set(contexts),
            active_docker_context=payload.get("active_docker_context"),
            postgres_instances=counts[0],
            redis_instances=counts[1],
            ollama_available=payload.get("ollama_available"),
            lmstudio_available=payload.get("lmstudio_available"),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError(REASON_INVENTORY_JSON_INVALID) from exc


def _run_read_only(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError from exc
    if completed.returncode:
        raise RuntimeError
    return completed.stdout


def _container_labels(container: dict[str, Any]) -> dict[str, str]:
    raw_labels = container.get("Labels", "")
    if isinstance(raw_labels, dict):
        return {
            str(key): str(value)
            for key, value in raw_labels.items()
            if isinstance(key, str) and isinstance(value, (str, int, float, bool))
        }
    if not isinstance(raw_labels, str):
        return {}
    labels: dict[str, str] = {}
    for item in raw_labels.split(","):
        key, separator, value = item.partition("=")
        if separator and key:
            labels[key] = value
    return labels


def _repository_name(image: Any) -> str:
    if not isinstance(image, str):
        return ""
    repository = image.split("@", 1)[0].rsplit("/", 1)[-1]
    if ":" in repository:
        repository = repository.split(":", 1)[0]
    return repository.lower()


def _name_tokens(name: Any) -> set[str]:
    if not isinstance(name, str):
        return set()
    token = ""
    tokens: set[str] = set()
    for character in name.lower().lstrip("/"):
        if character.isalnum():
            token += character
        elif token:
            tokens.add(token)
            token = ""
    if token:
        tokens.add(token)
    return tokens


def _container_runtime_kind(container: dict[str, Any]) -> str | None:
    """Classify only explicit Compose service, image repository, or name fields.

    Command text and arbitrary label values are deliberately ignored.  The
    accepted image repositories and name/service tokens are intentionally
    narrow to prevent incidental words from becoming runtime instances.
    """

    labels = _container_labels(container)
    service = labels.get("com.docker.compose.service", "").lower()
    if service in {"postgres", "postgresql", "pgvector"}:
        return "postgres"
    if service == "redis":
        return "redis"

    repository = _repository_name(container.get("Image"))
    if repository in {"postgres", "pgvector"}:
        return "postgres"
    if repository == "redis":
        return "redis"

    tokens = _name_tokens(container.get("Names"))
    if tokens & {"postgres", "postgresql", "pgvector"}:
        return "postgres"
    if "redis" in tokens:
        return "redis"
    return None


def _parse_container_rows(output: str) -> tuple[int, int]:
    postgres = redis = 0
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            container = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(container, dict):
            continue
        kind = _container_runtime_kind(container)
        if kind == "postgres":
            postgres += 1
        elif kind == "redis":
            redis += 1
    return postgres, redis


def _live_inventory(policy: RuntimePolicy | None = None) -> RuntimeInventory:
    contexts_output = _run_read_only(
        ["docker", "context", "ls", "--format", "{{.Name}}\t{{.Current}}"]
    )
    contexts: set[str] = set()
    active: str | None = None
    for line in contexts_output.splitlines():
        name, _, current = line.partition("\t")
        if name:
            contexts.add(name)
        if current.strip().lower() == "true":
            active = name

    allowlist = policy.docker_context_allowlist if policy else ()
    selected_contexts = list(allowlist) if allowlist else sorted(contexts)
    inventory = RuntimeInventory(
        docker_contexts=contexts,
        active_docker_context=active,
        postgres_instances=0,
        redis_instances=0,
        ollama_available=bool(_process_count("ollama")),
        lmstudio_available=bool(_process_count("lm studio")),
    )
    if not selected_contexts:
        inventory.reason_codes.add(REASON_LIVE_DOCKER_UNAVAILABLE)
        return inventory

    for context in selected_contexts:
        try:
            output = _run_read_only(
                ["docker", "--context", context, "ps", "--format", "{{json .}}"]
            )
        except RuntimeError:
            inventory.reason_codes.add(REASON_DOCKER_CONTEXT_UNAVAILABLE)
            continue
        postgres, redis = _parse_container_rows(output)
        inventory.postgres_instances += postgres
        inventory.redis_instances += redis
    return inventory


def _process_count(pattern: str) -> int:
    completed = subprocess.run(["pgrep", "-if", pattern], capture_output=True, text=True, check=False)
    return len([line for line in completed.stdout.splitlines() if line.strip()])


def _print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    print(f"preflight: {result['status']} ({result['mode']})")
    for reason in result["reason_codes"]:
        print(f"- {reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--filesystem-only", action="store_true")
    modes.add_argument("--inventory-json", type=Path)
    modes.add_argument("--live-runtime", action="store_true")
    args = parser.parse_args(argv)

    policy = _policy_from_environment()
    mode = "filesystem-only"
    inventory = RuntimeInventory()
    inventory_source = "not-checked"
    reasons = validate_filesystem(
        policy,
        require_separate_filesystem=policy.require_separate_filesystem,
    )
    try:
        if args.inventory_json:
            mode = "full-read-only"
            inventory_source = "inventory-json"
            inventory = _inventory_from_json(args.inventory_json)
            reasons.extend(validate_inventory(policy, inventory))
        elif args.live_runtime:
            mode = "full-read-only"
            inventory_source = "live-docker"
            inventory = _live_inventory(policy)
            reasons.extend(validate_inventory(policy, inventory))
    except (RuntimeError, ValueError, json.JSONDecodeError):
        reasons.append(
            REASON_LIVE_DOCKER_UNAVAILABLE if args.live_runtime else REASON_INVENTORY_JSON_INVALID
        )
    result = sanitize_result(
        policy,
        reasons,
        inventory,
        mode=mode,
        inventory_source=inventory_source,
    )
    _print_result(result, args.as_json)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
