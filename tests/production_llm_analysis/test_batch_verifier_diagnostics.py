import json
import stat
import sys
from types import SimpleNamespace

import pytest

from scripts.r10_1 import verify_batch_audit_plans as verifier
from src.modules.procurement_analysis import r10_1_producer
from src.modules.procurement_analysis.r10_1_producer import (
    R10_1BatchPlanningRejectedError,
    sanitize_batch_planning_diagnostics,
)
from src.modules.production_llm_analysis.batching import BatchPlanningError, BatchPolicy


def test_sanitize_batch_planning_diagnostics_drops_unapproved_and_unsafe_values() -> (
    None
):
    diagnostics = sanitize_batch_planning_diagnostics(
        {
            "completed_batch_count": 3,
            "payload_ratio": 0.75,
            "unknown": "discard",
            "nested": {"fragment_id": "secret"},
            "cursor": ["fragment-id"],
            "last_candidate_fragment_count": "not-a-number",
            "profile_path": "/private/customer/document.txt",
            "profile": "550e8400-e29b-41d4-a716-446655440000",
        }
    )

    assert diagnostics == {
        "completed_batch_count": 3,
        "payload_ratio": 0.75,
    }


def test_failure_record_contains_only_safe_aggregates() -> None:
    error = R10_1BatchPlanningRejectedError(
        sanitized_error_code="evidence_batch_planning_convergence_failed",
        profile="64k",
        plan_version="arv003-map-plan-v4",
        planning_diagnostics={"completed_batch_count": 4},
    )

    record = verifier._failure_record(error)

    assert record["failure_reason"] == "planning_convergence"
    assert record["metrics"]["remaining_batch_slots"] == 14
    assert record["metrics"]["max_batches"] == 18
    assert record["metrics"]["max_http_tokenizer_requests"] == 48
    assert "raw customer document text" not in json.dumps(record)


def test_product_wrapper_preserves_only_sanitized_planner_diagnostics(
    monkeypatch,
) -> None:
    planning_error = BatchPlanningError("raw customer document text")
    planning_error.code = "evidence_batch_planning_convergence_failed"
    planning_error.diagnostics = {
        "completed_batch_count": 3,
        "remaining_fragment_count": 12,
        "unknown": "customer document text",
        "cursor": ["fragment-id"],
        "profile": "550e8400-e29b-41d4-a716-446655440000",
    }
    monkeypatch.setattr(
        r10_1_producer,
        "build_evidence_batch_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(planning_error),
    )

    with pytest.raises(R10_1BatchPlanningRejectedError) as raised:
        r10_1_producer.build_r10_1_batch_plan(
            packet=SimpleNamespace(fragments=[]),
            customer_id="customer",
            project_id="project",
            procurement_case_id="case",
            registry_number="registry",
            run_id="run",
            documents=[],
            provider_name="provider",
            model="model",
            budget_policy=verifier._policy(4096),
            token_counter=SimpleNamespace(identity="fake-tokenizer"),
            batch_policy=BatchPolicy.approved_32k(tokenizer_identity="fake-tokenizer"),
            prompt_id="prompt",
            prompt_version="v1",
            output_schema_id="schema",
            output_schema_version="v1",
            grounding_policy_version="v1",
            controlled=True,
        )

    error = raised.value
    assert error.sanitized_error_code == planning_error.code
    assert error.profile == "32k"
    assert error.plan_version == "arv003-map-plan-v6"
    assert error.planning_diagnostics == {
        "completed_batch_count": 3,
        "remaining_fragment_count": 12,
    }
    assert error.__cause__ is planning_error
    assert "raw customer document text" not in str(error)


def test_write_diagnostics_is_atomic_and_private(tmp_path) -> None:
    output = tmp_path / "private" / "diagnostics.json"
    verifier._write_diagnostics(output, {"profiles": []})

    assert json.loads(output.read_text(encoding="utf-8")) == {"profiles": []}
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) & 0o077 == 0
    assert not list(output.parent.glob("tmp*"))


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def scalar(self, _query):
        return SimpleNamespace(
            customer_id="customer-not-serialized",
            project_id="project-not-serialized",
            procurement_case_id="case-not-serialized",
            id="run-not-serialized",
        )


