from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.shared.config.settings import get_settings
from src.shared.db.base import Base
from src.shared.storage.gate import check_ingestion_allowed
from src.tender_research.config import load_config
from src.tender_research.document_store import download_tender_documents
from src.tender_research.eis_loader import EisTenderLoader
from src.tender_research.rag.embeddings import build_embedding_provider
from src.tender_research.rag.indexer import DocumentChunkIndexer, DocumentEmbeddingIndexer
from src.tender_research.rag.vector_store import JsonVectorStore
from src.tender_research.repository import TenderRepository

logger = logging.getLogger(__name__)


class TenderPreparationStep:
    def __init__(self, name: str, status: str = "pending", message: str = "", details: str = ""):
        self.name = name
        self.status = status
        self.message = message
        self.details = details


class TenderPreparationResult:
    def __init__(
        self,
        status: str = "completed",
        registry_number: str = "",
        ready_for_analysis: bool = False,
        steps: list | None = None,
        tender_found: bool = False,
        documents_total: int = 0,
        documents_downloaded: int = 0,
        extracted_texts_total: int = 0,
        chunks_total: int = 0,
        chunks_created: int = 0,
        embeddings_total: int = 0,
        embeddings_created: int = 0,
        warnings: list | None = None,
        errors: list | None = None,
        tender_id: str | None = None,
    ):
        self.status = status
        self.registry_number = registry_number
        self.ready_for_analysis = ready_for_analysis
        self.steps = steps or []
        self.tender_found = tender_found
        self.documents_total = documents_total
        self.documents_downloaded = documents_downloaded
        self.extracted_texts_total = extracted_texts_total
        self.chunks_total = chunks_total
        self.chunks_created = chunks_created
        self.embeddings_total = embeddings_total
        self.embeddings_created = embeddings_created
        self.warnings = warnings or []
        self.errors = errors or []
        self.tender_id = tender_id


_DEFAULT_HASH_PROVIDER_NAMES = {"hash", "hashing", "local_hash"}


def _slugify(value: str) -> str:
    import re
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    return slug or "default"


def _vector_store_path(config, *, provider_name: str, model_name: str) -> str:
    if config.rag_vector_store_path:
        raw_path = config.rag_vector_store_path.format(
            provider=_slugify(provider_name),
            model=_slugify(model_name),
        )
        path = Path(raw_path)
    else:
        path = Path(config.data_dir) / "rag" / "vector_store.json"

    provider_alias = (config.rag_embeddings_provider or provider_name).strip().lower()
    if provider_alias in _DEFAULT_HASH_PROVIDER_NAMES and model_name == "local-hash-v1":
        return str(path)

    suffix = path.suffix or ".json"
    stem = path.stem if path.suffix else path.name
    named = f"{stem}__{_slugify(provider_name)}__{_slugify(model_name)}{suffix}"
    return str(path.with_name(named))


def _get_session() -> Session:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import sessionmaker
    return sessionmaker(bind=engine)()


