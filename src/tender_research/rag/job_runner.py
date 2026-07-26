from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.shared.config.settings import get_settings
from src.shared.db.base import Base
from src.shared.storage.gate import IngestionBlockedError, check_ingestion_allowed
from src.tender_research.rag.analysis_service import analyze_tender
from src.tender_research.rag.job_schemas import TenderJobStep
from src.tender_research.rag.job_service import (
    complete_job,
    fail_job,
    mark_job_running,
    update_job_progress,
)
from src.tender_research.rag.prepare_service import prepare_tender_for_analysis

logger = logging.getLogger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tender-analysis-job")
_FUTURES: dict[str, Any] = {}
_FUTURES_LOCK = threading.Lock()

_PREPARE_STEP_TITLES = {
    "check_tender_exists": "Проверка закупки",
    "load_or_ingest_tender": "Загрузка закупки",
    "download_documents": "Загрузка документов",
    "extract_text": "Извлечение текста",
    "build_chunks": "Построение чанков",
    "build_embeddings": "Построение эмбеддингов",
    "readiness_check": "Финальная проверка",
}

_ANALYZE_STEP_TITLES = {
    "retrieval": "Подготовка поиска",
    "section_analysis": "Анализ разделов",
    "save_report": "Сохранение отчёта",
    "record_history": "Сохранение в history",
    "completed": "Завершение",
}


def _get_session() -> Session:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine)()


def _sanitize_error_message(error: Exception) -> str:
    text = str(error).strip()
    return text or "Background job failed"


def _normalize_step_status(status: str) -> str:
    return {
        "in_progress": "running",
        "done": "completed",
        "completed_with_warning": "warning",
    }.get(status, status)


def _prepare_steps_payload(raw_steps: list[Any]) -> list[dict]:
    result: list[dict] = []
    total = max(len(raw_steps), 1)
    for index, step in enumerate(raw_steps, start=1):
        progress = min(100, round(index * 100 / total))
        result.append(
            {
                "name": getattr(step, "name", ""),
                "title": _PREPARE_STEP_TITLES.get(getattr(step, "name", ""), getattr(step, "name", "")),
                "status": _normalize_step_status(getattr(step, "status", "pending")),
                "progress_percent": progress,
                "message": getattr(step, "message", "") or "",
                "details": getattr(step, "details", "") or "",
            }
        )
    return result


def _analysis_steps_payload(
    current_step: str,
    progress_percent: int,
    message: str = "",
    *,
    section_states: list[dict] | None = None,
) -> list[dict]:
    ordered = ["retrieval", "section_analysis", "save_report", "record_history", "completed"]
    steps: list[dict] = []
    current_index = ordered.index(current_step) if current_step in ordered else -1
    for index, name in enumerate(ordered):
        status = "pending"
        step_progress = 0
        if index < current_index:
            status = "completed"
            step_progress = 100
        elif index == current_index:
            status = "completed" if name == "completed" else "running"
            step_progress = progress_percent
        steps.append(
            {
                "name": name,
                "title": _ANALYZE_STEP_TITLES[name],
                "status": status,
                "progress_percent": step_progress,
                "message": message if index == current_index else "",
                "details": None,
            }
        )
        if name == "section_analysis" and section_states:
            for section_state in section_states:
                steps.append(
                    {
                        "name": section_state.get("name", ""),
                        "title": section_state.get("title") or section_state.get("name", ""),
                        "status": _normalize_step_status(str(section_state.get("status", "pending"))),
                        "progress_percent": int(section_state.get("progress_percent", 0) or 0),
                        "message": str(section_state.get("message", "") or ""),
                        "details": section_state.get("details"),
                    }
                )
    return steps


