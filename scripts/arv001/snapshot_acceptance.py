"""Canonical ARV-001 snapshot-execution acceptance (zero-transport).

This module bridges an already attested prepared snapshot (PR #135 publication)
into the canonical complete-corpus execution contour without rebuilding
application state:

* it re-uses the exact ``prepared_snapshot_attestation`` and
  ``prepared_verification`` contracts (never a second, independent standard),
* it verifies ancestry and protected-path drift against the live repository,
* it copies the verified ``prepared.sqlite3`` byte-identically into an isolated
  private runtime outside the repository and never mutates the preserved
  snapshot,
* it reconstructs the canonical R10.1 provider input with ``build_r10_1_batch_plan``
  through the shared ``full_pre_provider`` helper, binds the final request body
  to the exact plan hash, and derives an exact live-output proof with the
  approved persistent tokenizer.

The default fork never starts transport and never spends authorization. The
``--execute-provider`` fork is a guarded code path that operators may exercise
only after a separately reviewed authorization grant; it delegates the whole
provider execution exactly once to the repository-owned controlled runner
against the isolated byte-identical copy and never starts transport from this
process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.arv001.full_pre_provider import (
    _check_protected_drift,
    _copy_snapshot,
    _reconstruct_actual_batch_requests,
)
from scripts.arv001.prepared_snapshot_attestation import (
    PreparedSnapshotAttestation,
    PreparedSnapshotAttestationError,
    verify_published_prepared_snapshot,
)
from scripts.arv001.prepared_verification import (
    PreparedVerificationError,
    PrivatePreparedVerificationDescriptor,
    verify_prepared_database_strict,
)
from src.modules.production_llm_analysis.batching import tokenizer_from_environment
from src.modules.production_llm_analysis.evidence import canonical_sha256
from src.modules.production_llm_analysis.live_output_boundary import (
    verify_exact_live_output_budget,
)
from src.modules.production_llm_analysis.transport_boundary import (
    boundary_root,
    load_authorization_state,
)

SNAPSHOT_ACCEPTANCE_SCHEMA_VERSION = "arv001-snapshot-acceptance-v1"
SNAPSHOT_DATABASE = "prepared.sqlite3"
SNAPSHOT_DESCRIPTOR = "prepared-verification.json"
SNAPSHOT_APPLICATION_DATA = "application-data"

_SAFE_CODE = re.compile(r"^[a-z0-9_.:-]{1,180}$")

_PREFLIGHT_EXPECTED_KEYS = {
    "status",
    "evidence_packet_hash",
    "batch_plan_hash",
    "ready_for_transport",
    "controlled_preflight_invocations",
    "controlled_provider_invocations",
    "provider_generation_calls",
}

_CONTROLLED_EXECUTION_EXPECTED_KEYS = {
    "status",
    "manifest_path",
    "manifest_hash",
    "request_id",
    "evidence_packet_hash",
    "provider",
    "model",
}

_CONTROLLED_MANIFEST_FILENAME = "controlled-evidence.manifest.json"

# ONE snapshot-acceptance execute-provider request maps to exactly ONE child
# process invocation of scripts.r10_1.run_controlled_provider_evidence. The
# child runner itself performs TWO internal executions (repeat_count == 2).
_CONTROLLED_RUNNER_INVOCATION_COUNT = 1
_CONTROLLED_EXPECTED_EXECUTION_COUNT = 2


class SnapshotAcceptanceError(RuntimeError):
    """Stable fail-closed failure for the snapshot-execution acceptance."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise SnapshotAcceptanceError(code)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise SnapshotAcceptanceError("snapshot_acceptance_unreadable") from exc
    return digest.hexdigest()


def _safe_failure(stderr: str) -> str:
    value = (
        stderr.strip().splitlines()[-1].strip().lower()
        if stderr.strip()
        else "controlled_runner_failed"
    )
    return value if _SAFE_CODE.fullmatch(value) else "controlled_runner_failed"


def _tree_hash(root: Path) -> str:
    """Re-derive the canonical application-data tree hash (same contract)."""
    from scripts.arv001.prepared_snapshot_attestation import _tree_hash as _canonical

    return _canonical(root)


def _copy_application_data(source: Path, target: Path) -> None:
    """Byte-copy the consumer snapshot tree into the isolated runtime."""
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        if child.is_dir():
            shutil.copytree(
                child,
                target / child.name,
                symlinks=False,
                dirs_exist_ok=True,
            )
        elif child.is_file() and not child.is_symlink():
            shutil.copy2(child, target / child.name, follow_symlinks=False)


