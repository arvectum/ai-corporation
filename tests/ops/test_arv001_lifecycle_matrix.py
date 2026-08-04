from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.arv001.runtime_doctor import ManagedLoopbackRuntime


class _Process:
    def __init__(
        self,
        *,
        terminate_stops: bool = True,
        kill_stops: bool = True,
        timeout_after_terminate: bool = False,
    ) -> None:
        self.running = True
        self.terminate_stops = terminate_stops
        self.kill_stops = kill_stops
        self.timeout_after_terminate = timeout_after_terminate
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return None if self.running else 0

    def terminate(self) -> None:
        self.terminated = True
        if self.terminate_stops:
            self.running = False

    def wait(self, timeout: float) -> int:
        self.wait_calls += 1
        if self.timeout_after_terminate and not self.killed:
            raise subprocess.TimeoutExpired("llama-server", timeout)
        return 0

    def kill(self) -> None:
        self.killed = True
        if self.kill_stops:
            self.running = False


def _runtime(tmp_path: Path, process: _Process, **changes) -> ManagedLoopbackRuntime:
    return ManagedLoopbackRuntime(
        binary=tmp_path / "llama-server",
        gguf=tmp_path / "model.gguf",
        process_factory=lambda *args, **kwargs: process,
        readiness_probe=changes.pop("readiness_probe", lambda port: True),
        timeout_seconds=changes.pop("timeout_seconds", 0.01),
        **changes,
    )


def test_startup_timeout_terminates_process_and_removes_private_logs(
    tmp_path: Path,
) -> None:
    process = _Process()
    runtime = _runtime(
        tmp_path,
        process,
        readiness_probe=lambda port: False,
        timeout_seconds=0,
    )

    with pytest.raises(RuntimeError, match="llama_runtime_readiness_timeout"):
        runtime.start()

    assert process.terminated is True
    assert process.poll() == 0
    assert runtime._temporary is None
    assert runtime._stdout is None
    assert runtime._stderr is None


def test_shutdown_uses_bounded_kill_fallback_and_leaves_no_orphan(
    tmp_path: Path,
) -> None:
    process = _Process(
        terminate_stops=False,
        kill_stops=True,
        timeout_after_terminate=True,
    )
    runtime = _runtime(tmp_path, process)

    runtime.start()
    runtime.stop()

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2
    assert process.poll() == 0
    assert runtime._temporary is None


def test_keyboard_interrupt_preserves_interrupt_and_cleans_runtime(
    tmp_path: Path,
) -> None:
    process = _Process()
    runtime = _runtime(tmp_path, process)

    with pytest.raises(KeyboardInterrupt), runtime:
        raise KeyboardInterrupt

    assert process.terminated is True
    assert process.poll() == 0
    assert runtime._temporary is None


def test_orphan_detection_is_fail_closed_after_nominal_wait(tmp_path: Path) -> None:
    process = _Process(terminate_stops=False, kill_stops=False)
    runtime = _runtime(tmp_path, process)

    runtime.start()
    with pytest.raises(RuntimeError, match="llama_runtime_orphan_detected"):
        runtime.stop()

    assert process.terminated is True
    assert process.poll() is None
    assert runtime._temporary is None
