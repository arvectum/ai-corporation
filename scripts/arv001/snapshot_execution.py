"""Read-only re-execution of an ARV-001 prepared snapshot.

The prepared snapshot (``prepared.sqlite3`` + ``application-data`` +
``prepared-verification.json``) is re-executed in a strictly read-only mode:
the target run is loaded from the exact database snapshot, all five execution
gates are evaluated against persisted state, and the snapshot hash is re-derived
from the consumer snapshot files. No provider is invoked and nothing is written.

The execution result is bound to the exact snapshot hash recorded in the private
descriptor. Any deviation from the zero-generation prepared state fails closed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from scripts.arv001.prepared_verification import (
    PreparedVerificationError,
    PrivatePreparedVerificationDescriptor,
    parse_private_descriptor,
    verify_prepared_database_strict,
)

_SNAPSHOT_HASH_ALGORITHM = "sha256-json-c14n-v1"

_GATE_DATABASE_SNAPSHOT = "database_snapshot"
_GATE_ARTIFACT_SNAPSHOT = "artifact_snapshot"
_GATE_GENERATION_RESULTS = "generation_results"
_GATE_PREPARED_STATE = "prepared_state"
_GATE_RUN_GENERATION = "run_generation"

_EXPECTED_GATE_ORDER = (
    _GATE_DATABASE_SNAPSHOT,
    _GATE_ARTIFACT_SNAPSHOT,
    _GATE_GENERATION_RESULTS,
    _GATE_PREPARED_STATE,
    _GATE_RUN_GENERATION,
)

# Gate outcomes: the execution snapshot itself supplies the database and artifact
# state; the prepared (zero-generation) phase makes generation gates not-needed.
_GATE_STATUS_SNAPSHOT = "snapshot"
_GATE_STATUS_NOT_NEEDED = "not-needed"
_GATE_STATUS_BLOCKED = "blocked"

# Read-only prepared-state files are expected to keep the restricted modes that
# the attestation layer verifies for a published snapshot.
_SNAPSHOT_FILE_MODE = 0o600
_SNAPSHOT_DIR_MODE = 0o700


class SnapshotExecutionError(RuntimeError):
    """Stable fail-closed failure for snapshot re-execution."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise SnapshotExecutionError(code)


@dataclass(frozen=True)
class GateOutcome:
    gate: str
    status: str
    detail: str


@dataclass(frozen=True)
class SnapshotExecutionResult:
    snapshot_id: str
    target_run_id: str
    snapshot_hash: str
    recomputed_snapshot_hash: str
    snapshot_hash_bound: bool
    database_sha256: str
    verified: bool
    provider_invocations: int
    write_count: int
    provider_results_absent: bool
    generation_artifacts_absent: bool
    gates: tuple[GateOutcome, ...]

    def gate_status(self, gate: str) -> str | None:
        for outcome in self.gates:
            if outcome.gate == gate:
                return outcome.status
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise SnapshotExecutionError("snapshot_file_unreadable") from exc
    return digest.hexdigest()


