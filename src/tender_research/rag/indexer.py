from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from sqlalchemy import select

from src.tender_research.config import TenderResearchConfig
from src.tender_research.models import ProcurementDocumentChunk
from src.tender_research.rag.chunker import ChunkingConfig, chunk_text
from src.tender_research.rag.embeddings import BaseEmbeddingProvider
from src.tender_research.rag.vector_store import JsonVectorStore
from src.tender_research.repository import TenderRepository


class DocumentChunkIndexer:
    def __init__(self, repo: TenderRepository, config: TenderResearchConfig):
        self._repo = repo
        self._config = config
        self._chunking = ChunkingConfig(
            chunk_size_chars=config.rag_chunk_size_chars,
            overlap_chars=config.rag_chunk_overlap_chars,
            min_chunk_chars=config.rag_min_chunk_chars,
        )

    def build(self, *, limit: int = 100) -> dict:
        summary = {
            "documents_seen": 0,
            "documents_chunked": 0,
            "chunks_created": 0,
            "chunks_skipped_existing": 0,
            "empty_text_skipped": 0,
        }
        documents = self._repo.list_extracted_documents(limit=limit)
        for document in documents:
            summary["documents_seen"] += 1
            text_path = document.extracted_text_path
            if not text_path or not Path(text_path).exists():
                summary["empty_text_skipped"] += 1
                continue
            text = Path(text_path).read_text(encoding="utf-8").strip()
            drafts = chunk_text(text, self._chunking)
            if not drafts:
                summary["empty_text_skipped"] += 1
                continue
            existing_chunks = self._repo.list_document_chunks(document.id)
            existing_by_index = {chunk.chunk_index: chunk for chunk in existing_chunks}
            existing_by_hash = {chunk.text_hash: chunk for chunk in existing_chunks}
            created_for_document = 0
            for draft in drafts:
                existing = existing_by_index.get(draft.chunk_index)
                if existing and existing.text_hash == draft.text_hash:
                    summary["chunks_skipped_existing"] += 1
                    continue
                if draft.text_hash in existing_by_hash:
                    summary["chunks_skipped_existing"] += 1
                    continue
                chunk = self._repo.upsert_document_chunk(
                    {
                        "tender_id": document.tender_id,
                        "document_id": document.id,
                        "chunk_index": draft.chunk_index,
                        "text": draft.text,
                        "text_hash": draft.text_hash,
                        "char_start": draft.char_start,
                        "char_end": draft.char_end,
                        "token_estimate": draft.token_estimate,
                        "source_file_name": document.file_name,
                        "source_text_path": text_path,
                        "raw_meta": {
                            "source": "extracted_text",
                            "text_length": len(text),
                            "document_sha256": document.sha256,
                        },
                    }
                )
                existing_by_index[draft.chunk_index] = chunk
                existing_by_hash[draft.text_hash] = chunk
                summary["chunks_created"] += 1
                created_for_document += 1
            if created_for_document:
                summary["documents_chunked"] += 1
        self._repo._session.commit()
        return summary

    def build_for_tender(self, tender_id: str, *, commit: bool = True) -> dict:
        summary = {
            "documents_seen": 0,
            "documents_chunked": 0,
            "chunks_created": 0,
            "chunks_skipped_existing": 0,
            "empty_text_skipped": 0,
        }
        documents = self._repo.list_extracted_documents_by_tender(tender_id)
        for document in documents:
            summary["documents_seen"] += 1
            text_path = document.extracted_text_path
            if not text_path or not Path(text_path).exists():
                summary["empty_text_skipped"] += 1
                continue
            text = Path(text_path).read_text(encoding="utf-8").strip()
            drafts = chunk_text(text, self._chunking)
            if not drafts:
                summary["empty_text_skipped"] += 1
                continue
            existing_chunks = self._repo.list_document_chunks(document.id)
            existing_by_index = {chunk.chunk_index: chunk for chunk in existing_chunks}
            existing_by_hash = {chunk.text_hash: chunk for chunk in existing_chunks}
            created_for_document = 0
            for draft in drafts:
                existing = existing_by_index.get(draft.chunk_index)
                if existing and existing.text_hash == draft.text_hash:
                    summary["chunks_skipped_existing"] += 1
                    continue
                if draft.text_hash in existing_by_hash:
                    summary["chunks_skipped_existing"] += 1
                    continue
                chunk = self._repo.upsert_document_chunk(
                    {
                        "tender_id": document.tender_id,
                        "document_id": document.id,
                        "chunk_index": draft.chunk_index,
                        "text": draft.text,
                        "text_hash": draft.text_hash,
                        "char_start": draft.char_start,
                        "char_end": draft.char_end,
                        "token_estimate": draft.token_estimate,
                        "source_file_name": document.file_name,
                        "source_text_path": text_path,
                        "raw_meta": {
                            "source": "extracted_text",
                            "text_length": len(text),
                            "document_sha256": document.sha256,
                        },
                    }
                )
                existing_by_index[draft.chunk_index] = chunk
                existing_by_hash[draft.text_hash] = chunk
                summary["chunks_created"] += 1
                created_for_document += 1
            if created_for_document:
                summary["documents_chunked"] += 1
        if commit:
            self._repo._session.commit()
        else:
            self._repo._session.flush()
        return summary


