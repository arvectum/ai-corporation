import hashlib
import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.modules.production_llm_analysis.document_recovery import (
    DocumentRecoveryRequest,
    _chunk_snapshot,
    _load_explicit_settings,
    _publish,
    _safe_entry,
    _same_filesystem,
    inspect_zip,
    validate_data_root,
)
from src.shared.config.settings import Settings as SharedSettings
from src.shared.db.base import Base
from src.tender_research.config import TenderResearchConfig, load_config
from src.tender_research.models import (
    ProcurementDocumentChunk,
    ProcurementTender,
    ProcurementTenderDocument,
)
from src.tender_research.rag.chunker import chunk_text
from src.tender_research.rag.indexer import DocumentChunkIndexer


def make_zip(path: Path, entries: list[tuple[str, bytes]]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return path


def _recovery_fixture(
    tmp_path: Path,
    monkeypatch,
    content: bytes = b"<doc>text</doc>",
    old_chars: int = 7,
):
    from src.modules.production_llm_analysis import document_recovery as recovery

    database = tmp_path / "recovery.db"
    engine = create_engine(f"sqlite:///{database}", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            ProcurementTender.__table__,
            ProcurementTenderDocument.__table__,
            ProcurementDocumentChunk.__table__,
        ],
    )
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(255))")
        )
        connection.execute(
            text(
                "INSERT INTO alembic_version (version_num) VALUES ('096_add_r8_canonical_snapshot_binding')"
            )
        )
    digest = hashlib.sha256(content).hexdigest()
    with Session(engine) as session:
        tender = ProcurementTender(
            source="eis",
            external_id="external",
            registry_number="registry",
            title="Tender",
        )
        session.add(tender)
        session.flush()
        session.add(
            ProcurementTenderDocument(
                tender_id=tender.id,
                file_name="doc.xml",
                sha256=digest,
                download_status="pending",
                text_extraction_status="pending",
                extracted_text_chars=old_chars,
            )
        )
        session.commit()
    archive = tmp_path / "archive.zip"
    make_zip(archive, [("doc.xml", content)])

    class FakeClient:
        def __init__(self, _settings):
            self.called = False

        def get_docs_by_reestr_number(self, _registry):
            self.called = True
            return SimpleNamespace(
                status="completed", archive_url="https://example.test/archive"
            )

        def download_archive(self, _url, destination):
            target = destination / "download.zip"
            target.write_bytes(archive.read_bytes())
            return SimpleNamespace(stored_name=target.name)

    client = FakeClient(None)

    def extract(fake, output_dir, _config):
        result = output_dir / f"{fake.id}.txt"
        result.write_text("text-ok", encoding="utf-8")
        fake.extracted_text_path = str(result)
        fake.text_extraction_status = "extracted"

    monkeypatch.setattr(
        recovery,
        "Settings",
        lambda **_kwargs: SharedSettings(database_url=f"sqlite:///{database}"),
    )
    monkeypatch.setattr(recovery, "get_zakupki_soap_settings", lambda: object())
    monkeypatch.setattr(
        recovery,
        "_schema_revision",
        lambda _engine: ("096_add_r8_canonical_snapshot_binding", True),
    )
    env_file = tmp_path / "env"
    env_file.write_text("AI_CORP_DATABASE_URL=ignored\n", encoding="utf-8")
    request = DocumentRecoveryRequest(
        "registry", env_file, tmp_path / "data", tmp_path / "backup"
    )
    return recovery, engine, client, extract, request, database


def test_inspect_zip_reports_exact_hash_and_safe_inventory(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "safe.zip", [("one.xml", b"<one/>"), ("two.xml", b"<two/>")]
    )

    inventory, matches = inspect_zip(archive)

    assert inventory.crc_valid
    assert inventory.unsafe_entries == 0
    assert inventory.regular_file_count == 2
    assert len(matches) == 2


@pytest.mark.parametrize("name", ["../escape.xml", "/absolute.xml", "C:/drive.xml"])
def test_inspect_zip_rejects_unsafe_paths(tmp_path: Path, name: str) -> None:
    archive = make_zip(tmp_path / "unsafe.zip", [(name, b"content")])

    inventory, matches = inspect_zip(archive)

    assert inventory.unsafe_entries > 0
    assert not matches


