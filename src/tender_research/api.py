from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from src.shared.config.settings import get_settings
from src.shared.db.diagnostics import get_database_diagnostics, masked_database_url
from src.shared.errors import AppError
from src.shared.storage.gate import check_ingestion_allowed, IngestionBlockedError
from src.shared.db.base import Base
from src.tender_research.config import load_config
from src.tender_research.rag.job_runner import submit_analyze_job, submit_prepare_job
from src.tender_research.rag.job_schemas import JobListResponse, JobStatusResponse, StartJobResponse
from src.tender_research.rag.job_service import create_job, get_job, list_jobs
from src.tender_research.rag.analysis_service import analyze_tender
from src.tender_research.rag.export_service import export_analysis_report_docx, export_analysis_report_pdf
from src.tender_research.rag.history_service import (
    get_analysis_run,
    get_analysis_run_report,
    get_latest_analysis_report as get_latest_report,
    list_analysis_runs,
)
from src.tender_research.rag.prepare_service import check_preparation_status, prepare_tender_for_analysis
from src.tender_research.rag.schemas import DEFAULT_ANALYSIS_MODE, TenderAnalysisResult

router = APIRouter(prefix="/api/tender-research", tags=["tender-research"])
logger = logging.getLogger(__name__)


class AnalyzeRequest(BaseModel):
    registry_number: str = Field(..., description="Registry number of the tender")
    provider: str | None = Field(default=None, description="Embedding provider override")
    model: str | None = Field(default=None, description="Embedding model override")
    base_url: str | None = Field(default=None, description="Embedding server base URL override")
    use_llm: bool = Field(default=False, description="Enable local LLM for analysis")
    llm_base_url: str | None = Field(default=None, description="Local LLM base URL override")
    llm_model: str | None = Field(default=None, description="Local LLM model override")
    llm_timeout_seconds: int | None = Field(default=None, description="Per-section LLM timeout override", ge=1, le=1800)
    analysis_mode: Literal["fast", "balanced", "detailed"] = Field(default=DEFAULT_ANALYSIS_MODE)
    limit: int | None = Field(default=None, description="Retrieval top-k override per section", ge=1, le=50)
    max_context_chars_per_section: int | None = Field(default=None, description="Context budget override per section", ge=500, le=50000)
    max_chunks_per_section: int | None = Field(default=None, description="Chunk count override per section", ge=1, le=50)
    save_report: bool = Field(default=False, description="Save report markdown to disk")


class SectionSchema(BaseModel):
    id: str
    title: str
    question: str
    answer: str
    sources: list[dict] = []
    status: str = "completed"


class SourceSchema(BaseModel):
    chunk_id: str
    registry_number: str | None
    tender_title: str
    customer_name: str | None
    document_id: str
    document_file_name: str
    score: float
    quote_preview: str


class PrepareRequest(BaseModel):
    registry_number: str = Field(..., description="Registry number of the tender")
    provider: str | None = Field(default=None, description="Embedding provider override")
    model: str | None = Field(default=None, description="Embedding model override")
    base_url: str | None = Field(default=None, description="Embedding server base URL override")
    limit_documents: int | None = Field(default=None, description="Limit documents to process")
    rebuild_chunks: bool = Field(default=False, description="Rebuild chunks even if they exist")
    rebuild_embeddings: bool = Field(default=False, description="Rebuild embeddings even if they exist")


class PreparationStepSchema(BaseModel):
    name: str
    status: str
    message: str = ""
    details: str = ""


class PrepareResponse(BaseModel):
    status: str
    registry_number: str
    ready_for_analysis: bool
    steps: list[PreparationStepSchema] = []
    tender_found: bool = False
    documents_total: int = 0
    documents_downloaded: int = 0
    extracted_texts_total: int = 0
    chunks_total: int = 0
    chunks_created: int = 0
    embeddings_total: int = 0
    embeddings_created: int = 0
    warnings: list[str] = []
    errors: list[str] = []


class PreparationStatusResponse(BaseModel):
    registry_number: str
    tender_found: bool = False
    documents_total: int = 0
    documents_downloaded: int = 0
    extracted_texts_total: int = 0
    chunks_total: int = 0
    embeddings_total: int = 0
    ready_for_analysis: bool = False
    missing: list[str] = []