class DocumentEmbeddingIndexer:
    def __init__(
        self,
        repo: TenderRepository,
        config: TenderResearchConfig,
        provider: BaseEmbeddingProvider,
        vector_store: JsonVectorStore,
    ):
        self._repo = repo
        self._config = config
        self._provider = provider
        self._vector_store = vector_store

    def build(self, *, limit: int = 1000) -> dict:
        started = time.perf_counter()
        batch_size = max(1, int(self._config.rag_embeddings_batch_size or 16))
        summary = {
            "chunks_seen": 0,
            "embeddings_created": 0,
            "embeddings_skipped_existing": 0,
            "embeddings_failed": 0,
            "provider": self._provider.provider_name,
            "model": self._provider.model_name,
            "dimension": self._provider.dimension,
            "batch_size": batch_size,
            "elapsed_seconds": 0.0,
            "avg_chunks_per_second": 0.0,
            "last_error": None,
        }
        chunks = list(
            self._repo._session.execute(
                select(ProcurementDocumentChunk)
                .order_by(ProcurementDocumentChunk.created_at.asc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        if not chunks:
            return summary

        embeddings = self._repo.list_document_embeddings(
            provider=self._provider.provider_name,
            model=self._provider.model_name,
            chunk_ids=[chunk.id for chunk in chunks],
        )
        existing_by_chunk_id = {
            embedding.chunk_id: embedding for embedding in embeddings
        }
        pending_chunks: list[ProcurementDocumentChunk] = []

        for chunk in chunks:
            summary["chunks_seen"] += 1
            existing = existing_by_chunk_id.get(chunk.id)
            if (
                existing
                and existing.vector_id
                and self._vector_store.has_vector(existing.vector_id)
            ):
                summary["embeddings_skipped_existing"] += 1
                continue
            pending_chunks.append(chunk)

        for start in range(0, len(pending_chunks), batch_size):
            batch = pending_chunks[start : start + batch_size]
            try:
                vectors = self._provider.embed_texts([chunk.text for chunk in batch])
            except Exception as exc:  # noqa: BLE001 - provider failures are summarized per batch
                summary["embeddings_failed"] += len(batch)
                summary["last_error"] = str(exc)
                continue

            if len(vectors) != len(batch):
                summary["embeddings_failed"] += len(batch)
                summary["last_error"] = (
                    f"Provider returned {len(vectors)} embeddings for batch of {len(batch)} chunks"
                )
                continue

            for chunk, vector in zip(batch, vectors):
                vector_id = chunk.id
                vector_hash = hashlib.sha256(
                    json.dumps(
                        vector, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest()
                self._vector_store.upsert(
                    vector_id,
                    vector,
                    metadata={
                        "chunk_id": chunk.id,
                        "document_id": chunk.document_id,
                        "tender_id": chunk.tender_id,
                        "provider": self._provider.provider_name,
                        "model": self._provider.model_name,
                    },
                )
                self._repo.upsert_document_embedding(
                    {
                        "chunk_id": chunk.id,
                        "provider": self._provider.provider_name,
                        "model": self._provider.model_name,
                        "dimension": len(vector),
                        "vector_id": vector_id,
                        "embedding_path": str(self._vector_store.path),
                        "embedding_hash": vector_hash,
                    }
                )
                summary["embeddings_created"] += 1

        summary["dimension"] = self._provider.dimension
        elapsed = time.perf_counter() - started
        summary["elapsed_seconds"] = round(elapsed, 3)
        summary["avg_chunks_per_second"] = (
            round(summary["chunks_seen"] / elapsed, 3) if elapsed > 0 else 0.0
        )
        self._vector_store.persist()
        self._repo._session.commit()
        return summary

    def build_for_tender(self, tender_id: str) -> dict:
        started = time.perf_counter()
        batch_size = max(1, int(self._config.rag_embeddings_batch_size or 16))
        summary = {
            "chunks_seen": 0,
            "embeddings_created": 0,
            "embeddings_skipped_existing": 0,
            "embeddings_failed": 0,
            "provider": self._provider.provider_name,
            "model": self._provider.model_name,
            "dimension": self._provider.dimension,
            "batch_size": batch_size,
            "elapsed_seconds": 0.0,
            "avg_chunks_per_second": 0.0,
            "last_error": None,
        }
        chunks = self._repo.list_chunks_by_tender(tender_id)
        if not chunks:
            return summary

        embeddings = self._repo.list_document_embeddings(
            provider=self._provider.provider_name,
            model=self._provider.model_name,
            chunk_ids=[chunk.id for chunk in chunks],
        )
        existing_by_chunk_id = {
            embedding.chunk_id: embedding for embedding in embeddings
        }
        pending_chunks: list[ProcurementDocumentChunk] = []

        for chunk in chunks:
            summary["chunks_seen"] += 1
            existing = existing_by_chunk_id.get(chunk.id)
            if (
                existing
                and existing.vector_id
                and self._vector_store.has_vector(existing.vector_id)
            ):
                summary["embeddings_skipped_existing"] += 1
                continue
            pending_chunks.append(chunk)

        for start in range(0, len(pending_chunks), batch_size):
            batch = pending_chunks[start : start + batch_size]
            try:
                vectors = self._provider.embed_texts([chunk.text for chunk in batch])
            except Exception as exc:  # noqa: BLE001 - provider failures are summarized per batch
                summary["embeddings_failed"] += len(batch)
                summary["last_error"] = str(exc)
                continue

            if len(vectors) != len(batch):
                summary["embeddings_failed"] += len(batch)
                summary["last_error"] = (
                    f"Provider returned {len(vectors)} embeddings for batch of {len(batch)} chunks"
                )
                continue

            for chunk, vector in zip(batch, vectors):
                vector_id = chunk.id
                vector_hash = hashlib.sha256(
                    json.dumps(
                        vector, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest()
                self._vector_store.upsert(
                    vector_id,
                    vector,
                    metadata={
                        "chunk_id": chunk.id,
                        "document_id": chunk.document_id,
                        "tender_id": chunk.tender_id,
                        "provider": self._provider.provider_name,
                        "model": self._provider.model_name,
                    },
                )
                self._repo.upsert_document_embedding(
                    {
                        "chunk_id": chunk.id,
                        "provider": self._provider.provider_name,
                        "model": self._provider.model_name,
                        "dimension": len(vector),
                        "vector_id": vector_id,
                        "embedding_path": str(self._vector_store.path),
                        "embedding_hash": vector_hash,
                    }
                )
                summary["embeddings_created"] += 1

        summary["dimension"] = self._provider.dimension
        elapsed = time.perf_counter() - started
        summary["elapsed_seconds"] = round(elapsed, 3)
        summary["avg_chunks_per_second"] = (
            round(summary["chunks_seen"] / elapsed, 3) if elapsed > 0 else 0.0
        )
        self._vector_store.persist()
        self._repo._session.commit()
        return summary