def prepare_tender_for_analysis(
    registry_number: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    limit_documents: int | None = None,
    rebuild_chunks: bool = False,
    rebuild_embeddings: bool = False,
    session: Session | None = None,
    progress_callback=None,
) -> TenderPreparationResult:
    check_ingestion_allowed()

    own_session = False
    if session is None:
        session = _get_session()
        own_session = True

    try:
        steps: list[TenderPreparationStep] = []
        warnings: list[str] = []
        errors: list[str] = []
        config = load_config()

        def emit_progress(progress_percent: int, current_step: str, message: str = "") -> None:
            if progress_callback is None:
                return
            progress_callback(
                {
                    "progress_percent": progress_percent,
                    "current_step": current_step,
                    "message": message,
                    "steps": steps,
                }
            )

        if provider:
            object.__setattr__(config, "rag_embeddings_provider", provider)
            if provider.strip().lower() not in _DEFAULT_HASH_PROVIDER_NAMES:
                object.__setattr__(config, "rag_embedding_dimension", None)
        if model:
            object.__setattr__(config, "rag_embeddings_model", model)
        if base_url:
            object.__setattr__(config, "rag_embeddings_base_url", base_url)

        repo = TenderRepository(session)

        tender = repo.get_tender_by_registry_number(registry_number)
        if not tender:
            step = TenderPreparationStep("check_tender_exists", "in_progress", "Tender not found in database, attempting to ingest...")
            steps.append(step)
            emit_progress(10, "check_tender_exists", step.message)
            try:
                loader = EisTenderLoader(mode="real")
                raw_tender = loader.fetch_by_registry_number(registry_number)
                if raw_tender:
                    tender = repo.upsert_tender({
                        "source": "eis",
                        "external_id": raw_tender.registry_number or raw_tender.external_id,
                        "registry_number": raw_tender.registry_number or registry_number,
                        "title": raw_tender.title,
                        "description": raw_tender.description,
                        "customer_name": raw_tender.customer_name,
                        "customer_inn": raw_tender.customer_inn,
                        "law_type": raw_tender.law_type,
                        "nmck_amount": raw_tender.nmck_amount,
                        "currency": raw_tender.currency,
                        "publication_date": raw_tender.publication_date,
                        "application_deadline": raw_tender.application_deadline,
                        "status": raw_tender.status,
                        "raw_payload": raw_tender.raw_payload,
                    })
                    if raw_tender.documents:
                        for doc in raw_tender.documents:
                            repo.upsert_document({
                                "tender_id": tender.id,
                                "source_document_id": doc.source_document_id,
                                "file_name": doc.file_name,
                                "file_url": doc.file_url,
                                "content_type": doc.content_type,
                                "size_bytes": doc.size_bytes,
                                "raw_meta": doc.raw_meta,
                            })
                    session.commit()
                    step.status = "completed"
                    step.message = f"Tender {registry_number} ingested from EIS"
                    emit_progress(25, "load_or_ingest_tender", step.message)
                else:
                    step.status = "failed"
                    step.message = f"Could not ingest tender {registry_number} from EIS"
                    emit_progress(10, "check_tender_exists", step.message)
                    errors.append(f"Tender {registry_number} not found in database and could not be ingested from EIS")
                    return TenderPreparationResult(
                        status="no_tender",
                        registry_number=registry_number,
                        ready_for_analysis=False,
                        steps=steps,
                        errors=errors,
                    )
            except Exception as e:
                logger.error("Failed to ingest tender %s: %s", registry_number, e)
                step.status = "failed"
                step.message = f"Ingestion failed: {e}"
                emit_progress(10, "check_tender_exists", step.message)
                errors.append(f"Cannot ingest tender: {e}")
                return TenderPreparationResult(
                    status="failed",
                    registry_number=registry_number,
                    ready_for_analysis=False,
                    steps=steps,
                    errors=errors,
                )
        else:
            steps.append(TenderPreparationStep("check_tender_exists", "completed", "Tender found in database"))
            emit_progress(10, "check_tender_exists", "Tender found in database")

        step = TenderPreparationStep("load_or_ingest_tender", "completed", "Tender data loaded")
        steps.append(step)
        emit_progress(25, "load_or_ingest_tender", step.message)

        step = TenderPreparationStep("download_documents", "in_progress", "Starting document download...")
        steps.append(step)
        emit_progress(40, "download_documents", step.message)
        try:
            result = download_tender_documents(repo, tender, config)
            downloaded = result.get("downloaded", 0)
            failed = result.get("failed", 0)
            if downloaded > 0 or failed == 0:
                step.status = "completed"
                step.message = f"Downloaded {downloaded} document(s)"
                step.details = f"failed={failed}" if failed else ""
            elif failed > 0 and downloaded == 0:
                step.status = "warning"
                step.message = f"All {failed} document(s) failed to download"
                step.details = "Check EIS availability or network"
                warnings.append(f"{failed} document(s) failed to download")
            else:
                step.status = "completed"
                step.message = "Documents already downloaded"
            emit_progress(40, "download_documents", step.message)
        except Exception as e:
            logger.error("Document download failed for %s: %s", registry_number, e)
            step.status = "failed"
            step.message = f"Download failed: {e}"
            warnings.append(f"Document download failed: {e}")
            emit_progress(40, "download_documents", step.message)

        step = TenderPreparationStep("extract_text", "in_progress", "Checking extracted text...")
        steps.append(step)
        emit_progress(55, "extract_text", step.message)
        docs_with_text = 0
        for doc in tender.documents:
            if doc.text_extraction_status == "extracted" and doc.extracted_text_path:
                docs_with_text += 1
        if docs_with_text > 0:
            step.status = "completed"
            step.message = f"Text extracted for {docs_with_text} document(s)"
        else:
            step.status = "completed"
            step.message = "No text extracted (documents may have unsupported formats)"
            if not any("document download" in w for w in warnings):
                warnings.append("No documents have extracted text available")
        emit_progress(55, "extract_text", step.message)

        emb_provider = build_embedding_provider(config)
        vector_store = JsonVectorStore(
            _vector_store_path(config, provider_name=emb_provider.provider_name, model_name=emb_provider.model_name),
            dimension=emb_provider.dimension or None,
        )

        chunk_indexer = DocumentChunkIndexer(repo, config)
        step = TenderPreparationStep("build_chunks", "in_progress", "Building chunks...")
        steps.append(step)
        emit_progress(70, "build_chunks", step.message)
        chunks_existing = repo.count_chunks_by_tender(tender.id)
        if chunks_existing > 0 and not rebuild_chunks:
            step.status = "skipped"
            step.message = f"Chunks already exist ({chunks_existing})"
        else:
            try:
                chunk_result = chunk_indexer.build_for_tender(tender.id)
                chunks_created = chunk_result.get("chunks_created", 0)
                chunks_skipped = chunk_result.get("chunks_skipped_existing", 0)
                if chunks_created > 0:
                    step.status = "completed"
                    step.message = f"Created {chunks_created} chunk(s)"
                    step.details = f"skipped_existing={chunks_skipped}" if chunks_skipped else ""
                elif chunks_skipped > 0:
                    step.status = "skipped"
                    step.message = f"Chunks already exist ({chunks_skipped} skipped)"
                else:
                    step.status = "completed"
                    step.message = "No chunks created"
                    if docs_with_text == 0:
                        warnings.append("No chunks created because no documents have extracted text")
            except Exception as e:
                logger.error("Chunk build failed for %s: %s", registry_number, e)
                step.status = "failed"
                step.message = f"Chunk build failed: {e}"
                warnings.append(f"Chunk build failed: {e}")
        emit_progress(70, "build_chunks", step.message)

        emb_indexer = DocumentEmbeddingIndexer(repo, config, emb_provider, vector_store)
        step = TenderPreparationStep("build_embeddings", "in_progress", "Building embeddings...")
        steps.append(step)
        emit_progress(90, "build_embeddings", step.message)
        emb_existing = repo.count_embeddings_by_tender(emb_provider.provider_name, emb_provider.model_name, tender.id)
        chunks_count = repo.count_chunks_by_tender(tender.id)
        if emb_existing >= chunks_count and not rebuild_embeddings:
            step.status = "skipped"
            step.message = f"Embeddings already exist ({emb_existing})"
        elif chunks_count == 0:
            step.status = "skipped"
            step.message = "No chunks to embed"
        else:
            try:
                emb_result = emb_indexer.build_for_tender(tender.id)
                emb_created = emb_result.get("embeddings_created", 0)
                emb_failed = emb_result.get("embeddings_failed", 0)
                last_error = emb_result.get("last_error")
                if emb_failed > 0 and emb_created == 0:
                    step.status = "failed"
                    step.message = f"Embedding failed: {last_error or 'unknown error'}"
                    warnings.append(f"Embedding failed: {last_error}")
                elif emb_created > 0:
                    step.status = "completed"
                    step.message = f"Created {emb_created} embedding(s)"
                    step.details = f"failed={emb_failed}" if emb_failed else ""
                else:
                    step.status = "skipped"
                    step.message = "Embeddings already exist"
            except Exception as e:
                logger.error("Embedding build failed for %s: %s", registry_number, e)
                step.status = "failed"
                step.message = f"Embedding build failed: {e}"
                warnings.append(f"Embedding build failed: {e}")
        emit_progress(90, "build_embeddings", step.message)

        step = TenderPreparationStep("readiness_check", "in_progress", "Checking readiness...")
        steps.append(step)
        emit_progress(100, "readiness_check", step.message)
        final_chunks = repo.count_chunks_by_tender(tender.id)
        final_embeddings = repo.count_embeddings_by_tender(emb_provider.provider_name, emb_provider.model_name, tender.id)
        final_docs_with_text = repo.count_extracted_documents_by_tender(tender.id)
        ready = final_chunks > 0 and final_embeddings > 0

        if ready:
            step.status = "completed"
            step.message = f"Ready for analysis (chunks={final_chunks}, embeddings={final_embeddings})"
        elif final_chunks > 0 and final_embeddings == 0:
            step.status = "warning"
            step.message = "Chunks exist but no embeddings"
            warnings.append("Chunks built but embeddings are missing")
        elif final_chunks == 0 and final_embeddings == 0:
            step.status = "failed"
            step.message = "No chunks or embeddings available"
            errors.append("No chunks or embeddings available for analysis")
        else:
            step.status = "warning"
            step.message = f"Incomplete: chunks={final_chunks}, embeddings={final_embeddings}"
            warnings.append(f"Incomplete preparation: chunks={final_chunks}, embeddings={final_embeddings}")
        emit_progress(100, "readiness_check", step.message)

        overall_status = "completed" if ready else "completed_with_warnings"
        if errors:
            overall_status = "failed"

        return TenderPreparationResult(
            status=overall_status,
            registry_number=registry_number,
            ready_for_analysis=ready,
            steps=steps,
            tender_found=True,
            documents_total=len(tender.documents),
            documents_downloaded=sum(1 for d in tender.documents if d.download_status == "downloaded"),
            extracted_texts_total=final_docs_with_text,
            chunks_total=final_chunks,
            chunks_created=sum(s.name == "build_chunks" and s.status == "completed" for s in steps),
            embeddings_total=final_embeddings,
            embeddings_created=sum(s.name == "build_embeddings" and s.status == "completed" for s in steps) if final_embeddings > 0 else 0,
            warnings=warnings,
            errors=errors,
            tender_id=tender.id,
        )
    finally:
        if own_session:
            session.close()


