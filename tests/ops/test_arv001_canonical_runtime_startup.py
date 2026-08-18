from pathlib import Path

from scripts.arv001 import full_pre_provider_canonical as canonical


def test_canonical_runtime_startup_window_covers_measured_cold_start(
    tmp_path: Path,
) -> None:
    runtime = canonical._canonical_managed_loopback_runtime(
        binary=tmp_path / "llama-server",
        gguf=tmp_path / "model.gguf",
    )

    assert canonical._CANONICAL_RUNTIME_STARTUP_TIMEOUT_SECONDS == 120.0
    assert runtime.timeout_seconds == 120.0
    # R2c-D1 measured a healthy approved cold start at 47 seconds. The
    # canonical acceptance must retain bounded headroom for that valid load.
    assert runtime.timeout_seconds > 47.0


def test_canonical_runtime_startup_window_remains_explicitly_overridable(
    tmp_path: Path,
) -> None:
    runtime = canonical._canonical_managed_loopback_runtime(
        binary=tmp_path / "llama-server",
        gguf=tmp_path / "model.gguf",
        timeout_seconds=5.0,
    )

    assert runtime.timeout_seconds == 5.0
