from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

from quality_gates.arv001.evaluate import (
    GateInputError,
    build_freeze_manifest,
    digest,
    evaluate,
    load_json,
    validate_package,
    validate_review,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "quality_gates" / "arv001"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "arv001" / "synthetic_review.json"
POLICY = load_json(PACKAGE / "policy.json")


def review_fixture() -> dict:
    return load_json(FIXTURE)


def real_passing_review() -> dict:
    review = review_fixture()
    review["evidence_class"] = "real"
    review["case_ref"] = "sanitized-real-case-001"
    review["notes"] = "Sanitized test stand-in; no tender text or customer identity."
    return review


def test_package_contract_is_closed_and_versioned() -> None:
    result = validate_package()

    assert result["task_id"] == "ARV-001"
    assert result["policy_version"] == "1.0.0"
    assert result["automatic_fail_rules"] >= 8
    assert result["status"] == "OK"


def test_synthetic_fixture_validates_but_cannot_freeze() -> None:
    review = review_fixture()
    schema = load_json(PACKAGE / "review.schema.json")

    jsonschema.validate(review, schema)
    result = evaluate(review, POLICY)

    assert result["verdict"] == "NOT_READY"
    assert result["release_allowed"] is False
    assert result["freeze_allowed"] is False
    assert result["reasons"]["not_ready"] == ["real_evidence_required"]


def test_real_review_passes_when_all_thresholds_and_reviews_pass() -> None:
    result = evaluate(real_passing_review(), POLICY)

    assert result["verdict"] == "PASS"
    assert result["release_allowed"] is True
    assert result["freeze_allowed"] is True
    assert all(
        metric["passed"] is True
        for metric in result["metrics"]
        if metric["value"] is not None
    )


def test_evaluation_is_byte_deterministic() -> None:
    review = real_passing_review()

    first = evaluate(copy.deepcopy(review), POLICY)
    second = evaluate(copy.deepcopy(review), POLICY)

    assert digest(first) == digest(second)
    assert first == second


def test_unknown_review_field_is_rejected() -> None:
    review = real_passing_review()
    review["raw_tender_text"] = "must never be accepted"

    with pytest.raises(GateInputError, match="review:unknown:raw_tender_text"):
        evaluate(review, POLICY)


def test_evidence_mismatch_is_automatic_fail() -> None:
    review = real_passing_review()
    review["claims"]["evidence_mismatches"] = 1

    result = evaluate(review, POLICY)

    assert result["verdict"] == "FAIL"
    assert "automatic_fail:evidence_mismatch" in result["reasons"]["fail"]


def test_open_sev1_blocks_release_even_when_metrics_pass() -> None:
    review = real_passing_review()
    review["defects"] = [
        {
            "id": "defect-sev1-001",
            "severity": "Sev-1",
            "category": "wrong_identity",
            "status": "open",
            "automatic_fail_rule": "wrong_procurement_identity",
            "evidence_ref": "review-sheet:identity",
            "rationale": "The report is bound to a different procurement identity."
        }
    ]

    result = evaluate(review, POLICY)

    assert result["verdict"] == "FAIL"
    assert result["open_defects"]["Sev-1"] == 1
    assert "automatic_fail:unresolved_sev1" in result["reasons"]["fail"]
    assert "automatic_fail:wrong_procurement_identity" in result["reasons"]["fail"]


def test_missing_truth_is_not_scored_as_zero() -> None:
    review = real_passing_review()
    review["critical_requirements"] = {
        "truth_total": None,
        "supported_found": None,
        "missing_truth_reason": "Independent manual truth pack is not accepted yet.",
    }

    result = evaluate(review, POLICY)
    metric = next(
        item
        for item in result["metrics"]
        if item["name"] == "critical_requirement_recall"
    )

    assert result["verdict"] == "NOT_READY"
    assert metric["value"] is None
    assert metric["passed"] is None
    assert "metric_missing:critical_requirement_recall" in result["reasons"]["not_ready"]


def test_disagreement_requires_distinct_adjudication() -> None:
    review = real_passing_review()
    review["decision"] = {
        "system": "GO_WITH_CONDITIONS",
        "reviewed": "NEEDS_REVIEW",
        "agreement": False,
        "positive_inputs_supported": True,
    }

    unresolved = evaluate(copy.deepcopy(review), POLICY)
    assert unresolved["verdict"] == "NOT_READY"
    assert "decision_disagreement_unresolved" in unresolved["reasons"]["not_ready"]

    review["adjudication"] = {
        "subject": "adjudicator-c",
        "decision": "NEEDS_REVIEW",
        "rationale": "The reviewed decision is accepted after checking the cited sources.",
    }
    resolved = evaluate(review, POLICY)
    assert resolved["verdict"] == "PASS"
    assert resolved["review"]["decision_agreement_resolved"] is True


def test_one_open_sev2_yields_conditional_not_pass() -> None:
    review = real_passing_review()
    review["defects"] = [
        {
            "id": "defect-sev2-001",
            "severity": "Sev-2",
            "category": "report_usability",
            "status": "accepted_risk",
            "automatic_fail_rule": None,
            "evidence_ref": "review-sheet:usability",
            "rationale": "One material table needs an explicit operator warning."
        }
    ]

    result = evaluate(review, POLICY)

    assert result["verdict"] == "CONDITIONAL"
    assert result["release_allowed"] is False
    assert result["reasons"]["conditional"] == [
        "open_sev2_requires_resolution_or_waiver"
    ]


def test_freeze_manifest_binds_exact_policy_review_evaluation_and_artifacts() -> None:
    review = real_passing_review()
    validated = validate_review(copy.deepcopy(review), POLICY)
    evaluation = evaluate(copy.deepcopy(review), POLICY)

    manifest = build_freeze_manifest(
        validated,
        evaluation,
        POLICY,
        frozen_at="2026-07-31T12:00:00Z",
        approval_id="arv001-local-acceptance-001",
    )
    schema = load_json(PACKAGE / "freeze_manifest.schema.json")
    jsonschema.validate(manifest, schema)

    assert manifest["status"] == "FROZEN"
    assert manifest["policy_sha256"] == evaluation["policy_sha256"]
    assert manifest["review_sha256"] == evaluation["review_sha256"]
    assert manifest["artifacts"] == review["artifacts"]
    assert manifest["reviewer_subjects"] == ["operator-a", "reviewer-b"]


def test_freeze_rejects_synthetic_review() -> None:
    review = review_fixture()
    evaluation = evaluate(copy.deepcopy(review), POLICY)

    with pytest.raises(GateInputError, match="freeze:not_allowed"):
        build_freeze_manifest(
            validate_review(review, POLICY),
            evaluation,
            POLICY,
            frozen_at="2026-07-31T12:00:00Z",
            approval_id="synthetic-must-not-freeze",
        )


def test_cli_returns_two_for_valid_but_not_releasable_review(tmp_path: Path) -> None:
    output = tmp_path / "evaluation.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(PACKAGE / "evaluate.py"),
            "evaluate",
            str(FIXTURE),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert json.loads(output.read_text(encoding="utf-8"))["verdict"] == "NOT_READY"
    assert "must never freeze" not in completed.stdout
