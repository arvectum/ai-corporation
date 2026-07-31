from __future__ import annotations

import json
import os
import shutil
from datetime import date, datetime
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar
from uuid import UUID, uuid4

from src.modules.procurement_analysis.r10_1_producer import (
    R10_1CanonicalProductionError,
)
from src.modules.production_llm_analysis import controlled_evidence
from src.modules.production_llm_analysis.controlled_evidence import (
    ControlledEvidenceBundle,
    ControlledEvidenceConflictError,
    ControlledEvidenceError,
)

_T = TypeVar("_T")
_INSTALL_MARKER = "_arv003_finalization_diagnostics_v1"

_MAPPING_FUNCTIONS = {
    "_map_supported_claims",
    "_question_rows",
    "_risk_rows",
    "_strings",
}
_OUTPUT_FUNCTIONS = {
    "_build_output_payloads",
    "_build_steps_from_outputs",
}
_RECOMMENDATION_FUNCTIONS = {"_build_final_recommendation"}
_RENDER_FUNCTIONS = {"_render_canonical_report_html"}
_PERSISTENCE_FUNCTIONS = {
    "_write_json",
    "persist_canonical_outputs",
}
_VERIFICATION_FUNCTIONS = {
    "validate_frozen_source_graph",
    "verify_canonical_bytes",
    "verify_persisted_canonical_outputs",
}
_PROVIDER_FUNCTIONS = {
    "run_production_llm_analysis",
    "execute",
    "complete",
    "_send_request",
    "_request",
}


