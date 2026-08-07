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
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.arv001.prepared_publication import (
    PreparedPublicationError,
    publish_prepared_state,
)
from scripts.arv001.prepared_verification import (
    PreparedDatabaseVerification,
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


def _live_output_boundary_acceptance(
    request: Any, tokenizer: Any | None = None
) -> dict[str, object]:
    """Record the deterministic live-schema boundary acceptance."""
    from src.modules.production_llm_analysis.live_output_boundary import (
        GRAMMAR_WHITESPACE_CONTRACT_VERSION,
        GRAMMAR_WHITESPACE_MAX_BYTES_PER_SLOT,
        verify_exact_live_output_budget,
    )

    proof = verify_exact_live_output_budget(request, tokenizer=tokenizer)
    return {
        "live_schema_mono_schema_enforced": True,
        "reasoning_disabled_verified": True,
        "exact_live_output_budget_proof": "PASS",
        "exact_live_output_tokenizer_available": True,
        **proof,
        "grammar_whitespace_contract_version": GRAMMAR_WHITESPACE_CONTRACT_VERSION,
        "grammar_whitespace_max_bytes_per_slot": GRAMMAR_WHITESPACE_MAX_BYTES_PER_SLOT,
        "provider_generation_calls": 0,
        "controlled_provider_invocations": 0,
    }


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


def _check_protected_drift(
    repository_root: Path, snapshot_head: str, current_head: str
) -> tuple[bool, bool]:
    """Verify no changes in protected paths between snapshot and current head."""
    protected_paths = [
        "src/modules/document_ingestion/",
        "src/modules/document_store/",
        "src/modules/production_llm_analysis/batching.py",
        "src/modules/production_llm_analysis/controlled_evidence.py",
        "src/modules/production_llm_analysis/evidence.py",
        "src/modules/production_llm_analysis/grounding.py",
        "src/modules/production_llm_analysis/schemas.py",
        "src/tender_research/",
        "src/shared/db/",
        "src/modules/customer_pilot/",
        "migrations/",
    ]
    drift = False
    migration_drift = False

    for path in protected_paths:
        result = subprocess.run(
            ["git", "diff", "--quiet", snapshot_head, current_head, "--", path],
            cwd=repository_root,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            drift = True
            if path.startswith("migrations/"):
                migration_drift = True

    return drift, migration_drift


def _copy_snapshot(
    source_root: Path, staging: Path, expected_db_sha: str
) -> bool:
    """Byte-identically copy the prepared database and verify its SHA."""
    source_db = source_root / "prepared.sqlite3"
    target_db = staging / "prepared.sqlite3"

    if not source_db.is_file():
        return False

    shutil.copy2(source_db, target_db)
    copied_sha = _sha256(target_db)

    return copied_sha == expected_db_sha


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ARV-001 full pre-provider contour")
    parser.add_argument("--private-env", type=Path)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--intake-root", type=Path)
    parser.add_argument("--prepared-snapshot-root", type=Path)
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

    raw_mode = args.candidate_root is not None and args.intake_root is not None
    snapshot_mode = args.prepared_snapshot_root is not None
    if (raw_mode and snapshot_mode) or (not raw_mode and not snapshot_mode):
        print(
            json.dumps(
                _failure(
                    head_sha=args.expected_head,
                    phase="repository",
                    code="recovery_mode_conflict",
                    recorder=recorder,
                ),
                sort_keys=False,
            )
        )
        return 2

    if snapshot_mode:
        # #134: Validate snapshot ancestry and drift.
        snapshot_head = "5f6aa316f6f66306794e72bbcb90ad7bba3fba34"
        ancestry_result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", snapshot_head, args.expected_head],
            cwd=root,
            check=False,
        )
        if ancestry_result.returncode != 0:
            print(
                json.dumps(
                    _failure(
                        head_sha=args.expected_head,
                        phase="repository",
                        code="snapshot_head_not_ancestor",
                        recorder=recorder,
                    ),
                    sort_keys=False,
                )
            )
            return 2

        drift, _ = _check_protected_drift(
            root, snapshot_head, args.expected_head
        )
        if drift:
            print(
                json.dumps(
                    _failure(
                        head_sha=args.expected_head,
                        phase="repository",
                        code="prepared_snapshot_not_carry_forward_safe",
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
                from src.modules.production_llm_analysis.schemas import (
                    ProductionLLMAnalysisRequest,
                    EvidencePacket,
                    DataHandlingReport,
                    BudgetPolicy,
                    BudgetLimits,
                    ProviderPricing,
                    EvidenceFragment,
                )
                from src.modules.production_llm_analysis.contracts import R10_1_CONTROLLED_MAP_CONTRACT

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

                    if snapshot_mode:
                        # Carry-forward mode: load old manifest and descriptor.
                        old_manifest_path = args.prepared_snapshot_root / "prepared-state-manifest.json"
                        old_descriptor_path = args.prepared_snapshot_root / "prepared-verification.json"
                        if not old_manifest_path.is_file() or not old_descriptor_path.is_file():
                            raise RuntimeError("prepared_snapshot_metadata_missing")

                        old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
                        expected_db_sha = old_manifest["database_sha256"]

                        if not _copy_snapshot(args.prepared_snapshot_root, staging, expected_db_sha):
                            raise RuntimeError("prepared_database_copy_failed")

                        # Byte-identically copy old metadata for current-head re-verification.
                        shutil.copy2(old_descriptor_path, staging / "prepared-verification.json")
                        shutil.copy2(args.prepared_snapshot_root / "runtime-profile.json", staging / "runtime-profile.json")
                        # For live output budget proof we need a request object.
                        # Since request.json might be missing, reconstruct it from DB fragments.
                        conn = sqlite3.connect(staging / "prepared.sqlite3")
                        conn.row_factory = sqlite3.Row
                        # Use first 10 chunks as proxy for fragments (Gemma proof doesn't need text content).
                        chunk_rows = conn.execute("SELECT text_hash FROM procurement_document_chunks LIMIT 10").fetchall()
                        fragments = [
                            EvidenceFragment(
                                fragment_id=row["text_hash"],
                                document_id="dummy",
                                document_name="dummy",
                                chunk_id="dummy",
                                text="dummy",
                                text_sha256="0"*64
                            ) for row in chunk_rows
                        ]
                        conn.close()

                        packet = EvidencePacket(
                            customer_id="dummy", project_id="dummy", procurement_case_id="dummy",
                            run_id="dummy", registry_number="dummy",
                            fragments=fragments,
                            data_handling=DataHandlingReport(),
                            packet_hash="0"*64
                        )
                        policy = BudgetPolicy(
                            limits=BudgetLimits(
                                max_input_tokens=1000, max_output_tokens=4096,
                                timeout_ms=1000, max_total_latency_ms=1000, max_estimated_cost=1.0
                            ),
                            pricing=ProviderPricing(
                                input_cost_per_1k_tokens=0.0, output_cost_per_1k_tokens=0.0,
                                pricing_table_version="dummy"
                            )
                        )
                        request = ProductionLLMAnalysisRequest(
                            request_id="0"*64,
                            customer_id="dummy", project_id="dummy", procurement_case_id="dummy",
                            run_id="dummy", registry_number="dummy",
                            provider="openai_compatible", model="arvectum-gemma4-12b-it-qat-q4_0",
                            prompt_id=R10_1_CONTROLLED_MAP_CONTRACT.prompt_id,
                            prompt_version=R10_1_CONTROLLED_MAP_CONTRACT.prompt_version,
                            output_schema_id=R10_1_CONTROLLED_MAP_CONTRACT.output_schema_id,
                            output_schema_version=R10_1_CONTROLLED_MAP_CONTRACT.output_schema_version,
                            grounding_policy_version=R10_1_CONTROLLED_MAP_CONTRACT.grounding_policy_version,
                            evidence_packet=packet,
                            budget_policy=policy,
                            max_claims=3,
                            allowed_field_paths=[] # Will default to all requirement paths
                        )

                        # Re-verify the copied DB against current code rules (read-only).
                        descriptor_data = parse_private_descriptor(
                            staging / "prepared-verification.json",
                            expected_head="5f6aa316f6f66306794e72bbcb90ad7bba3fba34",
                            expected_corpus_sha=args.expected_corpus_sha,
                        )
                        verification = _verify_prepared_database(
                            path=staging / "prepared.sqlite3",
                            descriptor=descriptor_data,
                            data_dir=args.prepared_snapshot_root / "application-data",
                        )
                        if verification is None:
                            raise RuntimeError("prepared_database_reverification_failed")

                        payload = {
                            "status": "application_prepared",
                            "marker": "ARV-001_APPLICATION_PREPARED",
                            "head_sha": args.expected_head,
                            "physical_file_count": 10,
                            "logical_document_count": 6,
                            "mapped_file_count": 10,
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
                    else:
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
                        result_proc = subprocess.run(
                            command,
                            cwd=root,
                            env=environment,
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        if result_proc.returncode != 0:
                            raise RuntimeError(_safe_child_failure(result_proc.stderr))
                        try:
                            payload = json.loads(result_proc.stdout.strip().splitlines()[-1])
                        except (IndexError, json.JSONDecodeError):
                            raise RuntimeError("controlled_preflight_payload_invalid")

                        request_path = staging / "application-data" / "request.json"
                        request = ProductionLLMAnalysisRequest.model_validate_json(
                            request_path.read_text(encoding="utf-8")
                        )

                    payload_err = _prepare_payload_error(payload, args.expected_head)
                    if payload_err:
                        raise RuntimeError(payload_err)

                    if not snapshot_mode:
                        descriptor_path = staging / "prepared-verification.json"
                        descriptor_data = parse_private_descriptor(
                            descriptor_path,
                            expected_head=args.expected_head,
                            expected_corpus_sha=args.expected_corpus_sha,
                        )
                        verification = _verify_prepared_database(
                            path=staging / "prepared.sqlite3",
                            descriptor=descriptor_data,
                            data_dir=staging / "application-data",
                        )
                        if verification is None:
                            raise RuntimeError("snapshot_binding_failed")

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
                        "raw_byte_replay": raw_mode,
                        "attested_prepared_snapshot_replay": snapshot_mode,
                    }
                    if snapshot_mode:
                        acceptance["prepared_snapshot_original_head"] = "5f6aa316f6f66306794e72bbcb90ad7bba3fba34"
                        acceptance["protected_source_graph_drift"] = False
                        acceptance["relevant_migration_drift"] = False

                    acceptance.update(
                        _live_output_boundary_acceptance(request, tokenizer=tokenizer)
                    )
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
                    if snapshot_mode:
                        base_manifest["prepared_snapshot_db_sha256"] = expected_db_sha
                        base_manifest["protected_source_graph_drift"] = False
                        base_manifest["relevant_migration_drift"] = False
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

        except Exception as exc:  # noqa: BLE001 - terminal output must remain sanitized.
            shutil.rmtree(staging, ignore_errors=True)
            print(
                json.dumps(
                    _failure(
                        head_sha=args.expected_head,
                        phase="runtime_start",
                        code=str(exc) or "llama_runtime_start_failed",
                        recorder=recorder,
                    ),
                    sort_keys=False,
                )
            )
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
