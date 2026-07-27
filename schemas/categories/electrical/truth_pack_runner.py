#!/usr/bin/env python3
"""Reproducible ARV-067H benchmark runner for candidate truth packs."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import yaml

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from truth_pack_generator import generate_all_packs, iter_items, load_profiles  # noqa: E402
from wave1_profile_matcher import evaluate_profile  # noqa: E402

PREDICTED_CATEGORY_OUTCOMES = {"EXACT", "LIKELY_ANALOG", "PARTIAL"}


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be object")
    return value


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _issue_set(mapping: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for key in (
        "critical_mismatch_attributes",
        "critical_missing_attributes",
        "required_issue_attributes",
        "optional_issue_attributes",
    ):
        result.update(str(value) for value in mapping.get(key, []))
    return result


def _score_items(
    items: Iterable[dict[str, Any]],
    profiles: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    total = 0
    outcome_correct = 0
    category_tp = category_fp = category_fn = 0
    attribute_tp = attribute_fp = attribute_fn = 0
    hard_negative_count = false_exact = false_analog = 0
    critical_mismatch_total = critical_mismatch_detected = 0
    review_count = 0
    outcome_counts: Counter[str] = Counter()
    predicted_counts: Counter[str] = Counter()

    for item in items:
        total += 1
        profile = profiles[str(item["profile_id"])]
        input_data = item["input"]
        result = evaluate_profile(
            profile,
            input_data["requested"],
            input_data["candidate"],
            evidence_confirmed=bool(input_data["evidence_confirmed"]),
        )
        expected_outcome = str(item["truth"]["expected_outcome"])
        predicted_outcome = str(result["status"])
        outcome_counts[expected_outcome] += 1
        predicted_counts[predicted_outcome] += 1
        outcome_correct += int(predicted_outcome == expected_outcome)

        truth_category = str(item["truth_category_id"]) if item["truth"]["positive"] else None
        predicted_category = (
            str(item["truth_category_id"])
            if predicted_outcome in PREDICTED_CATEGORY_OUTCOMES
            else None
        )
        if truth_category is not None and predicted_category == truth_category:
            category_tp += 1
        elif truth_category is not None and predicted_category is None:
            category_fn += 1
        elif truth_category is None and predicted_category is not None:
            category_fp += 1

        expected_issues = _issue_set(item["truth"])
        predicted_issues = _issue_set(result)
        attribute_tp += len(expected_issues & predicted_issues)
        attribute_fp += len(predicted_issues - expected_issues)
        attribute_fn += len(expected_issues - predicted_issues)

        if bool(item["truth"]["hard_negative"]):
            hard_negative_count += 1
            false_exact += int(predicted_outcome == "EXACT")
            false_analog += int(predicted_outcome == "LIKELY_ANALOG")
            critical_mismatch_total += 1
            expected_critical = set(item["truth"]["critical_mismatch_attributes"])
            detected_critical = set(result["critical_mismatch_attributes"])
            critical_mismatch_detected += int(
                predicted_outcome == "NO_MATCH" and expected_critical <= detected_critical
            )
        review_count += int(bool(result["requires_review"]))

    category_precision = _ratio(category_tp, category_tp + category_fp)
    category_recall = _ratio(category_tp, category_tp + category_fn)
    attribute_precision = _ratio(attribute_tp, attribute_tp + attribute_fp)
    attribute_recall = _ratio(attribute_tp, attribute_tp + attribute_fn)
    return {
        "item_count": total,
        "outcome_accuracy": _ratio(outcome_correct, total),
        "category_precision": category_precision,
        "category_recall": category_recall,
        "attribute_precision": attribute_precision,
        "attribute_recall": attribute_recall,
        "false_exact_rate_on_hard_negatives": _ratio(false_exact, hard_negative_count),
        "false_analog_rate_on_hard_negatives": _ratio(false_analog, hard_negative_count),
        "critical_mismatch_recall": _ratio(
            critical_mismatch_detected,
            critical_mismatch_total,
        ),
        "review_rate": _ratio(review_count, total),
        "expected_outcome_counts": dict(sorted(outcome_counts.items())),
        "predicted_outcome_counts": dict(sorted(predicted_counts.items())),
    }


def _leakage_report(items: list[dict[str, Any]]) -> dict[str, Any]:
    ids = [str(item["item_id"]) for item in items]
    hashes = [str(item["item_hash"]) for item in items]
    source_ids = [str(item["source_record_id"]) for item in items]
    surface_hashes = [
        hashlib.sha256(str(item["surface_text"]).encode("utf-8")).hexdigest()
        for item in items
    ]
    manufacturers: dict[str, set[str]] = {"train": set(), "dev": set(), "test": set()}
    for item in items:
        manufacturers[str(item["split"])].add(str(item["manufacturer_id"]))
    test_isolated = not (
        manufacturers["test"] & (manufacturers["train"] | manufacturers["dev"])
    )
    exact_hash_unique = len(hashes) == len(set(hashes))
    source_unique = len(source_ids) == len(set(source_ids))
    item_id_unique = len(ids) == len(set(ids))
    surface_unique = len(surface_hashes) == len(set(surface_hashes))
    return {
        "item_id_unique": item_id_unique,
        "item_hash_unique": exact_hash_unique,
        "source_record_id_unique": source_unique,
        "surface_text_hash_unique": surface_unique,
        "test_manufacturer_isolation": test_isolated,
        "train_manufacturers": sorted(manufacturers["train"]),
        "dev_manufacturers": sorted(manufacturers["dev"]),
        "test_manufacturers": sorted(manufacturers["test"]),
        "exact_leakage_detected": not (
            item_id_unique
            and exact_hash_unique
            and source_unique
            and surface_unique
            and test_isolated
        ),
        "shared_generator_bias_disclosed": True,
    }


def _acceptance_report() -> dict[str, Any]:
    acceptance = _load_yaml(HERE / "truth_pack_acceptance.v1.yaml")
    statuses = Counter(str(row["acceptance_status"]) for row in acceptance["profiles"])
    complete = bool(acceptance["summary"]["independent_acceptance_complete"])
    return {
        "profile_count": len(acceptance["profiles"]),
        "accepted_profiles": statuses.get("accepted", 0),
        "pending_profiles": statuses.get("pending", 0),
        "rejected_profiles": statuses.get("rejected", 0),
        "independent_acceptance_complete": complete,
        "machine_self_acceptance_forbidden": bool(
            acceptance["governance"]["machine_self_acceptance_forbidden"]
        ),
    }


def _metric_gates(metrics: dict[str, Any], gates: dict[str, Any]) -> dict[str, bool]:
    return {
        "category_precision": metrics["category_precision"]
        >= float(gates["category_precision_min"]),
        "category_recall": metrics["category_recall"]
        >= float(gates["category_recall_min"]),
        "attribute_precision": metrics["attribute_precision"]
        >= float(gates["attribute_precision_min"]),
        "attribute_recall": metrics["attribute_recall"]
        >= float(gates["attribute_recall_min"]),
        "false_exact": metrics["false_exact_rate_on_hard_negatives"]
        <= float(gates["false_exact_rate_max"]),
        "false_analog": metrics["false_analog_rate_on_hard_negatives"]
        <= float(gates["false_analog_rate_max"]),
        "critical_mismatch_recall": metrics["critical_mismatch_recall"]
        >= float(gates["critical_mismatch_recall_min"]),
        "review_rate": metrics["review_rate"] >= float(gates["review_rate_min"]),
    }


def run_benchmark(packs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    manifest = _load_yaml(HERE / "truth_pack_manifest.v1.yaml")
    profiles = load_profiles()
    packs = packs or generate_all_packs()
    items = list(iter_items(packs))
    all_metrics = _score_items(items, profiles)
    slice_metrics = {
        slice_id: _score_items(
            [item for item in items if slice_id in item["slice_labels"]],
            profiles,
        )
        for slice_id in ("unseen_manufacturer", "ocr")
    }
    per_profile = {
        str(pack["profile_id"]): _score_items(pack["items"], profiles)
        for pack in packs
    }
    leakage = _leakage_report(items)
    acceptance = _acceptance_report()
    metric_gates = _metric_gates(all_metrics, manifest["release_gates"])
    slice_gates = {
        slice_id: _metric_gates(metrics, manifest["release_gates"])
        for slice_id, metrics in slice_metrics.items()
    }
    roots_valid = len(packs) == int(manifest["profile_count"]) and all(
        len(str(pack["pack_root_hash"])) == 64 for pack in packs
    )
    metrics_passed = all(metric_gates.values()) and all(
        all(values.values()) for values in slice_gates.values()
    )
    leakage_passed = not bool(leakage["exact_leakage_detected"])
    acceptance_passed = bool(acceptance["independent_acceptance_complete"])
    release_passed = metrics_passed and leakage_passed and acceptance_passed and roots_valid
    return {
        "benchmark_id": manifest["benchmark_id"],
        "benchmark_version": manifest["version"],
        "status": "RELEASE_ELIGIBLE" if release_passed else "RELEASE_BLOCKED",
        "accuracy_claim_status": "synthetic_contract_replay_only",
        "profile_count": len(packs),
        "item_count": len(items),
        "positive_count": sum(bool(item["truth"]["positive"]) for item in items),
        "hard_negative_count": sum(
            bool(item["truth"]["hard_negative"]) for item in items
        ),
        "split_counts": dict(sorted(Counter(str(item["split"]) for item in items).items())),
        "source_format_counts": dict(
            sorted(Counter(str(item["source_format"]) for item in items).items())
        ),
        "pack_roots": {
            str(pack["profile_id"]): str(pack["pack_root_hash"]) for pack in packs
        },
        "metrics": all_metrics,
        "slice_metrics": slice_metrics,
        "per_profile_metrics": per_profile,
        "leakage": leakage,
        "acceptance": acceptance,
        "gates": {
            "metric_gates": metric_gates,
            "slice_gates": slice_gates,
            "metrics_passed": metrics_passed,
            "leakage_passed": leakage_passed,
            "pack_roots_valid": roots_valid,
            "independent_acceptance_passed": acceptance_passed,
            "release_gate_passed": release_passed,
        },
        "limitations": [
            "Synthetic items exercise the matcher contract, not live procurement accuracy.",
            "OCR items contain deterministic OCR-like surface noise but use structured candidate values downstream.",
            "The same generator family is shared across splits; exact item, source and manufacturer leakage is blocked, while generator bias remains disclosed.",
            "Independent human acceptance is pending, so shadow-runtime promotion is blocked.",
        ],
        "production_accuracy_claims_allowed": False,
        "runtime_import": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = run_benchmark()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(yaml.safe_dump(report, allow_unicode=True, sort_keys=False).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
