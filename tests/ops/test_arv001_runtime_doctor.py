from __future__ import annotations

import json
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.arv001 import runtime_doctor
from scripts.arv001.runtime_doctor import (
    ManagedLoopbackRuntime,
    discover_gguf,
    discover_llama_server,
    ephemeral_runtime_environment,
    probe_zero_generation,
    read_private_env,
    validate_settings,
    write_private_runtime_profile,
)


def _gguf(path: Path, metadata: dict[str, object], *, version: int = 3) -> Path:
    """Small metadata-only GGUF fixture; it deliberately has no tensor payload."""
    def string(value: str) -> bytes:
        encoded = value.encode("utf-8")
        return struct.pack("<Q", len(encoded)) + encoded

    payload = bytearray(b"GGUF" + struct.pack("<IQQ", version, 0, len(metadata)))
    for key, value in metadata.items():
        payload.extend(string(key))
        if isinstance(value, int):
            payload.extend(struct.pack("<I", 10) + struct.pack("<Q", value))
        else:
            payload.extend(struct.pack("<I", 8) + string(str(value)))
    path.write_bytes(bytes(payload))
    return path


def _approved_metadata(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "general.architecture": "gemma",
        "general.name": "Gemma 4 12B Instruct Q4_K_M",
        "general.parameter_count": 12_000_000_000,
        "general.quantization": "Q4_K_M",
    }
    result.update(changes)
    return result


def _env(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def test_private_env_reports_unprefixed_variables_without_reading_secrets(tmp_path: Path) -> None:
    values, errors = read_private_env(
        _env(tmp_path / "private.env", "LLM_PROVIDER=openai_compatible\n"),
        tmp_path / "repository",
    )

    assert values == {}
    assert errors == ("unprefixed_llm_environment_detected",)


def test_settings_aggregate_independent_errors() -> None:
    errors = validate_settings({"AI_CORP_LLM_PROVIDER": "wrong"})

    assert errors == (
        "configured_provider_not_approved",
        "configured_model_not_approved",
        "configured_retries_not_zero",
        "provider_credential_missing",
        "configured_base_url_not_loopback",
        "tokenizer_url_not_loopback",
        "tokenizer_identity_missing",
    )


def test_private_env_requires_strict_mode(tmp_path: Path) -> None:
    _, errors = read_private_env(
        _env(tmp_path / "private.env", "AI_CORP_LLM_MODEL=x\n", 0o644),
        tmp_path / "repository",
    )

    assert errors == ("private_env_mode_invalid",)


def test_private_env_accepts_export_syntax_without_exporting_to_shell(tmp_path: Path) -> None:
    values, errors = read_private_env(
        _env(tmp_path / "private.env", "export AI_CORP_LLM_PROVIDER=openai_compatible\n"),
        tmp_path / "repository",
    )

    assert errors == ()
    assert values == {"AI_CORP_LLM_PROVIDER": "openai_compatible"}


def test_gguf_discovery_rejects_ambiguous_candidates(tmp_path: Path) -> None:
    _gguf(tmp_path / "gemma-4-12b-q4-k-m-a.gguf", _approved_metadata())
    _gguf(tmp_path / "gemma-4-12b-q4-k-m-b.gguf", _approved_metadata())

    profile, errors = discover_gguf((tmp_path,))

    assert profile is None
    assert errors == ("approved_gguf_ambiguous",)


def test_gguf_discovery_returns_only_hash(tmp_path: Path) -> None:
    _gguf(tmp_path / "gemma-4-12b-q4-k-m.gguf", _approved_metadata())

    profile, errors = discover_gguf((tmp_path,))

    assert errors == ()
    assert profile is not None
    assert set(profile) == {"gguf_sha256"}


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"general.architecture": "llama"}, "approved_gguf_architecture_mismatch"),
        ({"general.name": "Gemma 3 12B Instruct Q4_K_M"}, "approved_gguf_model_profile_mismatch"),
        ({"general.name": "Gemma 4 9B Instruct Q4_K_M", "general.parameter_count": 9_000_000_000}, "approved_gguf_parameter_profile_mismatch"),
        ({"general.name": "Gemma 4 12B Base Q4_K_M"}, "approved_gguf_instruction_profile_mismatch"),
        ({"general.name": "Gemma 4 12B Instruct Q8_0", "general.quantization": "Q8_0"}, "approved_gguf_quantization_mismatch"),
    ],
)
def test_gguf_exact_validation_rejects_wrong_profile(tmp_path: Path, changes: dict[str, object], reason: str) -> None:
    _, errors = runtime_doctor.validate_gguf_path(_gguf(tmp_path / "neutral.gguf", _approved_metadata(**changes)))
    assert reason in errors