def test_inspect_zip_rejects_symlink_entry(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("link.xml")
    info.external_attr = (0o120777 << 16) | 0xA0000000
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(info, b"target")

    inventory, matches = inspect_zip(archive)

    assert inventory.symlink_entries == 1
    assert inventory.unsafe_entries > 0
    assert not matches


def test_inspect_zip_rejects_encrypted_entry(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "encrypted.zip", [("doc.xml", b"content")])
    data = bytearray(archive.read_bytes())
    local = data.index(b"PK\x03\x04")
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<H", data, local + 6, 1)
    struct.pack_into("<H", data, central + 8, 1)
    archive.write_bytes(data)

    inventory, _ = inspect_zip(archive)

    assert inventory.encrypted_entries == 1
    assert inventory.unsafe_entries > 0


def test_inspect_zip_rejects_duplicate_normalized_paths(tmp_path: Path) -> None:
    archive = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.xml", b"one")
        zf.writestr("a.xml", b"two")

    inventory, _ = inspect_zip(archive)

    assert inventory.duplicate_paths == 1
    assert inventory.unsafe_entries > 0


def test_validate_data_root_rejects_tmp_and_backup_overlap(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        validate_data_root(Path("/tmp/r10-data"), tmp_path / "backup")
    with pytest.raises(ValueError):
        validate_data_root(tmp_path / "data", tmp_path / "data")


def test_validate_data_root_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real-data"
    target.mkdir()
    link = tmp_path / "linked-data"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ValueError, match="non-symlink"):
        validate_data_root(link, tmp_path / "backup")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("doc.xml", True),
        ("nested/doc.xml", True),
        ("../doc.xml", False),
        ("/doc.xml", False),
        ("C:/doc.xml", False),
        ("bad\x00.xml", False),
    ],
)
def test_safe_entry_policy(name: str, expected: bool) -> None:
    assert _safe_entry(name) is expected


def test_inspect_zip_rejects_compression_bomb(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "bomb.zip", [("bomb.xml", b"0" * 500_000)])

    inventory, _ = inspect_zip(archive)

    assert inventory.unsafe_entries > 0


def test_inspect_zip_counts_nested_archive(tmp_path: Path) -> None:
    nested = make_zip(tmp_path / "nested.zip", [("inner.xml", b"<inner/>")])
    archive = tmp_path / "outer.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(nested, "bundle.zip")

    inventory, _ = inspect_zip(archive)

    assert inventory.nested_zip_count == 1
    assert inventory.nested_regular_file_count == 1


def test_inspect_zip_rejects_nested_nested_archive(tmp_path: Path) -> None:
    inner = make_zip(tmp_path / "inner.zip", [("doc.xml", b"doc")])
    nested = tmp_path / "nested.zip"
    with zipfile.ZipFile(nested, "w") as zf:
        zf.write(inner, "inner.zip")
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(nested, "bundle.zip")

    inventory, matches = inspect_zip(outer)

    assert inventory.nested_zip_count == 1
    assert inventory.unsafe_entries > 0
    assert not matches


def test_inspect_zip_rejects_crc_failure(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "crc.zip", [("doc.xml", b"content")])
    data = bytearray(archive.read_bytes())
    with zipfile.ZipFile(archive, "r") as zf:
        info = zf.infolist()[0]
    data[info.header_offset + 30 + len(info.filename)] ^= 0xFF
    archive.write_bytes(data)

    inventory, _ = inspect_zip(archive)

    assert inventory.crc_valid is False


def test_publish_uses_deterministic_replacement(tmp_path: Path) -> None:
    source_stage = tmp_path / "source-stage"
    text_stage = tmp_path / "text-stage"
    source_final = tmp_path / "root" / "source" / "sha.xml"
    text_final = tmp_path / "root" / "extracted" / "sha.txt"
    source_stage.write_bytes(b"xml")
    text_stage.write_text("text", encoding="utf-8")

    _publish(source_stage, text_stage, source_final, text_final)

    assert source_final.read_bytes() == b"xml"
    assert text_final.read_text(encoding="utf-8") == "text"
    assert not source_stage.exists()
    assert not text_stage.exists()


def test_recovery_request_defaults_to_dry_run(tmp_path: Path) -> None:
    request = DocumentRecoveryRequest(
        "registry", tmp_path / "env", tmp_path / "data", tmp_path / "backup"
    )

    assert request.apply is False
    assert request.build_chunks is False