def _assert_safe_file(path: Path, code: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SnapshotExecutionError(code) from exc
    _require(not stat.S_ISLNK(info.st_mode), code)
    _require(stat.S_ISREG(info.st_mode), code)
    _require(stat.S_IMODE(info.st_mode) == _SNAPSHOT_FILE_MODE, code)


def _assert_safe_dir(path: Path, code: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise SnapshotExecutionError(code) from exc
    _require(not stat.S_ISLNK(info.st_mode), code)
    _require(stat.S_ISDIR(info.st_mode), code)
    _require(stat.S_IMODE(info.st_mode) == _SNAPSHOT_DIR_MODE, code)


def _safe_relative(root: Path, value: str) -> Path:
    relative = Path(value)
    _require(
        not relative.is_absolute() and ".." not in relative.parts,
        "snapshot_storage_key_unsafe",
    )
    target = root / relative
    _require(not target.is_symlink() and target.is_file(), "snapshot_file_missing")
    return target


def _open_readonly_database(path: Path) -> sqlite3.Connection:
    _assert_safe_file(path, "snapshot_database_unsafe")
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _load_binding(
    connection: sqlite3.Connection,
    descriptor: PrivatePreparedVerificationDescriptor,
) -> sqlite3.Row:
    binding = connection.execute(
        """
        SELECT id, run_id, requirements_storage_key, requirements_file_sha256,
               canonical_report_storage_key, canonical_report_file_sha256,
               binding_manifest_storage_key, binding_manifest_file_sha256
        FROM pilot_run_results WHERE run_id = ?
        """,
        (descriptor.target_run_id,),
    ).fetchone()
    _require(binding is not None, "snapshot_binding_missing")
    assert binding is not None
    _require(str(binding["id"]) == descriptor.snapshot_id, "snapshot_identity_mismatch")
    return binding


def _recompute_snapshot_hash(
    data_dir: Path, binding: sqlite3.Row, descriptor: PrivatePreparedVerificationDescriptor
) -> str:
    """Re-derive the snapshot hash from the consumer snapshot files.

    The binding manifest records the hashes of the requirements and canonical
    report files; the snapshot hash itself is the SHA-256 of the manifest bytes.
    Recomputing both binds the on-disk consumer snapshot to the exact snapshot
    hash carried by the private descriptor.
    """
    manifest_path = _safe_relative(data_dir, str(binding["binding_manifest_storage_key"]))
    requirements = _safe_relative(data_dir, str(binding["requirements_storage_key"]))
    report = _safe_relative(data_dir, str(binding["canonical_report_storage_key"]))

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotExecutionError("snapshot_manifest_invalid") from exc
    _require(isinstance(manifest, dict), "snapshot_manifest_invalid")
    _require(
        manifest.get("requirements_file_sha256") == binding["requirements_file_sha256"]
        and manifest.get("canonical_report_file_sha256")
        == binding["canonical_report_file_sha256"],
        "snapshot_manifest_binding_mismatch",
    )
    _require(
        _sha256_file(requirements) == binding["requirements_file_sha256"]
        and _sha256_file(report) == binding["canonical_report_file_sha256"],
        "snapshot_file_hash_mismatch",
    )
    recomputed = _sha256_file(manifest_path)
    _require(recomputed == descriptor.snapshot_hash, "snapshot_hash_recomputation_mismatch")
    return recomputed


def _database_gate(
    connection: sqlite3.Connection,
    descriptor: PrivatePreparedVerificationDescriptor,
) -> GateOutcome:
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise SnapshotExecutionError("snapshot_database_query_failed") from exc
    _require(
        integrity is not None and integrity[0] == "ok",
        "snapshot_database_integrity_failed",
    )
    run = connection.execute(
        "SELECT id, status FROM tender_analysis_runs WHERE id = ?",
        (descriptor.target_run_id,),
    ).fetchone()
    _require(run is not None, "snapshot_target_run_missing")
    assert run is not None
    _require(str(run["status"]) == "completed", "snapshot_run_status_invalid")
    _load_binding(connection, descriptor)
    return GateOutcome(
        gate=_GATE_DATABASE_SNAPSHOT,
        status=_GATE_STATUS_SNAPSHOT,
        detail="exact_run_database_read_only",
    )


def _artifact_gate(
    data_dir: Path,
    binding: sqlite3.Row,
    descriptor: PrivatePreparedVerificationDescriptor,
) -> tuple[GateOutcome, str]:
    _assert_safe_dir(data_dir, "snapshot_data_dir_unsafe")
    requirements = _safe_relative(data_dir, str(binding["requirements_storage_key"]))
    report = _safe_relative(data_dir, str(binding["canonical_report_storage_key"]))
    manifest_path = _safe_relative(data_dir, str(binding["binding_manifest_storage_key"]))
    for path in (requirements, report, manifest_path):
        _assert_safe_file(path, "snapshot_artifact_unsafe")
    recomputed = _recompute_snapshot_hash(data_dir, binding, descriptor)
    return (
        GateOutcome(
            gate=_GATE_ARTIFACT_SNAPSHOT,
            status=_GATE_STATUS_SNAPSHOT,
            detail="consumer_snapshot_files_read_only",
        ),
        recomputed,
    )


def _generation_results_gate(
    run: sqlite3.Row,
    descriptor: PrivatePreparedVerificationDescriptor,
) -> GateOutcome:
    provider_absent = bool(
        not bool(run["used_llm"])
        and run["llm_model"] is None
        and run["report_path"] is None
    )
    if not provider_absent or not descriptor.provider_results_absent:
        return GateOutcome(
            gate=_GATE_GENERATION_RESULTS,
            status=_GATE_STATUS_BLOCKED,
            detail="provider_results_present",
        )
    if not descriptor.generation_artifacts_absent:
        return GateOutcome(
            gate=_GATE_GENERATION_RESULTS,
            status=_GATE_STATUS_BLOCKED,
            detail="generation_artifacts_present",
        )
    return GateOutcome(
        gate=_GATE_GENERATION_RESULTS,
        status=_GATE_STATUS_NOT_NEEDED,
        detail="zero_generation_state",
    )


def _prepared_state_gate(descriptor: PrivatePreparedVerificationDescriptor) -> GateOutcome:
    counters_consistent = bool(
        descriptor.controlled_preflight_verified
        and descriptor.controlled_preflight_invocations == 1
        and descriptor.controlled_provider_invocations == 0
        and descriptor.provider_generation_calls == 0
    )
    if not counters_consistent:
        return GateOutcome(
            gate=_GATE_PREPARED_STATE,
            status=_GATE_STATUS_BLOCKED,
            detail="control_counters_inconsistent",
        )
    return GateOutcome(
        gate=_GATE_PREPARED_STATE,
        status=_GATE_STATUS_NOT_NEEDED,
        detail="prepared_zero_generation",
    )


def _run_generation_gate(
    descriptor: PrivatePreparedVerificationDescriptor,
) -> GateOutcome:
    if not descriptor.gate5_ready or not descriptor.generation_artifacts_absent:
        return GateOutcome(
            gate=_GATE_RUN_GENERATION,
            status=_GATE_STATUS_BLOCKED,
            detail="generation_required",
        )
    return GateOutcome(
        gate=_GATE_RUN_GENERATION,
        status=_GATE_STATUS_NOT_NEEDED,
        detail="snapshot_binding_supplies_result",
    )


def execute_snapshot(
    *,
    database: Path,
    data_dir: Path,
    descriptor_path: Path,
    expected_head: str,
    expected_corpus_sha: str,
) -> SnapshotExecutionResult:
    """Re-execute an exact prepared run in read-only mode.

    The prepared database and consumer snapshot files are opened read-only. All
    five gates are evaluated against persisted state, the snapshot hash is
    re-derived from the binding files, and the result binds to the exact
    snapshot hash. This function never invokes a provider and never writes.
    """
    try:
        descriptor = parse_private_descriptor(
            descriptor_path,
            expected_head=expected_head,
            expected_corpus_sha=expected_corpus_sha,
        )
    except PreparedVerificationError as exc:
        raise SnapshotExecutionError(exc.code) from exc
    try:
        verified = verify_prepared_database_strict(
            path=database,
            data_dir=data_dir,
            descriptor=descriptor,
        )
    except PreparedVerificationError as exc:
        raise SnapshotExecutionError(exc.code) from exc

    connection = _open_readonly_database(database)
    try:
        run = connection.execute(
            """
            SELECT id, status, used_llm, llm_model, report_path
            FROM tender_analysis_runs WHERE id = ?
            """,
            (descriptor.target_run_id,),
        ).fetchone()
        _require(run is not None, "snapshot_target_run_missing")
        assert run is not None
        binding = _load_binding(connection, descriptor)

        gates: list[GateOutcome] = []
        gates.append(_database_gate(connection, descriptor))
        artifact_outcome, recomputed_hash = _artifact_gate(
            data_dir, binding, descriptor
        )
        gates.append(artifact_outcome)
        gates.append(_generation_results_gate(run, descriptor))
        gates.append(_prepared_state_gate(descriptor))
        gates.append(_run_generation_gate(descriptor))

        _require(
            tuple(outcome.gate for outcome in gates) == _EXPECTED_GATE_ORDER,
            "snapshot_gate_order_invalid",
        )
        _require(
            all(
                outcome.status == _GATE_STATUS_SNAPSHOT
                or outcome.status == _GATE_STATUS_NOT_NEEDED
                for outcome in gates
            ),
            "snapshot_gate_blocked",
        )
        _require(
            recomputed_hash == descriptor.snapshot_hash,
            "snapshot_hash_recomputation_mismatch",
        )
    finally:
        connection.close()

    return SnapshotExecutionResult(
        snapshot_id=descriptor.snapshot_id,
        target_run_id=descriptor.target_run_id,
        snapshot_hash=descriptor.snapshot_hash,
        recomputed_snapshot_hash=recomputed_hash,
        snapshot_hash_bound=recomputed_hash == descriptor.snapshot_hash,
        database_sha256=verified.database_sha256,
        verified=verified.target_run_verified,
        provider_invocations=0,
        write_count=0,
        provider_results_absent=verified.provider_results_absent,
        generation_artifacts_absent=verified.generation_artifacts_absent,
        gates=tuple(gates),
    )


def execute_prepared_state_root(
    *,
    root: Path,
    expected_head: str,
    expected_corpus_sha: str,
) -> SnapshotExecutionResult:
    """Re-execute a fully attested prepared-state publication.

    The published root is first attested (exact file set, filesystem safety,
    manifest identity, zero-generation invariants), then the exact run is
    re-executed read-only from the attested database, snapshot files and private
    descriptor.
    """
    from scripts.arv001.prepared_snapshot_attestation import (
        PreparedSnapshotAttestationError,
        verify_published_prepared_snapshot,
    )

    try:
        verify_published_prepared_snapshot(
            root,
            expected_head=expected_head,
            expected_corpus_sha=expected_corpus_sha,
        )
    except PreparedSnapshotAttestationError as exc:
        raise SnapshotExecutionError(exc.code) from exc
    return execute_snapshot(
        database=root / "prepared.sqlite3",
        data_dir=root / "application-data",
        descriptor_path=root / "prepared-verification.json",
        expected_head=expected_head,
        expected_corpus_sha=expected_corpus_sha,
    )