class AnalyzeResponse(BaseModel):
    status: str
    registry_number: str
    sections_count: int
    sources_count: int
    analysis_mode: str = DEFAULT_ANALYSIS_MODE
    report_markdown: str = ""
    report_path: str | None = None
    used_llm: bool = False
    llm_model: str | None = None
    llm_endpoint: str | None = None
    duration_seconds: float | None = None
    timings: dict = Field(default_factory=dict)
    per_section_timings: list[dict] = Field(default_factory=list)
    llm_calls_count: int = 0
    total_context_chars: int = 0
    max_section_context_chars: int = 0
    avg_section_llm_seconds: float | None = None
    retrieval_limit_used: int | None = None
    run_id: str | None = None
    warnings: list[str] = []
    errors: list[str] = []


class LatestReportResponse(BaseModel):
    registry_number: str
    report_markdown: str
    report_path: str
    created_at: str | None = None


class HistoryListItem(BaseModel):
    id: str
    registry_number: str
    status: str
    used_llm: bool
    sections_count: int
    sources_count: int
    report_path: str | None = None
    preview: str | None = None
    warnings: list[str] = []
    errors: list[str] = []
    duration_seconds: float | None = None
    source: str | None = None
    metadata: dict = Field(default_factory=dict)
    created_at: str | None = None


class HistoryListResponse(BaseModel):
    items: list[HistoryListItem]
    limit: int
    offset: int
    total: int


class HistoryRunDetail(BaseModel):
    id: str
    registry_number: str
    status: str
    used_llm: bool
    sections_count: int
    sources_count: int
    report_path: str | None = None
    preview: str | None = None
    warnings: list[str] = []
    errors: list[str] = []
    duration_seconds: float | None = None
    source: str | None = None
    metadata: dict = Field(default_factory=dict)
    created_at: str | None = None


class HistoryReportResponse(BaseModel):
    id: str
    registry_number: str
    report_markdown: str
    report_path: str | None = None


def _report_export_response(exported) -> FileResponse:
    return FileResponse(
        exported.file_path,
        media_type=exported.content_type,
        filename=exported.file_name,
    )


def _get_session() -> Session:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        Base.metadata.create_all(engine)
    except Exception:
        pass
    from sqlalchemy.orm import sessionmaker
    return sessionmaker(bind=engine)()


def _safe_filename(name: str) -> str:
    import re
    if not name or not re.match(r"^\d{11,25}$", name.strip()):
        return ""
    return name.strip()


_OLD_TABLE_COUNTS = [
    "procurement_tenders",
    "procurement_tender_documents",
    "procurement_document_chunks",
    "procurement_document_embeddings",
]


def _to_prepare_response(result) -> PrepareResponse:
    steps = [
        PreparationStepSchema(name=s.name, status=s.status, message=s.message, details=s.details)
        for s in result.steps
    ]
    return PrepareResponse(
        status=result.status,
        registry_number=result.registry_number,
        ready_for_analysis=result.ready_for_analysis,
        steps=steps,
        tender_found=result.tender_found,
        documents_total=result.documents_total,
        documents_downloaded=result.documents_downloaded,
        extracted_texts_total=result.extracted_texts_total,
        chunks_total=result.chunks_total,
        chunks_created=result.chunks_created,
        embeddings_total=result.embeddings_total,
        embeddings_created=result.embeddings_created,
        warnings=result.warnings,
        errors=result.errors,
    )


def _to_analyze_response(result: TenderAnalysisResult) -> AnalyzeResponse:
    return AnalyzeResponse(
        status=result.status,
        registry_number=result.registry_number,
        sections_count=result.sections_count,
        sources_count=result.sources_count,
        analysis_mode=result.analysis_mode,
        report_markdown=result.report_markdown,
        report_path=result.report_path,
        used_llm=result.used_llm,
        llm_model=result.llm_model,
        llm_endpoint=result.llm_endpoint,
        duration_seconds=result.duration_seconds,
        timings=result.timings,
        per_section_timings=result.per_section_timings,
        llm_calls_count=result.llm_calls_count,
        total_context_chars=result.total_context_chars,
        max_section_context_chars=result.max_section_context_chars,
        avg_section_llm_seconds=result.avg_section_llm_seconds,
        retrieval_limit_used=result.retrieval_limit_used,
        run_id=result.run_id,
        warnings=result.warnings,
        errors=result.errors,
    )


def _job_to_status_response(record) -> JobStatusResponse:
    return JobStatusResponse(**record.to_dict())