def check_preparation_status(
    registry_number: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    session: Session | None = None,
) -> dict:
    own_session = False
    if session is None:
        session = _get_session()
        own_session = True

    try:
        config = load_config()
        if provider:
            object.__setattr__(config, "rag_embeddings_provider", provider)
        if model:
            object.__setattr__(config, "rag_embeddings_model", model)

        repo = TenderRepository(session)
        emb_provider = build_embedding_provider(config)
        tender = repo.get_tender_by_registry_number(registry_number)

        if not tender:
            return {
                "registry_number": registry_number,
                "tender_found": False,
                "documents_total": 0,
                "documents_downloaded": 0,
                "extracted_texts_total": 0,
                "chunks_total": 0,
                "embeddings_total": 0,
                "ready_for_analysis": False,
                "missing": ["tender"],
            }

        docs_total = len(tender.documents)
        docs_downloaded = sum(1 for d in tender.documents if d.download_status == "downloaded")
        docs_with_text = repo.count_extracted_documents_by_tender(tender.id)
        chunks_total = repo.count_chunks_by_tender(tender.id)
        embeddings_total = repo.count_embeddings_by_tender(
            emb_provider.provider_name, emb_provider.model_name, tender.id
        )

        missing = []
        if not tender:
            missing.append("tender")
        if docs_total == 0:
            missing.append("documents")
        if docs_with_text == 0:
            missing.append("extracted_text")
        if chunks_total == 0:
            missing.append("chunks")
        if embeddings_total == 0:
            missing.append("embeddings")

        ready = chunks_total > 0 and embeddings_total > 0

        return {
            "registry_number": registry_number,
            "tender_found": True,
            "documents_total": docs_total,
            "documents_downloaded": docs_downloaded,
            "extracted_texts_total": docs_with_text,
            "chunks_total": chunks_total,
            "embeddings_total": embeddings_total,
            "ready_for_analysis": ready,
            "missing": missing,
        }
    finally:
        if own_session:
            session.close()
