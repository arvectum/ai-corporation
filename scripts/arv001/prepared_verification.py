"""Private exact-run descriptor and read-only prepared-state verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HASH = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_VERSION = "arv001-prepared-verification-v1"
_DESCRIPTOR_FIELDS = {
    "schema_version", "head_sha", "target_run_id", "customer_id", "project_id",
    "case_id", "tender_id", "run_status", "registry_identity_sha256",
    "corpus_sha256", "ordered_document_identity_hashes", "physical_document_count",
    "logical_document_count", "extracted_document_count", "chunk_count",
    "snapshot_id", "snapshot_hash", "source_graph_id", "source_graph_hash",
    "gate5_ready", "controlled_preflight_verified",
    "controlled_preflight_invocations", "controlled_provider_invocations",
    "provider_generation_calls", "provider_results_absent",
    "generation_artifacts_absent",
}


class PreparedVerificationError(RuntimeError):
    """Stable fail-closed descriptor/verification failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PrivatePreparedVerificationDescriptor:
    head_sha: str
    target_run_id: str
    customer_id: str
    project_id: str
    case_id: str
    tender_id: str
    run_status: str
    registry_identity_sha256: str
    corpus_sha256: str
    ordered_document_identity_hashes: tuple[str, ...]
    physical_document_count: int
    logical_document_count: int
    extracted_document_count: int
    chunk_count: int
    snapshot_id: str
    snapshot_hash: str
    source_graph_id: str
    source_graph_hash: str
    gate5_ready: bool
    controlled_preflight_verified: bool
    controlled_preflight_invocations: int
    controlled_provider_invocations: int
    provider_generation_calls: int
    provider_results_absent: bool
    generation_artifacts_absent: bool


@dataclass(frozen=True)
class PreparedDatabaseVerification:
    database_sha256: str
    physical_document_count: int
    extracted_document_count: int
    chunk_count: int
    target_run_verified: bool
    snapshot_binding_verified: bool
    source_graph_binding_verified: bool
    gate5_ready: bool
    controlled_preflight_verified: bool
    provider_results_absent: bool
    generation_artifacts_absent: bool


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise PreparedVerificationError(code)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_hash(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def canonical_document_identity_hashes(
    rows: Iterable[dict[str, Any]],
) -> tuple[str, ...]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        original_name = str(row.get("original_name") or "")
        sha256 = str(row.get("sha256") or "")
        size_bytes = row.get("size_bytes")
        if not original_name or not _HASH.fullmatch(sha256):
            raise PreparedVerificationError("document_identity_invalid")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
        ):
            raise PreparedVerificationError("document_identity_invalid")
        normalized.append(
            {
                "original_name": original_name,
                "sha256": sha256,
                "size_bytes": size_bytes,
            }
        )
    normalized.sort(key=lambda item: item["original_name"])
    return tuple(_canonical_hash(item) for item in normalized)


def registry_identity_sha256(registry_number: str) -> str:
    if not isinstance(registry_number, str) or not registry_number:
        raise PreparedVerificationError("registry_identity_invalid")
    return _sha256_bytes(registry_number.encode("utf-8"))


def _safe_relative(root: Path, value: str) -> Path:
    relative = Path(value)
    _require(
        not relative.is_absolute() and ".." not in relative.parts,
        "prepared_snapshot_path_unsafe",
    )
    target = root / relative
    _require(
        not target.is_symlink() and target.is_file(),
        "prepared_snapshot_file_missing",
    )
    return target


