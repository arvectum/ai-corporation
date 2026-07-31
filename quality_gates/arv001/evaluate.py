#!/usr/bin/env python3
"""Deterministic ARV-001 golden-report quality gate.

Only sanitized counts, stable references, reviewer aliases, and artifact hashes
are accepted. Tender text, provider bodies, credentials, and private paths are
outside this contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
POLICY_PATH = HERE / "policy.json"
REVIEW_SCHEMA_PATH = HERE / "review.schema.json"
MANIFEST_SCHEMA_PATH = HERE / "freeze_manifest.schema.json"
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9._-]{2,80}$")
POSITIVE_DECISIONS = {"GO", "GO_WITH_CONDITIONS"}


class GateInputError(ValueError):
    """The sanitized input violates the closed ARV-001 contract."""


@dataclass(frozen=True)
class Metric:
    name: str
    numerator: int | None
    denominator: int | None
    threshold: float
    comparator: str

    @property
    def value(self) -> float | None:
        if self.numerator is None or self.denominator in {None, 0}:
            return None
        return self.numerator / self.denominator

    @property
    def passed(self) -> bool | None:
        if self.value is None:
            return None
        if self.comparator == ">=":
            return self.value >= self.threshold
        if self.comparator == "<=":
            return self.value <= self.threshold
        raise AssertionError(f"unsupported comparator: {self.comparator}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "value": self.value,
            "threshold": self.threshold,
            "comparator": self.comparator,
            "passed": self.passed,
        }


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GateInputError(f"missing_file:{path.name}") from exc
    except json.JSONDecodeError as exc:
        raise GateInputError(
            f"invalid_json:{path.name}:{exc.lineno}:{exc.colno}"
        ) from exc


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def require(condition: bool, code: str) -> None:
    if not condition:
        raise GateInputError(code)


def closed_object(
    value: Any,
    *,
    required: set[str],
    optional: set[str] | frozenset[str] = frozenset(),
    code: str,
) -> dict[str, Any]:
    require(isinstance(value, dict), f"{code}:not_object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - set(optional)
    require(not missing, f"{code}:missing:{','.join(sorted(missing))}")
    require(not unknown, f"{code}:unknown:{','.join(sorted(unknown))}")
    return value


def nonnegative_int(value: Any, code: str) -> int:
    require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        code,
    )
    return value


def validate_sha256(value: Any, code: str) -> str:
    require(isinstance(value, str) and SHA256_RE.fullmatch(value) is not None, code)
    return value


def validate_truth_metric(value: Any, code: str) -> dict[str, Any]:
    metric = closed_object(
        value,
        required={"truth_total", "supported_found", "missing_truth_reason"},
        code=code,
    )
    truth_total = metric["truth_total"]
    supported_found = metric["supported_found"]
    missing_reason = metric["missing_truth_reason"]
    if truth_total is None or supported_found is None:
        require(
            truth_total is None and supported_found is None,
            f"{code}:partial_missing_truth",
        )
        require(
            isinstance(missing_reason, str) and missing_reason.strip(),
            f"{code}:missing_truth_reason_required",
        )
        return metric
    truth_total = nonnegative_int(truth_total, f"{code}:truth_total")
    supported_found = nonnegative_int(
        supported_found, f"{code}:supported_found"
    )
    require(supported_found <= truth_total, f"{code}:supported_exceeds_truth")
    require(missing_reason is None, f"{code}:unexpected_missing_truth_reason")
    return metric


def validate_review(value: Any, policy: dict[str, Any]) -> dict[str, Any]:
    review = closed_object(
        value,
        required={
            "schema_version",
            "task_id",
            "stage",
            "evidence_class",
            "case_ref",
            "producer_mode",
            "artifacts",
            "source_documents",
            "claims",
            "critical_requirements",
            "critical_risks",
            "safety",
            "decision",
            "reviewers",
            "defects",
        },
        optional={"critical_findings", "adjudication", "notes"},
        code="review",
    )
    require(review["schema_version"] == "arv001-review-v1", "review:schema")
    require(review["task_id"] == "ARV-001", "review:task")
    require(review["stage"] in policy["stages"], "review:stage")
    require(review["evidence_class"] in {"real", "synthetic"}, "review:evidence")
    require(
        isinstance(review["case_ref"], str)
        and ALIAS_RE.fullmatch(review["case_ref"]) is not None,
        "review:case_ref",
    )
    require(
        review["producer_mode"] in {"frozen_r9", "production_llm_r10_1"},
        "review:producer_mode",
    )

    artifacts = closed_object(
        review["artifacts"],
        required={
            "source_bundle_sha256",
            "canonical_output_sha256",
            "report_sha256",
        },
        optional={"source_graph_sha256"},
        code="artifacts",
    )
    for key, item in artifacts.items():
        validate_sha256(item, f"artifacts:{key}")

    sources = closed_object(
        review["source_documents"],
        required={"mandatory_total", "processed_total", "silent_losses"},
        code="source_documents",
    )
    mandatory_total = nonnegative_int(
        sources["mandatory_total"], "source_documents:mandatory_total"
    )
    processed_total = nonnegative_int(
        sources["processed_total"], "source_documents:processed_total"
    )
    silent_losses = nonnegative_int(
        sources["silent_losses"], "source_documents:silent_losses"
    )
    require(mandatory_total >= 1, "source_documents:mandatory_total_zero")
    require(
        processed_total <= mandatory_total,
        "source_documents:processed_exceeds_mandatory",
    )
    require(
        silent_losses <= mandatory_total,
        "source_documents:silent_losses_exceed_mandatory",
    )

    claims = closed_object(
        review["claims"],
        required={
            "material_total",
            "evidence_supported",
            "valid_locators",
            "evidence_mismatches",
        },
        code="claims",
    )
    material_total = nonnegative_int(claims["material_total"], "claims:material_total")
    evidence_supported = nonnegative_int(
        claims["evidence_supported"], "claims:evidence_supported"
    )
    valid_locators = nonnegative_int(claims["valid_locators"], "claims:valid_locators")
    evidence_mismatches = nonnegative_int(
        claims["evidence_mismatches"], "claims:evidence_mismatches"
    )
    require(material_total >= 1, "claims:material_total_zero")
    require(evidence_supported <= material_total, "claims:supported_exceeds_total")
    require(valid_locators <= evidence_supported, "claims:locators_exceed_supported")
    require(evidence_mismatches <= material_total, "claims:mismatches_exceed_total")

    validate_truth_metric(review["critical_requirements"], "critical_requirements")
    validate_truth_metric(review["critical_risks"], "critical_risks")

    critical_findings = review.get(
        "critical_findings", {"system_total": 0, "false_positive_total": 0}
    )
    critical_findings = closed_object(
        critical_findings,
        required={"system_total", "false_positive_total"},
        code="critical_findings",
    )
    finding_total = nonnegative_int(
        critical_findings["system_total"], "critical_findings:system_total"
    )
    false_positive_total = nonnegative_int(
        critical_findings["false_positive_total"],
        "critical_findings:false_positive_total",
    )
    require(
        false_positive_total <= finding_total,
        "critical_findings:false_positive_exceeds_total",
    )
    review["critical_findings"] = critical_findings

    safety = closed_object(
        review["safety"],
        required=set(policy["required_safety_gates"]),
        code="safety",
    )
    require(all(isinstance(item, bool) for item in safety.values()), "safety:boolean")

    decision = closed_object(
        review["decision"],
        required={"system", "reviewed", "agreement", "positive_inputs_supported"},
        code="decision",
    )
    allowed_decisions = set(policy["review_policy"]["final_decision_values"])
    require(decision["system"] in allowed_decisions, "decision:system")
    require(decision["reviewed"] in allowed_decisions, "decision:reviewed")
    require(isinstance(decision["agreement"], bool), "decision:agreement")
    require(
        isinstance(decision["positive_inputs_supported"], bool),
        "decision:positive_inputs_supported",
    )

    reviewers = review["reviewers"]
    require(isinstance(reviewers, list) and reviewers, "reviewers:empty")
    subjects: list[str] = []
    allowed_roles = set(policy["review_policy"]["allowed_roles"])
    for index, raw_reviewer in enumerate(reviewers):
        reviewer = closed_object(
            raw_reviewer,
            required={"subject", "role", "independent", "completed"},
            code=f"reviewers:{index}",
        )
        require(
            isinstance(reviewer["subject"], str)
            and ALIAS_RE.fullmatch(reviewer["subject"]) is not None,
            f"reviewers:{index}:subject",
        )
        require(reviewer["role"] in allowed_roles, f"reviewers:{index}:role")
        require(
            isinstance(reviewer["independent"], bool),
            f"reviewers:{index}:independent",
        )
        require(
            isinstance(reviewer["completed"], bool),
            f"reviewers:{index}:completed",
        )
        subjects.append(reviewer["subject"])
    require(len(subjects) == len(set(subjects)), "reviewers:duplicate_subject")

    adjudication = review.get("adjudication")
    if adjudication is not None:
        adjudication = closed_object(
            adjudication,
            required={"subject", "decision", "rationale"},
            code="adjudication",
        )
        require(
            isinstance(adjudication["subject"], str)
            and ALIAS_RE.fullmatch(adjudication["subject"]) is not None,
            "adjudication:subject",
        )
        require(adjudication["subject"] not in subjects, "adjudication:not_independent")
        require(adjudication["decision"] in allowed_decisions, "adjudication:decision")
        require(
            isinstance(adjudication["rationale"], str)
            and 10 <= len(adjudication["rationale"].strip()) <= 2000,
            "adjudication:rationale",
        )

    defects = review["defects"]
    require(isinstance(defects, list), "defects:not_array")
    categories = set(policy["defect_categories"])
    fail_rules = {item["id"] for item in policy["automatic_fail_rules"]}
    defect_ids: list[str] = []
    for index, raw_defect in enumerate(defects):
        defect = closed_object(
            raw_defect,
            required={
                "id",
                "severity",
                "category",
                "status",
                "automatic_fail_rule",
                "evidence_ref",
                "rationale",
            },
            code=f"defects:{index}",
        )
        require(
            isinstance(defect["id"], str)
            and ALIAS_RE.fullmatch(defect["id"]) is not None,
            f"defects:{index}:id",
        )
        require(
            defect["severity"] in {"Sev-1", "Sev-2", "Sev-3"},
            f"defects:{index}:severity",
        )
        require(defect["category"] in categories, f"defects:{index}:category")
        require(
            defect["status"] in {"open", "resolved", "accepted_risk"},
            f"defects:{index}:status",
        )
        require(
            defect["automatic_fail_rule"] is None
            or defect["automatic_fail_rule"] in fail_rules,
            f"defects:{index}:automatic_fail_rule",
        )
        require(
            isinstance(defect["evidence_ref"], str)
            and 3 <= len(defect["evidence_ref"]) <= 256,
            f"defects:{index}:evidence_ref",
        )
        require(
            isinstance(defect["rationale"], str)
            and 5 <= len(defect["rationale"].strip()) <= 2000,
            f"defects:{index}:rationale",
        )
        defect_ids.append(defect["id"])
    require(len(defect_ids) == len(set(defect_ids)), "defects:duplicate_id")
    return review


def evaluate(review_value: Any, policy_value: Any | None = None) -> dict[str, Any]:
    policy = policy_value if policy_value is not None else load_json(POLICY_PATH)
    review = validate_review(review_value, policy)
    thresholds = policy["thresholds"]
    sources = review["source_documents"]
    claims = review["claims"]
    requirements = review["critical_requirements"]
    risks = review["critical_risks"]
    findings = review["critical_findings"]

    metrics = [
        Metric(
            "material_claim_evidence_coverage",
            claims["evidence_supported"],
            claims["material_total"],
            thresholds["material_claim_evidence_coverage_min"],
            ">=",
        ),
        Metric(
            "material_claim_locator_validity",
            claims["valid_locators"],
            claims["evidence_supported"],
            thresholds["material_claim_locator_validity_min"],
            ">=",
        ),
        Metric(
            "critical_requirement_recall",
            requirements["supported_found"],
            requirements["truth_total"],
            thresholds["critical_requirement_recall_min"],
            ">=",
        ),
        Metric(
            "critical_risk_recall",
            risks["supported_found"],
            risks["truth_total"],
            thresholds["critical_risk_recall_min"],
            ">=",
        ),
        Metric(
            "critical_false_positive_rate",
            findings["false_positive_total"],
            findings["system_total"],
            thresholds["critical_false_positive_rate_max"],
            "<=",
        ),
        Metric(
            "source_document_coverage",
            sources["processed_total"],
            sources["mandatory_total"],
            thresholds["source_document_coverage_min"],
            ">=",
        ),
    ]

    fail: set[str] = set()
    not_ready: set[str] = set()
    conditional: set[str] = set()

    for gate_name, passed in review["safety"].items():
        if not passed:
            fail.add(f"safety_gate_failed:{gate_name}")
    if sources["silent_losses"]:
        fail.add("automatic_fail:silent_source_loss")
    if claims["evidence_mismatches"]:
        fail.add("automatic_fail:evidence_mismatch")
    decision = review["decision"]
    if decision["system"] in POSITIVE_DECISIONS and not decision["positive_inputs_supported"]:
        fail.add("automatic_fail:unsupported_positive_decision")

    open_defects = {"Sev-1": 0, "Sev-2": 0, "Sev-3": 0}
    for defect in review["defects"]:
        if defect["status"] != "resolved":
            open_defects[defect["severity"]] += 1
            if defect["automatic_fail_rule"]:
                fail.add(f"automatic_fail:{defect['automatic_fail_rule']}")
    if open_defects["Sev-1"] > thresholds["open_sev1_max"]:
        fail.add("automatic_fail:unresolved_sev1")

    stage_policy = policy["stages"][review["stage"]]
    if review["evidence_class"] == "synthetic" and not stage_policy[
        "synthetic_evidence_allowed"
    ]:
        not_ready.add("real_evidence_required")

    completed_reviewers = {
        item["subject"]
        for item in review["reviewers"]
        if item["completed"] and item["independent"] and item["role"] != "adjudicator"
    }
    reviewer_minimum = stage_policy.get(
        "minimum_independent_reviewers",
        stage_policy.get("minimum_independent_reviewers_per_case", 2),
    )
    if len(completed_reviewers) < reviewer_minimum:
        not_ready.add("independent_review_coverage_insufficient")

    agreement_resolved = decision["agreement"]
    if not agreement_resolved:
        adjudication = review.get("adjudication")
        agreement_resolved = bool(
            adjudication and adjudication["decision"] == decision["reviewed"]
        )
    if thresholds["decision_agreement_required"] and not agreement_resolved:
        not_ready.add("decision_disagreement_unresolved")

    for metric in metrics:
        if metric.passed is None:
            if metric.name == "critical_false_positive_rate" and metric.denominator == 0:
                continue
            not_ready.add(f"metric_missing:{metric.name}")
        elif not metric.passed:
            fail.add(f"threshold_failed:{metric.name}")

    if open_defects["Sev-2"] > thresholds["open_sev2_max_for_conditional"]:
        fail.add("too_many_open_sev2")
    elif open_defects["Sev-2"] > thresholds["open_sev2_max_for_pass"]:
        conditional.add("open_sev2_requires_resolution_or_waiver")

    if fail:
        verdict = "FAIL"
    elif not_ready:
        verdict = "NOT_READY"
    elif conditional:
        verdict = "CONDITIONAL"
    else:
        verdict = "PASS"

    return {
        "schema_version": "arv001-evaluation-v1",
        "task_id": "ARV-001",
        "policy_id": policy["policy_id"],
        "policy_version": policy["version"],
        "policy_sha256": digest(policy),
        "review_sha256": digest(review),
        "case_ref": review["case_ref"],
        "stage": review["stage"],
        "evidence_class": review["evidence_class"],
        "producer_mode": review["producer_mode"],
        "verdict": verdict,
        "metrics": [metric.as_dict() for metric in metrics],
        "open_defects": open_defects,
        "review": {
            "independent_completed": len(completed_reviewers),
            "minimum_required": reviewer_minimum,
            "decision_agreement_resolved": agreement_resolved,
        },
        "reasons": {
            "fail": sorted(fail),
            "not_ready": sorted(not_ready),
            "conditional": sorted(conditional),
        },
        "artifacts": review["artifacts"],
        "release_allowed": verdict == "PASS",
        "freeze_allowed": (
            verdict == "PASS"
            and review["stage"] == "initial_freeze"
            and review["evidence_class"] == "real"
        ),
        "claim_boundary": {
            "validated_model_accuracy_claimed": False,
            "automatic_threshold_tuning_performed": False,
            "source_graph_mutated": False,
            "external_execution_performed": False,
        },
    }


def build_freeze_manifest(
    review: dict[str, Any],
    evaluation: dict[str, Any],
    policy: dict[str, Any],
    *,
    frozen_at: str,
    approval_id: str,
) -> dict[str, Any]:
    require(evaluation["freeze_allowed"], "freeze:not_allowed")
    require(
        re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", frozen_at)
        is not None,
        "freeze:frozen_at",
    )
    require(
        re.fullmatch(r"[A-Za-z0-9._-]{3,100}", approval_id) is not None,
        "freeze:approval_id",
    )
    reviewer_subjects = sorted(
        item["subject"]
        for item in review["reviewers"]
        if item["completed"] and item["independent"] and item["role"] != "adjudicator"
    )
    return {
        "schema_version": "arv001-freeze-manifest-v1",
        "task_id": "ARV-001",
        "status": "FROZEN",
        "policy_id": policy["policy_id"],
        "policy_version": policy["version"],
        "policy_sha256": evaluation["policy_sha256"],
        "case_ref": review["case_ref"],
        "producer_mode": review["producer_mode"],
        "review_sha256": evaluation["review_sha256"],
        "evaluation_sha256": digest(evaluation),
        "artifacts": review["artifacts"],
        "reviewer_subjects": reviewer_subjects,
        "adjudicator_subject": (review.get("adjudication") or {}).get("subject"),
        "approval_id": approval_id,
        "frozen_at": frozen_at,
        "verdict": "PASS",
        "release_allowed": True,
        "evidence_class": "real",
        "source_graph_mutation": False,
        "external_execution": False,
    }


def validate_package() -> dict[str, Any]:
    policy = load_json(POLICY_PATH)
    review_schema = load_json(REVIEW_SCHEMA_PATH)
    manifest_schema = load_json(MANIFEST_SCHEMA_PATH)
    require(policy.get("task_id") == "ARV-001", "package:policy_task")
    require(policy.get("version") == "1.0.0", "package:policy_version")
    require(
        len(policy.get("automatic_fail_rules", [])) >= 8,
        "package:automatic_fail_rules",
    )
    require(
        review_schema.get("additionalProperties") is False,
        "package:review_schema_not_closed",
    )
    require(
        manifest_schema.get("additionalProperties") is False,
        "package:manifest_schema_not_closed",
    )
    require(
        review_schema.get("properties", {}).get("task_id", {}).get("const")
        == "ARV-001",
        "package:review_schema_task",
    )
    require(
        manifest_schema.get("properties", {}).get("task_id", {}).get("const")
        == "ARV-001",
        "package:manifest_schema_task",
    )
    return {
        "task_id": "ARV-001",
        "policy_version": policy["version"],
        "policy_sha256": digest(policy),
        "automatic_fail_rules": len(policy["automatic_fail_rules"]),
        "status": "OK",
    }


def write_json(value: Any, output: Path | None) -> None:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if output is None:
        sys.stdout.write(text)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="ARV-001 deterministic quality gate")
    commands = root.add_subparsers(dest="command", required=True)
    validate_command = commands.add_parser("validate-package")
    validate_command.add_argument("--output", type=Path)
    evaluate_command = commands.add_parser("evaluate")
    evaluate_command.add_argument("review", type=Path)
    evaluate_command.add_argument("--output", type=Path)
    freeze_command = commands.add_parser("freeze")
    freeze_command.add_argument("review", type=Path)
    freeze_command.add_argument("--frozen-at", required=True)
    freeze_command.add_argument("--approval-id", required=True)
    freeze_command.add_argument("--output", type=Path, required=True)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "validate-package":
            write_json(validate_package(), args.output)
            return 0
        policy = load_json(POLICY_PATH)
        review = load_json(args.review)
        evaluation = evaluate(review, policy)
        if args.command == "evaluate":
            write_json(evaluation, args.output)
            return 0 if evaluation["verdict"] == "PASS" else 2
        validated_review = validate_review(review, policy)
        manifest = build_freeze_manifest(
            validated_review,
            evaluation,
            policy,
            frozen_at=args.frozen_at,
            approval_id=args.approval_id,
        )
        write_json(manifest, args.output)
        return 0
    except (GateInputError, OSError) as exc:
        print(f"ARV-001 quality gate: FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
