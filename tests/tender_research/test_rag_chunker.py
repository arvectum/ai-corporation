from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.shared.db.base import Base
from src.tender_research.config import TenderResearchConfig
from src.tender_research.rag.chunker import ChunkingConfig, chunk_text
from src.tender_research.rag.indexer import DocumentChunkIndexer
from src.tender_research.repository import TenderRepository


def _repo_and_config(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    config = TenderResearchConfig(
        data_dir=str(tmp_path),
        rag_chunk_size_chars=200,
        rag_chunk_overlap_chars=40,
        rag_min_chunk_chars=60,
    )
    return TenderRepository(session), config


def test_chunk_text_creates_overlapping_chunks():
    text = ("Требования к составу заявки и условиям участия. " * 20).strip()
    chunks = chunk_text(
        text,
        ChunkingConfig(chunk_size_chars=180, overlap_chars=30, min_chunk_chars=50),
    )

    assert len(chunks) >= 2
    assert chunks[1].char_start < chunks[0].char_end
    assert chunks[0].token_estimate > 0


def test_chunk_text_deduplicates_hashes_and_compacts_indexes():
    repeated = "A" * 60
    final = "B" * 60
    chunks = chunk_text(
        f"{repeated} {repeated} {final}",
        ChunkingConfig(chunk_size_chars=60, overlap_chars=0, min_chunk_chars=10),
    )

    assert [chunk.text for chunk in chunks] == [repeated, final]
    assert [chunk.chunk_index for chunk in chunks] == [0, 1]
    assert len({chunk.text_hash for chunk in chunks}) == len(chunks)
    assert chunks[1].char_start > chunks[0].char_end


def test_chunk_indexer_skips_tiny_or_missing_text(tmp_path):
    repo, config = _repo_and_config(tmp_path)
    tender = repo.upsert_tender({"source": "eis", "external_id": "t-1", "title": "Tender"})
    tiny_text_path = tmp_path / "tiny.txt"
    tiny_text_path.write_text("short text", encoding="utf-8")
    repo.upsert_document(
        {
            "tender_id": tender.id,
            "file_name": "tiny.txt",
            "text_extraction_status": "extracted",
            "extracted_text_path": str(tiny_text_path),
        }
    )

    summary = DocumentChunkIndexer(repo, config).build(limit=10)

    assert summary["documents_seen"] == 1
    assert summary["empty_text_skipped"] == 1
    assert repo.count_document_chunks() == 0


def test_chunk_indexer_is_idempotent(tmp_path):
    repo, config = _repo_and_config(tmp_path)
    tender = repo.upsert_tender({"source": "eis", "external_id": "t-2", "title": "Tender"})
    text_path = tmp_path / "spec.txt"
    text_path.write_text(("Условия оплаты и состав заявки. " * 40).strip(), encoding="utf-8")
    document = repo.upsert_document(
        {
            "tender_id": tender.id,
            "file_name": "spec.txt",
            "text_extraction_status": "extracted",
            "extracted_text_path": str(text_path),
        }
    )

    first = DocumentChunkIndexer(repo, config).build(limit=10)
    second = DocumentChunkIndexer(repo, config).build(limit=10)

    chunks = repo.list_document_chunks(document.id)
    assert first["chunks_created"] == len(chunks)
    assert second["chunks_created"] == 0
    assert second["chunks_skipped_existing"] >= len(chunks)