def _verify_documents(
    connection: sqlite3.Connection,
    descriptor: PrivatePreparedVerificationDescriptor,
    metadata: dict[str, Any],
) -> tuple[list[sqlite3.Row], int, int]:
    # We strictly use only historical columns.
    documents = connection.execute(
        """
        SELECT file_name, sha256, size_bytes, raw_meta, text_extraction_status
        FROM procurement_tender_documents
        WHERE tender_id = ? ORDER BY file_name ASC
        """,
        (descriptor.tender_id,),
    ).fetchall()

    rows: list[dict[str, Any]] = []
    extracted = 0
    sha_values: list[str] = []

    for document in documents:
        raw_meta = _json_object(document["raw_meta"])
        corpus_descriptor = _json_object(raw_meta.get("corpus_descriptor"))
        rows.append(
            {
                "original_name": corpus_descriptor.get("original_name")
                or document["file_name"],
                "sha256": document["sha256"],
                "size_bytes": document["size_bytes"],
            }
        )
        sha_values.append(str(document["sha256"]))
        if document["text_extraction_status"] == "extracted":
            extracted += 1

    identities = canonical_document_identity_hashes(rows)
    _require(
        identities == descriptor.ordered_document_identity_hashes,
        "prepared_document_identity_mismatch",
    )

    metadata_hashes = metadata.get("arv001_document_identity_hashes")
    normalized_metadata = (
        sorted(str(value) for value in metadata_hashes)
        if isinstance(metadata_hashes, list)
        else []
    )
    
    # Supported historical representations: full identity hashes or raw file SHAs.
    # Note: internal document_identity_hash is NOT selected and NOT supported here
    # to maintain strict historical DB compatibility without migration knowledge.
    _require(
        normalized_metadata in (sorted(identities), sorted(sha_values)),
        "prepared_document_metadata_identity_mismatch",
    )

    chunks = int(
        connection.execute(
            "SELECT count(*) FROM procurement_document_chunks WHERE tender_id = ?",
            (descriptor.tender_id,),
        ).fetchone()[0]
    )
    _require(
        len(documents) == descriptor.physical_document_count
        and extracted == descriptor.extracted_document_count
        and chunks == descriptor.chunk_count,
        "prepared_document_counts_mismatch",
    )
    return list(documents), extracted, chunks


def _load_run(
    connection: sqlite3.Connection,
    descriptor: PrivatePreparedVerificationDescriptor,
) -> sqlite3.Row:
    # Use only established historical columns for run loading.
    run = connection.execute(
        """
        SELECT registry_number, status, used_llm, llm_model, report_path,
               customer_id, project_id, procurement_case_id, metadata_json
        FROM tender_analysis_runs WHERE id = ?
        """,
        (descriptor.target_run_id,),
    ).fetchone()
    
    _require(run is not None, "prepared_run_missing")
    assert run is not None
    _require(
        run["status"] == "completed"
        and str(run["customer_id"]) == descriptor.customer_id
        and str(run["project_id"]) == descriptor.project_id
        and str(run["procurement_case_id"]) == descriptor.case_id,
        "prepared_run_binding_mismatch",
    )
    
    metadata = _json_object(run["metadata_json"])
    # Historically tender_id might only be present in metadata_json.
    _require(str(metadata.get("arv001_tender_id")) == descriptor.tender_id, "prepared_run_binding_mismatch")

    return run


def _verify_ownership(
    connection: sqlite3.Connection,
    descriptor: PrivatePreparedVerificationDescriptor,
) -> None:
    case = connection.execute(
        """
        SELECT customer_id, project_id, current_run_id
        FROM procurement_cases WHERE id = ?
        """,
        (descriptor.case_id,),
    ).fetchone()
    project = connection.execute(
        "SELECT customer_id FROM pilot_projects WHERE id = ?",
        (descriptor.project_id,),
    ).fetchone()
    _require(case is not None and project is not None, "prepared_case_or_project_missing")
    assert case is not None and project is not None
    _require(
        str(case["customer_id"]) == descriptor.customer_id
        and str(case["project_id"]) == descriptor.project_id
        and str(case["current_run_id"]) == descriptor.target_run_id
        and str(project["customer_id"]) == descriptor.customer_id,
        "prepared_ownership_mismatch",
    )


def _verify_tender(
    connection: sqlite3.Connection,
    descriptor: PrivatePreparedVerificationDescriptor,
    run: sqlite3.Row,
) -> dict[str, Any]:
    metadata = _json_object(run["metadata_json"])
    _require(
        str(metadata.get("arv001_tender_id")) == descriptor.tender_id
        and metadata.get("arv001_corpus_sha256") == descriptor.corpus_sha256,
        "prepared_run_metadata_mismatch",
    )
    tender = connection.execute(
        "SELECT registry_number, content_hash FROM procurement_tenders WHERE id = ?",
        (descriptor.tender_id,),
    ).fetchone()
    _require(tender is not None, "prepared_tender_missing")
    assert tender is not None
    _require(
        tender["content_hash"] == descriptor.corpus_sha256
        and registry_identity_sha256(str(tender["registry_number"]))
        == descriptor.registry_identity_sha256
        and str(tender["registry_number"]) == str(run["registry_number"]),
        "prepared_tender_binding_mismatch",
    )
    return metadata