def test_recovery_request_keeps_apply_explicit(tmp_path: Path) -> None:
    request = DocumentRecoveryRequest(
        "registry",
        tmp_path / "env",
        tmp_path / "data",
        tmp_path / "backup",
        apply=True,
    )

    assert request.apply is True
    assert request.build_chunks is False


def test_build_chunks_requires_apply(tmp_path: Path) -> None:
    from src.modules.production_llm_analysis.document_recovery import (
        recover_procurement_documents,
    )

    request = DocumentRecoveryRequest(
        "registry",
        tmp_path / "env",
        tmp_path / "data",
        tmp_path / "backup",
        build_chunks=True,
    )
    with pytest.raises(ValueError, match="requires --apply"):
        recover_procurement_documents(request)


def test_recovery_dry_run_executes_extraction_without_persistent_mutation(
    tmp_path: Path, monkeypatch
) -> None:
    recovery, engine, client, extractor, request, _database = _recovery_fixture(
        tmp_path, monkeypatch
    )

    report = recovery.recover_procurement_documents(
        request,
        soap_client_factory=lambda _settings, **_kwargs: client,
        extraction_helper=extractor,
        root_validator=lambda data, _backup: data.resolve(),
    )

    assert report["final_status"] == "RECOVERY_PREFLIGHT_READY"
    assert report["extraction_attempted_count"] == 1
    assert report["extraction_success_count"] == 1
    assert report["persistent_filesystem_mutation_performed"] is False
    assert not (tmp_path / "data").exists()
    assert not (tmp_path / "backup").exists()
    assert client.called is True
    assert report["staging_created"] is True
    assert report["staging_cleanup_attempted"] is True
    assert report["staging_cleanup_succeeded"] is True
    assert report["staging_cleanup_retry_performed"] is False
    assert report["staging_persisted_after_cleanup"] is False
    assert report["temporary_diagnostics_used"] is False
    assert not list(tmp_path.glob(".r10-1-recovery-staging-*"))
    engine.dispose()


def test_staging_cleanup_does_not_follow_symlink(tmp_path: Path) -> None:
    from src.modules.production_llm_analysis import document_recovery as recovery

    staging = tmp_path / ".r10-1-recovery-staging-test"
    outside = tmp_path / "outside"
    staging.mkdir()
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    (staging / "link").symlink_to(outside, target_is_directory=True)

    assert recovery._cleanup_staging_strict(staging, tmp_path) is True
    assert not staging.exists()
    assert (outside / "keep.txt").exists()


def test_staging_cleanup_rejects_wrong_parent_and_prefix(tmp_path: Path) -> None:
    from src.modules.production_llm_analysis import document_recovery as recovery

    wrong_prefix = tmp_path / "staging"
    wrong_prefix.mkdir()
    with pytest.raises(recovery._StagingCleanupError) as error:
        recovery._cleanup_staging_strict(wrong_prefix, tmp_path)
    assert error.value.error_code == "staging_cleanup_path_changed"
    wrong_prefix.rmdir()

    outside = tmp_path / "outside"
    outside.mkdir()
    valid_name = tmp_path / ".r10-1-recovery-staging-valid"
    valid_name.mkdir()
    with pytest.raises(recovery._StagingCleanupError) as error:
        recovery._cleanup_staging_strict(valid_name, outside)
    assert error.value.error_code == "staging_cleanup_path_changed"
    valid_name.rmdir()