def test_gguf_exact_validation_uses_metadata_not_filename(tmp_path: Path) -> None:
    profile, errors = runtime_doctor.validate_gguf_path(_gguf(tmp_path / "neutral-name.gguf", _approved_metadata()))
    assert errors == ()
    assert profile is not None and set(profile) == {"gguf_sha256"}


def test_gguf_rejects_unsupported_version_and_malformed_metadata(tmp_path: Path) -> None:
    _, version_errors = runtime_doctor.validate_gguf_path(_gguf(tmp_path / "v.gguf", _approved_metadata(), version=2))
    (tmp_path / "bad.gguf").write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 999_999))
    _, metadata_errors = runtime_doctor.validate_gguf_path(tmp_path / "bad.gguf")
    assert version_errors == ("approved_gguf_version_unsupported",)
    assert metadata_errors == ("approved_gguf_metadata_invalid",)


def test_asset_selection_modes_are_mutually_exclusive(tmp_path: Path) -> None:
    gguf = _gguf(tmp_path / "model.gguf", _approved_metadata())
    binary = tmp_path / "llama-server"
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o700)
    _, errors = runtime_doctor.locate_runtime_assets((tmp_path,), gguf_path=gguf, llama_server_path=binary)
    assert errors == ("runtime_asset_selection_mode_invalid",)


def test_llama_server_requires_declared_capabilities(tmp_path: Path, monkeypatch) -> None:
    binary = tmp_path / "llama-server"
    binary.write_text("placeholder", encoding="utf-8")
    binary.chmod(0o700)
    monkeypatch.setattr(
        runtime_doctor,
        "_sanitized_command_output",
        lambda command: "arm64" if command[0] == "file" else "version",
    )

    profile, errors = discover_llama_server((tmp_path,))

    assert profile is None
    assert errors == ("llama_server_capability_missing",)


def test_managed_loopback_runtime_terminates_on_context_exit(tmp_path: Path) -> None:
    class Process:
        def __init__(self) -> None:
            self.running = True
            self.terminated = False

        def poll(self):
            return None if self.running else 0

        def terminate(self) -> None:
            self.terminated = True
            self.running = False

        def wait(self, timeout: float) -> int:
            return 0

        def kill(self) -> None:
            self.running = False

    process = Process()
    runtime = ManagedLoopbackRuntime(
        binary=tmp_path / "llama-server",
        gguf=tmp_path / "model.gguf",
        process_factory=lambda *args, **kwargs: process,
        readiness_probe=lambda port: True,
    )

    with runtime:
        assert runtime.port is not None

    assert process.terminated


def test_managed_runtime_preserves_body_error_when_cleanup_fails(tmp_path: Path) -> None:
    class Process:
        def poll(self):
            return None

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float) -> int:
            raise runtime_doctor.subprocess.TimeoutExpired("llama", timeout)

        def kill(self) -> None:
            return None

    runtime = ManagedLoopbackRuntime(
        binary=tmp_path / "llama-server", gguf=tmp_path / "model.gguf",
        process_factory=lambda *args, **kwargs: Process(), readiness_probe=lambda port: True,
    )
    with pytest.raises(ValueError, match="body failure"), runtime:
        raise ValueError("body failure")
    assert runtime.cleanup_reason == "llama_runtime_cleanup_failed"


def test_zero_generation_probes_require_exact_alias_and_persistent_tokenizer() -> None:
    class Tokenizer:
        persistent = True

    def request(*, url: str, method: str, body=None):
        if method == "GET":
            return 200, {"data": [{"id": "arvectum-gemma4-12b-it-qat-q4_0"}]}
        assert body is not None
        return 200, {"tokens": [1, 2]}

    profile, errors = probe_zero_generation(
        loopback_base_url="http://127.0.0.1:1",
        tokenizer_url="http://127.0.0.1:2/tokenize",
        tokenizer_adapter=Tokenizer(),
        tokenizer_identity="private-identity",
        request=request,
    )

    assert errors == ()
    assert profile is not None
    assert profile["provider_generation_calls"] == 0