def _load_snapshot_binding(
    connection: sqlite3.Connection,
    descriptor: PrivatePreparedVerificationDescriptor,
) -> sqlite3.Row:
    binding = connection.execute(
        """
        SELECT id, customer_id, project_id, procurement_case_id, run_id,
               source_analysis_run_id, requirements_storage_key,
               requirements_file_sha256, canonical_report_storage_key,
               canonical_report_file_sha256, binding_manifest_storage_key,
               binding_manifest_file_sha256, source_graph_hash,
               source_graph_hash_algorithm, verification_policy_version
        FROM pilot_run_results WHERE run_id = ?
        """,
        (descriptor.target_run_id,),
    ).fetchone()
    _require(binding is not None, "prepared_snapshot_binding_missing")
    assert binding is not None
    _require(
        str(binding["id"]) == descriptor.snapshot_id
        and str(binding["customer_id"]) == descriptor.customer_id
        and str(binding["project_id"]) == descriptor.project_id
        and str(binding["procurement_case_id"]) == descriptor.case_id
        and str(binding["run_id"]) == descriptor.target_run_id,
        "prepared_snapshot_identity_mismatch",
    )
    _require(
        str(binding["source_analysis_run_id"]) == descriptor.source_graph_id
        and binding["source_graph_hash"] == descriptor.source_graph_hash,
        "prepared_source_graph_mismatch",
    )
    _require(
        binding["binding_manifest_file_sha256"] == descriptor.snapshot_hash,
        "prepared_snapshot_hash_mismatch",
    )
    _require(
        binding["source_graph_hash_algorithm"] == "sha256-json-c14n-v1"
        and binding["verification_policy_version"]
        == "r8-frozen-canonical-verifier-v1",
        "prepared_snapshot_policy_mismatch",
    )
    return binding


def _verify_snapshot_files(
    data_dir: Path,
    binding: sqlite3.Row,
) -> tuple[Path, Path, Path]:
    requirements = _safe_relative(
        data_dir, str(binding["requirements_storage_key"])
    )
    report = _safe_relative(
        data_dir, str(binding["canonical_report_storage_key"])
    )
    manifest_path = _safe_relative(
        data_dir, str(binding["binding_manifest_storage_key"])
    )
    _require(
        _sha256_file(requirements) == binding["requirements_file_sha256"]
        and _sha256_file(report) == binding["canonical_report_file_sha256"]
        and _sha256_file(manifest_path)
        == binding["binding_manifest_file_sha256"],
        "prepared_snapshot_file_hash_mismatch",
    )
    return requirements, report, manifest_path


def _verify_snapshot_manifest(
    manifest_path: Path,
    descriptor: PrivatePreparedVerificationDescriptor,
    binding: sqlite3.Row,
) -> None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedVerificationError("prepared_snapshot_manifest_invalid") from exc
    expected_manifest = {
        "customer_id": descriptor.customer_id,
        "project_id": descriptor.project_id,
        "procurement_case_id": descriptor.case_id,
        "run_id": descriptor.target_run_id,
        "source_analysis_run_id": descriptor.source_graph_id,
        "source_graph_hash": descriptor.source_graph_hash,
        "requirements_file_sha256": binding["requirements_file_sha256"],
        "canonical_report_file_sha256": binding["canonical_report_file_sha256"],
    }
    _require(
        isinstance(manifest, dict)
        and all(manifest.get(key) == value for key, value in expected_manifest.items()),
        "prepared_snapshot_manifest_mismatch",
    )


