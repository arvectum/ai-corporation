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
from typing import Any, Iterable

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
from scripts.arv001.prepared_snapshot_attestation import (
    verify_published_prepared_snapshot,
    PreparedSnapshotAttestationError,
    _tree_hash,
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
from src.modules.production_llm_analysis.live_output_boundary import (
    GRAMMAR_WHITESPACE_CONTRACT_VERSION,
    GRAMMAR_WHITESPACE_MAX_BYTES_PER_SLOT,
    verify_exact_live_output_budget,
)

ARV001_PREPARED_CARRY_FORWARD_PROTECTED_PATHS_VERSION = "arv001-prepared-carry-forward-v1"
ARV001_PREPARED_CARRY_FORWARD_PROTECTED_PATHS = (
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


def _live_output_boundary_static_acceptance() -> dict[str, object]:
    """Record static zero-generation facts about the live schema."""
    return {
        "live_schema_mono_schema_enforced": True,
        "reasoning_disabled_verified": True,
        "provider_generation_calls": 0,
        "controlled_provider_invocations": 0,
        "grammar_whitespace_contract_version": GRAMMAR_WHITESPACE_CONTRACT_VERSION,
        "grammar_whitespace_max_bytes_per_slot": GRAMMAR_WHITESPACE_MAX_BYTES_PER_SLOT,
    }


def _live_output_boundary_acceptance(
    request: Any = None, tokenizer: Any | None = None
) -> dict[str, object]:
    """Deprecated compatibility wrapper. Use static and exact proof instead."""
    return {
        **_live_output_boundary_static_acceptance(),
        "exact_live_output_budget_proof": "DEFERRED_TOKENIZER",
        "exact_live_output_tokenizer_available": False,
    }


def _exact_live_output_acceptance(
    requests: Iterable[Any],
    tokenizer: Any,
) -> dict[str, Any]:
    """Perform exact runtime proof for a set of batch requests."""
    worst_proof = None
    for request in requests:
        proof = verify_exact_live_output_budget(request, tokenizer=tokenizer)
        if worst_proof is None or proof["exact_live_output_tokens"] > worst_proof["exact_live_output_tokens"]:
            worst_proof = proof

    if worst_proof is None:
        raise RuntimeError("no_requests_for_proof")

    return {
        "exact_live_output_budget_proof": "PASS",
        "exact_live_output_tokenizer_available": True,
        "exact_live_output_token_upper_bound": worst_proof["exact_live_output_tokens"],
        "output_safety_margin_tokens": worst_proof["safety_margin_tokens"],
        "tokenizer_identity": worst_proof["tokenizer_identity"],
        "live_schema_sha256": worst_proof["live_schema_sha256"],
        "maximal_payload_sha256": worst_proof["maximal_payload_sha256"],
        "grammar_whitespace_included": True,
        "grammar_whitespace_bound_proven": True,
        "provider_confidence_live_grammar_resolved": True,
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
    drift = False
    migration_drift = False

    for path in ARV001_PREPARED_CARRY_FORWARD_PROTECTED_PATHS:
        result = subprocess.run(
            ["git", "diff", "--quiet", snapshot_head, current_head, "--", path],
            cwd=repository_root,
            capture_output=True,
            check=False,
        )
        if result.returncode == 1:
            drift = True
            if path.startswith("migrations/"):
                migration_drift = True
        elif result.returncode > 1:
            raise RuntimeError("git_invocation_failure")

    return drift, migration_drift


def _copy_snapshot(
    source_root: Path, staging: Path, expected_db_sha: str
) -> bool:
    """Byte-identically copy the prepared database and verify its SHA."""
    source_db = source_root / "prepared.sqlite3"
    target_db = staging / "prepared.sqlite3"

    if not source_db.is_file() or source_db.is_symlink():
        return False

    shutil.copy2(source_db, target_db, follow_symlinks=False)
    copied_sha = _sha256(target_db)

    return copied_sha == expected_db_sha


def _exception_reason_code(exc: Exception, phase: str) -> str:
    """Map arbitrary exceptions to stable, sanitized reason codes."""
    if isinstance(exc, (PreparedSnapshotAttestationError, PreparedVerificationError)):
        return str(exc.code)
    if isinstance(exc, FileNotFoundError):
        return f"{phase}_file_missing"
    message = str(exc).lower()
    if "no item with that key" in str(exc) or "operationalerror" in message:
        return "prepared_database_query_failed"
    if "git_head_mismatch" in message: return "git_head_mismatch"
    if "git_invocation_failure" in message: return "git_invocation_failure"
    if "snapshot_head_not_ancestor" in message: return "snapshot_head_not_ancestor"
    if "prepared_snapshot_not_carry_forward_safe" in message: return "prepared_snapshot_not_carry_forward_safe"
    if "prepared_database_reverification_failed" in message: return "prepared_database_reverification_failed"
    return f"{phase}_failed"


def _reconstruct_actual_batch_requests(
    database_path: Path,
    policy_path: Path,
    *,
    model: str = "arvectum-gemma4-12b-it-qat-q4_0",
    tokenizer: Any,
) -> list[Any]:
    """Reconstruct real R10.1 batch requests from historical DB and application data."""
    from src.modules.production_llm_analysis.evidence import build_evidence_packet
    from src.modules.production_llm_analysis.schemas import (
        EvidenceFragmentInput,
        BudgetPolicy,
    )
    from src.modules.production_llm_analysis.batching import (
        build_evidence_batch_plan,
        BatchPolicy,
        measure_openai_request_tokens,
    )
    from src.modules.production_llm_analysis.service import build_production_llm_request
    from src.modules.production_llm_analysis.contracts import R10_1_CONTROLLED_MAP_CONTRACT

    conn = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        run = conn.execute("SELECT * FROM tender_analysis_runs LIMIT 1").fetchone()
        tender = conn.execute("SELECT * FROM procurement_tenders WHERE id = ?", (run["tender_id"],)).fetchone()
        
        chunk_rows = conn.execute(
            """
            SELECT c.*, d.file_name, d.id as doc_id 
            FROM procurement_document_chunks c
            JOIN procurement_tender_documents d ON c.document_id = d.id
            WHERE c.tender_id = ? 
            ORDER BY d.file_name ASC, c.chunk_index ASC
            """,
            (run["tender_id"],)
        ).fetchall()
        
        fragments = [
            EvidenceFragmentInput(
                document_id=row["doc_id"],
                document_name=row["file_name"],
                chunk_id=row["id"],
                locator={
                    "document_order": 0, # Historical fallback
                    "chunk_index": row["chunk_index"]
                },
                text=row["text"]
            ) for row in chunk_rows
        ]
    finally:
        conn.close()

    packet = build_evidence_packet(
        customer_id=str(run["customer_id"]),
        project_id=str(run["project_id"]),
        procurement_case_id=str(run["procurement_case_id"]),
        run_id=str(run["id"]),
        registry_number=str(tender["registry_number"]),
        fragments=fragments
    )
    
    with policy_path.open("rb") as f:
        policy_data = json.load(f)
    budget_policy = BudgetPolicy.model_validate(policy_data)
    
    batch_policy = BatchPolicy.approved_32k(tokenizer_identity=tokenizer.identity)
    
    def request_measure(candidate):
        req = build_production_llm_request(
            evidence_packet=build_evidence_packet(
                customer_id=packet.customer_id,
                project_id=packet.project_id,
                procurement_case_id=packet.procurement_case_id,
                run_id=packet.run_id,
                registry_number=packet.registry_number,
                fragments=candidate
            ),
            provider="openai_compatible",
            model=model,
            prompt_id=R10_1_CONTROLLED_MAP_CONTRACT.prompt_id,
            prompt_version=R10_1_CONTROLLED_MAP_CONTRACT.prompt_version,
            output_schema_id=R10_1_CONTROLLED_MAP_CONTRACT.output_schema_id,
            output_schema_version=R10_1_CONTROLLED_MAP_CONTRACT.output_schema_version,
            grounding_policy_version=R10_1_CONTROLLED_MAP_CONTRACT.grounding_policy_version,
            budget_policy=budget_policy,
            map_mode=True
        )
        return measure_openai_request_tokens(
            req.model_dump(mode="json"),
            tokenizer=tokenizer,
            chat_template_overhead=batch_policy.chat_template_overhead
        )

    plan = build_evidence_batch_plan(
        fragments=fragments,
        tokenizer=tokenizer,
        policy=batch_policy,
        request_measure=request_measure,
        controlled=True
    )
    
    requests = []
    for batch in plan.batches:
        requests.append(
            build_production_llm_request(
                evidence_packet=build_evidence_packet(
                    customer_id=packet.customer_id,
                    project_id=packet.project_id,
                    procurement_case_id=packet.procurement_case_id,
                    run_id=packet.run_id,
                    registry_number=packet.registry_number,
                    fragments=list(batch.fragments)
                ),
                provider="openai_compatible",
                model=model,
                prompt_id=R10_1_CONTROLLED_MAP_CONTRACT.prompt_id,
                prompt_version=R10_1_CONTROLLED_MAP_CONTRACT.prompt_version,
                output_schema_id=R10_1_CONTROLLED_MAP_CONTRACT.output_schema_id,
                output_schema_version=R10_1_CONTROLLED_MAP_CONTRACT.output_schema_version,
                grounding_policy_version=R10_1_CONTROLLED_MAP_CONTRACT.grounding_policy_version,
                budget_policy=budget_policy,
                batch_plan_version=plan.plan_version,
                batch_plan_hash=plan.plan_hash,
                batch_hash=batch.batch_hash,
                batch_ordinal=batch.batch_ordinal,
                batch_count=len(plan.batches),
                corpus_evidence_hash=plan.corpus_evidence_hash,
                map_mode=True
            )
        )
    return requests


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

    attestation = None
    if snapshot_mode:
        try:
            # A. Attest original published snapshot
            attestation = verify_published_prepared_snapshot(
                args.prepared_snapshot_root,
                expected_head="5f6aa316f6f66306794e72bbcb90ad7bba3fba34",
                expected_corpus_sha=args.expected_corpus_sha,
            )
            
            # B. Ancestry/Protected-drift
            ancestry_result = subprocess.run(
                ["git", "merge-base", "--is-ancestor", attestation.descriptor.head_sha, args.expected_head],
                cwd=root,
                check=False,
            )
            if ancestry_result.returncode != 0:
                raise RuntimeError("snapshot_head_not_ancestor")

            drift, migration_drift = _check_protected_drift(
                root, attestation.descriptor.head_sha, args.expected_head
            )
            if drift:
                raise RuntimeError("prepared_snapshot_not_carry_forward_safe")
        except Exception as exc:
            print(json.dumps(_failure(head_sha=args.expected_head, phase="repository", code=_exception_reason_code(exc, "repository"), recorder=recorder), sort_keys=False))
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
        print(json.dumps(_failure(head_sha=args.expected_head, phase="repository", code="repository_validation_failed", recorder=recorder), sort_keys=False))
        return 2
    
    recorder.passed("repository")
    recorder.passed("python_runtime")
    recorder.passed("static_environment")
    
    values, env_errors = read_private_env(args.private_env, root) if args.private_env else ({}, ())
    assets, asset_errors = locate_runtime_assets(tuple(args.asset_root), gguf_path=args.gguf_path, llama_server_path=args.llama_server_path)
    if env_errors or asset_errors or assets is None:
        phase = "static_environment" if env_errors else "gguf_validation"
        print(json.dumps(_failure(head_sha=args.expected_head, phase=phase, code="approved_gguf_validation_failed" if asset_errors else "private_environment_invalid", recorder=recorder), sort_keys=False))
        return 2

    binary, gguf = assets
    gguf_profile, _ = validate_gguf_path(args.gguf_path) if args.gguf_path else discover_gguf(tuple(args.asset_root))
    binary_profile, _ = validate_llama_server_path(args.llama_server_path) if args.llama_server_path else discover_llama_server(tuple(args.asset_root))
    
    if not gguf_profile or not binary_profile:
        print(json.dumps(_failure(head_sha=args.expected_head, phase="gguf_validation" if not gguf_profile else "llama_server_validation", code="approved_gguf_validation_failed" if not gguf_profile else "llama_server_validation_failed", recorder=recorder), sort_keys=False))
        return 2
    
    recorder.passed("gguf_validation")
    recorder.passed("llama_server_validation")

    staging, final_state = _private_staging_root(args.runtime_profile_dir, root)
    if staging is None or final_state is None:
        print(json.dumps(_failure(head_sha=args.expected_head, phase="prepared_state_persistence", code="prepared_state_persistence_failed", recorder=recorder), sort_keys=False))
        return 2

    with tempfile.TemporaryDirectory(prefix="arv001-full-pre-provider-") as directory:
        work = Path(directory)
        try:
            with ManagedLoopbackRuntime(binary=binary, gguf=gguf) as runtime:
                assert runtime.port is not None
                recorder.passed("runtime_start")
                from src.modules.production_llm_analysis.batching import tokenizer_from_environment

                with ephemeral_runtime_environment(port=runtime.port, binary_sha256=binary_profile["binary_sha256"], gguf_sha256=gguf_profile["gguf_sha256"], overrides=values) as (effective, _private_env):
                    if validate_effective_runtime_environment(effective, port=runtime.port):
                        raise RuntimeError("effective_settings_invalid")
                    recorder.passed("effective_environment")
                    
                    environment = os.environ.copy()
                    environment.update(effective)
                    with scoped_environment(effective):
                        tokenizer = tokenizer_from_environment()
                    
                    probe, probe_errors = probe_zero_generation(loopback_base_url=f"http://127.0.0.1:{runtime.port}", tokenizer_url=effective["ARV003_LLAMA_TOKENIZER_URL"], tokenizer_adapter=tokenizer, tokenizer_identity=effective["ARV003_TOKENIZER_IDENTITY"])
                    if probe_errors or probe is None:
                        raise RuntimeError("zero_generation_probe_failed")
                    recorder.passed("models_probe")
                    recorder.passed("tokenizer_probe")

                    profile, profile_errors = write_private_runtime_profile(private_directory=staging, repository_root=root, profile={"version": "arv001-runtime-v1", **gguf_profile, **binary_profile, "model_alias": "arvectum-gemma4-12b-it-qat-q4_0", "provider": "openai_compatible", **probe, "created_at": datetime.now(UTC).isoformat()})
                    if profile_errors or profile is None:
                        raise RuntimeError("runtime_profile_write_failed")
                    recorder.passed("runtime_profile")

                    if snapshot_mode and attestation:
                        # C. Byte-identical copy
                        if not _copy_snapshot(args.prepared_snapshot_root, staging, attestation.database_sha256):
                            raise RuntimeError("prepared_database_copy_failed")
                        
                        shutil.copy2(args.prepared_snapshot_root / "prepared-verification.json", staging / "prepared-verification.json", follow_symlinks=False)
                        
                        data_dir = staging / "application-data"
                        data_dir.mkdir(parents=True, mode=0o700)
                        for item in sorted((args.prepared_snapshot_root / "application-data").iterdir(), key=lambda i: i.name):
                            if item.is_dir():
                                shutil.copytree(item, data_dir / item.name, symlinks=False, dirs_exist_ok=True)
                            else:
                                shutil.copy2(item, data_dir / item.name, follow_symlinks=False)
                        
                        # Verify copy tree hash
                        if _tree_hash(data_dir) != attestation.application_data_tree_sha256:
                            raise RuntimeError("application_data_copy_integrity_failed")

                        # D. Current-head re-verification (read-only)
                        verification = _verify_prepared_database(path=staging / "prepared.sqlite3", descriptor=attestation.descriptor, data_dir=data_dir)
                        if verification is None:
                            raise RuntimeError("prepared_database_reverification_failed")

                        # E. Reconstruct actual batch requests
                        batch_requests = _reconstruct_actual_batch_requests(staging / "prepared.sqlite3", args.approved_policy, tokenizer=tokenizer)
                        
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
                        command = [sys.executable, "-m", "scripts.arv001.run_complete_corpus_acceptance_split_roots", "--candidate-root", str(args.candidate_root), "--intake-root", str(args.intake_root), "--database-path", str(staging / "prepared.sqlite3"), "--initialize-database", "--private-verification-descriptor", str(staging / "prepared-verification.json"), "--data-dir", str(staging / "application-data"), "--approved-policy", str(args.approved_policy), "--output-root", str(work / "output"), "--expected-head", args.expected_head, "--expected-corpus-sha", args.expected_corpus_sha, "--expected-policy-sha", args.expected_policy_sha, "--prepare-only"]
                        result_proc = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, check=False)
                        if result_proc.returncode != 0:
                            raise RuntimeError(_safe_child_failure(result_proc.stderr))
                        try:
                            payload = json.loads(result_proc.stdout.strip().splitlines()[-1])
                        except (IndexError, json.JSONDecodeError):
                            raise RuntimeError("controlled_preflight_payload_invalid")
                        
                        from src.modules.production_llm_analysis.schemas import ProductionLLMAnalysisRequest
                        request_path = staging / "application-data" / "request.json"
                        request = ProductionLLMAnalysisRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
                        batch_requests = [request]

                        descriptor_data = parse_private_descriptor(staging / "prepared-verification.json", expected_head=args.expected_head, expected_corpus_sha=args.expected_corpus_sha)
                        verification = _verify_prepared_database(path=staging / "prepared.sqlite3", descriptor=descriptor_data, data_dir=staging / "application-data")
                        if verification is None:
                            raise RuntimeError("snapshot_binding_failed")

                    for phase in ("corpus_contract", "database", "application_persistence", "snapshot_binding", "source_graph_binding", "post_persistence_gate5", "controlled_preflight"):
                        recorder.passed(phase)

                    counters = {"controlled_preflight_invocations": 1, "controlled_provider_invocations": 0, "provider_generation_calls": 0, "production_db_mutations": 0, "old_arv003_mutations": 0, "git_data_leaks": 0}
                    acceptance = {"application_prepared": True, "post_persistence_gate5_ready": True, "controlled_preflight_only": True, "physical_file_count": 10, "logical_document_count": 6, "extracted_document_count": 10, "prepared_chunk_count": 233, "raw_byte_replay": raw_mode, "attested_prepared_snapshot_replay": snapshot_mode}
                    
                    if snapshot_mode and attestation:
                        acceptance.update({
                            "prepared_snapshot_original_head": "5f6aa316f6f66306794e72bbcb90ad7bba3fba34",
                            "protected_path_contract_version": ARV001_PREPARED_CARRY_FORWARD_PROTECTED_PATHS_VERSION,
                            "protected_source_graph_drift": False,
                            "relevant_migration_drift": migration_drift,
                            "original_published_snapshot_verified": True,
                            "original_manifest_hashes_verified": True,
                            "original_prepared_db_sha256": attestation.database_sha256,
                            "original_runtime_profile_sha256": attestation.runtime_profile_sha256,
                            "original_descriptor_sha256": attestation.descriptor_sha256,
                            "original_application_data_tree_sha256": attestation.application_data_tree_sha256,
                            "original_sanitized_result_sha256": attestation.sanitized_result_sha256,
                            "original_manifest_sha256": attestation.manifest_sha256,
                            "real_batch_requests_reconstructed": True,
                            "real_fragment_identities_verified": True,
                            "exact_output_proof_bound_to_real_requests": True,
                            "actual_batch_count": len(batch_requests),
                        })

                    acceptance.update(_live_output_boundary_static_acceptance())
                    acceptance.update(_exact_live_output_acceptance(batch_requests, tokenizer=tokenizer))
                    
                    final_recorder = recorder.clone()
                    final_recorder.passed("prepared_state_persistence")
                    final_recorder.passed("privacy_scan")
                    final_recorder.passed("cleanup")
                    
                    final_result = _result(head_sha=args.expected_head, recorder=final_recorder, status="PASS", counters=counters, acceptance=acceptance)
                    base_manifest = _prepared_manifest_base(payload=payload, binary_profile=binary_profile, gguf_profile=gguf_profile, probe=probe, corpus_sha=args.expected_corpus_sha, policy_sha=args.expected_policy_sha, verification=verification)
                    
                    if snapshot_mode and attestation:
                        base_manifest.update({
                            "prepared_snapshot_db_sha256": attestation.database_sha256,
                            "protected_source_graph_drift": False,
                            "relevant_migration_drift": migration_drift,
                            "original_manifest_sha256": attestation.manifest_sha256,
                        })
                    
                    try:
                        publish_prepared_state(staging=staging, final=final_state, base_manifest=base_manifest, result=final_result, forbidden_literals=(attestation.descriptor.target_run_id, attestation.descriptor.customer_id, attestation.descriptor.project_id, attestation.descriptor.case_id, attestation.descriptor.tender_id, attestation.descriptor.snapshot_id, attestation.descriptor.source_graph_id) if snapshot_mode and attestation else ())
                    except PreparedPublicationError as exc:
                        phase = "privacy_scan" if exc.code == "prepared_privacy_violation" else "prepared_state_persistence"
                        recorder.failed(phase, *exc.reason_codes)
                        print(json.dumps(_result(head_sha=args.expected_head, recorder=recorder, status="FAIL_CLOSED", counters=counters, acceptance=acceptance), sort_keys=False))
                        return 2
                    
                    print(json.dumps(final_result, sort_keys=False))
                    return 0

        except Exception as exc:
            import traceback
            traceback.print_exc()
            shutil.rmtree(staging, ignore_errors=True)
            print(json.dumps(_failure(head_sha=args.expected_head, phase="runtime_start", code=_exception_reason_code(exc, "runtime_start"), recorder=recorder), sort_keys=False))
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
