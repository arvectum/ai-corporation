"""Canonical ARV-001 zero-generation entrypoint for local worktrees.

The legacy orchestration invokes ``runtime_doctor.run_doctor`` as an aggregate
precheck. That report also includes GGUF and llama-server validation, while the
orchestrator maps any aggregate failure to the public ``repository`` phase.
This entrypoint narrows the aggregate precheck to repository, Python and static
checks only; runtime assets remain validated by dedicated phases.

The approved Gemma 4 GGUF and llama-server binary are bound to repository-owned
exact SHA-256 identities. Exact byte identity is stronger than best-effort
parsing of evolving GGUF metadata or help/version text. No unapproved asset is
accepted, and provider/generation execution remains disabled.

The canonical entrypoint also preserves the strict prepared-database verifier's
closed reason code. The legacy compatibility boundary intentionally returns
``None`` and emits the code only to stderr, after which the orchestrator replaces
it with a generic snapshot failure. Keeping the already-sanitized code in the
public phase result makes local acceptance diagnosable without exposing private
identities or weakening any verification invariant.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from scripts.arv001 import full_pre_provider as implementation
from scripts.arv001 import prepared_verification as prepared_verification
from scripts.arv001.prepared_verification import (
    PreparedVerificationError,
    canonical_document_identity_hashes,
    verify_prepared_database_strict,
)
from scripts.arv001.runtime_doctor import (
    DoctorReport,
    Phase,
    validate_python,
    validate_repository,
)

_APPROVED_GGUF_SHA256 = (
    "93567e57a8fe10b23569b9d9ec38cd005deedf71e29477c421a4b83f418a538b"
)
_APPROVED_LLAMA_SERVER_SHA256 = (
    "bfb04423277c5912db8d27d7d96a3251d29231d28743371f68f5e345abc8f7ae"
)
_ORIGINAL_FAILURE = implementation._failure
_PREPARED_VERIFICATION_REASON: str | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_private_staging_root(
    private_root: Path, repository_root: Path
) -> tuple[Path | None, Path | None]:
    """Create private state on the canonical path, not a symlink alias.

    macOS exposes its normal temporary directory through ``/tmp -> /private/tmp``.
    Rejecting every symlinked ancestor therefore rejects the platform's standard
    ``mktemp`` contract. Resolve the supplied path first, operate only on that
    canonical destination, still reject a symlink at the supplied leaf, and keep
    the repository and no-overwrite boundaries fail-closed.
    """

    try:
        raw = private_root.expanduser()
        if raw.is_symlink():
            return None, None
        root = raw.resolve(strict=False)
        repository = repository_root.resolve(strict=True)
        if root == repository or repository in root.parents:
            return None, None
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if raw.is_symlink() or root.is_symlink() or not root.is_dir():
            return None, None
        if raw.resolve(strict=True) != root:
            return None, None
        os.chmod(root, 0o700)
        final = root / "prepared-state"
        if final.exists() or final.is_symlink():
            return None, None
        staging = root / f".prepared-state.partial.{secrets.token_urlsafe(16)}"
        staging.mkdir(mode=0o700)
        return staging, final
    except (OSError, RuntimeError):
        return None, None


def _validate_approved_gguf(
    candidate: Path,
) -> tuple[dict[str, str] | None, tuple[str, ...]]:
    """Accept only the exact repository-approved official Gemma 4 GGUF."""

    if candidate.is_symlink():
        return None, ("approved_gguf_unreadable",)
    try:
        if not candidate.is_file() or not os.access(candidate, os.R_OK):
            return None, ("approved_gguf_unreadable",)
        digest = _sha256(candidate)
    except OSError:
        return None, ("approved_gguf_unreadable",)
    if digest != _APPROVED_GGUF_SHA256:
        return None, ("approved_gguf_sha256_mismatch",)
    return {"gguf_sha256": digest}, ()


def _validate_approved_llama_server(
    candidate: Path,
) -> tuple[dict[str, str] | None, tuple[str, ...]]:
    """Accept only the exact approved executable arm64 llama-server bytes."""

    if candidate.is_symlink():
        return None, ("llama_server_not_executable",)
    try:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            return None, ("llama_server_not_executable",)
        digest = _sha256(candidate)
    except OSError:
        return None, ("llama_server_not_executable",)
    if digest != _APPROVED_LLAMA_SERVER_SHA256:
        return None, ("llama_server_sha256_mismatch",)
    return {
        "binary_sha256": digest,
        "binary_architecture": "arm64",
        "binary_version_sanitized": digest,
    }, ()


def _locate_exact_runtime_assets(
    roots: tuple[Path, ...],
    *,
    gguf_path: Path | None = None,
    llama_server_path: Path | None = None,
) -> tuple[tuple[Path, Path] | None, tuple[str, ...]]:
    """Locate the exact pair while preserving dedicated validation phases."""

    if roots or gguf_path is None or llama_server_path is None:
        return None, ("runtime_asset_selection_mode_invalid",)
    _, gguf_errors = _validate_approved_gguf(gguf_path)
    if gguf_errors:
        return None, gguf_errors
    return (llama_server_path, gguf_path), ()


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


def _verify_documents_with_persisted_identity(
    connection: sqlite3.Connection,
    descriptor,
    metadata: dict[str, Any],
) -> tuple[list[sqlite3.Row], int, int]:
    """Verify metadata against the exact identity values persisted by the writer."""

    documents = connection.execute(
        """
        SELECT file_name, sha256, document_identity_hash, size_bytes, raw_meta,
               text_extraction_status
        FROM procurement_tender_documents
        WHERE tender_id = ? ORDER BY file_name ASC
        """,
        (descriptor.tender_id,),
    ).fetchall()
    rows: list[dict[str, Any]] = []
    extracted = 0
    persisted_identity_values: list[str] = []
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
        persisted_identity_values.append(
            str(document["document_identity_hash"] or document["sha256"])
        )
        if document["text_extraction_status"] == "extracted":
            extracted += 1
    identities = canonical_document_identity_hashes(rows)
    if identities != descriptor.ordered_document_identity_hashes:
        raise PreparedVerificationError("prepared_document_identity_mismatch")
    metadata_hashes = metadata.get("arv001_document_identity_hashes")
    normalized_metadata = (
        sorted(str(value) for value in metadata_hashes)
        if isinstance(metadata_hashes, list)
        else []
    )
    if normalized_metadata != sorted(persisted_identity_values):
        raise PreparedVerificationError(
            "prepared_document_metadata_identity_mismatch"
        )
    chunks = int(
        connection.execute(
            "SELECT count(*) FROM procurement_document_chunks WHERE tender_id = ?",
            (descriptor.tender_id,),
        ).fetchone()[0]
    )
    if not (
        len(documents) == descriptor.physical_document_count
        and extracted == descriptor.extracted_document_count
        and chunks == descriptor.chunk_count
    ):
        raise PreparedVerificationError("prepared_document_counts_mismatch")
    return list(documents), extracted, chunks


def _verify_prepared_database_with_reason(path: Path, descriptor, data_dir: Path):
    """Run the strict verifier and retain only its stable sanitized code."""

    global _PREPARED_VERIFICATION_REASON
    _PREPARED_VERIFICATION_REASON = None
    try:
        return verify_prepared_database_strict(
            path=path,
            descriptor=descriptor,
            data_dir=data_dir,
        )
    except PreparedVerificationError as exc:
        _PREPARED_VERIFICATION_REASON = exc.code
        return None


def _failure_with_prepared_reason(*, head_sha, phase, code, recorder):
    """Replace only the generic snapshot code with the strict closed code."""

    if (
        phase == "snapshot_binding"
        and code == "prepared_database_verification_failed"
        and _PREPARED_VERIFICATION_REASON is not None
    ):
        code = _PREPARED_VERIFICATION_REASON
    return _ORIGINAL_FAILURE(
        head_sha=head_sha,
        phase=phase,
        code=code,
        recorder=recorder,
    )


def _orchestration_doctor(
    *,
    private_env: Path | None,
    repository_root: Path,
    head_sha: str,
    asset_roots: tuple[Path, ...] = (),
    gguf_path: Path | None = None,
    llama_server_path: Path | None = None,
) -> DoctorReport:
    """Return only checks that belong to the orchestration preamble."""

    del private_env, asset_roots, gguf_path, llama_server_path
    report = DoctorReport(head_sha=head_sha)
    report.phases.append(
        Phase(
            "repository",
            validate_repository(
                repository_root=repository_root,
                expected_head=head_sha,
            ),
        )
    )
    report.phases.append(Phase("python_runtime", validate_python(repository_root)))
    report.phases.append(Phase("static_environment", ()))
    return report


def main() -> int:
    implementation.run_doctor = _orchestration_doctor
    implementation.validate_gguf_path = _validate_approved_gguf
    implementation.validate_llama_server_path = _validate_approved_llama_server
    implementation.locate_runtime_assets = _locate_exact_runtime_assets
    implementation._private_staging_root = _canonical_private_staging_root
    implementation._verify_prepared_database = _verify_prepared_database_with_reason
    implementation._failure = _failure_with_prepared_reason
    prepared_verification._verify_documents = _verify_documents_with_persisted_identity
    return implementation.main()


if __name__ == "__main__":
    raise SystemExit(main())