@router.get("/health", response_model=dict)
def tender_research_health() -> dict:
    settings = get_settings()
    engine = create_engine(settings.database_url, future=True)
    diag = get_database_diagnostics(engine)
    table_counts: dict[str, int] = {}
    if diag.get("can_connect"):
        for table in _OLD_TABLE_COUNTS:
            try:
                with engine.connect() as conn:
                    row = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
                    table_counts[table] = row
            except Exception:
                table_counts[table] = -1
    return {
        "status": "ok",
        "database_dialect": diag.get("database_dialect"),
        "database_url_masked": diag.get("database_url_masked"),
        "can_connect": diag.get("can_connect", False),
        "current_migration": diag.get("current_migration"),
        "migration_head": diag.get("migration_head"),
        "pgvector_extension_available": diag.get("pgvector_extension_available", False),
        "table_counts": table_counts,
    }


@router.post("/prepare", response_model=PrepareResponse)
def prepare_tender_endpoint(payload: PrepareRequest) -> PrepareResponse:
    try:
        result = prepare_tender_for_analysis(
            registry_number=payload.registry_number,
            provider=payload.provider,
            model=payload.model,
            base_url=payload.base_url,
            limit_documents=payload.limit_documents,
            rebuild_chunks=payload.rebuild_chunks,
            rebuild_embeddings=payload.rebuild_embeddings,
        )
    except AppError:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return _to_prepare_response(result)


@router.post("/jobs/prepare", response_model=StartJobResponse)
def start_prepare_job_endpoint(payload: PrepareRequest) -> StartJobResponse:
    check_ingestion_allowed()
    request = payload.model_dump()
    session = _get_session()
    try:
        record = create_job(
            session,
            job_type="prepare",
            registry_number=payload.registry_number,
            request=request,
            source="api",
        )
    finally:
        session.close()

    try:
        submit_prepare_job(record.id, {**request, "source": "api"})
    except Exception as exc:
        logger.exception("Failed to submit prepare job %s", record.id)
        raise HTTPException(status_code=500, detail=f"Failed to submit background job: {exc}") from exc

    return StartJobResponse(
        job_id=record.id,
        job_type=record.job_type,
        registry_number=record.registry_number,
        status=record.status,
        status_url=f"/api/tender-research/jobs/{record.id}",
    )


@router.get("/prepare/{registry_number}/status", response_model=PreparationStatusResponse)
def get_preparation_status(registry_number: str) -> PreparationStatusResponse:
    try:
        safe_name = _safe_filename(registry_number)
        if safe_name != registry_number:
            raise HTTPException(status_code=400, detail="Invalid registry number")

        status = check_preparation_status(registry_number=registry_number)
        return PreparationStatusResponse(**status)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/analyze", response_model=AnalyzeResponse)
