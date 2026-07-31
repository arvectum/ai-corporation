from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.ops.audit_external_runtime_paths import (
    REASON_DOCKER_CONTEXT_UNAVAILABLE,
    REASON_EXTERNAL_ROOT_MISSING,
    REASON_EXTERNAL_ROOT_SAME_FILESYSTEM,
    REASON_EXTERNAL_ROOT_SYMLINK,
    REASON_REQUIRED_SUBROOT_MISSING,
    RuntimeInventory,
    RuntimePolicy,
    _live_inventory,
    _parse_container_rows,
    sanitize_result,
    validate_filesystem,
    validate_inventory,
)


def _policy(tmp_path: Path) -> RuntimePolicy:
    root = tmp_path / "external"
    internal = tmp_path / "internal"
    root.mkdir()
    internal.mkdir()
    return RuntimePolicy(external_root=root, internal_root=internal)


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
    assert "postgres_missing" not in reasons
    assert "redis_missing" not in reasons


def test_inventory_distinguishes_missing_and_duplicate() -> None:
    policy = RuntimePolicy(external_root=Path("/external"), internal_root=Path("/internal"))
    missing = validate_inventory(policy, RuntimeInventory(postgres_instances=0, redis_instances=0))
    duplicate = validate_inventory(policy, RuntimeInventory(postgres_instances=2, redis_instances=3))
    assert missing == ["postgres_missing", "redis_missing"]
    assert duplicate == ["postgres_duplicate", "redis_duplicate"]


def test_sanitized_json_contains_no_private_paths() -> None:
    policy = RuntimePolicy(external_root=Path("/Users/private/external"), internal_root=Path("/Users/private"))
    result = sanitize_result(policy, [], RuntimeInventory())
    encoded = json.dumps(result)
    assert "/Users/private" not in encoded
    assert "<external-runtime-root>" in encoded
    assert result["read_only"] is True


def _run_cli(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, str(root / "scripts/ops/audit_external_runtime_paths.py"), *args]
    return subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, check=False)


def _runtime_env(tmp_path: Path) -> dict[str, str]:
    external = tmp_path / "external"
    internal = tmp_path / "internal"
    external.mkdir(exist_ok=True)
    internal.mkdir(exist_ok=True)
    for name in RuntimePolicy(external, internal).required_subroots:
        (external / name).mkdir(exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        ARVECTUM_STORAGE_ROOT=str(external),
        ARVECTUM_INTERNAL_RUNTIME_ROOT=str(internal),
        ARVECTUM_DOCKER_CONTEXT="colima",
        ARVECTUM_REQUIRE_SEPARATE_FILESYSTEM="false",
    )
    return environment


def test_cli_filesystem_only_can_return_zero(tmp_path: Path) -> None:
    completed = _run_cli(tmp_path, "--filesystem-only", "--json", env=_runtime_env(tmp_path))
    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["mode"] == "filesystem-only"
    assert payload["inventory_source"] == "not-checked"
    assert payload["inventory"]["ollama"] == "not_checked"
    assert payload["inventory"]["lmstudio"] == "not_checked"


def test_cli_full_inventory_json_with_one_instance_each_returns_zero(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "docker_contexts": ["colima"],
                "active_docker_context": "colima",
                "postgres_instances": 1,
                "redis_instances": 1,
                "ollama_available": False,
                "lmstudio_available": False,
            }
        ),
        encoding="utf-8",
    )
    completed = _run_cli(tmp_path, "--inventory-json", str(inventory), "--json", env=_runtime_env(tmp_path))
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["mode"] == "full-read-only"


def test_cli_inventory_zero_and_duplicate_are_distinct(tmp_path: Path) -> None:
    for counts, expected in [((0, 0), {"postgres_missing", "redis_missing"}), ((2, 3), {"postgres_duplicate", "redis_duplicate"})]:
        inventory = tmp_path / f"inventory-{counts[0]}-{counts[1]}.json"
        inventory.write_text(
            json.dumps(
                {
                    "docker_contexts": ["colima"],
                    "active_docker_context": "colima",
                    "postgres_instances": counts[0],
                    "redis_instances": counts[1],
                }
            ),
            encoding="utf-8",
        )
        completed = _run_cli(tmp_path, "--inventory-json", str(inventory), "--json", env=_runtime_env(tmp_path))
        payload = json.loads(completed.stdout)
        assert completed.returncode == 1
        assert expected.issubset(payload["reason_codes"])


def test_cli_missing_env_is_sanitized_without_traceback(tmp_path: Path) -> None:
    environment = os.environ.copy()
    for name in ("ARVECTUM_STORAGE_ROOT", "AI_CORP_ARVECTUM_STORAGE_ROOT", "ARVECTUM_INTERNAL_RUNTIME_ROOT"):
        environment.pop(name, None)
    completed = _run_cli(tmp_path, "--filesystem-only", "--json", env=environment)
    assert completed.returncode == 1
    assert "external_root_not_configured" in json.loads(completed.stdout)["reason_codes"]
    assert "Traceback" not in completed.stdout + completed.stderr
    assert "external_root_not_configured" in completed.stdout