def test_staging_cleanup_performs_one_bounded_permission_retry(
    tmp_path: Path, monkeypatch
) -> None:
    from src.modules.production_llm_analysis import document_recovery as recovery

    staging = tmp_path / ".r10-1-recovery-staging-retry"
    staging.mkdir()
    (staging / "payload.txt").write_text("payload", encoding="utf-8")
    original_rmtree = recovery.shutil.rmtree
    calls = 0

    def fail_once(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("blocked")
        return original_rmtree(path, *args, **kwargs)

    state = {}
    monkeypatch.setattr(recovery.shutil, "rmtree", fail_once)
    assert recovery._cleanup_staging_strict(staging, tmp_path, state) is True
    assert calls == 2
    assert state["staging_cleanup_retry_performed"] is True
    assert not staging.exists()


def test_persistent_staging_cleanup_failure_is_not_success(
    tmp_path: Path, monkeypatch
) -> None:
    recovery, engine, client, extractor, request, _database = _recovery_fixture(
        tmp_path, monkeypatch
    )

    def always_blocked(*_args, **_kwargs):
        raise PermissionError("blocked")

    monkeypatch.setattr(recovery.shutil, "rmtree", always_blocked)
    report = recovery.recover_procurement_documents(
        request,
        soap_client_factory=lambda _settings, **_kwargs: client,
        extraction_helper=extractor,
        root_validator=lambda data, _backup: data.resolve(),
    )

    assert report["classification"] == "STAGING_CLEANUP_FAILED"
    assert report["final_status"] == "DOCUMENT_RECOVERY_STAGING_CLEANUP_FAILED"
    assert report["error_code"] == "staging_cleanup_permission_denied"
    assert report["staging_cleanup_succeeded"] is False
    assert report["staging_cleanup_retry_performed"] is True
    assert report["staging_persisted_after_cleanup"] is True
    assert report["final_status"] != "RECOVERY_PREFLIGHT_READY"
    monkeypatch.undo()
    for staging in tmp_path.glob(".r10-1-recovery-staging-*"):
        recovery.shutil.rmtree(staging)
    engine.dispose()


def test_recovery_dry_run_allows_char_count_drift(tmp_path: Path, monkeypatch) -> None:
    recovery, engine, client, extractor, request, _database = _recovery_fixture(
        tmp_path, monkeypatch, old_chars=999
    )

    report = recovery.recover_procurement_documents(
        request,
        soap_client_factory=lambda _settings, **_kwargs: client,
        extraction_helper=extractor,
        root_validator=lambda data, _backup: data.resolve(),
    )

    assert report["final_status"] == "RECOVERY_PREFLIGHT_READY"
    assert report["char_count_changed_document_count"] == 1
    assert report["old_extracted_chars_sum"] == 999
    assert report["new_extracted_chars_sum"] == 7
    engine.dispose()


def test_recovery_dry_run_fails_closed_on_extraction_failure(
    tmp_path: Path, monkeypatch
) -> None:
    recovery, engine, client, _extractor, request, _database = _recovery_fixture(
        tmp_path, monkeypatch
    )

    def failing_extractor(*_args):
        raise RuntimeError("extractor failed")

    report = recovery.recover_procurement_documents(
        request,
        soap_client_factory=lambda _settings, **_kwargs: client,
        extraction_helper=failing_extractor,
        root_validator=lambda data, _backup: data.resolve(),
    )

    assert report["final_status"] == "RECOVERY_PREFLIGHT_EXTRACTION_REJECTED"
    assert report["extraction_attempted_count"] == 1
    assert report["extraction_success_count"] == 0
    assert report["extraction_failed_count"] == 1
    assert report["provider_called"] is False
    assert not (tmp_path / "data").exists()
    engine.dispose()


def test_apply_without_build_chunks_does_not_call_indexer(
    tmp_path: Path, monkeypatch
) -> None:
    recovery, engine, client, extractor, request, _database = _recovery_fixture(
        tmp_path, monkeypatch
    )
    request = DocumentRecoveryRequest(
        request.registry_number,
        request.env_file,
        request.data_root,
        request.backup_dir,
        apply=True,
        build_chunks=False,
    )

    def forbidden_indexer(*_args, **_kwargs):
        raise AssertionError("chunk indexer must not run")

    report = recovery.recover_procurement_documents(
        request,
        soap_client_factory=lambda _settings, **_kwargs: client,
        extraction_helper=extractor,
        chunk_indexer_factory=forbidden_indexer,
        root_validator=lambda data, _backup: data.resolve(),
    )

    assert report["final_status"] == "DOCUMENTS_RESTORED"
    assert report["database_mutation_performed"] is True
    assert report["chunk_count_after_first_build"] == 0
    assert report["chunk_count_after_second_build"] == 0
    engine.dispose()


@pytest.mark.parametrize("bad_hash", [None, "not-a-sha256"])
def test_recovery_rejects_null_or_nonhex_hash_before_soap(
    tmp_path: Path, monkeypatch, bad_hash: str | None
) -> None:
    recovery, engine, client, extractor, request, _database = _recovery_fixture(
        tmp_path, monkeypatch
    )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE procurement_tender_documents SET sha256 = :sha"),
            {"sha": bad_hash},
        )

    report = recovery.recover_procurement_documents(
        request,
        soap_client_factory=lambda _settings, **_kwargs: client,
        extraction_helper=extractor,
        root_validator=lambda data, _backup: data.resolve(),
    )

    assert report["final_status"] == "RECOVERY_PREFLIGHT_IDENTITY_MISMATCH"
    assert client.called is False
    engine.dispose()


