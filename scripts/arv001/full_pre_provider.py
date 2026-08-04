"""One zero-generation ARV-001 orchestration entrypoint.

The command delegates corpus persistence to the split-root adapter in
``--prepare-only`` mode and never exposes private runtime values in stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from scripts.arv001.prepared_publication import (
    PreparedPublicationError,
    publish_prepared_state,
)
from scripts.arv001.prepared_verification import (
    PreparedDatabaseVerification,
    PreparedVerificationError,
    PrivatePreparedVerificationDescriptor,
    parse_private_descriptor,
    verify_prepared_database,
)
from scripts.arv001.runtime_doctor import (
    ManagedLoopbackRuntime,
    discover_gguf,
    discover_llama_server,
    ephemeral_runtime_environment,
    locate_runtime_assets,
    probe_zero_generation,
    read_private_env,
    run_doctor,
    scoped_environment,
    validate_effective_runtime_environment,
    validate_gguf_path,
    validate_llama_server_path,
    write_private_runtime_profile,
)

_PREPARE_PAYLOAD_FIELDS = {
    "status",
    "marker",
    "head_sha",
    "physical_file_count",
    "logical_document_count",
    "mapped_file_count",
    "extracted_document_count",
    "prepared_chunk_count",
    "post_persistence_gate5_ready",
    "controlled_preflight_invocations",
    "controlled_provider_invocations",
    "provider_generation_calls",
    "production_db_mutations",
    "old_arv003_mutations",
    "git_mutations",
}
_PHASE_ORDER = (
    "repository",
    "python_runtime",
    "static_environment",
    "gguf_validation",
    "llama_server_validation",
    "runtime_start",
    "effective_environment",
    "models_probe",
    "tokenizer_probe",
    "runtime_profile",
    "corpus_contract",
    "database",
    "application_persistence",
    "snapshot_binding",
    "source_graph_binding",
    "post_persistence_gate5",
    "controlled_preflight",
    "prepared_state_persistence",
    "privacy_scan",
    "cleanup",
)
_COUNTER_FIELDS = (
    "controlled_preflight_invocations",
    "controlled_provider_invocations",
    "provider_generation_calls",
    "production_db_mutations",
    "old_arv003_mutations",
    "git_data_leaks",
)


class _PhaseRecorder:
    """Internal state machine: a phase passes only after its real completion."""

    def __init__(self) -> None:
        self._states = {phase: ("SKIPPED_DEPENDENCY", ()) for phase in _PHASE_ORDER}

    def passed(self, phase: str) -> None:
        self._set(phase, "PASS", ())

    def failed(self, phase: str, *codes: str) -> None:
        self._set(phase, "FAIL", tuple(sorted(set(codes))))

    def _set(self, phase: str, status: str, codes: tuple[str, ...]) -> None:
        if phase not in self._states or status not in {"PASS", "FAIL"}:
            raise ValueError("invalid phase state")
        self._states[phase] = (status, codes)

    def sanitized(self) -> list[dict[str, object]]:
        return [
            {"phase": phase, "status": status, "reason_codes": list(codes)}
            for phase, (status, codes) in self._states.items()
        ]

    def clone(self) -> _PhaseRecorder:
        clone = _PhaseRecorder()
        clone._states = dict(self._states)
        return clone


def _result(
    *,
    head_sha: str,
    recorder: _PhaseRecorder,
    status: str,
    counters: dict[str, int] | None = None,
    acceptance: dict[str, object] | None = None,
) -> dict[str, object]:
    result = {
        "schema_version": "arv001-full-pre-provider-v1",
        "status": status,
        "head_sha": head_sha,
        "phases": recorder.sanitized(),
        "counters": {
            field: int((counters or {}).get(field, 0)) for field in _COUNTER_FIELDS
        },
        "acceptance": acceptance or {},
    }
    _validate_public_result(result)
    return result


def _failure(
    *, head_sha: str, phase: str, code: str, recorder: _PhaseRecorder | None = None
) -> dict[str, object]:
    recorder = recorder or _PhaseRecorder()
    recorder.failed(phase, code)
    return _result(head_sha=head_sha, recorder=recorder, status="FAIL_CLOSED")


def _validate_public_result(result: dict[str, object]) -> None:
    if set(result) != {
        "schema_version",
        "status",
        "head_sha",
        "phases",
        "counters",
        "acceptance",
    }:
        raise ValueError("public result schema")
    if result["status"] not in {"PASS", "FAIL_CLOSED"} or not isinstance(
        result["head_sha"], str
    ):
        raise ValueError("public result status")
    phases = result["phases"]
    if not isinstance(phases, list) or [
        phase.get("phase") for phase in phases if isinstance(phase, dict)
    ] != list(_PHASE_ORDER):
        raise ValueError("public phase order")
    for phase in phases:
        if not isinstance(phase, dict) or set(phase) != {
            "phase",
            "status",
            "reason_codes",
        }:
            raise ValueError("public phase schema")
        if phase["status"] not in {"PASS", "FAIL", "SKIPPED_DEPENDENCY"} or phase[
            "reason_codes"
        ] != sorted(phase["reason_codes"]):
            raise ValueError("public phase value")
    if not isinstance(result["counters"], dict) or set(result["counters"]) != set(
        _COUNTER_FIELDS
    ):
        raise ValueError("public counters")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_prepared_state_manifest(
    private_root: Path, payload: dict[str, object]
) -> bool:
    database = private_root / "prepared.sqlite3"
    if not database.is_file() or database.is_symlink():
        return False
    manifest = {
        "schema_version": "arv001-prepared-state-v1",
        "database_sha256": _sha256(database),
        "head_sha": payload["head_sha"],
        "physical_file_count": payload["physical_file_count"],
        "logical_document_count": payload["logical_document_count"],
        "extracted_document_count": payload["extracted_document_count"],
        "prepared_chunk_count": payload["prepared_chunk_count"],
        "provider_generation_calls": 0,
    }
    target = private_root / "prepared-state-manifest.json"
    if target.exists():
        return False
    staged = private_root / ".prepared-state-manifest.partial"
    staged.write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    os.chmod(staged, 0o600)
    os.replace(staged, target)
    return True


def _private_staging_root(
    private_root: Path, repository_root: Path
) -> tuple[Path | None, Path | None]:
    """Create a non-symlink private staging directory without exposing its path."""
    try:
        raw = private_root.expanduser()
        if raw.is_symlink() or any(parent.is_symlink() for parent in raw.parents):
            return None, None
        root = raw.resolve()
        if root == repository_root or repository_root in root.parents:
            return None, None
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        final = root / "prepared-state"
        if final.exists() or final.is_symlink():
            return None, None
        staging = root / f".prepared-state.partial.{secrets.token_urlsafe(16)}"
        staging.mkdir(mode=0o700)
        return staging, final
    except OSError:
        return None, None


def _verify_prepared_database(
    path: Path,
    descriptor: PrivatePreparedVerificationDescriptor | None = None,
    data_dir: Path | None = None,
) -> PreparedDatabaseVerification | None:
    if descriptor is None or data_dir is None:
        return None
    return verify_prepared_database(path=path, descriptor=descriptor, data_dir=data_dir)


def _prepared_manifest_base(
    *,
    payload: dict[str, object],
    binary_profile: dict[str, str],
    gguf_profile: dict[str, str],
    probe: dict[str, object],
    corpus_sha: str,
    policy_sha: str,
    verification: PreparedDatabaseVerification,
) -> dict[str, object]:
    return {
        "schema_version": "arv001-prepared-state-v1",
        "head_sha": payload["head_sha"],
        "corpus_sha256": corpus_sha,
        "policy_sha256": policy_sha,
        "binary_sha256": binary_profile["binary_sha256"],
        "gguf_sha256": gguf_profile["gguf_sha256"],
        "tokenizer_identity_sha256": probe["tokenizer_identity_sha256"],
        "database_sha256": verification.database_sha256,
        "physical_document_count": verification.physical_document_count,
        "logical_document_count": 6,
        "extracted_document_count": verification.extracted_document_count,
        "chunk_count": verification.chunk_count,
        "snapshot_binding_verified": verification.snapshot_binding_verified,
        "source_graph_binding_verified": verification.source_graph_binding_verified,
        "gate5_ready": verification.gate5_ready,
        "controlled_preflight_verified": verification.controlled_preflight_verified,
        "controlled_preflight_invocations": 1,
        "controlled_provider_invocations": 0,
        "provider_generation_calls": 0,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _prepare_payload_error(payload: object, expected_head: str) -> str | None:
    if not isinstance(payload, dict):
        return "child_payload_invalid"
    if set(payload) != _PREPARE_PAYLOAD_FIELDS:
        return "child_payload_schema_invalid"
    expected = {
        "status": "application_prepared",
        "marker": "ARV-001_APPLICATION_PREPARED",
        "head_sha": expected_head,
        "physical_file_count": 10,
        "logical_document_count": 6,
        "extracted_document_count": 10,
        "prepared_chunk_count": 233,
        "post_persistence_gate5_ready": True,
        "controlled_preflight_invocations": 1,
        "controlled_provider_invocations": 0,
        "provider_generation_calls": 0,
        "production_db_mutations": 0,
        "old_arv003_mutations": 0,
        "git_mutations": 0,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            return f"child_{key}_invalid"
    if (
        not isinstance(payload["mapped_file_count"], int)
        or payload["mapped_file_count"] != 10
    ):
        return "child_mapped_file_count_invalid"
    return None


def _safe_child_failure(stderr: str) -> str:
    """Keep a repository reason code from the child without exposing diagnostics."""
    import re

    value = stderr.strip().splitlines()[-1].strip().lower() if stderr.strip() else ""
    if re.fullmatch(r"[a-z0-9_:-]{1,120}", value or ""):
        return value
    return "application_persistence_failed"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ARV-001 full pre-provider contour")
    parser.add_argument("--private-env", type=Path)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--intake-root", type=Path, required=True)
    parser.add_argument("--approved-policy", type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--expected-corpus-sha", required=True)
    parser.add_argument("--expected-policy-sha", required=True)
    parser.add_argument("--asset-root", action="append", type=Path, default=[])
    parser.add_argument("--gguf-path", type=Path)
    parser.add_argument("--llama-server-path", type=Path)
    parser.add_argument("--runtime-profile-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    root = Path(__file__).resolve().parents[2]
    recorder = _PhaseRecorder()
    exact_mode = args.gguf_path is not None and args.llama_server_path is not None
    if (
        (args.gguf_path is None) != (args.llama_server_path is None)
        or (exact_mode and args.asset_root)
        or (not exact_mode and not args.asset_root)
    ):
        print(
            json.dumps(
                _failure(
                    head_sha=args.expected_head,
                    phase="gguf_validation",
                    code="runtime_asset_selection_mode_invalid",
                    recorder=recorder,
                ),
                sort_keys=False,
            )
        )
        return 2
    doctor = run_doctor(
        private_env=None,
        repository_root=root,
        head_sha=args.expected_head,
        asset_roots=tuple(args.asset_root),
        gguf_path=args.gguf_path,
        llama_server_path=args.llama_server_path,
    ).sanitized()
    if doctor["status"] != "PASS":
        print(
            json.dumps(
                _failure(
                    head_sha=args.expected_head,
                    phase="repository",
                    code="repository_validation_failed",
                    recorder=recorder,
                ),
                sort_keys=False,
            )
        )
        return 2
    recorder.passed("repository")
    recorder.passed("python_runtime")
    recorder.passed("static_environment")
    values, env_errors = (
        read_private_env(args.private_env, root) if args.private_env else ({}, ())
    )
    assets, asset_errors = locate_runtime_assets(
        tuple(args.asset_root),
        gguf_path=args.gguf_path,
        llama_server_path=args.llama_server_path,
    )
    if env_errors or asset_errors or assets is None:
        phase = "static_environment" if env_errors else "gguf_validation"
        code = (
            "private_environment_invalid"
            if env_errors
            else "approved_gguf_validation_failed"
        )
        print(
            json.dumps(
                _failure(
                    head_sha=args.expected_head,
                    phase=phase,
                    code=code,
                    recorder=recorder,
                ),
                sort_keys=False,
            )
        )
        return 2
    binary, gguf = assets
    gguf_profile, _ = (
        validate_gguf_path(args.gguf_path)
        if args.gguf_path
        else discover_gguf(tuple(args.asset_root))
    )
    binary_profile, _ = (
        validate_llama_server_path(args.llama_server_path)
        if args.llama_server_path
        else discover_llama_server(tuple(args.asset_root))
    )
    if not gguf_profile:
        print(
            json.dumps(
                _failure(
                    head_sha=args.expected_head,
                    phase="gguf_validation",
                    code="approved_gguf_validation_failed",
                    recorder=recorder,
                ),
                sort_keys=False,
            )
        )
        return 2
    recorder.passed("gguf_validation")
    if not binary_profile:
        print(
            json.dumps(
                _failure(
                    head_sha=args.expected_head,
                    phase="llama_server_validation",
                    code="llama_server_validation_failed",
                    recorder=recorder,
                ),
                sort_keys=False,
            )
        )
        return 2
    recorder.passed("llama_server_validation")
    staging, final_state = _private_staging_root(args.runtime_profile_dir, root)
    if staging is None or final_state is None:
        print(
            json.dumps(
                _failure(
                    head_sha=args.expected_head,
                    phase="prepared_state_persistence",
                    code="prepared_state_persistence_failed",
                    recorder=recorder,
                ),
                sort_keys=False,
            )
        )
        return 2
    with tempfile.TemporaryDirectory(prefix="arv001-full-pre-provider-") as directory:
        work = Path(directory)
        try:
            with ManagedLoopbackRuntime(binary=binary, gguf=gguf) as runtime:
                assert runtime.port is not None
                recorder.passed("runtime_start")
                from src.modules.production_llm_analysis.batching import (
                    tokenizer_from_environment,
                )

                with ephemeral_runtime_environment(
                    port=runtime.port,
                    binary_sha256=binary_profile["binary_sha256"],
                    gguf_sha256=gguf_profile["gguf_sha256"],
                    overrides=values,
                ) as (effective, _private_env):
                    if validate_effective_runtime_environment(
                        effective, port=runtime.port
                    ):
                        raise RuntimeError("effective_settings_invalid")
                    recorder.passed("effective_environment")
                    environment = os.environ.copy()
                    environment.update(effective)
                    with scoped_environment(effective):
                        tokenizer = tokenizer_from_environment()
                    probe, probe_errors = probe_zero_generation(
                        loopback_base_url=f"http://127.0.0.1:{runtime.port}",
                        tokenizer_url=effective["ARV003_LLAMA_TOKENIZER_URL"],
                        tokenizer_adapter=tokenizer,
                        tokenizer_identity=effective["ARV003_TOKENIZER_IDENTITY"],
                    )
                    if probe_errors or probe is None:
                        raise RuntimeError("zero_generation_probe_failed")
                    recorder.passed("models_probe")
                    recorder.passed("tokenizer_probe")
                    profile, profile_errors = write_private_runtime_profile(
                        private_directory=staging,
                        repository_root=root,
                        profile={
                            "version": "arv001-runtime-v1",
                            **gguf_profile,
                            **binary_profile,
                            "model_alias": "arvectum-gemma4-12b-it-qat-q4_0",
                            "provider": "openai_compatible",
                            **probe,
                            "created_at": datetime.now(UTC).isoformat(),
                        },
                    )
                    if profile_errors or profile is None:
                        raise RuntimeError("runtime_profile_write_failed")
                    recorder.passed("runtime_profile")
                    command = [
                        sys.executable,
                        "-m",
                        "scripts.arv001.run_complete_corpus_acceptance_split_roots",
                        "--candidate-root",
                        str(args.candidate_root),
                        "--intake-root",
                        str(args.intake_root),
                        "--database-path",
                        str(staging / "prepared.sqlite3"),
                        "--initialize-database",
                        "--private-verification-descriptor",
                        str(staging / "prepared-verification.json"),
                        "--data-dir",
                        str(staging / "application-data"),
                        "--approved-policy",
                        str(args.approved_policy),
                        "--output-root",
                        str(work / "output"),
                        "--expected-head",
                        args.expected_head,
                        "--expected-corpus-sha",
                        args.expected_corpus_sha,
                        "--expected-policy-sha",
                        args.expected_policy_sha,
                        "--prepare-only",
                    ]
                    result = subprocess.run(
                        command,
                        cwd=root,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
        except Exception:  # noqa: BLE001 - terminal output must remain sanitized.
            shutil.rmtree(staging, ignore_errors=True)
            print(
                json.dumps(
                    _failure(
                        head_sha=args.expected_head,
                        phase="runtime_start",
                        code="llama_runtime_start_failed",
                        recorder=recorder,
                    ),
                    sort_keys=False,
                )
            )
            return 2
        if result.returncode != 0:
            shutil.rmtree(staging, ignore_errors=True)
            print(
                json.dumps(
                    _failure(
                        head_sha=args.expected_head,
                        phase="application_persistence",
                        code=_safe_child_failure(result.stderr),
                        recorder=recorder,
                    ),
                    sort_keys=False,
                )
            )
            return result.returncode
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            shutil.rmtree(staging, ignore_errors=True)
            print(
                json.dumps(
                    _failure(
                        head_sha=args.expected_head,
                        phase="controlled_preflight",
                        code="controlled_preflight_payload_invalid",
                        recorder=recorder,
                    ),
                    sort_keys=False,
                )
            )
            return 2
    payload_error = _prepare_payload_error(payload, args.expected_head)
    if payload_error:
        shutil.rmtree(staging, ignore_errors=True)
        print(
            json.dumps(
                _failure(
                    head_sha=args.expected_head,
                    phase="controlled_preflight",
                    code="controlled_preflight_payload_invalid",
                    recorder=recorder,
                ),
                sort_keys=False,
            )
        )
        return 2
    descriptor_path = staging / "prepared-verification.json"
    try:
        descriptor_data = parse_private_descriptor(
            descriptor_path,
            expected_head=args.expected_head,
            expected_corpus_sha=args.expected_corpus_sha,
        )
    except PreparedVerificationError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        print(
            json.dumps(
                _failure(
                    head_sha=args.expected_head,
                    phase="prepared_state_persistence",
                    code=exc.code,
                    recorder=recorder,
                ),
                sort_keys=False,
            )
        )
        return 2
    verification = _verify_prepared_database(
        staging / "prepared.sqlite3",
        descriptor_data,
        staging / "application-data",
    )
    if verification is None:
        shutil.rmtree(staging, ignore_errors=True)
        print(
            json.dumps(
                _failure(
                    head_sha=args.expected_head,
                    phase="snapshot_binding",
                    code="prepared_database_verification_failed",
                    recorder=recorder,
                ),
                sort_keys=False,
            )
        )
        return 2
    for phase in (
        "corpus_contract",
        "database",
        "application_persistence",
        "snapshot_binding",
        "source_graph_binding",
        "post_persistence_gate5",
        "controlled_preflight",
    ):
        recorder.passed(phase)
    counters = {
        "controlled_preflight_invocations": 1,
        "controlled_provider_invocations": 0,
        "provider_generation_calls": 0,
        "production_db_mutations": 0,
        "old_arv003_mutations": 0,
        "git_data_leaks": 0,
    }
    acceptance = {
        "application_prepared": True,
        "post_persistence_gate5_ready": True,
        "controlled_preflight_only": True,
        "physical_file_count": 10,
        "logical_document_count": 6,
        "extracted_document_count": 10,
        "prepared_chunk_count": 233,
    }
    final_recorder = recorder.clone()
    final_recorder.passed("prepared_state_persistence")
    final_recorder.passed("privacy_scan")
    final_recorder.passed("cleanup")
    final_result = _result(
        head_sha=args.expected_head,
        recorder=final_recorder,
        status="PASS",
        counters=counters,
        acceptance=acceptance,
    )
    base_manifest = _prepared_manifest_base(
        payload=payload,
        binary_profile=binary_profile,
        gguf_profile=gguf_profile,
        probe=probe,
        corpus_sha=args.expected_corpus_sha,
        policy_sha=args.expected_policy_sha,
        verification=verification,
    )
    try:
        publish_prepared_state(
            staging=staging,
            final=final_state,
            base_manifest=base_manifest,
            result=final_result,
            forbidden_literals=(
                descriptor_data.target_run_id,
                descriptor_data.customer_id,
                descriptor_data.project_id,
                descriptor_data.case_id,
                descriptor_data.tender_id,
                descriptor_data.snapshot_id,
                descriptor_data.source_graph_id,
            ),
        )
    except PreparedPublicationError as exc:
        phase = (
            "privacy_scan"
            if exc.code == "prepared_privacy_violation"
            else "prepared_state_persistence"
        )
        recorder.failed(phase, *exc.reason_codes)
        failed_result = _result(
            head_sha=args.expected_head,
            recorder=recorder,
            status="FAIL_CLOSED",
            counters=counters,
            acceptance=acceptance,
        )
        print(json.dumps(failed_result, sort_keys=False))
        return 2
    print(json.dumps(final_result, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