def _section_states_from_timings(per_section_timings: list[dict] | None) -> list[dict]:
    result: list[dict] = []
    for item in per_section_timings or []:
        status = str(item.get("status") or "pending")
        result.append(
            {
                "name": f"section:{item.get('section_id', '')}",
                "title": f"{item.get('section_index', '?')}/{len(per_section_timings or [])} — {item.get('section_title', '')}",
                "status": "warning" if status in {"retrieval_only_fallback", "insufficient_context", "no_context"} else "completed",
                "progress_percent": 100,
                "message": "Готово с предупреждением." if status == "retrieval_only_fallback" else "Раздел завершён.",
                "details": {
                    "section_id": item.get("section_id"),
                    "section_title": item.get("section_title"),
                    "section_index": item.get("section_index"),
                    "total_sections": len(per_section_timings or []),
                    "retrieval_seconds": item.get("retrieval_seconds"),
                    "llm_seconds": item.get("llm_seconds"),
                    "duration_seconds": item.get("duration_seconds"),
                    "chunks_retrieved": item.get("chunks_retrieved"),
                    "chunks_used": item.get("chunks_used"),
                    "context_chars": item.get("context_chars"),
                    "context_tokens_estimate": item.get("context_tokens_estimate"),
                    "fallback_reason": item.get("fallback_reason"),
                },
            }
        )
    return result


def _prepare_result_payload(result) -> dict:
    return {
        "status": result.status,
        "registry_number": result.registry_number,
        "ready_for_analysis": result.ready_for_analysis,
        "tender_found": result.tender_found,
        "documents_total": result.documents_total,
        "documents_downloaded": result.documents_downloaded,
        "extracted_texts_total": result.extracted_texts_total,
        "chunks_total": result.chunks_total,
        "chunks_created": result.chunks_created,
        "embeddings_total": result.embeddings_total,
        "embeddings_created": result.embeddings_created,
        "steps": [
            {
                "name": step.name,
                "title": _PREPARE_STEP_TITLES.get(step.name, step.name),
                "status": _normalize_step_status(step.status),
                "progress_percent": 0,
                "message": step.message,
                "details": step.details,
            }
            for step in result.steps
        ],
    }


def _analyze_result_payload(result) -> dict:
    preview = (result.report_markdown or "").strip()
    if len(preview) > 1500:
        preview = preview[:1500].rsplit(" ", 1)[0] + "..."
    return {
        "status": result.status,
        "registry_number": result.registry_number,
        "sections_count": result.sections_count,
        "sources_count": result.sources_count,
        "analysis_mode": result.analysis_mode,
        "used_llm": result.used_llm,
        "llm_model": result.llm_model,
        "llm_endpoint": result.llm_endpoint,
        "duration_seconds": result.duration_seconds,
        "timings": result.timings,
        "per_section_timings": result.per_section_timings,
        "llm_calls_count": result.llm_calls_count,
        "total_context_chars": result.total_context_chars,
        "max_section_context_chars": result.max_section_context_chars,
        "avg_section_llm_seconds": result.avg_section_llm_seconds,
        "retrieval_limit_used": result.retrieval_limit_used,
        "warnings": result.warnings,
        "errors": result.errors,
        "report_path": result.report_path,
        "analysis_run_id": result.run_id,
        "run_id": result.run_id,
        "preview": preview,
    }


def _resolve_analyze_job_status(result) -> tuple[str, list[str]]:
    warnings = [warning for warning in (result.warnings or []) if warning]
    if result.status == "failed":
        return "failed", warnings
    if result.sources_count <= 0:
        message = "Analysis completed, but no cited sources were found."
        if message not in warnings:
            warnings.append(message)
        return "completed_with_warnings", warnings
    if result.status in {"completed", "completed_with_warnings"}:
        return result.status, warnings
    return "completed", warnings


