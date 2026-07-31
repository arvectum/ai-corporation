from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from src.modules.procurement_analysis.r10_1_producer import (
    R10_1CanonicalProductionError,
)
from src.modules.production_llm_analysis import controlled_evidence
from src.modules.production_llm_analysis.controlled_evidence import (
    ControlledEvidenceError,
)
from src.modules.production_llm_analysis import (
    llama_finalization_diagnostics as diagnostics,
)


def test_json_safe_value_normalizes_runner_metadata() -> None:
    source = {
        "amount": Decimal("123.45"),
        "run_id": UUID("12345678-1234-5678-1234-567812345678"),
        "created_at": datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
        "nested": (Decimal("1.25"),),
    }

    normalized = diagnostics._json_safe_value(source)

    assert normalized == {
        "amount": 123.45,
        "run_id": "12345678-1234-5678-1234-567812345678",
        "created_at": "2026-07-31T12:00:00+00:00",
        "nested": [1.25],
    }
    json.dumps(normalized, allow_nan=False)


def test_r10_unexpected_persistence_error_gets_owned_phase_code() -> None:
    def persist_canonical_outputs() -> None:
        raise TypeError("private detail")

    with pytest.raises(
        R10_1CanonicalProductionError,
        match="^r10_1_finalization_persistence_failed$",
    ) as captured:
        diagnostics._run_r10_with_phase_diagnostics(persist_canonical_outputs)

    assert isinstance(captured.value.__cause__, TypeError)


def test_r10_owned_error_is_not_reclassified() -> None:
    def operation() -> None:
        raise R10_1CanonicalProductionError("existing_owned_code")

    with pytest.raises(
        R10_1CanonicalProductionError,
        match="^existing_owned_code$",
    ):
        diagnostics._run_r10_with_phase_diagnostics(operation)


def test_manifest_build_failure_is_sanitized_and_stage_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        controlled_evidence,
        "produce_r10_1_canonical_analysis",
        lambda **_: object(),
    )

    def fail_manifest(**_: object) -> dict[str, object]:
        raise ValueError("private manifest detail")

    monkeypatch.setattr(
        controlled_evidence,
        "build_sanitized_controlled_evidence_manifest",
        fail_manifest,
    )
    policy = SimpleNamespace(
        budget=object(),
        provider="openai_compatible",
        model="local-model",
    )
    target = tmp_path / "result"

    with pytest.raises(
        ControlledEvidenceError,
        match="^controlled_evidence_manifest_build_failed$",
    ):
        diagnostics._run_controlled_provider_evidence_with_diagnostics(
            output_root=target,
            customer_id="customer",
            project_id="project",
            procurement_case_id="case",
            registry_number="registry",
            run_id="run",
            metadata={},
            documents=[],
            provider_factory=object,
            policy=policy,
            controlled=True,
        )

    assert not target.exists()
    assert not list(tmp_path.glob(".result.partial.*"))


def test_publish_failure_gets_exact_code_and_stage_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production = object()
    monkeypatch.setattr(
        controlled_evidence,
        "produce_r10_1_canonical_analysis",
        lambda **_: production,
    )
    monkeypatch.setattr(
        controlled_evidence,
        "build_sanitized_controlled_evidence_manifest",
        lambda **_: {"manifest_hash": "hash"},
    )
    monkeypatch.setattr(
        controlled_evidence,
        "_write_manifest",
        lambda path, manifest: path.write_text("{}", encoding="utf-8"),
    )
    monkeypatch.setattr(
        controlled_evidence,
        "_relocate_production",
        lambda item, destination: item,
    )

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("private publish detail")

    monkeypatch.setattr(diagnostics.os, "replace", fail_replace)
    policy = SimpleNamespace(
        budget=object(),
        provider="openai_compatible",
        model="local-model",
    )
    target = tmp_path / "result"

    with pytest.raises(
        ControlledEvidenceError,
        match="^controlled_evidence_publish_failed$",
    ):
        diagnostics._run_controlled_provider_evidence_with_diagnostics(
            output_root=target,
            customer_id="customer",
            project_id="project",
            procurement_case_id="case",
            registry_number="registry",
            run_id="run",
            metadata={},
            documents=[],
            provider_factory=object,
            policy=policy,
            controlled=True,
        )

    assert not target.exists()
    assert not list(tmp_path.glob(".result.partial.*"))