def _invoke_main(monkeypatch, tmp_path, *, profile: str, plans: dict[str, object]):
    calls: list[str] = []
    diagnostics_output = tmp_path / "diagnostics.json"
    lineage_output = tmp_path / "existing-lineage.json"
    lineage_output.write_text("unchanged", encoding="utf-8")
    monkeypatch.setattr(verifier, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(
        verifier,
        "resolve_customer_run_inputs",
        lambda *_args: SimpleNamespace(documents=[]),
    )
    monkeypatch.setattr(verifier, "_persisted_evidence_fragments", lambda _docs: [])
    monkeypatch.setattr(
        verifier,
        "tokenizer_from_environment",
        lambda: SimpleNamespace(identity="fake-tokenizer"),
    )
    monkeypatch.setattr(verifier, "_source_head", lambda: "a" * 40)

    def fake_plan(_args, *, profile, **_kwargs):
        calls.append(profile)
        result = plans[profile]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(verifier, "_plan", fake_plan)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_batch_audit_plans.py",
            "--registry-number",
            "registry-never-serialized",
            "--legacy-plan-32k",
            str(tmp_path / "legacy-32k.json"),
            "--legacy-plan-64k",
            str(tmp_path / "legacy-64k.json"),
            "--lineage-output",
            str(lineage_output),
            "--profile",
            profile,
            "--diagnostic-only",
            "--diagnostics-output",
            str(diagnostics_output),
        ],
    )
    result = verifier.main()
    return result, calls, diagnostics_output, lineage_output


def _successful_plan(profile: str) -> SimpleNamespace:
    return SimpleNamespace(
        plan_version="arv003-map-plan-v4",
        plan_hash="allowed-plan-hash",
        planning_diagnostics={
            "completed_batch_count": 1,
            "unexpected_document_text": "must-not-serialize",
        },
    )


def test_diagnostic_only_selected_profile_never_runs_other_profile(
    monkeypatch, tmp_path
) -> None:
    result, calls, output, lineage = _invoke_main(
        monkeypatch,
        tmp_path,
        profile="64k",
        plans={"64k": _successful_plan("64k")},
    )

    assert result == 0
    assert calls == ["64k"]
    assert output.exists()
    assert "must-not-serialize" not in output.read_text(encoding="utf-8")
    assert lineage.exists()  # Existing lineage must remain untouched.
    assert lineage.read_text(encoding="utf-8") == "unchanged"


def test_diagnostic_failure_stops_next_profile_and_keeps_lineage(
    monkeypatch, tmp_path
) -> None:
    error = R10_1BatchPlanningRejectedError(
        sanitized_error_code="evidence_batch_planning_convergence_failed",
        profile="32k",
        plan_version="arv003-map-plan-v4",
        planning_diagnostics={"completed_batch_count": 2},
    )
    result, calls, output, lineage = _invoke_main(
        monkeypatch,
        tmp_path,
        profile="both",
        plans={"32k": error, "64k": _successful_plan("64k")},
    )

    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert result == 2
    assert calls == ["32k"]
    assert artifact["profiles"][0]["sanitized_error_code"] == error.sanitized_error_code
    assert "registry-never-serialized" not in output.read_text(encoding="utf-8")
    assert lineage.read_text(encoding="utf-8") == "unchanged"


def test_unexpected_failure_returns_three_without_raw_message(
    monkeypatch, tmp_path
) -> None:
    result, _calls, output, _lineage = _invoke_main(
        monkeypatch,
        tmp_path,
        profile="32k",
        plans={"32k": RuntimeError("customer document text must not escape")},
    )

    assert result == 3
    assert "customer document text" not in output.read_text(encoding="utf-8")
