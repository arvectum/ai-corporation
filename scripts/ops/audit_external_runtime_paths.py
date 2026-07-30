"""Read-only validation for the external Arvectum runtime root.

The default mode only inspects local filesystem metadata and injected runtime
inventory.  It never creates directories, touches Docker/Colima, or contacts
model/provider endpoints.  Live runtime adapters can be added without making
the safe default destructive or network-active.
"""

from __future__ import annotations

import argparse
import json
import os
import stat as stat_module
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REASON_EXTERNAL_ROOT_MISSING = "external_root_missing"
REASON_EXTERNAL_ROOT_SYMLINK = "external_root_symlink"
REASON_EXTERNAL_ROOT_NOT_DIRECTORY = "external_root_not_directory"
REASON_EXTERNAL_ROOT_NOT_WRITABLE = "external_root_not_writable"
REASON_EXTERNAL_ROOT_SAME_FILESYSTEM = "external_root_same_filesystem"
REASON_REQUIRED_SUBROOT_MISSING = "required_subroot_missing"
REASON_REQUIRED_SUBROOT_SYMLINK = "required_subroot_symlink"
REASON_DOCKER_CONTEXT_UNKNOWN = "docker_context_unknown"
REASON_DUPLICATE_POSTGRES = "duplicate_postgres"
REASON_DUPLICATE_REDIS = "duplicate_redis"

PUBLIC_LABELS = {
    "internal_root": "<internal-runtime-root>",
    "external_root": "<external-runtime-root>",
    "docker_endpoint": "<docker-endpoint>",
}


@dataclass(frozen=True)
class RuntimePolicy:
    external_root: Path
    internal_root: Path
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
    required_ollama: bool = False
    required_lmstudio: bool = False


@dataclass
class RuntimeInventory:
    docker_contexts: set[str] = field(default_factory=set)
    active_docker_context: str | None = None
    postgres_instances: int = 0
    redis_instances: int = 0
    ollama_available: bool | None = None
    lmstudio_available: bool | None = None


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
    if root.is_symlink():
        return [REASON_EXTERNAL_ROOT_SYMLINK]
    if not root.exists():
        return [REASON_EXTERNAL_ROOT_MISSING]
    if not root.is_dir():
        return [REASON_EXTERNAL_ROOT_NOT_DIRECTORY]
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


def validate_inventory(policy: RuntimePolicy, inventory: RuntimeInventory) -> list[str]:
    reasons: list[str] = []
    if policy.docker_context and policy.docker_context not in inventory.docker_contexts:
        reasons.append(REASON_DOCKER_CONTEXT_UNKNOWN)
    if policy.docker_context and inventory.active_docker_context != policy.docker_context:
        reasons.append(REASON_DOCKER_CONTEXT_UNKNOWN)
    if inventory.postgres_instances != 1:
        reasons.append(REASON_DUPLICATE_POSTGRES)
    if inventory.redis_instances != 1:
        reasons.append(REASON_DUPLICATE_REDIS)
    if policy.required_ollama and inventory.ollama_available is not True:
        reasons.append("required_ollama_unavailable")
    if policy.required_lmstudio and inventory.lmstudio_available is not True:
        reasons.append("required_lmstudio_unavailable")
    return reasons


def sanitize_result(policy: RuntimePolicy, reasons: list[str], inventory: RuntimeInventory) -> dict[str, Any]:
    return {
        "status": "ok" if not reasons else "failed",
        "reason_codes": sorted(set(reasons)),
        "paths": PUBLIC_LABELS,
        "required_subroots": list(policy.required_subroots),
        "docker_context": policy.docker_context or "<unspecified>",
        "inventory": {
            "active_docker_context": inventory.active_docker_context or "<unspecified>",
            "postgres_instances": inventory.postgres_instances,
            "redis_instances": inventory.redis_instances,
            "ollama": "available" if inventory.ollama_available is True else "degraded",
            "lmstudio": "available" if inventory.lmstudio_available is True else "degraded",
        },
        "read_only": True,
    }


def _policy_from_environment() -> RuntimePolicy:
    raw_root = os.environ.get("ARVECTUM_STORAGE_ROOT") or os.environ.get("AI_CORP_ARVECTUM_STORAGE_ROOT")
    if not raw_root:
        raw_root = "<external-runtime-root>"
    return RuntimePolicy(
        external_root=Path(raw_root).expanduser(),
        internal_root=Path(os.environ.get("ARVECTUM_INTERNAL_RUNTIME_ROOT", "/")),
        docker_context=os.environ.get("ARVECTUM_DOCKER_CONTEXT"),
        required_ollama=os.environ.get("ARVECTUM_OLLAMA_REQUIRED", "false").lower() == "true",
        required_lmstudio=os.environ.get("ARVECTUM_LMSTUDIO_REQUIRED", "false").lower() == "true",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    policy = _policy_from_environment()
    reasons = validate_filesystem(policy)
    # No live Docker/model/provider calls occur in the safe default command.
    inventory = RuntimeInventory()
    result = sanitize_result(policy, reasons + validate_inventory(policy, inventory), inventory)
    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"preflight: {result['status']}")
        for reason in result["reason_codes"]:
            print(f"- {reason}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
