from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "schemas" / "categories" / "electrical"


def test_arv067h_committed_release_report_matches_contract_state() -> None:
    report = yaml.safe_load(
        (DIRECTORY / "truth_pack_release_report.v1.yaml").read_text(encoding="utf-8")
    )
    assert report["status"] == "RELEASE_BLOCKED"
    assert report["profile_count"] == 15
    assert report["item_count"] == 2400
    assert report["positive_count"] == 1500
    assert report["hard_negative_count"] == 900
    assert report["source_format_counts"] == {
        "plain_text": 705,
        "technical_spec_table": 630,
        "catalog_card": 555,
        "ocr_scan": 510,
    }
    assert report["metrics"]["category_precision"] == 1.0
    assert report["metrics"]["category_recall"] == 0.8
    assert report["slice_metrics"]["ocr"]["item_count"] == 510
    assert report["slice_metrics"]["ocr"]["category_recall"] == 0.809524
    assert report["gates"]["synthetic_contract_metrics_passed"] is True
    assert report["gates"]["independent_acceptance_passed"] is False
    assert report["gates"]["release_gate_passed"] is False
    assert report["production_accuracy_claims_allowed"] is False
    assert report["shadow_runtime_promotion_allowed"] is False
    assert report["runtime_import"] is False