def _isolated_staging_root(root: Path) -> Path:
    """Create a private staging directory for the byte-identical copy."""
    if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
        raise SnapshotAcceptanceError("isolated_runtime_path_unsafe")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    staging = root / f".snapshot-acceptance.partial.{os.getpid()}"
    staging.mkdir(mode=0o700)
    return staging


def _database_registry_number(database: Path, run_id: str) -> str:
    """Read the registry number bound to the exact run of the isolated copy."""
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
        row = connection.execute(
            "SELECT registry_number FROM tender_analysis_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        connection.close()
    except sqlite3.Error as exc:
        raise SnapshotAcceptanceError("snapshot_database_query_failed") from exc
    _require(row is not None, "snapshot_target_run_missing")
    assert row is not None
    value = str(row[0])
    _require(bool(value) and re.fullmatch(r"[A-Za-zА-Яа-я0-9./-]{1,120}", value), "snapshot_registry_number_invalid")
    return value


def _run_repository_preflight(
    *,
    repository_root: Path,
    run_id: str,
    registry_number: str,
    approved_policy: Path,
    isolated_database: Path,
    application_data: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Reach the repository-owned transport boundary in zero-transport mode.

    The canonical ``run_controlled_provider_evidence --preflight-only`` is
    invoked again the isolated byte-identical database copy. The runner builds
    deterministic evidence state and stops before any provider transport.
    """
    env = os.environ.copy()
    env["AI_CORP_DATABASE_URL"] = f"sqlite:///{isolated_database.as_posix()}"
    env["AI_CORP_ARVECTUM_DATA_DIR"] = str(application_data)
    env["AI_CORP_ARVECTUM_STORAGE_ROOT"] = str(output_root)
    env["AI_CORP_LLM_MAX_RETRIES"] = "0"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.r10_1.run_controlled_provider_evidence",
            "--preflight-only",
            "--run-id",
            run_id,
            "--expected-registry-number",
            registry_number,
            "--approved-policy",
            str(approved_policy),
            "--output-root",
            str(output_root),
        ],
        cwd=repository_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SnapshotAcceptanceError(
            "snapshot_controlled_preflight_failed:" + _safe_failure(result.stderr)
        )
    try:
        response = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise SnapshotAcceptanceError(
            "snapshot_controlled_preflight_output_invalid"
        ) from exc
    _require(
        isinstance(response, dict) and set(response) == _PREFLIGHT_EXPECTED_KEYS,
        "snapshot_controlled_preflight_schema_invalid",
    )
    _require(
        response.get("status") == "controlled_preflight_complete"
        and response.get("ready_for_transport") is True
        and response.get("controlled_preflight_invocations") == 1
        and response.get("controlled_provider_invocations") == 0
        and response.get("provider_generation_calls") == 0,
        "snapshot_controlled_preflight_invalid",
    )
    return dict(response)


def _durable_transport_facts(output_root: Path) -> dict[str, Any]:
    """Read repository-owned durable transport evidence for the controlled run.

    The durable boundary (a sibling of the controlled output root, never inside
    the disposable partial stage) is the only allowed source for transport and
    authorization facts. These are never inferred from a success status alone.
    """
    durable = load_authorization_state(boundary_root(output_root))
    descriptor = durable.get("failure_descriptor") or {}
    facts: dict[str, Any] = {
        "durable_transport_marker_present": bool(
            durable.get("transport_started") is True
        ),
        "transport_started": bool(durable.get("transport_started") is True),
        "authorization_consumed": bool(
            durable.get("authorization_consumed") is True
        ),
    }
    retry = descriptor.get("retry_count")
    if isinstance(retry, int):
        facts["retry_count"] = retry
    code = descriptor.get("sanitized_failure_code")
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
        facts["sanitized_failure_code"] = code
    return facts


def _read_controlled_manifest(response: dict[str, Any], resolved_root: Path) -> dict[str, Any]:
    """Read, locate-safely and verify the repository-produced controlled manifest.

    The manifest path is taken only from the child runner response and must live
    inside the resolved isolated output root. Symlinks and traversal are
    rejected. The manifest hash is independently recomputed with the canonical
    JSON contract and must match both the response manifest_hash and the
    manifest's own manifest_hash.
    """
    raw_path = response.get("manifest_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise SnapshotAcceptanceError("snapshot_controlled_manifest_invalid")
    manifest_path = Path(raw_path)
    if not manifest_path.is_absolute() or manifest_path.is_symlink():
        raise SnapshotAcceptanceError("snapshot_controlled_manifest_symlink")
    try:
        resolved_manifest = manifest_path.resolve()
    except OSError as exc:
        raise SnapshotAcceptanceError(
            "snapshot_controlled_manifest_unreadable"
        ) from exc
    if not resolved_manifest.is_relative_to(resolved_root):
        raise SnapshotAcceptanceError("snapshot_controlled_manifest_path_escape")
    for ancestor in manifest_path.parents:
        if ancestor == resolved_root or ancestor == resolved_root.parent:
            continue
        if ancestor.exists() and ancestor.is_symlink():
            raise SnapshotAcceptanceError("snapshot_controlled_manifest_symlink")
    if resolved_manifest.name != _CONTROLLED_MANIFEST_FILENAME:
        raise SnapshotAcceptanceError("snapshot_controlled_manifest_name_invalid")
    try:
        payload = json.loads(resolved_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotAcceptanceError(
            "snapshot_controlled_manifest_unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise SnapshotAcceptanceError("snapshot_controlled_manifest_invalid")
    manifest_hash = payload.get("manifest_hash")
    recomputed = canonical_sha256(
        {key: value for key, value in payload.items() if key != "manifest_hash"}
    )
    response_hash = response.get("manifest_hash")
    if not isinstance(manifest_hash, str) or manifest_hash != response_hash:
        raise SnapshotAcceptanceError("snapshot_controlled_manifest_hash_mismatch")
    if recomputed != manifest_hash:
        raise SnapshotAcceptanceError("snapshot_controlled_manifest_hash_mismatch")
    return payload


def _verify_controlled_stable_identity(manifest: dict[str, Any], response: dict[str, Any]) -> None:
    """Verify stable identity consistency between the durable manifest and stdout."""
    stable = manifest.get("stable_identity")
    if not isinstance(stable, dict):
        raise SnapshotAcceptanceError("snapshot_controlled_manifest_invalid")
    pairs = {
        "request_id": ("request_id", "request_id"),
        "evidence_packet_hash": ("evidence_packet_hash", "evidence_packet_hash"),
        "provider": ("provider", "provider"),
        "model": ("model", "model"),
    }
    for label, (manifest_key, response_key) in pairs.items():
        if stable.get(manifest_key) != response.get(response_key):
            raise SnapshotAcceptanceError(
                f"snapshot_controlled_identity_{label}_mismatch"
            )


def _run_repository_controlled_execution(
    *,
    repository_root: Path,
    run_id: str,
    registry_number: str,
    approved_policy: Path,
    isolated_database: Path,
    application_data: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Run the authorized provider execution through the repository runner.

    This is the only place the acceptance contour may reach the provider. The
    repository-owned ``run_controlled_provider_evidence`` is invoked exactly
    once against the isolated byte-identical copy with transport permanently
    retry-free. No provider call is ever started from this process; execution
    metrics are derived from the verified durable manifest and transport facts
    from the durable transport boundary. Nothing is inferred from success status
    alone and nothing is retried.
    """
    env = os.environ.copy()
    env["AI_CORP_DATABASE_URL"] = f"sqlite:///{isolated_database.as_posix()}"
    env["AI_CORP_ARVECTUM_DATA_DIR"] = str(application_data)
    env["AI_CORP_ARVECTUM_STORAGE_ROOT"] = str(output_root)
    env["AI_CORP_LLM_MAX_RETRIES"] = "0"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.r10_1.run_controlled_provider_evidence",
            "--run-id",
            run_id,
            "--expected-registry-number",
            registry_number,
            "--approved-policy",
            str(approved_policy),
            "--output-root",
            str(output_root),
        ],
        cwd=repository_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        facts = _durable_transport_facts(output_root)
        failure_code = (
            facts.get("sanitized_failure_code")
            or _safe_failure(result.stderr)
        )
        error = SnapshotAcceptanceError(
            "snapshot_controlled_execution_failed:" + failure_code
        )
        error.transport_started = facts["transport_started"]
        error.authorization_consumed = facts["authorization_consumed"]
        error.durable_transport_marker_present = facts[
            "durable_transport_marker_present"
        ]
        error.retry_count = facts.get("retry_count", 0)
        raise error
    try:
        response = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise SnapshotAcceptanceError(
            "snapshot_controlled_execution_output_invalid"
        ) from exc
    _require(
        isinstance(response, dict)
        and set(response) == _CONTROLLED_EXECUTION_EXPECTED_KEYS,
        "snapshot_controlled_execution_schema_invalid",
    )
    _require(
        response.get("status") == "controlled_evidence_complete",
        "snapshot_controlled_execution_invalid",
    )

    resolved_root = output_root.resolve()
    manifest = _read_controlled_manifest(response, resolved_root)
    _verify_controlled_stable_identity(manifest, response)

    repeat_count = manifest.get("repeat_count")
    _require(
        isinstance(repeat_count, int) and repeat_count == _CONTROLLED_EXPECTED_EXECUTION_COUNT,
        "snapshot_controlled_execution_repeat_count_invalid",
    )
    executions = manifest.get("executions")
    _require(
        isinstance(executions, list) and len(executions) == _CONTROLLED_EXPECTED_EXECUTION_COUNT,
        "snapshot_controlled_execution_invalid",
    )

    stable = manifest.get("stable_identity") or {}
    batch_count = stable.get("batch_count")
    if not isinstance(batch_count, int):
        counts = [
            entry.get("batch_count")
            for entry in executions
            if isinstance(entry, dict)
        ]
        batch_count = max(counts) if counts else None
    provider_calls = sum(
        int(entry["provider_call_count"])
        for entry in executions
        if isinstance(entry, dict)
        and isinstance(entry.get("provider_call_count"), int)
    )
    retry_count = sum(
        int(entry["retry_count"])
        for entry in executions
        if isinstance(entry, dict)
        and isinstance(entry.get("retry_count"), int)
    )

    facts = _durable_transport_facts(output_root)
    return {
        "authorized_execution_path_implemented": True,
        "controlled_runner_used": True,
        "exact_target_run_reused": True,
        "isolated_db_used_by_provider_path": True,
        "status": response["status"],
        "controlled_output_identity": response["request_id"],
        "controlled_output_manifest_path": response["manifest_path"],
        "controlled_output_manifest_hash": response["manifest_hash"],
        "controlled_evidence_packet_hash": response["evidence_packet_hash"],
        "controlled_provider": response["provider"],
        "controlled_model": response["model"],
        "controlled_provider_invocations": _CONTROLLED_RUNNER_INVOCATION_COUNT,
        "execution_count": repeat_count,
        "batch_count_per_execution": batch_count,
        "provider_generation_calls": provider_calls,
        "retry_count": retry_count,
        "durable_transport_marker_present": facts[
            "durable_transport_marker_present"
        ],
        "transport_started": facts["transport_started"],
        "authorization_consumed": facts["authorization_consumed"],
        "ready_for_transport": False,
    }


def run_snapshot_acceptance(
    *,
    prepared_snapshot_root: Path,
    repository_root: Path,
    current_head: str,
    prepared_snapshot_original_head: str,
    expected_corpus_sha: str,
    approved_policy: Path,
    isolated_runtime_root: Path,
    tokenizer: Any = None,
    execute_provider: bool = False,
    authorization_granted: bool = False,
) -> dict[str, Any]:
    """Execute the preserved snapshot through the read-only acceptance gate.

    Returns a JSON-serializable acceptance payload and fails closed on any
    drift, mismatch or provider presence. Never starts transport on its own.
    """
    report: dict[str, Any] = {
        "schema_version": SNAPSHOT_ACCEPTANCE_SCHEMA_VERSION,
        "prepared_snapshot_execution_mode": True,
        "raw_byte_replay": False,
        "attested_prepared_snapshot_replay": True,
        "current_head": current_head,
        "execute_provider_requested": execute_provider,
    }

    if not repository_root.is_dir() or repository_root.is_symlink():
        raise SnapshotAcceptanceError("repository_root_invalid")
    if not prepared_snapshot_root.is_dir() or prepared_snapshot_root.is_symlink():
        raise SnapshotAcceptanceError("prepared_snapshot_root_invalid")
    if (
        not approved_policy.is_file()
        or approved_policy.is_symlink()
        or not _SAFE_CODE.fullmatch(approved_policy.name or "")
    ):
        raise SnapshotAcceptanceError("approved_policy_missing")
    if not (isinstance(prepared_snapshot_original_head, str) and len(prepared_snapshot_original_head) == 40):
        raise SnapshotAcceptanceError("prepared_snapshot_original_head_invalid")
    if not (isinstance(current_head, str) and len(current_head) == 40):
        raise SnapshotAcceptanceError("current_head_invalid")

    # A. Exact attestation of the published prepared snapshot (PR #135).
    try:
        attestation: PreparedSnapshotAttestation = verify_published_prepared_snapshot(
            prepared_snapshot_root,
            expected_head=prepared_snapshot_original_head,
            expected_corpus_sha=expected_corpus_sha,
        )
    except PreparedSnapshotAttestationError as exc:
        raise SnapshotAcceptanceError(exc.code) from exc
    descriptor: PrivatePreparedVerificationDescriptor = attestation.descriptor
    report.update(
        {
            "original_published_snapshot_verified": True,
            "original_manifest_hashes_verified": True,
            "snapshot_attestation_verified": True,
            "original_head": descriptor.head_sha,
            "original_prepared_db_sha256": attestation.database_sha256,
            "original_manifest_sha256": attestation.manifest_sha256,
            "original_descriptor_sha256": attestation.descriptor_sha256,
            "original_runtime_profile_sha256": attestation.runtime_profile_sha256,
            "original_application_data_tree_sha256": (
                attestation.application_data_tree_sha256
            ),
            "original_sanitized_result_sha256": attestation.sanitized_result_sha256,
        }
    )

    # B. Ancestry + protected drift against the live repository head.
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", descriptor.head_sha, current_head],
        cwd=repository_root,
        check=False,
    )
    _require(ancestry.returncode == 0, "snapshot_head_not_ancestor")
    drift, migration_drift = _check_protected_drift(
        repository_root, descriptor.head_sha, current_head
    )
    _require(not drift, "prepared_snapshot_not_carry_forward_safe")
    report.update(
        {
            "snapshot_ancestry_verified": True,
            "protected_path_drift": False,
            "migration_drift": migration_drift,
            "corpus_sha256": descriptor.corpus_sha256,
        }
    )

    # C. Exact-run re-verification using only the preserved descriptor.
    try:
        verification = verify_prepared_database_strict(
            path=prepared_snapshot_root / SNAPSHOT_DATABASE,
            data_dir=prepared_snapshot_root / SNAPSHOT_APPLICATION_DATA,
            descriptor=descriptor,
        )
    except PreparedVerificationError as exc:
        raise SnapshotAcceptanceError(exc.code) from exc
    _require(verification.target_run_verified, "prepared_database_reverification_failed")
    _require(verification.provider_results_absent, "prepared_provider_state_present")
    report.update(
        {
            "target_run_id": descriptor.target_run_id,
            "new_run_created": False,
            "prepare_documents_called": False,
            "create_application_data_called": False,
            "target_run_reused": True,
            "physical_documents": descriptor.physical_document_count,
            "logical_documents": descriptor.logical_document_count,
            "extracted_documents": descriptor.extracted_document_count,
            "chunks": descriptor.chunk_count,
            "provider_results_absent": verification.provider_results_absent,
            "generation_artifacts_absent": verification.generation_artifacts_absent,
        }
    )

    # D. Byte-identical isolated private copy (outside the repository).
    staging = _isolated_staging_root(isolated_runtime_root)
    target_db = staging / SNAPSHOT_DATABASE
    _require(
        _copy_snapshot(prepared_snapshot_root, staging, attestation.database_sha256),
        "snapshot_database_copy_failed",
    )
    copied_db_sha = _sha256_file(target_db)
    _require(copied_db_sha == attestation.database_sha256, "snapshot_database_copy_sha_mismatch")
    shutil.copy2(
        prepared_snapshot_root / SNAPSHOT_DESCRIPTOR,
        staging / SNAPSHOT_DESCRIPTOR,
        follow_symlinks=False,
    )
    target_app = staging / SNAPSHOT_APPLICATION_DATA
    _copy_application_data(
        prepared_snapshot_root / SNAPSHOT_APPLICATION_DATA, target_app
    )
    _require(
        _tree_hash(target_app) == attestation.application_data_tree_sha256,
        "snapshot_application_data_copy_mismatch",
    )
    try:
        copied_verification = verify_prepared_database_strict(
            path=target_db,
            data_dir=target_app,
            descriptor=descriptor,
        )
    except PreparedVerificationError as exc:
        raise SnapshotAcceptanceError(exc.code) from exc
    _require(copied_verification.target_run_verified, "isolated_db_verification_failed")
    report.update(
        {
            "byte_identical_db_copy_verified": True,
            "database_sha256_before": attestation.database_sha256,
            "database_sha256_after": copied_db_sha,
            "isolated_application_data_tree_sha256": _tree_hash(target_app),
        }
    )

    # E. Canonical provider-input reconstruction on the isolated copy.
    tokenizer = tokenizer or tokenizer_from_environment()
    if not bool(getattr(tokenizer, "persistent", False)):
        raise SnapshotAcceptanceError("exact_persistent_tokenizer_missing")
    reconstruction = _reconstruct_actual_batch_requests(
        target_db,
        approved_policy,
        tokenizer=tokenizer,
        descriptor=descriptor,
    )
    plan = reconstruction.plan
    _require(plan is not None, "canonical_plan_missing")
    _require(
        reconstruction.target_run_binding_verified
        and reconstruction.canonical_evidence_projection_match,
        "canonical_provider_input_reconstruction_failed",
    )
    requests = list(reconstruction.requests)
    report.update(
        {
            "canonical_planner_used": True,
            "canonical_planner_function": "build_r10_1_batch_plan",
            "batch_plan_version": str(plan.plan_version),
            "batch_plan_hash": str(plan.plan_hash),
            "actual_batch_count": len(requests),
            "evidence_packet_hash": reconstruction.evidence_packet_hash,
            "target_run_binding_verified": True,
            "canonical_evidence_projection_match": True,
        }
    )

    # F. Final request-body identity bound to the exact plan hash.
    for request in requests:
        _require(
            str(getattr(request, "batch_plan_hash", "")) == str(plan.plan_hash),
            "snapshot_request_body_plan_mismatch",
        )
    report["final_request_body_identity_verified"] = True
    report["request_body_batch_plan_hash"] = str(plan.plan_hash)

    # G. Exact live-output proof using the approved persistent tokenizer.
    proofs = [
        verify_exact_live_output_budget(request, tokenizer=tokenizer)
        for request in requests
    ]
    worst = max(proofs, key=lambda proof: int(proof["exact_live_output_tokens"]))
    report.update(
        {
            "exact_live_output_budget_proof": "PASS",
            "exact_live_output_token_upper_bound": int(
                worst["exact_live_output_tokens"]
            ),
            "output_safety_margin_tokens": int(worst["safety_margin_tokens"]),
            "tokenizer_identity": str(worst["tokenizer_identity"]),
            "exact_live_output_tokenizer_available": True,
        }
    )

    # H. Zero-transport enforcement / authorized streaming boundary.
    if execute_provider:
        _require(authorization_granted, "snapshot_authorization_not_granted")
        report.update(
            _run_repository_controlled_execution(
                repository_root=repository_root,
                run_id=descriptor.target_run_id,
                registry_number=_database_registry_number(
                    target_db, descriptor.target_run_id
                ),
                approved_policy=approved_policy,
                isolated_database=target_db,
                application_data=target_app,
                output_root=staging / "controlled-runtime-output",
            )
        )
        return report
    report["authorization_consumed"] = False
    report["transport_started"] = False
    report["controlled_provider_invocations"] = 0
    report["provider_generation_calls"] = 0
    report["ready_for_transport"] = True

    return report


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ARV-001 snapshot-execution acceptance (zero-transport)."
    )
    parser.add_argument("--prepared-snapshot-root", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--current-head", required=True)
    parser.add_argument("--prepared-snapshot-original-head", required=True)
    parser.add_argument("--expected-corpus-sha", required=True)
    parser.add_argument("--approved-policy", type=Path, required=True)
    parser.add_argument("--isolated-runtime-root", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        report = run_snapshot_acceptance(
            prepared_snapshot_root=args.prepared_snapshot_root,
            repository_root=args.repository_root,
            current_head=args.current_head,
            prepared_snapshot_original_head=args.prepared_snapshot_original_head,
            expected_corpus_sha=args.expected_corpus_sha,
            approved_policy=args.approved_policy,
            isolated_runtime_root=args.isolated_runtime_root,
        )
    except SnapshotAcceptanceError as exc:
        print(f"snapshot_acceptance:{exc.code}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())