def test_cli_output_never_reveals_absolute_paths(tmp_path: Path) -> None:
    environment = _runtime_env(tmp_path)
    completed = _run_cli(tmp_path, "--filesystem-only", "--json", env=environment)
    assert str(tmp_path) not in completed.stdout
    assert str(tmp_path) not in completed.stderr


def test_container_classifier_ignores_incidental_words() -> None:
    rows = "\n".join(
        json.dumps(row)
        for row in (
            {"Names": "worker", "Image": "example/worker:latest", "Command": "uses postgres and redis"},
            {"Names": "worker-two", "Image": "example/worker:latest", "Labels": "note=redis"},
            {"Names": "db", "Image": "postgres:16", "Command": "irrelevant"},
            {"Names": "cache", "Image": "example/cache:latest", "Labels": "com.docker.compose.service=redis"},
        )
    )
    assert _parse_container_rows(rows) == (1, 1)


def test_live_inventory_aggregates_postgres_across_contexts(monkeypatch) -> None:
    calls: list[list[str]] = []
    context_rows = "default\tfalse\ncolima\ttrue\nother\tfalse\n"
    outputs = {
        "default": json.dumps({"Names": "db-default", "Image": "postgres:16"}) + "\n",
        "colima": json.dumps({"Names": "db-colima", "Image": "postgres:16"}) + "\n",
        "other": json.dumps({"Names": "worker", "Image": "example/worker", "Command": "redis"}) + "\n",
    }

    def fake_run(command: list[str]) -> str:
        calls.append(command)
        if command[:3] == ["docker", "context", "ls"]:
            return context_rows
        return outputs[command[2]]

    monkeypatch.setattr("scripts.ops.audit_external_runtime_paths._run_read_only", fake_run)
    monkeypatch.setattr("scripts.ops.audit_external_runtime_paths._process_count", lambda _pattern: 0)
    inventory = _live_inventory(RuntimePolicy(None, None))

    assert inventory.postgres_instances == 2
    assert inventory.redis_instances == 0
    assert inventory.active_docker_context == "colima"
    assert [call[2] for call in calls[1:]] == ["colima", "default", "other"]
    assert all("--context" in call for call in calls[1:])


def test_live_inventory_aggregates_redis_across_contexts(monkeypatch) -> None:
    def fake_run(command: list[str]) -> str:
        if command[:3] == ["docker", "context", "ls"]:
            return "one\ttrue\ntwo\tfalse\n"
        return json.dumps({"Names": f"cache-{command[3]}", "Image": "redis:7"}) + "\n"

    monkeypatch.setattr("scripts.ops.audit_external_runtime_paths._run_read_only", fake_run)
    monkeypatch.setattr("scripts.ops.audit_external_runtime_paths._process_count", lambda _pattern: 0)
    inventory = _live_inventory(RuntimePolicy(None, None))

    assert inventory.redis_instances == 2
    assert "redis_duplicate" in validate_inventory(RuntimePolicy(None, None), inventory)


def test_live_inventory_allowlist_and_unavailable_context_are_sanitized(monkeypatch) -> None:
    seen: list[list[str]] = []

    def fake_run(command: list[str]) -> str:
        seen.append(command)
        if command[:3] == ["docker", "context", "ls"]:
            return "colima\ttrue\n"
        if command[2] == "missing":
            raise RuntimeError
        return json.dumps({"Names": "db", "Image": "postgres:16"}) + "\n"

    monkeypatch.setattr("scripts.ops.audit_external_runtime_paths._run_read_only", fake_run)
    monkeypatch.setattr("scripts.ops.audit_external_runtime_paths._process_count", lambda _pattern: 0)
    inventory = _live_inventory(
        RuntimePolicy(None, None, docker_context_allowlist=("colima", "missing"))
    )

    assert inventory.postgres_instances == 1
    assert REASON_DOCKER_CONTEXT_UNAVAILABLE in inventory.reason_codes
    assert seen[1:] == [
        ["docker", "--context", "colima", "ps", "--format", "{{json .}}"],
        ["docker", "--context", "missing", "ps", "--format", "{{json .}}"],
    ]
    assert all("endpoint" not in " ".join(call).lower() for call in seen)


def test_cli_unavailable_context_is_sanitized_without_traceback(tmp_path: Path) -> None:
    docker_bin = tmp_path / "bin"
    docker_bin.mkdir()
    fake_docker = docker_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if sys.argv[1:4] == ['context', 'ls', '--format']:\n"
        "    print('colima\\ttrue')\n"
        "    print('missing\\tfalse')\n"
        "elif 'missing' in sys.argv:\n"
        "    raise SystemExit(1)\n"
        "else:\n"
        "    print(json.dumps({'Names': 'db', 'Image': 'postgres:16'}))\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = _runtime_env(tmp_path)
    environment["PATH"] = f"{docker_bin}:{environment['PATH']}"
    completed = _run_cli(tmp_path, "--live-runtime", "--json", env=environment)
    payload = json.loads(completed.stdout)

    assert completed.returncode == 1
    assert REASON_DOCKER_CONTEXT_UNAVAILABLE in payload["reason_codes"]
    assert "Traceback" not in completed.stdout + completed.stderr
    assert str(tmp_path) not in completed.stdout + completed.stderr
    assert "endpoint" not in completed.stdout.lower()