def _verify_zero_generation_state(
    connection: sqlite3.Connection,
    descriptor: PrivatePreparedVerificationDescriptor,
    run: sqlite3.Row,
    *,
    documents: list[sqlite3.Row],
    extracted: int,
    chunks: int,
) -> tuple[bool, bool, bool, bool]:
    artifact_count = int(
        connection.execute(
            "SELECT count(*) FROM pilot_artifacts WHERE run_id = ?",
            (descriptor.target_run_id,),
        ).fetchone()[0]
    )
    provider_absent = bool(
        not bool(run["used_llm"])
        and run["llm_model"] is None
        and run["report_path"] is None
        and artifact_count == 0
    )
    generation_absent = artifact_count == 0
    gate5_ready = bool(
        run["status"] == "completed"
        and len(documents) == descriptor.physical_document_count
        and extracted == descriptor.extracted_document_count
        and chunks == descriptor.chunk_count
    )
    controlled_preflight_verified = bool(
        descriptor.controlled_preflight_verified
        and descriptor.controlled_preflight_invocations == 1
        and descriptor.controlled_provider_invocations == 0
        and descriptor.provider_generation_calls == 0
    )
    _require(
        provider_absent and descriptor.provider_results_absent,
        "prepared_provider_state_present",
    )
    _require(
        generation_absent and descriptor.generation_artifacts_absent,
        "prepared_generation_artifacts_present",
    )
    _require(gate5_ready, "prepared_gate5_mismatch")
    _require(controlled_preflight_verified, "prepared_preflight_state_mismatch")
    return provider_absent, generation_absent, gate5_ready, controlled_preflight_verified


def verify_prepared_database_strict(
    *,
    path: Path,
    data_dir: Path,
    descriptor: PrivatePreparedVerificationDescriptor,
) -> PreparedDatabaseVerification:
    """Independently verify the descriptor against exact persisted rows and bytes."""
    _require(
        not path.is_symlink()
        and path.is_file()
        and not data_dir.is_symlink()
        and data_dir.is_dir(),
        "prepared_database_path_invalid",
    )
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            _require(
                integrity is not None and integrity[0] == "ok",
                "prepared_database_integrity_failed",
            )
            run = _load_run(connection, descriptor)
            _verify_ownership(connection, descriptor)
            metadata = _verify_tender(connection, descriptor, run)
            documents, extracted, chunks = _verify_documents(
                connection, descriptor, metadata
            )
            binding = _load_snapshot_binding(connection, descriptor)
            _, _, manifest_path = _verify_snapshot_files(data_dir, binding)
            _verify_snapshot_manifest(manifest_path, descriptor, binding)
            (
                provider_absent,
                generation_absent,
                gate5_ready,
                controlled_preflight_verified,
            ) = _verify_zero_generation_state(
                connection,
                descriptor,
                run,
                documents=documents,
                extracted=extracted,
                chunks=chunks,
            )
            return PreparedDatabaseVerification(
                database_sha256=_sha256_file(path),
                physical_document_count=len(documents),
                extracted_document_count=extracted,
                chunk_count=chunks,
                target_run_verified=True,
                snapshot_binding_verified=True,
                source_graph_binding_verified=True,
                gate5_ready=gate5_ready,
                controlled_preflight_verified=controlled_preflight_verified,
                provider_results_absent=provider_absent,
                generation_artifacts_absent=generation_absent,
            )
        finally:
            connection.close()
    except PreparedVerificationError:
        raise
    except OSError as exc:
        raise PreparedVerificationError("prepared_database_io_failed") from exc
    except sqlite3.Error as exc:
        raise PreparedVerificationError("prepared_database_query_failed") from exc


def verify_prepared_database(
    *,
    path: Path,
    data_dir: Path,
    descriptor: PrivatePreparedVerificationDescriptor,
) -> PreparedDatabaseVerification | None:
    """Compatibility boundary for callers not yet migrated to the strict contract."""
    try:
        return verify_prepared_database_strict(
            path=path,
            data_dir=data_dir,
            descriptor=descriptor,
        )
    except PreparedVerificationError as exc:
        # The code is closed, sanitized and contains no private values. Keeping it on
        # stderr makes real local acceptance diagnosable before lifecycle cleanup.
        import sys

        print(f"prepared_verification:{exc.code}", file=sys.stderr)
        return None