def _json_safe_value(value: Any) -> Any:
    """Normalize runner-owned metadata without stringifying arbitrary objects."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return value


def _traceback_function_names(exc: BaseException) -> set[str]:
    names: set[str] = set()
    traceback = exc.__traceback__
    while traceback is not None:
        names.add(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    return names


def _classify_r10_unexpected_exception(exc: Exception) -> str:
    """Map a private traceback to a stable repository-owned phase code."""

    names = _traceback_function_names(exc)
    if names & _PROVIDER_FUNCTIONS:
        return "r10_1_provider_execution_unclassified_failed"
    if names & _MAPPING_FUNCTIONS:
        return "r10_1_finalization_mapping_failed"
    if names & _OUTPUT_FUNCTIONS:
        return "r10_1_finalization_output_build_failed"
    if names & _RECOMMENDATION_FUNCTIONS:
        return "r10_1_finalization_recommendation_failed"
    if names & _RENDER_FUNCTIONS:
        return "r10_1_finalization_html_render_failed"
    if names & _PERSISTENCE_FUNCTIONS:
        return "r10_1_finalization_persistence_failed"
    if names & _VERIFICATION_FUNCTIONS:
        return "r10_1_finalization_verification_failed"
    if "loads" in names and "produce_r10_1_canonical_analysis" in names:
        return "r10_1_finalization_canonical_reload_failed"
    return "r10_1_finalization_unclassified_failed"


def _run_r10_with_phase_diagnostics(
    producer: Callable[..., _T], *args: Any, **kwargs: Any
) -> _T:
    try:
        return producer(*args, **kwargs)
    except R10_1CanonicalProductionError:
        raise
    except Exception as exc:
        raise R10_1CanonicalProductionError(
            _classify_r10_unexpected_exception(exc)
        ) from exc


def _run_controlled_phase(code: str, operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except ControlledEvidenceError:
        raise
    except R10_1CanonicalProductionError:
        raise
    except Exception as exc:
        raise ControlledEvidenceError(code) from exc


def _run_controlled_provider_evidence_with_diagnostics(
    *,
    output_root: Path,
    customer_id: str,
    project_id: str,
    procurement_case_id: str,
    registry_number: str,
    run_id: str,
    metadata: dict[str, Any],
    documents: list[Any],
    provider_factory: Callable[[], Any],
    policy: Any,
    evidence_chunks: list[dict[str, Any]] | None = None,
    token_counter: Any | None = None,
    controlled: bool = False,
) -> ControlledEvidenceBundle:
    """Run Gate 5 while preserving an exact sanitized finalization phase code."""

    target = output_root.resolve()
    if target.exists():
        raise ControlledEvidenceConflictError("controlled_evidence_target_exists")

    _run_controlled_phase(
        "controlled_evidence_stage_setup_failed",
        lambda: target.parent.mkdir(parents=True, exist_ok=True),
    )
    stage = target.parent / f".{target.name}.partial.{uuid4().hex}"
    if stage.exists():
        raise ControlledEvidenceConflictError("controlled_evidence_stage_exists")
    _run_controlled_phase(
        "controlled_evidence_stage_setup_failed",
        lambda: stage.mkdir(mode=0o750),
    )

    try:
        productions: list[Any] = []
        for index in (1, 2):
            production = controlled_evidence.produce_r10_1_canonical_analysis(
                customer_id=customer_id,
                project_id=project_id,
                procurement_case_id=procurement_case_id,
                registry_number=registry_number,
                run_id=run_id,
                output_dir=stage / f"execution-{index}",
                metadata=metadata,
                documents=documents,
                evidence_chunks=evidence_chunks,
                token_counter=token_counter,
                controlled=controlled,
                provider=provider_factory(),
                budget_policy=policy.budget,
                provider_name=policy.provider,
                model=policy.model,
            )
            productions.append(production)

        manifest = _run_controlled_phase(
            "controlled_evidence_manifest_build_failed",
            lambda: controlled_evidence.build_sanitized_controlled_evidence_manifest(
                policy=policy,
                productions=productions,
            ),
        )
        manifest_path = stage / "controlled-evidence.manifest.json"
        _run_controlled_phase(
            "controlled_evidence_manifest_write_failed",
            lambda: controlled_evidence._write_manifest(manifest_path, manifest),
        )
        bundle = _run_controlled_phase(
            "controlled_evidence_bundle_build_failed",
            lambda: ControlledEvidenceBundle(
                manifest=manifest,
                manifest_path=target / manifest_path.name,
                first=controlled_evidence._relocate_production(
                    productions[0], target / "execution-1"
                ),
                second=controlled_evidence._relocate_production(
                    productions[1], target / "execution-2"
                ),
            ),
        )
        _run_controlled_phase(
            "controlled_evidence_publish_failed",
            lambda: os.replace(stage, target),
        )
        return bundle
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _wrap_runner_metadata(metadata_builder: Callable[..., dict[str, Any]]):
    @wraps(metadata_builder)
    def wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        metadata = _json_safe_value(metadata_builder(*args, **kwargs))
        try:
            json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            from scripts.r10_1.run_controlled_provider_evidence import (
                ControlledRunnerConfigurationError,
            )

            raise ControlledRunnerConfigurationError(
                "runner_metadata_not_json_serializable"
            ) from exc
        return metadata

    setattr(wrapped, _INSTALL_MARKER, True)
    return wrapped


def _wrap_producer(producer: Callable[..., _T]):
    @wraps(producer)
    def wrapped(*args: Any, **kwargs: Any) -> _T:
        return _run_r10_with_phase_diagnostics(producer, *args, **kwargs)

    setattr(wrapped, _INSTALL_MARKER, True)
    return wrapped


setattr(_run_controlled_provider_evidence_with_diagnostics, _INSTALL_MARKER, True)


def install_llama_finalization_diagnostics() -> None:
    """Install llama-entry-point-only diagnostics and JSON-safe runner metadata."""

    from scripts.r10_1 import run_controlled_provider_evidence as runner_module

    current = runner_module.run_controlled_provider_evidence
    if bool(getattr(current, _INSTALL_MARKER, False)):
        return

    if not bool(getattr(runner_module._metadata, _INSTALL_MARKER, False)):
        runner_module._metadata = _wrap_runner_metadata(runner_module._metadata)

    producer = controlled_evidence.produce_r10_1_canonical_analysis
    if not bool(getattr(producer, _INSTALL_MARKER, False)):
        controlled_evidence.produce_r10_1_canonical_analysis = _wrap_producer(producer)

    controlled_evidence.run_controlled_provider_evidence = (
        _run_controlled_provider_evidence_with_diagnostics
    )
    runner_module.run_controlled_provider_evidence = (
        _run_controlled_provider_evidence_with_diagnostics
    )