def analyze_tender_endpoint(payload: AnalyzeRequest) -> AnalyzeResponse:
    try:
        result = analyze_tender(
            registry_number=payload.registry_number,
            provider=payload.provider,
            model=payload.model,
            base_url=payload.base_url,
            use_llm=payload.use_llm,
            llm_base_url=payload.llm_base_url,
            llm_model=payload.llm_model,
            llm_timeout_seconds=payload.llm_timeout_seconds,
            limit=payload.limit,
            analysis_mode=payload.analysis_mode,
            max_context_chars_per_section=payload.max_context_chars_per_section,
            max_chunks_per_section=payload.max_chunks_per_section,
            save_report=payload.save_report,
            record_history=True,
            history_source="api",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return _to_analyze_response(result)


@router.post("/jobs/analyze", response_model=StartJobResponse)
def start_analyze_job_endpoint(payload: AnalyzeRequest) -> StartJobResponse:
    request = payload.model_dump()
    session = _get_session()
    try:
        record = create_job(
            session,
            job_type="analyze",
            registry_number=payload.registry_number,
            request=request,
            source="api",
        )
    finally:
        session.close()

    try:
        submit_analyze_job(record.id, {**request, "source": "api"})
    except Exception as exc:
        logger.exception("Failed to submit analyze job %s", record.id)
        raise HTTPException(status_code=500, detail=f"Failed to submit background job: {exc}") from exc

    return StartJobResponse(
        job_id=record.id,
        job_type=record.job_type,
        registry_number=record.registry_number,
        status=record.status,
        status_url=f"/api/tender-research/jobs/{record.id}",
    )


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_tender_analysis_job(job_id: str) -> JobStatusResponse:
    session = _get_session()
    try:
        record = get_job(session, job_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Analysis job not found")
        return _job_to_status_response(record)
    finally:
        session.close()


@router.get("/jobs", response_model=JobListResponse)
def list_tender_analysis_jobs(
    registry_number: str | None = Query(default=None),
    job_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> JobListResponse:
    session = _get_session()
    try:
        items, total = list_jobs(
            session,
            registry_number=registry_number,
            job_type=job_type,
            status=status,
            limit=limit,
            offset=offset,
        )
        return JobListResponse(
            items=[_job_to_status_response(item) for item in items],
            limit=limit,
            offset=offset,
            total=total,
        )
    finally:
        session.close()


@router.get("/analyze/{registry_number}/latest", response_model=LatestReportResponse)
def get_latest_analysis_report(registry_number: str) -> LatestReportResponse:
    safe_name = _safe_filename(registry_number)
    if safe_name != registry_number:
        raise HTTPException(status_code=400, detail="Invalid registry number")

    config = load_config()
    session = _get_session()
    try:
        record, markdown, error = get_latest_report(session, registry_number, config.data_dir)
        if record is None:
            raise HTTPException(status_code=404, detail=error or "No analysis runs found for this registry number")
        if markdown is None:
            raise HTTPException(status_code=404, detail=error or "Report file is missing or inaccessible")
        return LatestReportResponse(
            registry_number=record.registry_number,
            report_markdown=markdown,
            report_path=record.report_path or "",
            created_at=record.created_at.isoformat() if record.created_at else None,
        )
    finally:
        session.close()


@router.get("/analyze/history", response_model=HistoryListResponse)
def list_analysis_history(
    registry_number: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> HistoryListResponse:
    session = _get_session()
    try:
        items, total = list_analysis_runs(
            session,
            registry_number=registry_number,
            status=status,
            limit=limit,
            offset=offset,
        )
        return HistoryListResponse(
            items=[HistoryListItem(**r.to_dict()) for r in items],
            limit=limit,
            offset=offset,
            total=total,
        )
    finally:
        session.close()


@router.get("/analyze/history/{run_id}", response_model=HistoryRunDetail)
def get_analysis_history_run(run_id: str) -> HistoryRunDetail:
    session = _get_session()
    try:
        record = get_analysis_run(session, run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Analysis run not found")
        return HistoryRunDetail(**record.to_dict())
    finally:
        session.close()


@router.get("/analyze/history/{run_id}/report", response_model=HistoryReportResponse)
def get_analysis_history_report(run_id: str) -> HistoryReportResponse:
    config = load_config()
    session = _get_session()
    try:
        record, markdown, error = get_analysis_run_report(session, run_id, config.data_dir)
        if record is None:
            raise HTTPException(status_code=404, detail=error or "Analysis run not found")
        if markdown is None:
            raise HTTPException(status_code=404, detail=error or "Report file is missing or inaccessible")
        return HistoryReportResponse(
            id=record.id,
            registry_number=record.registry_number,
            report_markdown=markdown,
            report_path=record.report_path,
        )
    finally:
        session.close()


@router.get("/analyze/history/{run_id}/export/docx")
def export_analysis_history_report_docx(run_id: str) -> FileResponse:
    config = load_config()
    session = _get_session()
    try:
        try:
            exported = export_analysis_report_docx(run_id, data_dir=config.data_dir, session=session)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc) or "Report file is missing or inaccessible") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc) or "Invalid export request") from exc
        except Exception as exc:
            logger.exception("DOCX export failed for analysis run %s", run_id)
            raise HTTPException(status_code=500, detail="Failed to export DOCX report") from exc
        return _report_export_response(exported)
    finally:
        session.close()


@router.get("/analyze/history/{run_id}/export/pdf")
def export_analysis_history_report_pdf(run_id: str) -> FileResponse:
    config = load_config()
    session = _get_session()
    try:
        try:
            exported = export_analysis_report_pdf(run_id, data_dir=config.data_dir, session=session)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc) or "Report file is missing or inaccessible") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc) or "Invalid export request") from exc
        except Exception as exc:
            logger.exception("PDF export failed for analysis run %s", run_id)
            raise HTTPException(status_code=500, detail="Failed to export PDF report") from exc
        return _report_export_response(exported)
    finally:
        session.close()