def write_private_verification_descriptor(
    path: Path,
    *,
    application: dict[str, Any],
    head_sha: str,
    registry: str,
    corpus_sha: str,
    logical_count: int,
    preflight: dict[str, Any],
    invocation: dict[str, Any],
    controlled_output_root: Path,
) -> None:
    """Derive private descriptor facts from persisted entities and verified bytes."""
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise PreparedVerificationError("private_verification_descriptor_unsafe")

    from sqlalchemy import func, select

    from src.modules.customer_pilot.binding_verifier import verify_run_snapshot_binding
    from src.modules.customer_pilot.models import (
        PilotArtifact,
        PilotRunResult,
        ProcurementCase,
    )
    from src.shared.db.session import SessionLocal
    from src.tender_research.models import (
        ProcurementTenderDocument,
        TenderAnalysisRun,
    )

    with SessionLocal() as session:
        run = session.scalar(
            select(TenderAnalysisRun).where(
                TenderAnalysisRun.id == application["run_id"]
            )
        )
        case = session.scalar(
            select(ProcurementCase).where(ProcurementCase.id == application["case_id"])
        )
        binding = session.scalar(
            select(PilotRunResult).where(PilotRunResult.run_id == application["run_id"])
        )
        if not run or not case or not binding:
            raise PreparedVerificationError("private_verification_binding_missing")
        verified = verify_run_snapshot_binding(run=run, case=case, binding=binding)
        documents = session.scalars(
            select(ProcurementTenderDocument)
            .where(ProcurementTenderDocument.tender_id == application["tender_id"])
            .order_by(ProcurementTenderDocument.file_name.asc())
        ).all()
        identities = canonical_document_identity_hashes(
            {
                "original_name": str(
                    ((document.raw_meta or {}).get("corpus_descriptor") or {}).get(
                        "original_name"
                    )
                    or document.file_name
                ),
                "sha256": document.sha256,
                "size_bytes": document.size_bytes,
            }
            for document in documents
        )
        artifact_count = int(
            session.scalar(
                select(func.count())
                .select_from(PilotArtifact)
                .where(PilotArtifact.run_id == run.id)
            )
            or 0
        )
        provider_results_absent = bool(
            not run.used_llm
            and run.llm_model is None
            and run.report_path is None
            and artifact_count == 0
        )
        generation_artifacts_absent = bool(
            artifact_count == 0 and not controlled_output_root.exists()
        )
        controlled_preflight_verified = bool(
            preflight.get("ready_for_controlled_execution") is True
            and invocation.get("controlled_preflight_invocations") == 1
            and invocation.get("controlled_provider_invocations") == 0
            and invocation.get("provider_generation_calls") == 0
        )
        descriptor = {
            "schema_version": _SCHEMA_VERSION,
            "head_sha": head_sha,
            "target_run_id": str(run.id),
            "customer_id": str(run.customer_id),
            "project_id": str(run.project_id),
            "case_id": str(case.id),
            "tender_id": str(application["tender_id"]),
            "run_status": str(run.status),
            "registry_identity_sha256": registry_identity_sha256(registry),
            "corpus_sha256": corpus_sha,
            "ordered_document_identity_hashes": list(identities),
            "physical_document_count": len(documents),
            "logical_document_count": logical_count,
            "extracted_document_count": application["extracted_document_count"],
            "chunk_count": application["chunk_count"],
            "snapshot_id": str(binding.id),
            "snapshot_hash": verified.binding_manifest_file_sha256,
            "source_graph_id": verified.source_analysis_run_id,
            "source_graph_hash": verified.source_graph_hash,
            "gate5_ready": bool(preflight.get("ready_for_controlled_execution")),
            "controlled_preflight_verified": controlled_preflight_verified,
            "controlled_preflight_invocations": invocation[
                "controlled_preflight_invocations"
            ],
            "controlled_provider_invocations": invocation[
                "controlled_provider_invocations"
            ],
            "provider_generation_calls": invocation["provider_generation_calls"],
            "provider_results_absent": provider_results_absent,
            "generation_artifacts_absent": generation_artifacts_absent,
        }

    temporary = path.with_name(f".{path.name}.partial.{os.getpid()}")
    temporary.write_text(
        json.dumps(descriptor, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def parse_private_descriptor(
    path: Path, *, expected_head: str, expected_corpus_sha: str
) -> PrivatePreparedVerificationDescriptor:
    if path.is_symlink() or not path.is_file():
        raise PreparedVerificationError("descriptor_missing_or_unsafe")
    if path.stat().st_mode & 0o777 != 0o600:
        raise PreparedVerificationError("descriptor_mode_invalid")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedVerificationError("descriptor_json_invalid") from exc
    if not isinstance(value, dict) or set(value) != _DESCRIPTOR_FIELDS:
        raise PreparedVerificationError("descriptor_schema_invalid")
    if value["schema_version"] != _SCHEMA_VERSION:
        raise PreparedVerificationError("descriptor_version_invalid")

    string_fields = (
        "head_sha", "target_run_id", "customer_id", "project_id", "case_id",
        "tender_id", "run_status", "registry_identity_sha256", "corpus_sha256",
        "snapshot_id", "snapshot_hash", "source_graph_id", "source_graph_hash",
    )
    if any(
        not isinstance(value[field], str) or not value[field] for field in string_fields
    ):
        raise PreparedVerificationError("descriptor_type_invalid")
    if value["head_sha"] != expected_head:
        raise PreparedVerificationError("descriptor_head_mismatch")
    if value["corpus_sha256"] != expected_corpus_sha:
        raise PreparedVerificationError("descriptor_corpus_mismatch")
    if value["run_status"] != "completed":
        raise PreparedVerificationError("descriptor_run_status_invalid")
    for field in (
        "registry_identity_sha256", "corpus_sha256", "snapshot_hash",
        "source_graph_hash",
    ):
        if not _HASH.fullmatch(value[field]):
            raise PreparedVerificationError("descriptor_hash_invalid")

    identities = value["ordered_document_identity_hashes"]
    if (
        not isinstance(identities, list)
        or len(identities) != 10
        or len(set(identities)) != 10
        or any(
            not isinstance(item, str) or not _HASH.fullmatch(item)
            for item in identities
        )
    ):
        raise PreparedVerificationError("descriptor_document_identities_invalid")
    expected_counts = {
        "physical_document_count": 10,
        "logical_document_count": 6,
        "extracted_document_count": 10,
        "chunk_count": 233,
        "controlled_preflight_invocations": 1,
        "controlled_provider_invocations": 0,
        "provider_generation_calls": 0,
    }
    for field, expected in expected_counts.items():
        if (
            not isinstance(value[field], int)
            or isinstance(value[field], bool)
            or value[field] != expected
        ):
            raise PreparedVerificationError("descriptor_count_invalid")
    for field in (
        "gate5_ready", "controlled_preflight_verified",
        "provider_results_absent", "generation_artifacts_absent",
    ):
        if value[field] is not True:
            raise PreparedVerificationError("descriptor_boolean_invalid")

    return PrivatePreparedVerificationDescriptor(
        head_sha=value["head_sha"],
        target_run_id=value["target_run_id"],
        customer_id=value["customer_id"],
        project_id=value["project_id"],
        case_id=value["case_id"],
        tender_id=value["tender_id"],
        run_status=value["run_status"],
        registry_identity_sha256=value["registry_identity_sha256"],
        corpus_sha256=value["corpus_sha256"],
        ordered_document_identity_hashes=tuple(identities),
        physical_document_count=value["physical_document_count"],
        logical_document_count=value["logical_document_count"],
        extracted_document_count=value["extracted_document_count"],
        chunk_count=value["chunk_count"],
        snapshot_id=value["snapshot_id"],
        snapshot_hash=value["snapshot_hash"],
        source_graph_id=value["source_graph_id"],
        source_graph_hash=value["source_graph_hash"],
        gate5_ready=value["gate5_ready"],
        controlled_preflight_verified=value["controlled_preflight_verified"],
        controlled_preflight_invocations=value["controlled_preflight_invocations"],
        controlled_provider_invocations=value["controlled_provider_invocations"],
        provider_generation_calls=value["provider_generation_calls"],
        provider_results_absent=value["provider_results_absent"],
        generation_artifacts_absent=value["generation_artifacts_absent"],
    )