def test_zero_generation_probe_reports_independent_failures() -> None:
    profile, errors = probe_zero_generation(
        loopback_base_url="http://127.0.0.1:1",
        tokenizer_url="http://127.0.0.1:2/tokenize",
        tokenizer_adapter=object(),
        request=lambda **kwargs: None,
    )

    assert profile is None
    assert errors == (
        "loopback_models_probe_failed",
        "tokenizer_probe_failed",
        "exact_persistent_tokenizer_missing",
        "tokenizer_identity_missing",
    )


def test_ephemeral_environment_uses_dynamic_loopback_and_restores_files() -> None:
    with ephemeral_runtime_environment(port=43210, binary_sha256="a" * 64, gguf_sha256="b" * 64) as (values, path):
        assert values["AI_CORP_OPENAI_BASE_URL"] == "http://127.0.0.1:43210/v1"
        assert values["ARV003_LLAMA_TOKENIZER_URL"] == "http://127.0.0.1:43210/tokenize"
        assert values["AI_CORP_OPENAI_API_KEY"]
        assert path.exists()
        assert path.stat().st_mode & 0o777 == 0o600
        assert values["AI_CORP_OPENAI_API_KEY"] not in values["ARV003_TOKENIZER_IDENTITY"]
    assert not path.exists()


def test_ephemeral_identity_is_deterministic_across_ports() -> None:
    with ephemeral_runtime_environment(port=41001, binary_sha256="a" * 64, gguf_sha256="b" * 64) as (first, _):
        first_identity = first["ARV003_TOKENIZER_IDENTITY"]
    with ephemeral_runtime_environment(port=41002, binary_sha256="a" * 64, gguf_sha256="b" * 64) as (second, _):
        assert second["ARV003_TOKENIZER_IDENTITY"] == first_identity
    with ephemeral_runtime_environment(port=41002, binary_sha256="c" * 64, gguf_sha256="b" * 64) as (changed, _):
        assert changed["ARV003_TOKENIZER_IDENTITY"] != first_identity


def test_ephemeral_environment_rejects_unsupported_static_override() -> None:
    with (
        pytest.raises(ValueError, match="effective_settings_invalid"),
        ephemeral_runtime_environment(
            port=41001,
            binary_sha256="a" * 64,
            gguf_sha256="b" * 64,
            overrides={"AI_CORP_LLM_MODEL": "wrong"},
        ),
    ):
        pass


def test_zero_generation_uses_real_models_get_and_tokenize_post() -> None:
    observed: dict[str, object] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def do_GET(self) -> None:
            observed["models"] = self.path
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data":[{"id":"arvectum-gemma4-12b-it-qat-q4_0"}]}')

        def do_POST(self) -> None:
            observed["tokenizer"] = self.path
            length = int(self.headers["Content-Length"])
            observed["body"] = json.loads(self.rfile.read(length))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"tokens":[1,2]}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        class Tokenizer:
            persistent = True

        base = f"http://127.0.0.1:{server.server_port}"
        profile, errors = probe_zero_generation(
            loopback_base_url=base,
            tokenizer_url=base + "/tokenize",
            tokenizer_adapter=Tokenizer(),
            tokenizer_identity="configured-identity",
        )
    finally:
        server.shutdown()
        worker.join()

    assert errors == ()
    assert profile is not None
    assert observed["models"] == "/v1/models"
    assert observed["tokenizer"] == "/tokenize"
    assert observed["body"] == {
        "content": "ARV-001 tokenizer probe",
        "add_special": False,
        "parse_special": True,
        "with_pieces": False,
    }


def test_runtime_profile_is_closed_and_private(tmp_path: Path) -> None:
    profile, errors = write_private_runtime_profile(
        private_directory=tmp_path / "private",
        repository_root=tmp_path / "repository",
        profile={"version": "v1", "provider_generation_calls": 0},
    )

    assert errors == ()
    assert profile is not None
    assert profile.stat().st_mode & 0o777 == 0o600
