"""Fail-closed, provider-neutral recovery of one procurement's documents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from dotenv import load_dotenv
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from src.modules.tender_operator_agent_demo.settings import (
    clear_zakupki_soap_settings_cache,
    get_zakupki_soap_settings,
)
from src.modules.tender_operator_agent_demo.zakupki_soap_client import ZakupkiSoapClient
from src.shared.config.settings import Settings, get_settings
from src.shared.network.etp_trust import (
    ETPTrustConfigurationError,
    TrustPolicy,
    build_ssl_context,
    policy_from_settings,
    resolve_host_policy,
)
from src.tender_research.config import load_config
from src.tender_research.document_store import _try_extract
from src.tender_research.models import (
    ProcurementDocumentChunk,
    ProcurementTender,
    ProcurementTenderDocument,
)
from src.tender_research.rag.chunker import normalize_text
from src.tender_research.rag.indexer import DocumentChunkIndexer
from src.tender_research.repository import TenderRepository

MAX_ENTRIES = 5_000
MAX_TOTAL_UNCOMPRESSED = 1 << 30
MAX_ENTRY_UNCOMPRESSED = 200 << 20
MAX_RATIO = 200
HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MINIMUM_REQUIRED_ALEMBIC_REVISION = "096_add_r8_canonical_snapshot_binding"


@dataclass(frozen=True)
class DocumentRecoveryRequest:
    registry_number: str
    env_file: Path
    data_root: Path
    backup_dir: Path
    apply: bool = False
    build_chunks: bool = False


@dataclass
class ZipInventory:
    entry_count: int = 0
    regular_file_count: int = 0
    directory_count: int = 0
    nested_zip_count: int = 0
    nested_regular_file_count: int = 0
    compressed_bytes: int = 0
    uncompressed_bytes: int = 0
    crc_valid: bool = True
    unsafe_entries: int = 0
    encrypted_entries: int = 0
    symlink_entries: int = 0
    traversal_entries: int = 0
    duplicate_paths: int = 0
    entries: list[tuple[Path, str, int]] | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_entry(name: str) -> bool:
    if "\x00" in name:
        return False
    path = PurePosixPath(name.replace("\\", "/"))
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and not (len(name) > 1 and name[1] == ":")
    )


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)


def inspect_zip(
    path: Path,
) -> tuple[ZipInventory, dict[str, list[tuple[Path, str, int]]]]:
    """Inspect outer and one nested archive, retaining enough identity for apply."""
    inventory = ZipInventory(entries=[])
    matches: dict[str, list[tuple[Path, str, int]]] = {}
    seen: set[str] = set()

    def scan(zip_path: Path, level: int) -> None:
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
            inventory.entry_count += len(infos)
            if inventory.entry_count > MAX_ENTRIES:
                inventory.unsafe_entries += 1
                return
            for info in infos:
                if info.is_dir():
                    inventory.directory_count += 1
                    continue
                inventory.regular_file_count += 1
                if level == 1:
                    inventory.nested_regular_file_count += 1
                inventory.compressed_bytes += info.compress_size
                inventory.uncompressed_bytes += info.file_size
                normalized = str(PurePosixPath(info.filename.replace("\\", "/")))
                unsafe = not _safe_entry(info.filename)
                if normalized in seen:
                    inventory.duplicate_paths += 1
                    unsafe = True
                seen.add(normalized)
                if not _safe_entry(info.filename):
                    inventory.traversal_entries += 1
                if info.flag_bits & 1:
                    inventory.encrypted_entries += 1
                    unsafe = True
                if _is_symlink(info):
                    inventory.symlink_entries += 1
                    unsafe = True
                if info.file_size > MAX_ENTRY_UNCOMPRESSED:
                    unsafe = True
                if (
                    info.compress_size
                    and info.file_size / info.compress_size > MAX_RATIO
                ):
                    unsafe = True
                if unsafe:
                    inventory.unsafe_entries += 1
                    continue
                if info.filename.lower().endswith(".zip"):
                    if level >= 1:
                        inventory.unsafe_entries += 1
                        continue
                    fd, nested_name = tempfile.mkstemp(
                        prefix="r10-1-nested-", suffix=".zip"
                    )
                    os.close(fd)
                    nested = Path(nested_name)
                    os.chmod(nested, 0o600)
                    try:
                        with archive.open(info) as source, nested.open("wb") as target:
                            for block in iter(lambda: source.read(1 << 20), b""):
                                target.write(block)
                        inventory.nested_zip_count += 1
                        scan(nested, 1)
                    finally:
                        nested.unlink(missing_ok=True)
                    continue
                digest = hashlib.sha256()
                try:
                    with archive.open(info) as source:
                        for block in iter(lambda: source.read(1 << 20), b""):
                            digest.update(block)
                except (OSError, RuntimeError, zlib.error, zipfile.BadZipFile):
                    inventory.crc_valid = False
                    continue
                matches.setdefault(digest.hexdigest(), []).append(
                    (zip_path, info.filename, level)
                )
                inventory.entries.append((zip_path, info.filename, level))
            try:
                inventory.crc_valid = inventory.crc_valid and archive.testzip() is None
            except (OSError, RuntimeError, zlib.error, zipfile.BadZipFile):
                inventory.crc_valid = False

    try:
        scan(path, 0)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error):
        inventory.crc_valid = False
        inventory.unsafe_entries += 1
    if inventory.uncompressed_bytes > MAX_TOTAL_UNCOMPRESSED:
        inventory.unsafe_entries += 1
    if inventory.compressed_bytes > path.stat().st_size + 65_536:
        inventory.unsafe_entries += 1
    return inventory, matches


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_data_root(data_root: Path, backup_dir: Path) -> Path:
    checkout = Path(__file__).resolve().parents[3]
    if not data_root.is_absolute() or data_root.is_symlink():
        raise ValueError("data_root must be an absolute non-symlink path")
    if not backup_dir.is_absolute() or backup_dir.is_symlink():
        raise ValueError("backup_dir must be an absolute non-symlink path")
    _reject_lexical_symlink_parents(data_root)
    _reject_lexical_symlink_parents(backup_dir)
    resolved = data_root.resolve(strict=False)
    backup = backup_dir.resolve(strict=False)
    tmp_root = Path("/tmp").resolve()
    if (
        resolved == tmp_root
        or tmp_root in resolved.parents
        or backup == tmp_root
        or tmp_root in backup.parents
    ):
        raise ValueError("data_root cannot be inside /tmp")
    if _inside(resolved, checkout) or _inside(backup, checkout):
        raise ValueError("recovery paths cannot be inside checkout")
    if resolved == backup or _inside(backup, resolved) or _inside(resolved, backup):
        raise ValueError("recovery paths overlap")
    for candidate in (resolved, backup):
        current = candidate
        while current != current.parent:
            if current.is_symlink():
                raise ValueError("recovery path has symlink parent")
            current = current.parent
    return resolved


def _reject_lexical_symlink_parents(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError("recovery path has symlink parent")


def _nearest_existing_ancestor(path: Path) -> Path:
    current = path
    while not current.exists():
        if current == current.parent:
            raise ValueError("recovery target has no existing ancestor")
        current = current.parent
    if current.is_symlink():
        raise ValueError("recovery target ancestor is symlink")
    return current


def _safe_backup_dir(path: Path, data_root: Path, *, create: bool) -> Path:
    if (
        not path.is_absolute()
        or path.is_symlink()
        or _inside(path.resolve(strict=False), data_root)
    ):
        raise ValueError("backup_dir must be an absolute path outside data_root")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    return path


def _document_layout(
    data_root: Path, registry: str, digest: str, extension: str
) -> tuple[Path, Path]:
    root = data_root / "tender_research" / "procurements" / registry
    return (
        root / "source" / f"{digest}{extension.lower()}",
        root / "extracted" / f"{digest}.txt",
    )


def _publish(
    source_stage: Path, text_stage: Path, source_final: Path, text_final: Path
) -> None:
    """Compatibility helper for atomic replacement of two prepared files."""
    source_final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text_final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(source_stage, 0o600)
    os.chmod(text_stage, 0o600)
    os.replace(source_stage, source_final)
    os.replace(text_stage, text_final)


def _report(**values: Any) -> dict[str, Any]:
    defaults = {
        "mode": "dry-run",
        "database_ready": False,
        "alembic_revision": None,
        "alembic_repository_head": None,
        "alembic_minimum_required_revision": MINIMUM_REQUIRED_ALEMBIC_REVISION,
        "alembic_minimum_present": False,
        "alembic_minimum_is_ancestor": False,
        "alembic_database_at_repository_head": False,
        "soap_status": None,
        "zip_safety_passed": False,
        "expected_document_count": 0,
        "unique_exact_match_count": 0,
        "missing_expected_hash_count": 0,
        "ambiguous_expected_hash_count": 0,
        "extra_archive_regular_file_count": 0,
        "extraction_attempted_count": 0,
        "extraction_success_count": 0,
        "extraction_failed_count": 0,
        "old_extracted_chars_sum": 0,
        "new_extracted_chars_sum": 0,
        "char_count_changed_document_count": 0,
        "files_staged_count": 0,
        "files_published_count": 0,
        "documents_updated_count": 0,
        "chunk_count_before": 0,
        "chunk_count_after_first_build": 0,
        "chunk_count_after_second_build": 0,
        "nonempty_chunk_count": 0,
        "token_estimate": 0,
        "chunk_hashes_stable": False,
        "backup_created": False,
        "database_mutation_performed": False,
        "persistent_filesystem_mutation_performed": False,
        "filesystem_rollback_performed": False,
        "provider_called": False,
        "transport_policy_ready": False,
        "transport_policy_injected": False,
        "runtime_diagnostics_enabled": False,
        "temporary_diagnostics_used": False,
        "staging_created": False,
        "staging_cleanup_attempted": False,
        "staging_cleanup_succeeded": False,
        "staging_cleanup_retry_performed": False,
        "staging_persisted_after_cleanup": False,
        "classification": "DOCUMENT_RECOVERY_REJECTED",
        "final_status": "DOCUMENT_RECOVERY_REJECTED",
    }
    return {**defaults, **values}


def _transport_policy_error_code(exc: Exception) -> str:
    message = str(exc).lower()
    if "required" in message:
        return "tls_policy_missing"
    if "enabled state" in message or "disabled" in message:
        return "tls_policy_disabled"
    return "tls_policy_file_invalid"


def _validate_recovery_endpoint(
    soap_settings: Any, policy: TrustPolicy, endpoint_url: str
) -> str | None:
    """Return a safe policy error code, without exposing endpoint details."""
    if not hasattr(soap_settings, "allowed_hosts"):
        return None
    parsed = urlparse(endpoint_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return "soap_endpoint_not_https"
    if not any(
        hostname == allowed
        or (allowed.startswith(".") and hostname.endswith(allowed))
        or hostname.endswith(f".{allowed}")
        for allowed in soap_settings.allowed_hosts
        if allowed
    ):
        return "soap_host_not_allowed"
    if not getattr(soap_settings, "require_direct_ru_route", False):
        try:
            build_ssl_context(hostname, policy)
        except ETPTrustConfigurationError:
            return "tls_policy_ca_invalid"
        return None
    host_policy = resolve_host_policy(hostname, policy)
    if not policy.enabled:
        return "tls_policy_disabled"
    if (
        not policy.proxy_bypass_enabled
        or host_policy is None
        or not host_policy.direct_connection
    ):
        return "tls_policy_direct_route_required"
    try:
        build_ssl_context(hostname, policy)
    except ETPTrustConfigurationError:
        return "tls_policy_ca_invalid"
    return None


def _schema_graph_state(
    script: ScriptDirectory, revisions: tuple[str, ...]
) -> dict[str, Any]:
    heads = tuple(script.get_heads())
    repository_head = heads[0] if len(heads) == 1 else None
    current_revision = revisions[0] if len(revisions) == 1 else None
    minimum_present = False
    minimum_is_ancestor = False
    database_at_repository_head = False
    if repository_head is not None:
        try:
            minimum_present = (
                script.get_revision(MINIMUM_REQUIRED_ALEMBIC_REVISION) is not None
            )
            database_script = (
                script.get_revision(current_revision)
                if current_revision is not None
                else None
            )
            if database_script is not None and minimum_present:
                cursor = database_script
                seen: set[str] = set()
                while cursor.revision != MINIMUM_REQUIRED_ALEMBIC_REVISION:
                    if cursor.revision in seen:
                        break
                    seen.add(cursor.revision)
                    down_revision = cursor.down_revision
                    if isinstance(down_revision, tuple) or down_revision is None:
                        break
                    cursor = script.get_revision(down_revision)
                    if cursor is None:
                        break
                minimum_is_ancestor = (
                    cursor.revision == MINIMUM_REQUIRED_ALEMBIC_REVISION
                )
            database_at_repository_head = current_revision == repository_head
        except Exception:  # noqa: BLE001
            minimum_present = False
            minimum_is_ancestor = False
            database_at_repository_head = False
    return {
        "revision": current_revision,
        "ready": (
            len(heads) == 1
            and len(revisions) == 1
            and repository_head is not None
            and current_revision == repository_head
            and minimum_present
            and minimum_is_ancestor
            and database_at_repository_head
        ),
        "alembic_repository_head": repository_head,
        "alembic_minimum_required_revision": MINIMUM_REQUIRED_ALEMBIC_REVISION,
        "alembic_minimum_present": minimum_present,
        "alembic_minimum_is_ancestor": minimum_is_ancestor,
        "alembic_database_at_repository_head": database_at_repository_head,
    }


def _schema_gate_details(engine) -> dict[str, Any]:
    config = AlembicConfig(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    with engine.connect() as connection:
        revisions = tuple(
            str(value)
            for value in connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars()
        )
    return _schema_graph_state(script, revisions)


def _schema_revision(engine) -> tuple[str | None, bool]:
    details = _schema_gate_details(engine)
    return details["revision"], details["ready"]


def _valid_hashes(documents: list[ProcurementTenderDocument]) -> list[str] | None:
    values = []
    for document in documents:
        value = document.sha256
        if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
            return None
        values.append(value.lower())
    return values if len(values) == len(set(values)) else None


def _expected_paths(
    data_root: Path, registry: str, document: ProcurementTenderDocument
) -> tuple[Path, Path]:
    extension = Path(document.file_name or "").suffix.lower() or ".xml"
    return _document_layout(data_root, registry, document.sha256.lower(), extension)


def _regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _utf8_nonempty(path: Path) -> str | None:
    if not _regular(path):
        return None
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return value if value.strip() else None


def _chunk_snapshot(chunks, documents, tender_id):
    ids = {document.id for document in documents}
    valid = bool(chunks)
    nonempty = 0
    tokens = 0
    counts = {document_id: 0 for document_id in ids}
    document_lengths: dict[str, int] = {}
    for document in documents:
        value = (
            _utf8_nonempty(Path(document.extracted_text_path))
            if document.extracted_text_path
            else None
        )
        if value is None:
            valid = False
            continue
        document_lengths[document.id] = len(normalize_text(value))
    ordered = []
    previous_start: dict[str, int] = {}
    previous_index: dict[str, int] = {}
    for chunk in sorted(chunks, key=lambda item: (item.document_id, item.chunk_index)):
        value = chunk.text or ""
        if value.strip():
            nonempty += 1
        counts[chunk.document_id] = counts.get(chunk.document_id, 0) + 1
        tokens += int(chunk.token_estimate or 0)
        expected_index = previous_index.get(chunk.document_id, -1) + 1
        document_length = document_lengths.get(chunk.document_id, 0)
        if (
            chunk.tender_id != tender_id
            or chunk.document_id not in ids
            or not value.strip()
            or not isinstance(chunk.text_hash, str)
            or HASH_RE.fullmatch(chunk.text_hash) is None
            or chunk.text_hash.lower()
            != hashlib.sha256(value.encode("utf-8")).hexdigest()
            or chunk.chunk_index < 0
            or chunk.chunk_index != expected_index
            or chunk.char_start < 0
            or chunk.char_end <= chunk.char_start
            or chunk.char_end > document_length
            or (chunk.chunk_index == 0 and chunk.char_start != 0)
            or (
                chunk.document_id in previous_start
                and chunk.char_start < previous_start[chunk.document_id]
            )
            or chunk.token_estimate <= 0
        ):
            valid = False
        previous_start[chunk.document_id] = chunk.char_start
        previous_index[chunk.document_id] = chunk.chunk_index
        ordered.append(
            (
                chunk.document_id,
                chunk.chunk_index,
                chunk.text_hash,
                chunk.char_start,
                chunk.char_end,
                chunk.token_estimate,
            )
        )
    valid = valid and nonempty == len(chunks) and tokens > 0 and all(counts.values())
    if any(document_id not in document_lengths for document_id in ids):
        valid = False
    return (
        valid,
        {
            "chunk_count": len(chunks),
            "nonempty_chunk_count": nonempty,
            "token_estimate": tokens,
            "chunks_by_document": counts,
        },
        ordered,
    )


def _existing_state(data_root, registry, tender, documents, chunks):
    files_valid = True
    for document in documents:
        source, extracted = _expected_paths(data_root, registry, document)
        text_value = _utf8_nonempty(extracted)
        if (
            document.local_path != str(source)
            or document.extracted_text_path != str(extracted)
            or document.download_status not in {"downloaded", "completed", "ready"}
            or document.text_extraction_status != "extracted"
            or not _regular(source)
            or _sha256(source) != document.sha256.lower()
            or text_value is None
            or document.extracted_text_chars != len(text_value)
        ):
            files_valid = False
    chunks_valid, metrics, ordered = _chunk_snapshot(chunks, documents, tender.id)
    return files_valid and chunks_valid, {
        **metrics,
        "files_valid": files_valid,
        "ordered": ordered,
    }


def _backup_documents(path, registry, documents, chunks):
    payload = {
        "registry_number": registry,
        "documents": [
            {
                "id": d.id,
                "local_path": d.local_path,
                "extracted_text_path": d.extracted_text_path,
                "download_status": d.download_status,
                "text_extraction_status": d.text_extraction_status,
                "extracted_text_chars": d.extracted_text_chars,
                "size_bytes": d.size_bytes,
                "sha256": d.sha256,
            }
            for d in documents
        ],
        "chunks": [
            {"id": c.id, "document_id": c.document_id, "text_hash": c.text_hash}
            for c in chunks
        ],
    }
    fd, name = tempfile.mkstemp(
        prefix="r10-1-document-recovery-", suffix=".json", dir=path
    )
    os.close(fd)
    backup = Path(name)
    os.chmod(backup, 0o600)
    backup.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return backup


def _same_filesystem(staging: Path, final_parent: Path) -> bool:
    try:
        return staging.stat().st_dev == final_parent.stat().st_dev
    except OSError:
        return False


class _StagingCleanupError(RuntimeError):
    def __init__(self, error_code: str):
        super().__init__(error_code)
        self.error_code = error_code


def _validate_staging_path(staging: Path, allowed_parent: Path) -> None:
    if staging.parent != allowed_parent or not staging.name.startswith(
        ".r10-1-recovery-staging-"
    ):
        raise _StagingCleanupError("staging_cleanup_path_changed")
    try:
        if staging.is_symlink():
            raise _StagingCleanupError("staging_cleanup_symlink_rejected")
    except OSError as exc:
        raise _StagingCleanupError("staging_cleanup_os_error") from exc


def _repair_staging_permissions(staging: Path) -> None:
    for root, dirs, files in os.walk(staging, topdown=True, followlinks=False):
        root_path = Path(root)
        os.chmod(root_path, 0o700)
        for name in dirs:
            path = root_path / name
            if path.is_symlink():
                continue
            os.chmod(path, 0o700)
        for name in files:
            path = root_path / name
            if path.is_symlink():
                continue
            os.chmod(path, 0o600)


def _cleanup_staging_strict(
    staging: Path, allowed_parent: Path, cleanup_state: dict[str, Any] | None = None
) -> bool:
    _validate_staging_path(staging, allowed_parent)
    if not staging.exists() and not staging.is_symlink():
        return True
    try:
        shutil.rmtree(staging)
    except PermissionError:
        if cleanup_state is not None:
            cleanup_state["staging_cleanup_retry_performed"] = True
        try:
            _validate_staging_path(staging, allowed_parent)
            _repair_staging_permissions(staging)
            shutil.rmtree(staging)
        except PermissionError as exc:
            raise _StagingCleanupError("staging_cleanup_permission_denied") from exc
        except OSError as exc:
            raise _StagingCleanupError("staging_cleanup_os_error") from exc
    except OSError as exc:
        raise _StagingCleanupError("staging_cleanup_os_error") from exc
    try:
        if staging.exists() or staging.is_symlink():
            raise _StagingCleanupError("staging_cleanup_not_empty")
    except OSError as exc:
        raise _StagingCleanupError("staging_cleanup_os_error") from exc
    return True


def _load_explicit_settings(env_file: Path) -> Settings:
    original_environment = os.environ.copy()
    try:
        for key in list(os.environ):
            if key.startswith(("AI_CORP_", "ARVECTUM_ETP_")):
                del os.environ[key]
        get_settings.cache_clear()
        return Settings(_env_file=env_file, _env_file_encoding="utf-8")
    finally:
        os.environ.clear()
        os.environ.update(original_environment)
        get_settings.cache_clear()


def _soap_settings_from_env_file(env_file: Path):
    """Read SOAP settings without leaving the explicit env file in process env."""
    original_environment = os.environ.copy()
    try:
        for key in list(os.environ):
            if key.startswith(("AI_CORP_", "ARVECTUM_ETP_", "ZAKUPKI_GOV_RU_SOAP_")):
                del os.environ[key]
        load_dotenv(env_file, override=True)
        clear_zakupki_soap_settings_cache()
        return get_zakupki_soap_settings()
    finally:
        os.environ.clear()
        os.environ.update(original_environment)
        clear_zakupki_soap_settings_cache()


def _extract_to_stage(document, source, extracted, extractor, config, extract_dir):
    fake = SimpleNamespace(
        text_extraction_status="pending",
        local_path=str(source),
        file_name=document.file_name,
        id=document.id,
        extracted_text_path=None,
        extracted_text_chars=None,
    )
    extractor(fake, extract_dir, config)
    produced = Path(fake.extracted_text_path) if fake.extracted_text_path else None
    if produced is None or not _regular(produced):
        raise RuntimeError("extraction_failed")
    value = _utf8_nonempty(produced)
    if value is None or fake.text_extraction_status != "extracted":
        raise RuntimeError("extraction_output_invalid")
    os.replace(produced, extracted)
    return len(value)


def recover_procurement_documents(
    request: DocumentRecoveryRequest,
    *,
    soap_client_factory: Callable[[Any], Any] = ZakupkiSoapClient,
    extraction_helper: Callable[..., None] = _try_extract,
    chunk_indexer_factory: Callable[..., Any] = DocumentChunkIndexer,
    engine_factory: Callable[..., Any] = create_engine,
    root_validator: Callable[[Path, Path], Path] = validate_data_root,
) -> dict[str, Any]:
    cleanup_state = {
        "staging_created": False,
        "staging_cleanup_attempted": False,
        "staging_cleanup_succeeded": False,
        "staging_cleanup_retry_performed": False,
        "staging_persisted_after_cleanup": False,
        "temporary_diagnostics_used": False,
    }
    try:
        report = _recover_procurement_documents_impl(
            request,
            soap_client_factory=soap_client_factory,
            extraction_helper=extraction_helper,
            chunk_indexer_factory=chunk_indexer_factory,
            engine_factory=engine_factory,
            root_validator=root_validator,
            cleanup_state=cleanup_state,
        )
    except _StagingCleanupError as exc:
        return _report(
            mode="apply" if request.apply else "dry-run",
            classification="STAGING_CLEANUP_FAILED",
            final_status="DOCUMENT_RECOVERY_STAGING_CLEANUP_FAILED",
            error_code=exc.error_code,
            **cleanup_state,
        )
    report.update(cleanup_state)
    return report


def _recover_procurement_documents_impl(
    request: DocumentRecoveryRequest,
    *,
    soap_client_factory: Callable[[Any], Any] = ZakupkiSoapClient,
    extraction_helper: Callable[..., None] = _try_extract,
    chunk_indexer_factory: Callable[..., Any] = DocumentChunkIndexer,
    engine_factory: Callable[..., Any] = create_engine,
    root_validator: Callable[[Path, Path], Path] = validate_data_root,
    cleanup_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a complete dry-run preflight; persistence requires explicit ``apply``."""
    if request.build_chunks and not request.apply:
        raise ValueError("--build-chunks requires --apply")
    data_root = root_validator(request.data_root, request.backup_dir)
    backup_dir = _safe_backup_dir(request.backup_dir, data_root, create=False)
    if request.env_file.is_symlink() or not request.env_file.is_file():
        raise ValueError("env_file must be a regular non-symlink file")
    settings = _load_explicit_settings(request.env_file)
    recovery_config = load_config(settings)
    try:
        trust_policy = policy_from_settings(
            settings,
            base_dir=request.env_file.parent,
        )
    except ETPTrustConfigurationError as exc:
        return _report(
            mode="apply" if request.apply else "dry-run",
            classification="RECOVERY_TRANSPORT_POLICY_INVALID",
            final_status="RECOVERY_TRANSPORT_POLICY_INVALID",
            error_code=_transport_policy_error_code(exc),
        )
    engine = engine_factory(settings.database_url)
    session_factory = sessionmaker(bind=engine)
    soap_settings = _soap_settings_from_env_file(request.env_file)
    transport_base = {
        "transport_policy_ready": True,
        "transport_policy_injected": True,
        "runtime_diagnostics_enabled": False,
    }
    staging = None
    staging_parent = None
    diagnostics_dir = None
    cleanup_state = cleanup_state or {}
    try:
        with session_factory() as session:
            try:
                schema_details = _schema_gate_details(engine)
                revision = schema_details["revision"]
                schema_ready = schema_details["ready"]
            except (SQLAlchemyError, OSError, RuntimeError):
                return _report(
                    mode="apply" if request.apply else "dry-run",
                    classification="SCHEMA_MISMATCH",
                    final_status="DOCUMENT_RECOVERY_SCHEMA_MISMATCH",
                )
            base = {
                "mode": "apply" if request.apply else "dry-run",
                "database_ready": True,
                "alembic_revision": revision,
                **schema_details,
                **transport_base,
            }
            if not schema_ready:
                return _report(
                    **base,
                    classification="SCHEMA_MISMATCH",
                    final_status="DOCUMENT_RECOVERY_SCHEMA_MISMATCH",
                )
            tender = session.execute(
                select(ProcurementTender).where(
                    ProcurementTender.registry_number == request.registry_number
                )
            ).scalar_one_or_none()
            if tender is None:
                return _report(
                    **base, final_status="RECOVERY_PREFLIGHT_IDENTITY_MISMATCH"
                )
            documents = list(
                session.execute(
                    select(ProcurementTenderDocument)
                    .where(ProcurementTenderDocument.tender_id == tender.id)
                    .order_by(ProcurementTenderDocument.id)
                ).scalars()
            )
            chunks = list(
                session.execute(
                    select(ProcurementDocumentChunk).where(
                        ProcurementDocumentChunk.tender_id == tender.id
                    )
                ).scalars()
            )
            hashes = _valid_hashes(documents)
            base["expected_document_count"] = len(documents)
            if not documents or hashes is None:
                return _report(
                    **base,
                    classification="IDENTITY_MISMATCH",
                    final_status="RECOVERY_PREFLIGHT_IDENTITY_MISMATCH",
                )
            existing_valid, existing_metrics = _existing_state(
                data_root, request.registry_number, tender, documents, chunks
            )
            final_dir = (
                data_root / "tender_research" / "procurements" / request.registry_number
            )
            if final_dir.exists():
                if existing_valid:
                    return _report(
                        **base,
                        **existing_metrics,
                        classification="ALREADY_RECOVERED_AND_CHUNKED",
                        final_status="ALREADY_RECOVERED_AND_CHUNKED",
                    )
                if (
                    request.apply
                    and request.build_chunks
                    and existing_metrics["files_valid"]
                ):
                    return _run_chunks_only(
                        request,
                        session,
                        tender,
                        documents,
                        chunks,
                        backup_dir,
                        data_root,
                        settings,
                        revision,
                        chunk_indexer_factory,
                        recovery_config,
                    )
                if existing_metrics["files_valid"]:
                    return _report(
                        **base,
                        **existing_metrics,
                        classification="DOCUMENTS_ALREADY_RESTORED_CHUNKS_NOT_REQUESTED",
                        final_status="DOCUMENTS_ALREADY_RESTORED_CHUNKS_NOT_REQUESTED",
                    )
                return _report(
                    **base,
                    classification="EXISTING_STATE_CONFLICT",
                    final_status="DOCUMENT_RECOVERY_REJECTED",
                )
            final_parent = final_dir.parent
            staging_parent = _nearest_existing_ancestor(final_parent)
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".r10-1-recovery-staging-{uuid.uuid4().hex}-",
                    dir=staging_parent,
                )
            )
            os.chmod(staging, 0o700)
            cleanup_state["staging_created"] = True
            if not _same_filesystem(staging, staging_parent):
                return _report(
                    **base,
                    classification="SAME_FILESYSTEM_REQUIRED",
                    final_status="DOCUMENT_RECOVERY_STAGING_FAILED",
                )
            if hasattr(soap_settings, "configured") and not soap_settings.configured:
                return _report(
                    **base,
                    classification="RECOVERY_TRANSPORT_CONFIGURATION_REJECTED",
                    final_status="RECOVERY_TRANSPORT_CONFIGURATION_REJECTED",
                    error_code="soap_not_configured",
                )
            if (
                hasattr(soap_settings, "token_configured")
                and not soap_settings.token_configured
            ):
                return _report(
                    **base,
                    classification="RECOVERY_TRANSPORT_CONFIGURATION_REJECTED",
                    final_status="RECOVERY_TRANSPORT_CONFIGURATION_REJECTED",
                    error_code="soap_token_missing",
                )
            endpoint_error = _validate_recovery_endpoint(
                soap_settings,
                trust_policy,
                getattr(soap_settings, "active_docs_endpoint", "https://example.test"),
            )
            if endpoint_error:
                return _report(
                    **base,
                    classification="RECOVERY_TRANSPORT_POLICY_INVALID",
                    final_status="RECOVERY_TRANSPORT_POLICY_INVALID",
                    error_code=endpoint_error,
                )
            diagnostics_dir = staging / "soap-diagnostics"
            archive_stage = staging / "archive.zip"
            client = soap_client_factory(
                soap_settings,
                trust_policy=trust_policy,
                diagnostics_dir=diagnostics_dir,
                runtime_status_enabled=False,
            )
            result = client.get_docs_by_reestr_number(request.registry_number)
            base["soap_status"] = result.status
            if result.status != "completed" or not result.archive_url:
                return _report(
                    **base,
                    classification="ARCHIVE_UNAVAILABLE",
                    final_status="RECOVERY_PREFLIGHT_ARCHIVE_REJECTED",
                )
            archive_error = _validate_recovery_endpoint(
                soap_settings, trust_policy, result.archive_url
            )
            if archive_error:
                return _report(
                    **base,
                    classification="RECOVERY_TRANSPORT_POLICY_INVALID",
                    final_status="RECOVERY_TRANSPORT_POLICY_INVALID",
                    error_code="archive_host_not_allowed"
                    if archive_error == "soap_host_not_allowed"
                    else archive_error,
                )
            downloaded = client.download_archive(result.archive_url, staging)
            downloaded_path = staging / downloaded.stored_name
            if not downloaded_path.is_file() or downloaded_path.is_symlink():
                return _report(
                    **base,
                    classification="ARCHIVE_DOWNLOAD_FAILED",
                    final_status="RECOVERY_PREFLIGHT_ARCHIVE_REJECTED",
                )
            os.replace(downloaded_path, archive_stage)
            inventory, matches = inspect_zip(archive_stage)
            if inventory.nested_zip_count:
                return _report(
                    **base,
                    zip_safety_passed=False,
                    classification="NESTED_ARCHIVE_UNSUPPORTED",
                    final_status="NESTED_ARCHIVE_UNSUPPORTED",
                )
            unique = {
                digest: entries[0]
                for digest, entries in matches.items()
                if len(entries) == 1
            }
            missing = len(set(hashes) - set(matches))
            ambiguous = sum(len(entries) > 1 for entries in matches.values())
            extra = max(inventory.regular_file_count - len(unique), 0)
            base.update(
                {
                    "zip_safety_passed": inventory.crc_valid
                    and inventory.unsafe_entries == 0,
                    "unique_exact_match_count": len(unique),
                    "missing_expected_hash_count": missing,
                    "ambiguous_expected_hash_count": ambiguous,
                    "extra_archive_regular_file_count": extra,
                }
            )
            if not base["zip_safety_passed"]:
                return _report(
                    **base,
                    classification="ARCHIVE_UNSAFE_OR_CORRUPT",
                    final_status="RECOVERY_PREFLIGHT_ARCHIVE_REJECTED",
                )
            if missing or ambiguous or extra or len(unique) != len(documents):
                return _report(
                    **base,
                    classification="IDENTITY_MISMATCH",
                    final_status="DOCUMENT_RECOVERY_REJECTED",
                )
            stage_procurement = staging / "procurement"
            source_dir, text_dir, extract_dir = (
                stage_procurement / "source",
                stage_procurement / "extracted",
                staging / "extract-output",
            )
            source_dir.mkdir(mode=0o700, parents=True)
            text_dir.mkdir(mode=0o700)
            extract_dir.mkdir(mode=0o700)
            old_chars = sum(int(d.extracted_text_chars or 0) for d in documents)
            new_chars = 0
            changed = 0
            extraction_success = 0
            for document in documents:
                entry_archive, entry_name, _level = unique[document.sha256.lower()]
                digest = document.sha256.lower()
                source = (
                    source_dir
                    / f"{digest}{Path(document.file_name or entry_name).suffix.lower() or '.xml'}"
                )
                extracted = text_dir / f"{digest}.txt"
                with (
                    zipfile.ZipFile(entry_archive) as archive,
                    archive.open(entry_name) as source_stream,
                    source.open("wb") as target,
                ):
                    for block in iter(lambda: source_stream.read(1 << 20), b""):
                        target.write(block)
                os.chmod(source, 0o600)
                try:
                    chars = _extract_to_stage(
                        document,
                        source,
                        extracted,
                        extraction_helper,
                        recovery_config,
                        extract_dir,
                    )
                except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
                    return _report(
                        **base,
                        extraction_attempted_count=extraction_success + 1,
                        extraction_success_count=extraction_success,
                        extraction_failed_count=1,
                        old_extracted_chars_sum=old_chars,
                        new_extracted_chars_sum=new_chars,
                        char_count_changed_document_count=changed,
                        files_staged_count=extraction_success * 2,
                        classification="EXTRACTION_FAILED",
                        final_status="RECOVERY_PREFLIGHT_EXTRACTION_REJECTED",
                        error_code=type(exc).__name__,
                    )
                os.chmod(extracted, 0o600)
                new_chars += chars
                changed += chars != int(document.extracted_text_chars or 0)
                extraction_success += 1
            base.update(
                {
                    "extraction_attempted_count": len(documents),
                    "extraction_success_count": extraction_success,
                    "extraction_failed_count": 0,
                    "old_extracted_chars_sum": old_chars,
                    "new_extracted_chars_sum": new_chars,
                    "char_count_changed_document_count": changed,
                    "files_staged_count": len(documents) * 2,
                }
            )
            if not request.apply:
                return _report(
                    **base,
                    classification="RECOVERY_PREFLIGHT_READY",
                    final_status="RECOVERY_PREFLIGHT_READY",
                )
            if final_dir.exists():
                return _report(
                    **base,
                    classification="EXISTING_STATE_CONFLICT",
                    final_status="DOCUMENT_RECOVERY_REJECTED",
                )
            return _publish_and_commit(
                data_root,
                request,
                session,
                tender,
                documents,
                chunks,
                stage_procurement,
                final_dir,
                backup_dir,
                settings,
                revision,
                chunk_indexer_factory,
                recovery_config,
                base,
            )
    except (OSError, RuntimeError, ValueError, SQLAlchemyError) as exc:
        return _report(
            mode="apply" if request.apply else "dry-run",
            classification="DOCUMENT_RECOVERY_REJECTED",
            final_status="DOCUMENT_RECOVERY_REJECTED",
            error_code=type(exc).__name__,
        )
    finally:
        try:
            if diagnostics_dir is not None:
                cleanup_state["temporary_diagnostics_used"] = (
                    diagnostics_dir.exists() or diagnostics_dir.is_symlink()
                )
            if staging is not None:
                cleanup_state["staging_cleanup_attempted"] = True
                try:
                    _cleanup_staging_strict(staging, staging_parent, cleanup_state)
                except _StagingCleanupError:
                    cleanup_state["staging_persisted_after_cleanup"] = (
                        staging.exists() or staging.is_symlink()
                    )
                    raise
                cleanup_state["staging_cleanup_succeeded"] = True
                cleanup_state["staging_persisted_after_cleanup"] = (
                    staging.exists() or staging.is_symlink()
                )
        finally:
            engine.dispose()


