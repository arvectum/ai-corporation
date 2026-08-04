"""Sanitized, zero-generation validation for the local ARV-001 runtime.

This module is intentionally usable as a library by ``full_pre_provider``.
It never accepts credentials on the command line and it never calls a
generation endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Self
from urllib.parse import urlparse

ENV_KEYS = (
    "AI_CORP_LLM_PROVIDER",
    "AI_CORP_LLM_MODEL",
    "AI_CORP_LLM_MAX_RETRIES",
    "AI_CORP_OPENAI_API_KEY",
    "AI_CORP_OPENAI_BASE_URL",
    "ARV003_LLAMA_TOKENIZER_URL",
    "ARV003_TOKENIZER_IDENTITY",
)
_UNPREFIXED = {"LLM_PROVIDER", "LLM_MODEL", "LLM_MAX_RETRIES", "OPENAI_API_KEY", "OPENAI_BASE_URL"}
_EXPECTED_MODEL = "arvectum-gemma4-12b-it-qat-q4_0"
_MAX_DISCOVERY_DEPTH = 6
_TOKENIZER_PROBE_BODY = {
    "content": "ARV-001 tokenizer probe",
    "add_special": False,
    "parse_special": True,
    "with_pieces": False,
}
_TOKENIZER_CONTRACT_VERSION = "llama-server-tokenize-v1"
_GGUF_VERSION = 3
_GGUF_MAX_METADATA = 4096
_GGUF_MAX_TENSORS = 1_000_000
_GGUF_MAX_STRING_BYTES = 1_000_000
# Gemma 4's tokenizer vocabulary is larger than the legacy 65k bound.  Keep a
# bounded ceiling while allowing a complete tokenizer metadata array.
_GGUF_MAX_ARRAY_ITEMS = 1_000_000

# This is deliberately a closed, small contract.  The private runtime profile
# uses a subset of these keys and never retains commands, paths, or raw output.
SANITIZED_RESULT_FIELDS = frozenset(
    {"schema_version", "status", "head_sha", "phases", "counters", "ready_for_exact_head_authorization"}
)


@dataclass(frozen=True)
class Phase:
    phase: str
    reason_codes: tuple[str, ...] = ()

    def sanitized(self) -> dict[str, object]:
        return {"phase": self.phase, "status": "PASS" if not self.reason_codes else "FAIL", "reason_codes": list(self.reason_codes)}


@dataclass
class DoctorReport:
    head_sha: str
    phases: list[Phase] = field(default_factory=list)
    runtime_profile: dict[str, object] | None = None

    def sanitized(self) -> dict[str, object]:
        passed = not any(phase.reason_codes for phase in self.phases)
        return {
            "schema_version": "arv001-full-pre-provider-v1",
            "status": "PASS" if passed else "FAIL_CLOSED",
            "head_sha": self.head_sha,
            "phases": [phase.sanitized() for phase in self.phases],
            "counters": {"controlled_preflight_invocations": 0, "controlled_provider_invocations": 0, "provider_generation_calls": 0},
            "ready_for_exact_head_authorization": False,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bounded_files(roots: tuple[Path, ...], name: str) -> list[Path]:
    """Return non-symlink files matching *name* under explicitly approved roots."""
    found: list[Path] = []
    for root in roots:
        if root.is_symlink():
            continue
        try:
            root = root.resolve(strict=True)
        except OSError:
            continue
        if not root.is_dir() or root.is_symlink():
            continue
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            try:
                depth = len(current_path.relative_to(root).parts)
            except ValueError:
                continue
            directories[:] = sorted(
                directory
                for directory in directories
                if depth < _MAX_DISCOVERY_DEPTH and not (current_path / directory).is_symlink()
            )
            for filename in sorted(files):
                candidate = current_path / filename
                if (not name or filename == name) and not candidate.is_symlink():
                    found.append(candidate)
    return found


def _approved_gguf_name(path: Path) -> bool:
    value = path.name.lower().replace("_", "-")
    return "gemma" in value and "4" in value and "12b" in value and ("q4-k-m" in value or "q4km" in value)


class _GGUFMetadataError(ValueError):
    """A deliberately detail-free error for malformed untrusted GGUF headers."""


class _GGUFReader:
    """Bounded reader for the GGUF header and metadata table only.

    Tensor payload bytes are never read.  The reader still walks tensor-info
    records so truncated/malformed headers cannot be accepted as a model.
    """

    _SCALAR_WIDTHS: ClassVar[dict[int, int]] = {
        0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8,
    }

    def __init__(self, stream, size: int) -> None:
        self.stream = stream
        self.size = size

    def _read(self, count: int) -> bytes:
        if count < 0 or count > self.size - self.stream.tell():
            raise _GGUFMetadataError
        result = self.stream.read(count)
        if len(result) != count:
            raise _GGUFMetadataError
        return result

    def _u32(self) -> int:
        return int.from_bytes(self._read(4), "little")

    def _u64(self) -> int:
        return int.from_bytes(self._read(8), "little")

    def _string(self) -> str:
        length = self._u64()
        if length > _GGUF_MAX_STRING_BYTES:
            raise _GGUFMetadataError
        try:
            return self._read(length).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _GGUFMetadataError from exc

    def _value(self, value_type: int):
        if value_type == 8:
            return self._string()
        if value_type == 9:
            item_type, count = self._u32(), self._u64()
            if count > _GGUF_MAX_ARRAY_ITEMS or item_type == 9:
                raise _GGUFMetadataError
            return tuple(self._value(item_type) for _ in range(count))
        width = self._SCALAR_WIDTHS.get(value_type)
        if width is None:
            raise _GGUFMetadataError
        raw = self._read(width)
        if value_type in {4, 10}:
            return int.from_bytes(raw, "little", signed=False)
        if value_type in {5, 11}:
            return int.from_bytes(raw, "little", signed=True)
        return None

    def read(self) -> dict[str, object]:
        if self._read(4) != b"GGUF":
            raise _GGUFMetadataError("magic")
        if self._u32() != _GGUF_VERSION:
            raise _GGUFMetadataError("version")
        tensor_count, metadata_count = self._u64(), self._u64()
        if tensor_count > _GGUF_MAX_TENSORS or metadata_count > _GGUF_MAX_METADATA:
            raise _GGUFMetadataError
        metadata: dict[str, object] = {}
        for _ in range(metadata_count):
            key = self._string()
            if not key or key in metadata:
                raise _GGUFMetadataError
            metadata[key] = self._value(self._u32())
        tensor_types: list[int] = []
        for _ in range(tensor_count):
            self._string()
            dimensions = self._u32()
            if dimensions > 8:
                raise _GGUFMetadataError
            self._read(dimensions * 8)
            tensor_types.append(self._u32())
            self._read(8)  # tensor-data offset
        metadata["__tensor_types"] = tuple(tensor_types)
        return metadata


def _gguf_metadata(candidate: Path) -> tuple[dict[str, object] | None, str | None]:
    """Read only bounded GGUF header data, returning no content in diagnostics."""
    try:
        with candidate.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if size < 24:
                return None, "approved_gguf_header_invalid"
            try:
                return _GGUFReader(stream, size).read(), None
            except _GGUFMetadataError as exc:
                return None, (
                    "approved_gguf_version_unsupported" if str(exc) == "version" else "approved_gguf_metadata_invalid"
                )
    except OSError:
        return None, "approved_gguf_unreadable"


def _profile_text(metadata: dict[str, object], *keys: str) -> str:
    return " ".join(str(metadata.get(key, "")) for key in keys).lower().replace("_", "-")


def _validate_gguf_profile(metadata: dict[str, object]) -> tuple[str, ...]:
    errors: list[str] = []
    if str(metadata.get("general.architecture", "")).lower() not in {"gemma", "gemma4"}:
        errors.append("approved_gguf_architecture_mismatch")
    model = _profile_text(metadata, "general.name", "general.basename", "general.description", "general.base_model.0.name")
    provenance = _profile_text(metadata, "general.base_model.0.organization", "general.base_model.0.repo_url")
    if "gemma4" not in model and "gemma 4" not in model and "gemma-4" not in model:
        errors.append("approved_gguf_model_profile_mismatch")
    parameter_count = metadata.get("general.parameter_count")
    parameter_text = f"{parameter_count} {model}"
    if not (
        "12b" in parameter_text.lower()
        or "12 b" in parameter_text.lower()
        or isinstance(parameter_count, int) and 10_000_000_000 <= parameter_count <= 14_000_000_000
    ):
        errors.append("approved_gguf_parameter_profile_mismatch")
    if "instruct" not in model and "chat" not in model and "it" not in model:
        errors.append("approved_gguf_instruction_profile_mismatch")
    quantization = _profile_text(metadata, "general.quantization", "general.quantization_type", "general.description", "general.name")
    tensor_types = metadata.get("__tensor_types", ())
    # GGML_TYPE_Q4_0 is 2.  QAT Q4_0 files may retain no human-readable
    # quantization key, so verify the actual tensor storage type as well.
    if "q4-k-m" not in quantization and "q4km" not in quantization and not ("qat" in provenance or "qat" in model) and not (isinstance(tensor_types, tuple) and 2 in tensor_types):
        errors.append("approved_gguf_quantization_mismatch")
    return tuple(sorted(errors))


def discover_gguf(roots: tuple[Path, ...]) -> tuple[dict[str, str] | None, tuple[str, ...]]:
    candidates = [
        path
        for path in _bounded_files(roots, "")
        if path.suffix.lower() == ".gguf" and _approved_gguf_name(path)
    ]
    if not candidates:
        return None, ("approved_gguf_not_found",)
    if len(candidates) != 1:
        return None, ("approved_gguf_ambiguous",)
    return validate_gguf_path(candidates[0])


def validate_gguf_path(candidate: Path) -> tuple[dict[str, str] | None, tuple[str, ...]]:
    if candidate.is_symlink():
        return None, ("approved_gguf_unreadable",)
    try:
        if not candidate.is_file() or not os.access(candidate, os.R_OK):
            return None, ("approved_gguf_unreadable",)
        metadata, error = _gguf_metadata(candidate)
        if error:
            return None, (error,)
        assert metadata is not None
        profile_errors = _validate_gguf_profile(metadata)
        if profile_errors:
            return None, profile_errors
        return {"gguf_sha256": _sha256(candidate)}, ()
    except OSError:
        return None, ("approved_gguf_unreadable",)


def _approved_gguf_candidate(roots: tuple[Path, ...]) -> Path | None:
    candidates = [
        path
        for path in _bounded_files(roots, "")
        if path.suffix.lower() == ".gguf" and _approved_gguf_name(path)
    ]
    return candidates[0] if len(candidates) == 1 else None


def _candidate_binaries(roots: tuple[Path, ...]) -> list[Path]:
    return _bounded_files(roots, "llama-server")


def _sanitized_command_output(command: list[str]) -> str:
    try:
        # llama-server  builds may initialize a sizable option registry before
        # exiting --help; keep inspection bounded while allowing that startup.
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    # The current llama-server help is ~55 KB; truncating at 20 KB can remove
    # required capability flags near the end.  Keep a bounded 128 KB window.
    return (result.stdout + "\n" + result.stderr).lower()[:131072]


def discover_llama_server(roots: tuple[Path, ...]) -> tuple[dict[str, str] | None, tuple[str, ...]]:
    candidates = _candidate_binaries(roots)
    if not candidates:
        return None, ("llama_server_not_found",)
    if len(candidates) != 1:
        return None, ("llama_server_ambiguous",)
    return validate_llama_server_path(candidates[0])


def validate_llama_server_path(binary: Path) -> tuple[dict[str, str] | None, tuple[str, ...]]:
    if binary.is_symlink():
        return None, ("llama_server_not_executable",)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return None, ("llama_server_not_executable",)
    architecture = _sanitized_command_output(["file", "-b", str(binary)])
    if "arm64" not in architecture and "aarch64" not in architecture:
        return None, ("llama_server_architecture_mismatch",)
    help_output = _sanitized_command_output([str(binary), "--help"])
    if not all(flag in help_output for flag in ("--host", "--port", "--model", "--alias")):
        return None, ("llama_server_capability_missing",)
    version = _sanitized_command_output([str(binary), "--version"])
    if not version:
        return None, ("llama_server_capability_missing",)
    return {
        "binary_sha256": _sha256(binary),
        "binary_architecture": "arm64",
        "binary_version_sanitized": hashlib.sha256(version.encode("utf-8")).hexdigest(),
    }, ()


def locate_runtime_assets(
    roots: tuple[Path, ...], *, gguf_path: Path | None = None, llama_server_path: Path | None = None
) -> tuple[tuple[Path, Path] | None, tuple[str, ...]]:
    """Locate validated assets for private orchestration; never serialize paths."""
    exact_mode = gguf_path is not None and llama_server_path is not None
    if (gguf_path is None) != (llama_server_path is None) or (exact_mode and roots) or (not exact_mode and not roots):
        return None, ("runtime_asset_selection_mode_invalid",)
    _, gguf_errors = validate_gguf_path(gguf_path) if gguf_path else discover_gguf(roots)
    _, binary_errors = validate_llama_server_path(llama_server_path) if llama_server_path else discover_llama_server(roots)
    errors = tuple(sorted(set(gguf_errors + binary_errors)))
    if errors:
        return None, errors
    gguf = gguf_path if gguf_path else _approved_gguf_candidate(roots)
    binaries = [llama_server_path] if llama_server_path else _candidate_binaries(roots)
    if gguf is None or len(binaries) != 1:
        return None, ("runtime_asset_selection_invalid",)
    return (binaries[0], gguf), ()


def write_private_runtime_profile(
    *, private_directory: Path, profile: dict[str, object], repository_root: Path
) -> tuple[Path | None, tuple[str, ...]]:
    try:
        directory = private_directory.expanduser().resolve()
    except OSError:
        return None, ("runtime_profile_directory_invalid",)
    if directory == repository_root or repository_root in directory.parents:
        return None, ("runtime_profile_inside_repository",)
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        allowed = {
            "version", "binary_sha256", "gguf_sha256", "binary_architecture",
            "binary_version_sanitized", "model_alias", "provider", "loopback_verified",
            "models_probe_verified", "tokenizer_probe_verified", "tokenizer_persistent",
            "tokenizer_identity_sha256", "created_at", "provider_generation_calls",
        }
        if set(profile) - allowed:
            return None, ("runtime_profile_schema_invalid",)
        target = directory / "runtime-profile.json"
        target.write_text(json.dumps(profile, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        os.chmod(target, 0o600)
        return target, ()
    except OSError:
        return None, ("runtime_profile_write_failed",)


@contextmanager
def ephemeral_runtime_environment(
    *, port: int, binary_sha256: str, gguf_sha256: str, overrides: dict[str, str] | None = None
):
    """Create a private, disposable effective environment after port selection."""
    identity = hashlib.sha256(
        f"{binary_sha256}:{gguf_sha256}:{_EXPECTED_MODEL}:{_TOKENIZER_CONTRACT_VERSION}".encode()
    ).hexdigest()
    values = {
        "AI_CORP_LLM_PROVIDER": "openai_compatible",
        "AI_CORP_LLM_MODEL": _EXPECTED_MODEL,
        "AI_CORP_LLM_MAX_RETRIES": "0",
        "AI_CORP_OPENAI_API_KEY": secrets.token_urlsafe(32),
        "AI_CORP_OPENAI_BASE_URL": f"http://127.0.0.1:{port}/v1",
        "ARV003_LLAMA_TOKENIZER_URL": f"http://127.0.0.1:{port}/tokenize",
        "ARV003_TOKENIZER_IDENTITY": f"arv001-{identity}",
    }
    for key, value in (overrides or {}).items():
        if key in {"AI_CORP_LLM_PROVIDER", "AI_CORP_LLM_MODEL", "AI_CORP_LLM_MAX_RETRIES"} and values[key] != value:
            raise ValueError("effective_settings_invalid")
    with tempfile.TemporaryDirectory(prefix="arv001-private-env-") as raw_directory:
        directory = Path(raw_directory)
        os.chmod(directory, 0o700)
        path = directory / "runtime.env"
        path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
        os.chmod(path, 0o600)
        yield values, path


def validate_effective_runtime_environment(values: dict[str, str], *, port: int) -> tuple[str, ...]:
    expected = {
        "AI_CORP_LLM_PROVIDER": "openai_compatible",
        "AI_CORP_LLM_MODEL": _EXPECTED_MODEL,
        "AI_CORP_LLM_MAX_RETRIES": "0",
        "AI_CORP_OPENAI_BASE_URL": f"http://127.0.0.1:{port}/v1",
        "ARV003_LLAMA_TOKENIZER_URL": f"http://127.0.0.1:{port}/tokenize",
    }
    errors = ["effective_settings_invalid" for key, value in expected.items() if values.get(key) != value]
    if not values.get("AI_CORP_OPENAI_API_KEY") or not values.get("ARV003_TOKENIZER_IDENTITY"):
        errors.append("effective_settings_invalid")
    return tuple(sorted(set(errors)))


@contextmanager
def scoped_environment(values: dict[str, str]):
    """Temporarily expose only supplied values to adapters requiring os.environ."""
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class ManagedLoopbackRuntime:
    """No-shell, private-artifact lifecycle for an isolated llama-server."""

    def __init__(
        self,
        *,
        binary: Path,
        gguf: Path,
        alias: str = _EXPECTED_MODEL,
        timeout_seconds: float = 30,
        process_factory=subprocess.Popen,
        readiness_probe=None,
    ) -> None:
        self.binary = binary
        self.gguf = gguf
        self.alias = alias
        self.timeout_seconds = timeout_seconds
        self.process_factory = process_factory
        self.readiness_probe = readiness_probe or self._default_readiness_probe
        self.port: int | None = None
        self.process: subprocess.Popen | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._stdout = None
        self._stderr = None
        self.cleanup_reason: str | None = None

    @staticmethod
    def _free_port() -> int:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _default_readiness_probe(self, port: int) -> bool:
        connection: http.client.HTTPConnection | None = None
        try:
            # HTTPConnection talks directly to the numeric loopback address;
            # unlike urllib it never inherits proxy environment variables.
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            connection.request("GET", "/health")
            response = connection.getresponse()
            return 200 <= response.status < 300
        except OSError:
            return False
        finally:
            if connection is not None:
                connection.close()

    def start(self) -> None:
        self.port = self._free_port()
        self._temporary = tempfile.TemporaryDirectory(prefix="arv001-llama-runtime-")
        private = Path(self._temporary.name)
        self._stdout = (private / "stdout.log").open("w", encoding="utf-8")
        self._stderr = (private / "stderr.log").open("w", encoding="utf-8")
        command = [
            str(self.binary), "--model", str(self.gguf), "--host", "127.0.0.1",
            "--port", str(self.port), "--alias", self.alias,
        ]
        try:
            self.process = self.process_factory(command, stdout=self._stdout, stderr=self._stderr)
            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("llama_runtime_exited_early")
                if self.readiness_probe(self.port):
                    return
                time.sleep(0.1)
            raise RuntimeError("llama_runtime_readiness_timeout")
        except BaseException:
            try:
                self.stop()
            except RuntimeError as cleanup_error:
                self.cleanup_reason = str(cleanup_error)
            raise

    def stop(self) -> None:
        failed = False
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    failed = True
        for handle in (self._stdout, self._stderr):
            if handle is not None:
                handle.close()
        if self._temporary is not None:
            self._temporary.cleanup()
        self._stdout = self._stderr = self._temporary = None
        if failed:
            raise RuntimeError("llama_runtime_cleanup_failed")
        if self.process is not None and self.process.poll() is None:
            raise RuntimeError("llama_runtime_orphan_detected")

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None:
            try:
                self.stop()
            except RuntimeError as cleanup_error:
                self.cleanup_reason = str(cleanup_error)
            return
        self.stop()


def _safe_loopback_url(value: str, *, path: str) -> tuple[str, int] | None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.port is None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != path
    ):
        return None
    return parsed.hostname, parsed.port


def _request_json(*, url: str, method: str, body: dict[str, object] | None = None) -> tuple[int, object] | None:
    expected_path = "/v1/models" if method == "GET" else "/tokenize"
    target = _safe_loopback_url(url, path=expected_path)
    if target is None:
        return None
    host, port = target
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        payload = json.dumps(body, separators=(",", ":")) if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        connection.request(method, expected_path, body=payload, headers=headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            return response.status, None
        return response.status, json.loads(response.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def probe_zero_generation(
    *,
    loopback_base_url: str,
    tokenizer_url: str,
    expected_alias: str = _EXPECTED_MODEL,
    tokenizer_adapter: object | None = None,
    request=_request_json,
    tokenizer_identity: str | None = None,
) -> tuple[dict[str, object] | None, tuple[str, ...]]:
    """Probe only GET /v1/models and POST /tokenize on the managed loopback."""
    errors: list[str] = []
    models_url = loopback_base_url.rstrip("/") + "/v1/models"
    models_response = request(url=models_url, method="GET")
    models = models_response[1] if models_response and models_response[0] == 200 else None
    identifiers = (
        [item.get("id") for item in models.get("data", []) if isinstance(item, dict)]
        if isinstance(models, dict)
        else []
    )
    if not identifiers:
        errors.append("loopback_models_probe_failed")
    elif identifiers != [expected_alias]:
        errors.append("loopback_model_alias_mismatch")

    if _safe_loopback_url(tokenizer_url, path="/tokenize") is None:
        errors.append("tokenizer_endpoint_unsafe")
        tokenizer = None
    else:
        tokenizer_response = request(url=tokenizer_url, method="POST", body=_TOKENIZER_PROBE_BODY)
        if tokenizer_response and 300 <= tokenizer_response[0] < 400:
            errors.append("tokenizer_redirect_unsafe")
            tokenizer = None
        else:
            tokenizer = tokenizer_response[1] if tokenizer_response and tokenizer_response[0] == 200 else None
        if tokenizer is None:
            errors.append("tokenizer_probe_failed")
        elif not (
            isinstance(tokenizer, dict)
            and isinstance(tokenizer.get("tokens"), list)
            and tokenizer["tokens"]
            and all(isinstance(token, int) for token in tokenizer["tokens"])
        ):
            errors.append("tokenizer_response_invalid")
    if tokenizer_adapter is not None and not bool(getattr(tokenizer_adapter, "persistent", False)):
        errors.append("exact_persistent_tokenizer_missing")
    identity = tokenizer_identity
    if not isinstance(identity, str) or not identity:
        errors.append("tokenizer_identity_missing")
    if errors:
        return None, tuple(errors)
    return {
        "loopback_verified": True,
        "models_probe_verified": True,
        "tokenizer_probe_verified": True,
        "tokenizer_persistent": bool(getattr(tokenizer_adapter, "persistent", True)),
        "tokenizer_identity_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "provider_generation_calls": 0,
    }, ()


def _loopback(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "::1", "localhost"}


def read_private_env(path: Path, repository_root: Path) -> tuple[dict[str, str], tuple[str, ...]]:
    """Read allow-listed dotenv values without exporting them into the shell."""
    errors: list[str] = []
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return {}, ("private_env_missing",)
    if path.is_symlink() or not resolved.is_file():
        errors.append("private_env_unsafe")
    if repository_root == resolved or repository_root in resolved.parents:
        errors.append("private_env_inside_repository")
    try:
        mode = stat.S_IMODE(resolved.stat().st_mode)
    except OSError:
        return {}, tuple(sorted(set(errors + ["private_env_unsafe"])))
    if mode != 0o600:
        errors.append("private_env_mode_invalid")
    values: dict[str, str] = {}
    try:
        source = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}, tuple(sorted(set(errors + ["private_env_unreadable"])))
    for raw in source.splitlines():
        if not raw or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key.removeprefix("export ").strip()
        if key in ENV_KEYS or key in _UNPREFIXED:
            values[key] = value.strip().strip('"').strip("'")
    if any(key in values for key in _UNPREFIXED) and not any(key in values for key in ENV_KEYS):
        errors.append("unprefixed_llm_environment_detected")
    return {key: values[key] for key in ENV_KEYS if key in values}, tuple(sorted(set(errors)))


def validate_settings(values: dict[str, str]) -> tuple[str, ...]:
    errors: list[str] = []
    if values.get("AI_CORP_LLM_PROVIDER") != "openai_compatible": errors.append("configured_provider_not_approved")
    if values.get("AI_CORP_LLM_MODEL") != _EXPECTED_MODEL: errors.append("configured_model_not_approved")
    if values.get("AI_CORP_LLM_MAX_RETRIES") != "0": errors.append("configured_retries_not_zero")
    if not values.get("AI_CORP_OPENAI_API_KEY"): errors.append("provider_credential_missing")
    if not _loopback(values.get("AI_CORP_OPENAI_BASE_URL", "")): errors.append("configured_base_url_not_loopback")
    if not _loopback(values.get("ARV003_LLAMA_TOKENIZER_URL", "")): errors.append("tokenizer_url_not_loopback")
    if not values.get("ARV003_TOKENIZER_IDENTITY"): errors.append("tokenizer_identity_missing")
    return tuple(errors)


def validate_python(repository_root: Path) -> tuple[str, ...]:
    errors: list[str] = []
    if sys.version_info[:2] != (3, 11):
        errors.append("python_version_not_311")
    if Path.cwd().resolve() != repository_root:
        errors.append("repository_root_not_current_directory")
    try:
        import redis  # noqa: F401
        import sqlalchemy  # noqa: F401

        from src.shared.config.settings import get_settings  # noqa: F401
    except ImportError:
        errors.append("dependency_complete_interpreter_required")
    return tuple(errors)


def validate_repository(*, repository_root: Path, expected_head: str) -> tuple[str, ...]:
    try:
        top_level = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], cwd=repository_root, text=True
        ).strip()
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository_root, text=True
        ).strip()
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=repository_root, text=True
        ).strip()
        worktree = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repository_root, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ("git_repository_unavailable",)
    errors: list[str] = []
    if Path(top_level).resolve() != repository_root.resolve():
        errors.append("repository_root_mismatch")
    if head != expected_head:
        errors.append("git_head_mismatch")
    if branch != "fix/arv001-final-one-pass" or worktree:
        errors.append("git_worktree_not_clean")
    return tuple(errors)


def run_doctor(*, private_env: Path | None, repository_root: Path, head_sha: str, asset_roots: tuple[Path, ...] = (), gguf_path: Path | None = None, llama_server_path: Path | None = None) -> DoctorReport:
    """Run all independent checks that do not require starting a model."""
    report = DoctorReport(head_sha=head_sha)
    report.phases.append(Phase("repository", validate_repository(repository_root=repository_root, expected_head=head_sha)))
    report.phases.append(Phase("python_runtime", validate_python(repository_root)))
    values, env_errors = read_private_env(private_env, repository_root) if private_env else ({}, ())
    report.phases.append(Phase("static_environment", env_errors))
    if private_env:
        report.phases.append(Phase("settings", validate_settings(values)))
    _, gguf_errors = validate_gguf_path(gguf_path) if gguf_path else discover_gguf(asset_roots)
    _, binary_errors = validate_llama_server_path(llama_server_path) if llama_server_path else discover_llama_server(asset_roots)
    report.phases.append(Phase("gguf_discovery", gguf_errors))
    report.phases.append(Phase("llama_server_discovery", binary_errors))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="ARV-001 zero-generation runtime doctor")
    parser.add_argument("--private-env", type=Path)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--asset-root", action="append", type=Path, default=[])
    parser.add_argument("--gguf-path", type=Path)
    parser.add_argument("--llama-server-path", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    report = run_doctor(private_env=args.private_env, repository_root=root, head_sha=args.expected_head, asset_roots=tuple(args.asset_root), gguf_path=args.gguf_path, llama_server_path=args.llama_server_path)
    print(json.dumps(report.sanitized(), ensure_ascii=True, sort_keys=False))
    return 0 if report.sanitized()["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