def test_recovery_rejects_schema_revision_mismatch_before_soap(
    tmp_path: Path, monkeypatch
) -> None:
    recovery, engine, client, extractor, request, _database = _recovery_fixture(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(
        recovery,
        "_schema_revision",
        lambda _engine: ("095_old_revision", False),
    )

    report = recovery.recover_procurement_documents(
        request,
        soap_client_factory=lambda _settings, **_kwargs: client,
        extraction_helper=extractor,
        root_validator=lambda data, _backup: data.resolve(),
    )

    assert report["final_status"] == "DOCUMENT_RECOVERY_SCHEMA_MISMATCH"
    assert client.called is False
    engine.dispose()


def test_legacy_chunk_indexer_commits_by_default() -> None:
    class FakeSession:
        def __init__(self):
            self.commits = 0
            self.flushes = 0

        def commit(self):
            self.commits += 1

        def flush(self):
            self.flushes += 1

    class FakeRepo:
        def __init__(self):
            self._session = FakeSession()

        def list_extracted_documents_by_tender(self, _tender_id):
            return []

    repo = FakeRepo()
    indexer = DocumentChunkIndexer(repo, TenderResearchConfig())

    indexer.build_for_tender("tender")
    assert repo._session.commits == 1
    assert repo._session.flushes == 0

    indexer.build_for_tender("tender", commit=False)
    assert repo._session.commits == 1
    assert repo._session.flushes == 1


def _multi_chunk_fixture(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)

    class FakeSession:
        def __init__(self):
            self.commits = 0
            self.flushes = 0

        def commit(self):
            self.commits += 1

        def flush(self):
            self.flushes += 1

    text_value = " ".join(f"word-{index}" for index in range(2_000))
    text_path = tmp_path / "full.txt"
    text_path.write_text(text_value, encoding="utf-8")
    document = SimpleNamespace(
        id="document-1",
        tender_id="tender-1",
        extracted_text_path=str(text_path),
        file_name="document.xml",
        sha256=hashlib.sha256(text_value.encode()).hexdigest(),
    )

    class FakeRepo:
        def __init__(self):
            self._session = FakeSession()
            self.chunks = []

        def list_extracted_documents_by_tender(self, _tender_id):
            return [document]

        def list_document_chunks(self, document_id):
            return [chunk for chunk in self.chunks if chunk.document_id == document_id]

        def upsert_document_chunk(self, data):
            chunk = SimpleNamespace(**data, id=f"chunk-{len(self.chunks)}")
            self.chunks.append(chunk)
            return chunk

    repo = FakeRepo()
    indexer = DocumentChunkIndexer(
        repo,
        TenderResearchConfig(
            rag_chunk_size_chars=1500,
            rag_chunk_overlap_chars=200,
            rag_min_chunk_chars=120,
        ),
    )
    expected_drafts = chunk_text(text_value, indexer._chunking)
    indexer.build_for_tender("tender-1", commit=False)
    assert len(repo.chunks) == len(expected_drafts)
    return document, repo.chunks, text_value


def test_chunk_snapshot_accepts_real_multichunk_bounds(tmp_path: Path) -> None:
    document, chunks, _text_value = _multi_chunk_fixture(tmp_path)

    valid, _metrics, _ordered = _chunk_snapshot(chunks, [document], "tender-1")

    assert valid is True
    assert len(chunks) >= 5
    assert any(chunk.char_start > 0 for chunk in chunks[1:])
    assert any(chunk.char_end > len(chunk.text) for chunk in chunks)


def test_chunk_snapshot_rejects_end_beyond_full_document(tmp_path: Path) -> None:
    document, chunks, text_value = _multi_chunk_fixture(tmp_path)
    chunks[-1].char_end = len(text_value) + 1

    valid, _metrics, _ordered = _chunk_snapshot(chunks, [document], "tender-1")

    assert valid is False


def test_chunk_snapshot_rejects_wrong_hash_duplicate_and_missing_index(
    tmp_path: Path,
) -> None:
    document, chunks, _text_value = _multi_chunk_fixture(tmp_path)
    chunks[1].text_hash = "0" * 64
    valid_hash, _, _ = _chunk_snapshot(chunks, [document], "tender-1")
    assert valid_hash is False

    document, chunks, _text_value = _multi_chunk_fixture(tmp_path / "duplicate")
    chunks[1].chunk_index = chunks[0].chunk_index
    valid_duplicate, _, _ = _chunk_snapshot(chunks, [document], "tender-1")
    assert valid_duplicate is False

    document, chunks, _text_value = _multi_chunk_fixture(tmp_path / "missing")
    chunks[1].chunk_index = chunks[1].chunk_index + 1
    valid_missing, _, _ = _chunk_snapshot(chunks, [document], "tender-1")
    assert valid_missing is False


def test_explicit_settings_win_over_process_environment_and_config_is_shared(
    tmp_path: Path, monkeypatch
) -> None:
    env_file = tmp_path / "explicit.env"
    env_file.write_text(
        "AI_CORP_DATABASE_URL=sqlite:///explicit.db\n"
        "AI_CORP_DOCUMENT_EXTRACT_MAX_CHARS=321\n"
        "AI_CORP_RAG_CHUNK_SIZE_CHARS=777\n"
        "AI_CORP_RAG_CHUNK_OVERLAP_CHARS=33\n"
        "AI_CORP_RAG_MIN_CHUNK_CHARS=44\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_CORP_DATABASE_URL", "sqlite:///wrong.db")
    monkeypatch.setenv("AI_CORP_DOCUMENT_EXTRACT_MAX_CHARS", "999999")
    monkeypatch.setenv("AI_CORP_RAG_CHUNK_SIZE_CHARS", "9999")
    before = dict(__import__("os").environ)

    settings = _load_explicit_settings(env_file)
    config = load_config(settings)

    assert settings.database_url == "sqlite:///explicit.db"
    assert config.document_extract_max_chars == 321
    assert config.rag_chunk_size_chars == 777
    assert config.rag_chunk_overlap_chars == 33
    assert config.rag_min_chunk_chars == 44
    assert dict(__import__("os").environ) == before


def test_explicit_settings_restore_environment_after_exception(
    tmp_path: Path, monkeypatch
) -> None:
    from src.modules.production_llm_analysis import document_recovery as recovery

    env_file = tmp_path / "explicit.env"
    env_file.write_text(
        "AI_CORP_DATABASE_URL=sqlite:///explicit.db\n", encoding="utf-8"
    )
    monkeypatch.setenv("AI_CORP_DATABASE_URL", "sqlite:///wrong.db")
    before = dict(__import__("os").environ)
    original = recovery.Settings

    def fail(**_kwargs):
        raise RuntimeError("settings failure")

    monkeypatch.setattr(recovery, "Settings", fail)
    with pytest.raises(RuntimeError, match="settings failure"):
        recovery._load_explicit_settings(env_file)
    assert dict(__import__("os").environ) == before
    monkeypatch.setattr(recovery, "Settings", original)


def test_lexical_symlink_parent_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        validate_data_root(link / "data", tmp_path / "backup")


def test_same_filesystem_compares_target_device(monkeypatch, tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    target = tmp_path / "target"
    staging.mkdir()
    target.mkdir()
    real_stat = Path.stat

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_dev=result.st_dev + 1)
        return result

    monkeypatch.setattr(Path, "stat", fake_stat)
    assert _same_filesystem(staging, target) is False


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("RECOVERY_PREFLIGHT_READY", 0),
        ("DOCUMENTS_RESTORED", 0),
        ("DOCUMENTS_RESTORED_AND_CHUNKS_BUILT", 0),
        ("DOCUMENTS_ALREADY_RESTORED_CHUNKS_NOT_REQUESTED", 0),
        ("ALREADY_RECOVERED_AND_CHUNKED", 0),
        ("DOCUMENT_RECOVERY_REJECTED", 2),
        ("DOCUMENT_RECOVERY_STAGING_CLEANUP_FAILED", 2),
    ],
)
def test_cli_exit_code_matches_final_status(
    monkeypatch, status: str, expected: int
) -> None:
    from scripts.r10_1 import recover_procurement_documents as cli

    monkeypatch.setattr(
        cli,
        "recover_procurement_documents",
        lambda _request: {"final_status": status},
    )
    monkeypatch.setattr(
        __import__("sys"),
        "argv",
        [
            "recover_procurement_documents.py",
            "--env-file",
            "/explicit.env",
            "--registry-number",
            "registry",
            "--data-root",
            "/data",
            "--backup-dir",
            "/backup",
        ],
    )

    assert cli.main() == expected