def _publish_and_commit(
    data_root,
    request,
    session,
    tender,
    documents,
    chunks,
    stage_procurement,
    final_dir,
    backup_dir,
    settings,
    revision,
    indexer_factory,
    recovery_config,
    base,
):
    _safe_backup_dir(backup_dir, data_root, create=True)
    backup = _backup_documents(backup_dir, request.registry_number, documents, chunks)
    published = False
    created_parents: list[Path] = []
    try:
        parent = final_dir.parent
        while not parent.exists():
            created_parents.append(parent)
            parent = parent.parent
        for directory in reversed(created_parents):
            directory.mkdir(mode=0o700)
        if not _same_filesystem(stage_procurement, final_dir.parent):
            raise RuntimeError("staging_filesystem_mismatch")
        os.replace(stage_procurement, final_dir)
        published = True
        base["persistent_filesystem_mutation_performed"] = True
        for document in documents:
            source, extracted = _expected_paths(
                data_root, request.registry_number, document
            )
            document.local_path = str(source)
            document.extracted_text_path = str(extracted)
            document.download_status = "downloaded"
            document.text_extraction_status = "extracted"
            document.extracted_text_chars = len(_utf8_nonempty(extracted) or "")
            document.size_bytes = source.stat().st_size
        metrics_first = metrics_second = {
            "chunk_count": len(chunks),
            "nonempty_chunk_count": 0,
            "token_estimate": 0,
        }
        if request.build_chunks:
            repo = TenderRepository(session)
            indexer = indexer_factory(repo, recovery_config)
            indexer.build_for_tender(tender.id, commit=False)
            first_chunks = list(
                session.execute(
                    select(ProcurementDocumentChunk).where(
                        ProcurementDocumentChunk.tender_id == tender.id
                    )
                ).scalars()
            )
            valid_first, metrics_first, ordered_first = _chunk_snapshot(
                first_chunks, documents, tender.id
            )
            if not valid_first:
                raise RuntimeError("chunk_build_failed")
            indexer.build_for_tender(tender.id, commit=False)
            second_chunks = list(
                session.execute(
                    select(ProcurementDocumentChunk).where(
                        ProcurementDocumentChunk.tender_id == tender.id
                    )
                ).scalars()
            )
            valid_second, metrics_second, ordered_second = _chunk_snapshot(
                second_chunks, documents, tender.id
            )
            if (
                not valid_second
                or metrics_first != metrics_second
                or ordered_first != ordered_second
            ):
                raise RuntimeError("chunk_idempotency_failed")
        session.commit()
        base.update(
            {
                "backup_created": backup.exists(),
                "files_published_count": len(documents) * 2,
                "documents_updated_count": len(documents),
                "chunk_count_before": len(chunks),
                "chunk_count_after_first_build": metrics_first["chunk_count"],
                "chunk_count_after_second_build": metrics_second["chunk_count"],
                "nonempty_chunk_count": metrics_second["nonempty_chunk_count"],
                "token_estimate": metrics_second["token_estimate"],
                "chunk_hashes_stable": request.build_chunks,
                "database_mutation_performed": True,
            }
        )
        status = (
            "DOCUMENTS_RESTORED_AND_CHUNKS_BUILT"
            if request.build_chunks
            else "DOCUMENTS_RESTORED"
        )
        return _report(**base, classification=status, final_status=status)
    except (
        OSError,
        RuntimeError,
        ValueError,
        SQLAlchemyError,
        zipfile.BadZipFile,
    ) as exc:
        session.rollback()
        if published:
            try:
                shutil.rmtree(final_dir)
                base["filesystem_rollback_performed"] = True
            except OSError:
                return _report(
                    **base,
                    classification="DOCUMENT_RECOVERY_ROLLBACK_FAILED",
                    final_status="DOCUMENT_RECOVERY_ROLLBACK_FAILED",
                )
        for directory in created_parents:
            try:
                directory.rmdir()
            except OSError:
                break
        if "staging" in str(exc):
            classification = "DOCUMENT_RECOVERY_STAGING_FAILED"
        else:
            classification = (
                "DOCUMENT_RECOVERY_CHUNK_BUILD_FAILED"
                if "chunk" in str(exc)
                else "DOCUMENT_RECOVERY_DATABASE_COMMIT_FAILED"
            )
        return _report(
            **base, classification=classification, final_status=classification
        )


