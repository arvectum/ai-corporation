"""Controlled, provider-neutral recovery of one procurement's documents."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from src.modules.tender_operator_agent_demo.settings import (
    clear_zakupki_soap_settings_cache,
    get_zakupki_soap_settings,
)
from src.modules.tender_operator_agent_demo.zakupki_soap_client import ZakupkiSoapClient
from src.shared.config.settings import Settings, get_settings
from src.tender_research.config import load_config
from src.tender_research.document_store import _try_extract
from src.tender_research.models import (
    ProcurementDocumentChunk,
    ProcurementTender,
    ProcurementTenderDocument,
)
from src.tender_research.rag.indexer import DocumentChunkIndexer
from src.tender_research.repository import TenderRepository

MAX_ENTRIES = 5_000
MAX_TOTAL_UNCOMPRESSED = 1 << 30
MAX_ENTRY_UNCOMPRESSED = 200 << 20
MAX_RATIO = 200


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
    entries: list[tuple[str, str, int]] | None = None


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


def inspect_zip(path: Path) -> tuple[ZipInventory, dict[str, list[tuple[str, str]]]]:
    """Inspect an archive and return hash -> (entry name, level) matches."""
    inventory = ZipInventory(entries=[])
    matches: dict[str, list[tuple[str, str]]] = {}
    seen: set[str] = set()

    def scan(zip_path: Path, level: int) -> None:
        with zipfile.ZipFile(zip_path) as archive:
            inventory.entry_count += len(archive.infolist())
            if inventory.entry_count > MAX_ENTRIES:
                inventory.unsafe_entries += 1
                return
            for info in archive.infolist():
                if info.is_dir():
                    inventory.directory_count += 1
                    continue
                inventory.regular_file_count += 1
                if level == 1:
                    inventory.nested_regular_file_count += 1
                inventory.compressed_bytes += info.compress_size
                inventory.uncompressed_bytes += info.file_size
                unsafe = not _safe_entry(info.filename)
                normalized = str(PurePosixPath(info.filename.replace("\\", "/")))
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
                if level >= 1 and info.filename.lower().endswith(".zip"):
                    inventory.unsafe_entries += 1
                    continue
                if level == 0 and info.filename.lower().endswith(".zip"):
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
                with archive.open(info) as source:
                    for block in iter(lambda: source.read(1 << 20), b""):
                        digest.update(block)
                matches.setdefault(digest.hexdigest(), []).append(
                    (info.filename, str(level))
                )
                inventory.entries.append((str(zip_path), info.filename, level))
            try:
                inventory.crc_valid = inventory.crc_valid and archive.testzip() is None
            except (OSError, zipfile.BadZipFile, RuntimeError):
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


def validate_data_root(data_root: Path, backup_dir: Path) -> Path:
    if not data_root.is_absolute() or data_root.is_symlink():
        raise ValueError("data_root must be an absolute non-symlink path")
    resolved = data_root.resolve(strict=False)
    tmp_root = Path("/tmp").resolve()
    if resolved == tmp_root or tmp_root in resolved.parents:
        raise ValueError("data_root cannot be inside /tmp")
    if (resolved / ".git").exists() or resolved == backup_dir.resolve(strict=False):
        raise ValueError("data_root is not an approved root")
    return resolved


def _safe_backup_dir(path: Path, data_root: Path, *, create: bool) -> Path:
    if (
        path.is_symlink()
        or path.resolve(strict=False) == data_root
        or data_root in path.resolve(strict=False).parents
    ):
        raise ValueError("backup_dir must be outside data_root")
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    return path


def _document_layout(
    data_root: Path, registry: str, digest: str, extension: str
) -> tuple[Path, Path]:
    root = data_root / "tender_research" / "procurements" / registry
    source = root / "source" / f"{digest}{extension.lower()}"
    extracted = root / "extracted" / f"{digest}.txt"
    return source, extracted


def _publish(
    source_stage: Path, text_stage: Path, source_final: Path, text_final: Path
) -> None:
    source_final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text_final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(source_stage, 0o600)
    os.chmod(text_stage, 0o600)
    os.replace(source_stage, source_final)
    os.replace(text_stage, text_final)


def _backup_documents(
    path: Path,
    registry: str,
    documents: list[ProcurementTenderDocument],
    chunks: list[ProcurementDocumentChunk],
) -> Path:
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
                "created_at": d.created_at.isoformat(),
                "updated_at": d.updated_at.isoformat(),
            }
            for d in documents
        ],
        "chunks": [
            {"id": c.id, "document_id": c.document_id, "text_hash": c.text_hash}
            for c in chunks
        ],
    }
    fd, filename = tempfile.mkstemp(
        prefix="r10-1-document-recovery-", suffix=".json", dir=path
    )
    os.close(fd)
    backup = Path(filename)
    os.chmod(backup, 0o600)
    backup.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return backup


def _report(**values: Any) -> dict[str, Any]:
    values.setdefault("provider_called", False)
    return values


def recover_procurement_documents(
    request: DocumentRecoveryRequest,
    *,
    soap_client_factory: Callable[[Any], Any] = ZakupkiSoapClient,
    extraction_helper: Callable[..., None] = _try_extract,
    chunk_indexer_factory: Callable[..., Any] = DocumentChunkIndexer,
    engine_factory: Callable[..., Any] = create_engine,
) -> dict[str, Any]:
    """Recover one tender; default mode is a non-mutating dry-run."""
    if request.build_chunks and not request.apply:
        raise ValueError("--build-chunks requires --apply")
    data_root = validate_data_root(request.data_root, request.backup_dir)
    if request.apply:
        data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(data_root, 0o700)
    backup_dir = _safe_backup_dir(request.backup_dir, data_root, create=request.apply)
    if request.env_file.is_symlink() or not request.env_file.is_file():
        raise ValueError("env_file must be a regular non-symlink file")
    settings = Settings(_env_file=request.env_file, _env_file_encoding="utf-8")
    load_dotenv(request.env_file, override=True)
    get_settings.cache_clear()
    clear_zakupki_soap_settings_cache()
    engine = engine_factory(settings.database_url)
    SessionFactory = sessionmaker(bind=engine)
    soap_settings = get_zakupki_soap_settings()
    staging = Path(tempfile.mkdtemp(prefix="r10-1-recovery-"))
    try:
        with SessionFactory() as session:
            revision = session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            tender = session.execute(
                select(ProcurementTender).where(
                    ProcurementTender.registry_number == request.registry_number
                )
            ).scalar_one_or_none()
            if tender is None:
                return _report(
                    mode="apply" if request.apply else "dry-run",
                    database_ready=True,
                    alembic_revision=revision,
                    classification="DOCUMENT_RECOVERY_REJECTED",
                    final_status="RECOVERY_PREFLIGHT_IDENTITY_MISMATCH",
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
            hashes = [d.sha256.lower() for d in documents]
            if (
                len(documents) == 0
                or any(len(h) != 64 for h in hashes)
                or len(hashes) != len(set(hashes))
            ):
                return _report(
                    mode="apply" if request.apply else "dry-run",
                    database_ready=True,
                    alembic_revision=revision,
                    expected_document_count=len(documents),
                    classification="IDENTITY_MISMATCH",
                    final_status="RECOVERY_PREFLIGHT_IDENTITY_MISMATCH",
                )
            if (
                request.apply
                and all(
                    d.local_path
                    and d.extracted_text_path
                    and Path(d.local_path).is_file()
                    and not Path(d.local_path).is_symlink()
                    and _sha256(Path(d.local_path)) == d.sha256.lower()
                    and Path(d.extracted_text_path).is_file()
                    and not Path(d.extracted_text_path).is_symlink()
                    and len(Path(d.extracted_text_path).read_text(encoding="utf-8"))
                    == d.extracted_text_chars
                    for d in documents
                )
                and (not request.build_chunks or bool(chunks))
            ):
                return _report(
                    mode="apply",
                    database_ready=True,
                    alembic_revision=revision,
                    expected_document_count=len(documents),
                    chunk_count_before=len(chunks),
                    classification="ALREADY_RECOVERED_AND_CHUNKED",
                    final_status="ALREADY_RECOVERED_AND_CHUNKED",
                )
            client = soap_client_factory(soap_settings)
            result = client.get_docs_by_reestr_number(request.registry_number)
            if result.status != "completed" or not result.archive_url:
                return _report(
                    mode="apply" if request.apply else "dry-run",
                    database_ready=True,
                    alembic_revision=revision,
                    soap_status=result.status,
                    classification="ARCHIVE_UNAVAILABLE",
                    final_status="RECOVERY_PREFLIGHT_ARCHIVE_REJECTED",
                )
            downloaded = client.download_archive(result.archive_url, staging)
            archive = staging / downloaded.stored_name
            if not archive.is_file() or archive.is_symlink():
                return _report(
                    mode="apply" if request.apply else "dry-run",
                    soap_status=result.status,
                    classification="ARCHIVE_DOWNLOAD_FAILED",
                    final_status="RECOVERY_PREFLIGHT_ARCHIVE_REJECTED",
                )
            inventory, matches = inspect_zip(archive)
            unique = {
                h: entries[0] for h, entries in matches.items() if len(entries) == 1
            }
            missing = len(set(hashes) - set(matches))
            ambiguous = sum(len(v) > 1 for v in matches.values())
            extra = max(inventory.regular_file_count - len(unique), 0)
            safety = inventory.crc_valid and inventory.unsafe_entries == 0
            if not safety:
                return _report(
                    mode="apply" if request.apply else "dry-run",
                    database_ready=True,
                    alembic_revision=revision,
                    soap_status=result.status,
                    archive_size_bytes=archive.stat().st_size,
                    zip_safety_passed=False,
                    expected_document_count=len(documents),
                    unique_exact_match_count=len(unique),
                    missing_expected_hash_count=missing,
                    ambiguous_expected_hash_count=ambiguous,
                    extra_archive_regular_file_count=extra,
                    classification="ARCHIVE_UNSAFE_OR_CORRUPT",
                    final_status="RECOVERY_PREFLIGHT_ARCHIVE_REJECTED",
                )
            if request.apply and (
                missing or ambiguous or extra or len(unique) != len(documents)
            ):
                return _report(
                    mode="apply",
                    database_ready=True,
                    alembic_revision=revision,
                    soap_status=result.status,
                    zip_safety_passed=True,
                    classification="IDENTITY_MISMATCH",
                    final_status="DOCUMENT_RECOVERY_REJECTED",
                )
            if not request.apply:
                return _report(
                    mode="dry-run",
                    database_ready=True,
                    alembic_revision=revision,
                    soap_status=result.status,
                    zip_safety_passed=True,
                    expected_document_count=len(documents),
                    unique_exact_match_count=len(unique),
                    missing_expected_hash_count=missing,
                    ambiguous_expected_hash_count=ambiguous,
                    extra_archive_regular_file_count=extra,
                    classification="RECOVERY_PREFLIGHT_READY"
                    if len(unique) == len(documents) and not missing and not ambiguous
                    else "RECOVERY_PREFLIGHT_IDENTITY_MISMATCH",
                    final_status="RECOVERY_PREFLIGHT_READY"
                    if len(unique) == len(documents) and not missing and not ambiguous
                    else "RECOVERY_PREFLIGHT_IDENTITY_MISMATCH",
                )
            return _apply_recovery(
                request,
                session,
                tender,
                documents,
                chunks,
                archive,
                unique,
                data_root,
                backup_dir,
                extraction_helper,
                chunk_indexer_factory,
                settings,
                revision,
                result.status,
                inventory,
            )
    finally:
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
        engine.dispose()


def _apply_recovery(
    request,
    session,
    tender,
    documents,
    chunks,
    archive,
    unique,
    data_root,
    backup_dir,
    extraction_helper,
    chunk_indexer_factory,
    settings,
    revision,
    soap_status,
    inventory,
):
    backup = _backup_documents(backup_dir, request.registry_number, documents, chunks)
    config = load_config()
    staged: list[tuple[Path, Path, ProcurementTenderDocument, int]] = []
    try:
        for document in documents:
            entry_name, _level = unique[document.sha256.lower()]
            source_stage = Path(
                tempfile.mkstemp(
                    prefix="r10-1-source-",
                    suffix=Path(entry_name).suffix.lower(),
                    dir="/tmp",
                )[1]
            )
            os.chmod(source_stage, 0o600)
            with (
                zipfile.ZipFile(archive) as z,
                z.open(entry_name) as source,
                source_stage.open("wb") as target,
            ):
                for block in iter(lambda: source.read(1 << 20), b""):
                    target.write(block)
            digest = _sha256(source_stage)
            extension = Path(entry_name).suffix.lower() or ".xml"
            source_final, text_final = _document_layout(
                data_root, request.registry_number, digest, extension
            )
            text_stage = Path(tempfile.mkstemp(prefix="r10-1-text-", dir="/tmp")[1])
            os.chmod(text_stage, 0o600)
            fake = SimpleNamespace(
                text_extraction_status="pending",
                local_path=str(source_stage),
                file_name=f"{digest}{extension}",
                id=document.id,
                extracted_text_path=None,
                extracted_text_chars=None,
            )
            extraction_helper(fake, text_stage.parent, config)
            produced = (
                Path(fake.extracted_text_path) if fake.extracted_text_path else None
            )
            if (
                fake.text_extraction_status != "extracted"
                or produced is None
                or not produced.is_file()
            ):
                raise RuntimeError("extraction_failed")
            os.replace(produced, text_stage)
            chars = len(text_stage.read_text(encoding="utf-8"))
            staged.append((source_stage, text_stage, document, chars))
        published = []
        for source_stage, text_stage, document, chars in staged:
            source_final, text_final = _document_layout(
                data_root,
                request.registry_number,
                document.sha256.lower(),
                Path(source_stage).suffix,
            )
            _publish(source_stage, text_stage, source_final, text_final)
            published.extend([source_final, text_final])
            document.local_path = str(source_final)
            document.extracted_text_path = str(text_final)
            document.download_status = "downloaded"
            document.text_extraction_status = "extracted"
            document.extracted_text_chars = chars
            document.size_bytes = source_final.stat().st_size
        session.commit()
        chunk_before = len(chunks)
        after_first = after_second = chunk_before
        stable = True
        if request.build_chunks:
            repo = TenderRepository(session)
            indexer = chunk_indexer_factory(repo, config)
            indexer.build_for_tender(tender.id)
            session.expire_all()
            after_first = len(
                session.execute(
                    select(ProcurementDocumentChunk).where(
                        ProcurementDocumentChunk.tender_id == tender.id
                    )
                )
                .scalars()
                .all()
            )
            first_hashes = [
                c.text_hash
                for c in session.execute(
                    select(ProcurementDocumentChunk)
                    .where(ProcurementDocumentChunk.tender_id == tender.id)
                    .order_by(
                        ProcurementDocumentChunk.document_id,
                        ProcurementDocumentChunk.chunk_index,
                    )
                ).scalars()
            ]
            indexer.build_for_tender(tender.id)
            session.expire_all()
            final_chunks = (
                session.execute(
                    select(ProcurementDocumentChunk)
                    .where(ProcurementDocumentChunk.tender_id == tender.id)
                    .order_by(
                        ProcurementDocumentChunk.document_id,
                        ProcurementDocumentChunk.chunk_index,
                    )
                )
                .scalars()
                .all()
            )
            after_second = len(final_chunks)
            stable = first_hashes == [c.text_hash for c in final_chunks]
        return _report(
            mode="apply",
            database_ready=True,
            alembic_revision=revision,
            soap_status=soap_status,
            backup_created=True,
            files_published=len(published),
            documents_updated=len(documents),
            chunk_count_before=chunk_before,
            chunk_count_after_first_build=after_first,
            chunk_count_after_second_build=after_second,
            chunk_hashes_stable=stable,
            classification="DOCUMENTS_RESTORED_AND_CHUNKS_BUILT"
            if request.build_chunks and stable
            else "DOCUMENTS_RESTORED",
            final_status="DOCUMENTS_RESTORED_AND_CHUNKS_BUILT"
            if request.build_chunks and stable
            else "DOCUMENTS_RESTORED",
        )
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile):
        session.rollback()
        for source_stage, text_stage, _, _ in staged:
            source_stage.unlink(missing_ok=True)
            text_stage.unlink(missing_ok=True)
        return _report(
            mode="apply",
            database_ready=True,
            alembic_revision=revision,
            backup_created=backup.exists(),
            classification="DOCUMENT_RECOVERY_DATABASE_COMMIT_FAILED",
            final_status="DOCUMENT_RECOVERY_DATABASE_COMMIT_FAILED",
        )