def run_prepare_job(job_id: str, request: dict) -> None:
    session = _get_session()
    try:
        check_ingestion_allowed()

        mark_job_running(session, job_id)

        def on_progress(payload: dict) -> None:
            update_job_progress(
                session,
                job_id,
                progress_percent=int(payload.get("progress_percent", 0) or 0),
                current_step=str(payload.get("current_step") or "running"),
                steps=_prepare_steps_payload(payload.get("steps") or []),
                message=str(payload.get("message") or ""),
            )

        result = prepare_tender_for_analysis(
            registry_number=request["registry_number"],
            provider=request.get("provider"),
            model=request.get("model"),
            base_url=request.get("base_url"),
            limit_documents=request.get("limit_documents"),
            rebuild_chunks=bool(request.get("rebuild_chunks", False)),
            rebuild_embeddings=bool(request.get("rebuild_embeddings", False)),
            session=session,
            progress_callback=on_progress,
        )
        steps_payload = _prepare_steps_payload(result.steps)
        if result.status == "failed":
            fail_job(
                session,
                job_id,
                errors=result.errors or ["Prepare job failed"],
                warnings=result.warnings,
                steps=steps_payload,
                current_step="readiness_check",
            )
            return
        complete_job(
            session,
            job_id,
            result=_prepare_result_payload(result),
            warnings=result.warnings,
            status=result.status if result.status in {"completed", "completed_with_warnings"} else "completed",
            steps=steps_payload,
        )
    except IngestionBlockedError as exc:
        logger.warning("Prepare background job %s blocked by storage gate: %s", job_id, exc)
        fail_job(
            session,
            job_id,
            errors=[exc.code],
            current_step="gate_check",
        )
    except Exception as exc:
        logger.exception("Prepare background job %s failed", job_id)
        fail_job(session, job_id, errors=[_sanitize_error_message(exc)], current_step="failed")
    finally:
        session.close()
        with _FUTURES_LOCK:
            _FUTURES.pop(job_id, None)


def run_analyze_job(job_id: str, request: dict) -> None:
    session = _get_session()
    try:
        mark_job_running(session, job_id)
        update_job_progress(
            session,
            job_id,
            progress_percent=10,
            current_step="retrieval",
            steps=_analysis_steps_payload("retrieval", 10, "Подготавливаем поиск по документам…"),
            message="Подготавливаем поиск по документам…",
        )

        def on_progress(payload: dict) -> None:
            current_step = str(payload.get("current_step") or "running")
            progress_percent = int(payload.get("progress_percent", 0) or 0)
            message = str(payload.get("message") or "")
            section_states = payload.get("steps") if isinstance(payload.get("steps"), list) else None
            update_job_progress(
                session,
                job_id,
                progress_percent=progress_percent,
                current_step=current_step,
                steps=_analysis_steps_payload(
                    current_step,
                    progress_percent,
                    message,
                    section_states=section_states,
                ),
                message=message,
            )

        result = analyze_tender(
            registry_number=request["registry_number"],
            provider=request.get("provider"),
            model=request.get("model"),
            base_url=request.get("base_url"),
            use_llm=bool(request.get("use_llm", False)),
            llm_base_url=request.get("llm_base_url"),
            llm_model=request.get("llm_model"),
            llm_timeout_seconds=request.get("llm_timeout_seconds"),
            limit=request.get("limit"),
            analysis_mode=request.get("analysis_mode") or "balanced",
            max_context_chars_per_section=request.get("max_context_chars_per_section"),
            max_chunks_per_section=request.get("max_chunks_per_section"),
            save_report=bool(request.get("save_report", False)),
            record_history=True,
            history_source=request.get("source") or "api",
            session=session,
            progress_callback=on_progress,
        )
        status, warnings = _resolve_analyze_job_status(result)
        if result.status == "failed":
            fail_job(
                session,
                job_id,
                errors=result.errors or ["Analyze job failed"],
                warnings=result.warnings,
                steps=_analysis_steps_payload("record_history", 98, "Анализ завершился с ошибкой."),
                current_step="record_history",
            )
            return
        complete_job(
            session,
            job_id,
            result=_analyze_result_payload(result),
            warnings=warnings,
            report_path=result.report_path,
            analysis_run_id=result.run_id,
            status=status,
            steps=_analysis_steps_payload(
                "completed",
                100,
                "Анализ завершён.",
                section_states=_section_states_from_timings(result.per_section_timings),
            ),
        )
    except Exception as exc:
        logger.exception("Analyze background job %s failed", job_id)
        fail_job(session, job_id, errors=[_sanitize_error_message(exc)], current_step="failed")
    finally:
        session.close()
        with _FUTURES_LOCK:
            _FUTURES.pop(job_id, None)


def submit_prepare_job(job_id: str, request: dict) -> None:
    with _FUTURES_LOCK:
        _FUTURES[job_id] = _EXECUTOR.submit(run_prepare_job, job_id, request)


def submit_analyze_job(job_id: str, request: dict) -> None:
    with _FUTURES_LOCK:
        _FUTURES[job_id] = _EXECUTOR.submit(run_analyze_job, job_id, request)