def _run_chunks_only(
    request,
    session,
    tender,
    documents,
    chunks,
    backup_dir,
    data_root,
    settings,
    revision,
    indexer_factory,
    recovery_config,
):
    base = _report(
        mode="apply",
        database_ready=True,
        alembic_revision=revision,
        expected_document_count=len(documents),
        persistent_filesystem_mutation_performed=False,
    )
    try:
        _safe_backup_dir(backup_dir, data_root, create=True)
        backup = _backup_documents(
            backup_dir, request.registry_number, documents, chunks
        )
        indexer = indexer_factory(TenderRepository(session), recovery_config)
        indexer.build_for_tender(tender.id, commit=False)
        current = list(
            session.execute(
                select(ProcurementDocumentChunk).where(
                    ProcurementDocumentChunk.tender_id == tender.id
                )
            ).scalars()
        )
        valid, metrics, ordered = _chunk_snapshot(current, documents, tender.id)
        if not valid:
            raise RuntimeError("chunk_build_failed")
        indexer.build_for_tender(tender.id, commit=False)
        final = list(
            session.execute(
                select(ProcurementDocumentChunk).where(
                    ProcurementDocumentChunk.tender_id == tender.id
                )
            ).scalars()
        )
        valid2, metrics2, ordered2 = _chunk_snapshot(final, documents, tender.id)
        if not valid2 or metrics != metrics2 or ordered != ordered2:
            raise RuntimeError("chunk_idempotency_failed")
        session.commit()
        return _report(
            **base,
            backup_created=backup.exists(),
            chunk_count_before=len(chunks),
            chunk_count_after_first_build=metrics["chunk_count"],
            chunk_count_after_second_build=metrics2["chunk_count"],
            nonempty_chunk_count=metrics2["nonempty_chunk_count"],
            token_estimate=metrics2["token_estimate"],
            chunk_hashes_stable=True,
            database_mutation_performed=True,
            classification="DOCUMENTS_RESTORED_AND_CHUNKS_BUILT",
            final_status="DOCUMENTS_RESTORED_AND_CHUNKS_BUILT",
        )
    except (OSError, RuntimeError, ValueError, SQLAlchemyError) as exc:
        session.rollback()
        return _report(
            **base,
            classification="DOCUMENT_RECOVERY_CHUNK_BUILD_FAILED",
            final_status="DOCUMENT_RECOVERY_CHUNK_BUILD_FAILED",
            error_code=type(exc).__name__,
        )
