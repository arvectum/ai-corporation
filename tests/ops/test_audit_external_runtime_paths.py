from __future__ import annotations

import json
import os
from pathlib import Path

from scripts.ops.audit_external_runtime_paths import (
    REASON_EXTERNAL_ROOT_MISSING,
    REASON_EXTERNAL_ROOT_SAME_FILESYSTEM,
    REASON_EXTERNAL_ROOT_SYMLINK,
    REASON_REQUIRED_SUBROOT_MISSING,
    RuntimeInventory,
    RuntimePolicy,
    sanitize_result,
    validate_filesystem,
    validate_inventory,
)


def _policy(tmp_path: Path) -> RuntimePolicy:
    root = tmp_path / "external"
    root.mkdir()
    return RuntimePolicy(external_root=root, internal_root=tmp_path / "internal")


def _make_subroots(policy: RuntimePolicy) -> None:
    for name in policy.required_subroots:
        (policy.external_root / name).mkdir()


def test_missing_external_root_fails_without_creating_it(tmp_path: Path) -> None:
    policy = RuntimePolicy(external_root=tmp_path / "missing", internal_root=tmp_path)
    assert validate_filesystem(policy, require_separate_filesystem=False) == [REASON_EXTERNAL_ROOT_MISSING]
    assert not policy.external_root.exists()


def test_missing_required_subroot_is_reported(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    assert f"{REASON_REQUIRED_SUBROOT_MISSING}:data" in validate_filesystem(
        policy, require_separate_filesystem=False
    )


def test_root_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "external"
    link.symlink_to(target, target_is_directory=True)
    policy = RuntimePolicy(external_root=link, internal_root=tmp_path)
    assert validate_filesystem(policy, require_separate_filesystem=False) == [REASON_EXTERNAL_ROOT_SYMLINK]


def test_same_filesystem_is_rejected_when_required(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    policy.internal_root.mkdir()
    _make_subroots(policy)
    assert validate_filesystem(policy) == [REASON_EXTERNAL_ROOT_SAME_FILESYSTEM]


def test_read_only_mode_is_rejected(tmp_path: Path, monkeypatch) -> None:
    policy = _policy(tmp_path)
    _make_subroots(policy)
    monkeypatch.setattr(os, "access", lambda *_args: False)
    assert "external_root_not_writable" in validate_filesystem(policy, require_separate_filesystem=False)


def test_symlink_subroot_is_rejected(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    for name in policy.required_subroots:
        (policy.external_root / name).mkdir()
    (policy.external_root / "data").rmdir()
    (policy.external_root / "data").symlink_to(target, target_is_directory=True)
    assert "required_subroot_symlink:data" in validate_filesystem(
        policy, require_separate_filesystem=False
    )


def test_inventory_requires_one_postgres_and_redis() -> None:
    policy = RuntimePolicy(
        external_root=Path("/external"),
        internal_root=Path("/internal"),
        docker_context="colima",
    )
    reasons = validate_inventory(policy, RuntimeInventory(docker_contexts={"desktop-linux"}))
    assert "docker_context_unknown" in reasons
    assert "duplicate_postgres" in reasons
    assert "duplicate_redis" in reasons


def test_sanitized_json_contains_no_private_paths() -> None:
    policy = RuntimePolicy(external_root=Path("/Users/private/external"), internal_root=Path("/Users/private"))
    result = sanitize_result(policy, [], RuntimeInventory())
    encoded = json.dumps(result)
    assert "/Users/private" not in encoded
    assert "<external-runtime-root>" in encoded
    assert result["read_only"] is True
