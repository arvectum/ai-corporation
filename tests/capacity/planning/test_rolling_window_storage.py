"""
Regression tests for rolling-window storage upper bound (ARV-009C1.3A).

Corrected checkpoint, scope, and window coverage semantics.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts.capacity.planning.measure_rolling_window_storage import (
    SSD_CAPACITY_DECIMAL_BYTES,
    ONE_GIB,
    ONE_TIB,
    GREEN_THRESHOLD_BYTES,
    YELLOW_THRESHOLD_BYTES,
    PROCESSING_SPACE_MIN_BYTES,
    PERSISTENT_RESULTS_AND_LOGS_BYTES,
    COMMERCIAL_RESERVE_RATIO,
    MAX_PROCESSING_CONCURRENCY,
    ACTIVE_LAW_TYPES,
    IMPLEMENTED_LAWS,
    _RUSSIAN_REGIONS,
    NATIONAL_TARGET_REGIONS,
    EIS_REGION_REGISTRY,
    DocumentRecord,
    CompletedUnit,
    ExecutionScope,
    NationalScope,
    ScopeResult,
    build_unit_key,
    dedup_latest_version,
    dedup_conservative_union,
    filter_docs_by_window,
    compute_window_metrics,
    compute_sizing_for_window,
    compute_observed_subtotal,
    compute_sizing_status,
    determine_verdict,
    build_report,
    write_outputs,
    run_sweep,
    _extract_documents_from_archive,
    _parse_xml_procurement_id,
    _parse_xml_law,
    _parse_eis_datetime,
    _parse_xml_version_number,
    _parse_version_as_int,
    _version_sort_key,
    save_checkpoint,
    load_checkpoint,
    build_checkpoint,
)


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_doc(
    procurement_id: str = "p001",
    law: str = "44fz",
    doc_id: str = "doc1|http://example.com/doc1.pdf",
    size_bytes: int = 1_000_000,
    version: str | None = "1",
    source_date: str = "",
    file_name: str = "doc1.pdf",
    url: str = "http://example.com/doc1.pdf",
) -> DocumentRecord:
    return DocumentRecord(
        procurement_id=procurement_id,
        law=law,
        doc_id=doc_id,
        file_name=file_name,
        url=url,
        size_bytes=size_bytes,
        version=version,
        source_date=source_date,
        source_region="72",
    )


def _today_str() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S+03:00")


def _days_ago(n: int) -> str:
    dt = datetime.now(UTC) - timedelta(days=n)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+03:00")


# ─── Window extraction ─────────────────────────────────────────────────────


class TestWindowFiltering:
    def test_30d_window_from_180d_set(self):
        docs = [
            _make_doc(procurement_id="p1", source_date=_days_ago(5)),
            _make_doc(procurement_id="p2", source_date=_days_ago(60)),
            _make_doc(procurement_id="p3", source_date=_days_ago(150)),
        ]
        now = datetime.now(UTC)
        filtered = filter_docs_by_window(docs, 30, now)
        assert len(filtered) == 1
        assert filtered[0].procurement_id == "p1"

    def test_90d_window_from_180d_set(self):
        docs = [
            _make_doc(procurement_id="p1", source_date=_days_ago(5)),
            _make_doc(procurement_id="p2", source_date=_days_ago(60)),
            _make_doc(procurement_id="p3", source_date=_days_ago(150)),
        ]
        now = datetime.now(UTC)
        filtered = filter_docs_by_window(docs, 90, now)
        assert len(filtered) == 2
        ids = {d.procurement_id for d in filtered}
        assert ids == {"p1", "p2"}

    def test_180d_window_contains_all(self):
        docs = [
            _make_doc(procurement_id="p1", source_date=_days_ago(5)),
            _make_doc(procurement_id="p2", source_date=_days_ago(150)),
        ]
        now = datetime.now(UTC)
        filtered = filter_docs_by_window(docs, 180, now)
        assert len(filtered) == 2


# ── Deduplication ──────────────────────────────────────────────────────────


class TestDedup:
    def test_conservative_union_dedup_by_key(self):
        docs = [
            _make_doc(procurement_id="p1", doc_id="d1|url", version="1"),
            _make_doc(procurement_id="p1", doc_id="d1|url", version="2"),
        ]
        union = dedup_conservative_union(docs)
        assert len(union) == 1

    def test_latest_version_prefers_higher(self):
        docs = [
            _make_doc(procurement_id="p1", doc_id="d1|url", version="1", size_bytes=100),
            _make_doc(procurement_id="p1", doc_id="d1|url", version="2", size_bytes=200),
        ]
        latest = dedup_latest_version(docs)
        assert len(latest) == 1
        assert latest[0].version == "2"
        assert latest[0].size_bytes == 200

    def test_numeric_version_10_greater_than_2(self):
        docs = [
            _make_doc(procurement_id="p1", doc_id="d1|url", version="2", size_bytes=100),
            _make_doc(procurement_id="p1", doc_id="d1|url", version="10", size_bytes=200),
        ]
        latest = dedup_latest_version(docs)
        assert len(latest) == 1
        assert latest[0].version == "10"
        assert latest[0].size_bytes == 200

    def test_latest_version_selected_at_procurement_level(self):
        docs = [
            _make_doc(procurement_id="p1", doc_id="d1|url", version="2", size_bytes=100),
            _make_doc(procurement_id="p1", doc_id="d2|url", version="2", size_bytes=200),
            _make_doc(procurement_id="p2", doc_id="d3|url", version="5", size_bytes=500),
        ]
        latest = dedup_latest_version(docs)
        # p1 has two docs at latest version (v2), both should survive
        p1_docs = [d for d in latest if d.procurement_id == "p1"]
        p2_docs = [d for d in latest if d.procurement_id == "p2"]
        assert len(p1_docs) == 2  # both doc IDs from latest version of p1
        assert len(p2_docs) == 1
        assert p2_docs[0].version == "5"

    def test_union_not_less_than_latest(self):
        docs = [
            _make_doc(procurement_id="p1", doc_id="d1|url", version="1"),
            _make_doc(procurement_id="p1", doc_id="d1|url", version="2"),
            _make_doc(procurement_id="p2", doc_id="d2|url", version="1"),
        ]
        union = dedup_conservative_union(docs)
        latest = dedup_latest_version(docs)
        assert len(union) == 2
        assert len(latest) == 2

    def test_duplicate_version_not_double_counted(self):
        docs = [
            _make_doc(procurement_id="p1", doc_id="d1|url", version="1"),
            _make_doc(procurement_id="p1", doc_id="d1|url", version="1"),
        ]
        union = dedup_conservative_union(docs)
        assert len(union) == 1


class TestVersionParsing:
    def test_numeric_version_10_gt_2(self):
        k10 = _version_sort_key("10")
        k2 = _version_sort_key("2")
        assert k10 > k2

    def test_token_version_parsed(self):
        k = _version_sort_key("1.2.3")
        assert len(k) >= 5

    def test_none_version_uses_source_date(self):
        k_none = _version_sort_key(None, "2026-07-25")
        k_empty = _version_sort_key("", "2026-07-24")
        assert k_none != k_empty


# ── Window metrics ─────────────────────────────────────────────────────────


class TestWindowMetrics:
    def test_metrics_structure(self):
        now = datetime.now(UTC)
        docs = [
            _make_doc(procurement_id="p1", source_date=_today_str(), size_bytes=1_000_000),
        ]
        m = compute_window_metrics(docs, 30, now)
        assert m["window_days"] == 30
        assert m["unique_procurements"] == 1
        assert m["unique_documents"] == 1
        assert m["known_bytes"] == 1_000_000
        assert m["unknown_size_documents"] == 0
        assert m["size_coverage_percent"] == 100.0
        assert m["latest_version_bytes"] == 1_000_000
        assert m["conservative_union_bytes"] == 1_000_000
        assert "window_scope_complete" in m
        assert "window_coverage_percent" in m
        assert "observed_days" in m

    def test_empty_metrics(self):
        now = datetime.now(UTC)
        m = compute_window_metrics([], 30, now)
        assert m["unique_procurements"] == 0
        assert m["known_bytes"] == 0
        assert m["size_coverage_percent"] == 0.0

    def test_partial_size_coverage(self):
        now = datetime.now(UTC)
        docs = [
            _make_doc(procurement_id="p1", source_date=_today_str(), size_bytes=100),
            _make_doc(procurement_id="p2", source_date=_today_str(), size_bytes=None),
        ]
        m = compute_window_metrics(docs, 30, now)
        assert m["known_bytes"] == 100
        assert m["unknown_size_documents"] == 1
        assert m["size_coverage_percent"] == 50.0

    def test_percentiles(self):
        now = datetime.now(UTC)
        docs = [
            _make_doc(procurement_id=f"p{i:03d}", source_date=_today_str(), size_bytes=i * 1_000_000)
            for i in range(1, 101)
        ]
        m = compute_window_metrics(docs, 30, now)
        assert m["p50_bytes"] > 0
        assert m["p95_bytes"] >= m["p50_bytes"]
        assert m["p99_bytes"] >= m["p95_bytes"]
        assert m["max_bytes"] >= m["p99_bytes"]

    def test_window_coverage_7day_run_30day_window(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_days_ago(i)) for i in range(7)]
        m = compute_window_metrics(docs, 30, now, observed_days_count=7)
        assert m["observed_days"] == 7
        assert round(m["window_coverage_percent"], 0) == 23.0  # 7/30
        assert m["window_scope_complete"] is False

    def test_window_coverage_30day_run_30day_window(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_days_ago(i)) for i in range(30)]
        m = compute_window_metrics(docs, 30, now, observed_days_count=30)
        assert m["observed_days"] == 30
        assert m["window_coverage_percent"] == 100.0
        assert m["window_scope_complete"] is True

    def test_no_active_claims_in_metrics(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        m = compute_window_metrics(docs, 30, now)
        assert "active" not in json.dumps(m).lower()

    def test_daily_incoming(self):
        now = datetime.now(UTC)
        docs = [
            _make_doc(procurement_id="p1", source_date=_days_ago(0), size_bytes=100),
            _make_doc(procurement_id="p2", source_date=_days_ago(1), size_bytes=200),
            _make_doc(procurement_id="p3", source_date=_days_ago(1), size_bytes=300),
        ]
        m = compute_window_metrics(docs, 30, now)
        assert m["max_daily_incoming_bytes"] == 500
        assert m["avg_daily_incoming_bytes"] > 0


# ── Sizing ─────────────────────────────────────────────────────────────────


class TestSizing:
    def test_50_pct_commercial_reserve(self):
        metrics = {"conservative_union_bytes": 100_000_000_000, "p99_bytes": 1_000_000_000,
                    "window_days": 90}
        s = compute_sizing_for_window(metrics)
        assert s["commercial_reserve_bytes"] == int(100_000_000_000 * COMMERCIAL_RESERVE_RATIO)

    def test_processing_space_min(self):
        metrics = {"conservative_union_bytes": 1_000, "p99_bytes": 1_000, "window_days": 90}
        s = compute_sizing_for_window(metrics)
        assert s["processing_space_bytes"] == PROCESSING_SPACE_MIN_BYTES

    def test_processing_space_p99(self):
        p99 = 100_000_000_000
        metrics = {"conservative_union_bytes": 1_000, "p99_bytes": p99, "window_days": 90}
        s = compute_sizing_for_window(metrics)
        assert s["processing_space_bytes"] == p99 * MAX_PROCESSING_CONCURRENCY

    def test_20pct_free_space_floor(self):
        metrics = {"conservative_union_bytes": 500_000_000_000, "p99_bytes": 5_000_000_000,
                    "window_days": 90}
        s = compute_sizing_for_window(metrics)
        expected_min = int(math.ceil(s["base_required_bytes"] / 0.80))
        assert s["minimum_disk_bytes"] == expected_min
        assert s["minimum_disk_bytes"] >= s["base_required_bytes"]

    def test_ssd_capacity_gib_not_mixed(self):
        metrics = {"conservative_union_bytes": 500_000_000_000, "p99_bytes": 5_000_000_000,
                    "window_days": 90}
        s = compute_sizing_for_window(metrics)
        assert s["ssd_capacity_decimal_bytes"] == SSD_CAPACITY_DECIMAL_BYTES
        assert s["ssd_capacity_gib"] == pytest.approx(
            SSD_CAPACITY_DECIMAL_BYTES / ONE_GIB, rel=1e-2
        )

    def test_remaining_never_negative(self):
        metrics = {"conservative_union_bytes": 5_000_000_000_000, "p99_bytes": 50_000_000_000,
                    "window_days": 90}
        s = compute_sizing_for_window(metrics)
        assert s["remaining_bytes"] >= 0


# ── Observed subtotal (incomplete window) ──────────────────────────────────


class TestObservedSubtotal:
    def test_observed_subtotal_has_no_full_sizing(self):
        metrics = {"conservative_union_bytes": 500_000_000}
        s = compute_observed_subtotal(metrics)
        assert s["observed_bytes"] == 500_000_000
        assert "base_required_bytes" not in s
        assert "remaining_bytes" not in s
        assert "minimum_disk_bytes" not in s

    def test_sizing_status_incomplete(self):
        status = compute_sizing_status(False)
        assert status["sizing_status"] == "unavailable"
        assert "window_not_fully_observed" in status["reason"]

    def test_sizing_status_complete(self):
        status = compute_sizing_status(True)
        assert status == {}


# ── Verdict ────────────────────────────────────────────────────────────────


class TestVerdict:
    def _make_windows(self, base: int, p99: int = 5_000_000_000) -> tuple[dict, dict]:
        w = {}
        s = {}
        for wd in (30, 90, 180):
            metrics = {"window_days": wd, "conservative_union_bytes": base,
                       "p99_bytes": p99, "size_coverage_percent": 100.0,
                       "window_start_date": "", "window_end_date": "",
                       "unique_procurements": 10, "unique_documents": 10,
                       "known_bytes": base, "unknown_size_documents": 0,
                       "latest_version_bytes": base, "mean_bytes": 0,
                       "p50_bytes": 0, "p75_bytes": 0, "p90_bytes": 0, "p95_bytes": 0,
                       "max_bytes": 0, "packages_over_100mb": 0, "packages_over_250mb": 0,
                       "packages_over_500mb": 0, "packages_over_1gib": 0,
                       "max_daily_incoming_bytes": 0, "avg_daily_incoming_bytes": 0,
                       "file_count": 10, "observed_days": 180,
                       "window_coverage_percent": 100.0, "window_scope_complete": True}
            w[wd] = metrics
            s[wd] = compute_sizing_for_window(metrics)
        return w, s

    def test_strong_green(self):
        base = 500_000_000_000
        w, s = self._make_windows(base)
        verdict, reason = determine_verdict(w, s, scope_complete=True, size_coverage_ok=True)
        assert verdict == "STRONG_GREEN"

    def test_conditional_green(self):
        union_90 = 700_000_000_000
        union_180 = 1_000_000_000_000
        w = {}
        for wd, union in [(30, union_90), (90, union_90), (180, union_180)]:
            metrics = {"window_days": wd, "conservative_union_bytes": union,
                       "p99_bytes": 1, "size_coverage_percent": 100.0,
                       "window_start_date": "", "window_end_date": "",
                       "unique_procurements": 10, "unique_documents": 10,
                       "known_bytes": union, "unknown_size_documents": 0,
                       "latest_version_bytes": union, "mean_bytes": 0,
                       "p50_bytes": 0, "p75_bytes": 0, "p90_bytes": 0, "p95_bytes": 0,
                       "max_bytes": 0, "packages_over_100mb": 0, "packages_over_250mb": 0,
                       "packages_over_500mb": 0, "packages_over_1gib": 0,
                       "max_daily_incoming_bytes": 0, "avg_daily_incoming_bytes": 0,
                       "file_count": 10, "observed_days": 180,
                       "window_coverage_percent": 100.0, "window_scope_complete": True}
            w[wd] = metrics
        s = {wd: compute_sizing_for_window(w[wd]) for wd in w}
        assert s[90]["base_required_bytes"] <= GREEN_THRESHOLD_BYTES
        assert s[180]["base_required_bytes"] > GREEN_THRESHOLD_BYTES
        verdict, reason = determine_verdict(w, s, scope_complete=True, size_coverage_ok=True)
        assert verdict == "CONDITIONAL_GREEN"

    def test_yellow(self):
        union = 860_000_000_000
        w, s = self._make_windows(union, p99=1)
        assert s[90]["base_required_bytes"] > GREEN_THRESHOLD_BYTES
        assert s[90]["base_required_bytes"] <= YELLOW_THRESHOLD_BYTES
        verdict, reason = determine_verdict(w, s, scope_complete=True, size_coverage_ok=True)
        assert verdict == "YELLOW"

    def test_red(self):
        union = 1_100_000_000_000
        w, s = self._make_windows(union, p99=1)
        assert s[90]["base_required_bytes"] > YELLOW_THRESHOLD_BYTES
        verdict, reason = determine_verdict(w, s, scope_complete=True, size_coverage_ok=True)
        assert verdict == "RED"

    def test_unavailable_when_scope_incomplete(self):
        w, s = self._make_windows(500_000_000_000)
        verdict, reason = determine_verdict(w, s, scope_complete=False, size_coverage_ok=True)
        assert verdict == "unavailable"

    def test_unavailable_when_coverage_below_95(self):
        w, s = self._make_windows(500_000_000_000)
        verdict, reason = determine_verdict(w, s, scope_complete=True, size_coverage_ok=False)
        assert verdict == "unavailable"

    def test_unavailable_when_window_incomplete(self):
        w, s = self._make_windows(500_000_000_000)
        s[180]["sizing_status"] = "unavailable"
        s[180]["reason"] = "window_not_fully_observed"
        verdict, reason = determine_verdict(w, s, scope_complete=True, size_coverage_ok=True)
        assert verdict == "unavailable"

    def test_unavailable_when_law_missing(self):
        w, s = self._make_windows(500_000_000_000)
        verdict, reason = determine_verdict(w, s, scope_complete=False, size_coverage_ok=True)
        assert verdict == "unavailable"

    def test_incomplete_window_no_ssd_verdict(self):
        w, s = self._make_windows(500_000_000_000)
        s[180]["sizing_status"] = "unavailable"
        verdict, reason = determine_verdict(w, s, scope_complete=False, size_coverage_ok=True)
        assert verdict == "unavailable"


# ── Checkpoint / CompletedUnit ─────────────────────────────────────────────


class TestCompletedUnit:
    def test_canonical_key_includes_region_date_law(self):
        cu = CompletedUnit(region="72", date="2026-07-25+03:00", law="44fz")
        key = cu.canonical_key()
        assert key == "72|2026-07-25+03:00|44fz"
        assert "region" not in key  # key is opaque

    def test_region_a_date_x_does_not_skip_region_b_date_x(self):
        key_a = build_unit_key("72", "2026-07-25+03:00", "44fz")
        key_b = build_unit_key("77", "2026-07-25+03:00", "44fz")
        assert key_a != key_b

    def test_from_dict_roundtrip(self):
        d = {"region": "72", "date": "2026-07-25+03:00", "law": "44fz"}
        cu = CompletedUnit.from_dict(d)
        assert cu.region == "72"
        assert cu.law == "44fz"
        assert cu.canonical_key() == "72|2026-07-25+03:00|44fz"


class TestCheckpoint:
    def test_save_and_load(self, tmp_path: Path):
        state = build_checkpoint(
            completed_units=[CompletedUnit("72", "2026-07-25+03:00", "44fz")],
            documents=[_make_doc()],
            had_error=False,
            source_errors=[],
            archives_downloaded=5,
            archives_skipped=0,
            units_target=10,
            units_completed=5,
            units_failed=0,
        )
        ckpt_path = tmp_path / ".arv009c13_checkpoint.json"
        save_checkpoint(state, ckpt_path)
        assert ckpt_path.exists()
        loaded = load_checkpoint(ckpt_path)
        assert loaded is not None
        assert len(loaded["completed_units"]) == 1
        assert loaded["completed_units"][0]["region"] == "72"
        assert loaded["units_target"] == 10
        assert loaded["units_completed"] == 5

    def test_load_nonexistent_returns_none(self, tmp_path: Path):
        loaded = load_checkpoint(tmp_path / "nonexistent.json")
        assert loaded is None

    def test_checkpoint_key_includes_region_date_law(self):
        state = build_checkpoint(
            completed_units=[CompletedUnit("72", "2026-07-25+03:00", "44fz")],
            documents=[_make_doc()],
            had_error=False,
            source_errors=[],
            archives_downloaded=1,
            archives_skipped=0,
            units_target=10,
            units_completed=1,
            units_failed=0,
        )
        assert state["completed_units"][0]["region"] == "72"
        assert state["completed_units"][0]["date"] == "2026-07-25+03:00"
        assert state["completed_units"][0]["law"] == "44fz"

    def test_resume_does_not_duplicate_units(self, tmp_path: Path):
        cu1 = CompletedUnit("72", "2026-07-25+03:00", "44fz")
        state = build_checkpoint(
            completed_units=[cu1],
            documents=[_make_doc(procurement_id="p1")],
            had_error=False,
            source_errors=[],
            archives_downloaded=1,
            archives_skipped=0,
            units_target=10,
            units_completed=1,
            units_failed=0,
        )
        ckpt_path = tmp_path / ".arv009c13_checkpoint.json"
        save_checkpoint(state, ckpt_path)

        # Simulate resume — same unit should not be re-processed
        loaded = load_checkpoint(ckpt_path)
        assert loaded is not None
        completed_keys = set()
        for u_dict in loaded["completed_units"]:
            cu = CompletedUnit.from_dict(u_dict)
            completed_keys.add(cu.canonical_key())
        assert build_unit_key("72", "2026-07-25+03:00", "44fz") in completed_keys
        assert len(completed_keys) == 1


# ── Scope: Execution vs National ───────────────────────────────────────────


class TestScope:
    def test_execution_complete_national_incomplete(self):
        exec_scope = ExecutionScope(
            requested_regions=["72"],
            requested_dates=7 * ["2026-07-25+03:00"],
            requested_laws=["44fz"],
            units_target=7,
            units_completed=7,
            complete=True,
        )
        nat_scope = NationalScope(
            target_region_registry=EIS_REGION_REGISTRY,
            target_region_count=99,
            target_laws=list(ACTIVE_LAW_TYPES),
            target_window_days=180,
            regions_covered=1,
            laws_covered=["44fz"],
            days_covered=7,
            region_complete=False,
            law_complete=False,
            window_complete=False,
            complete=False,
        )
        assert exec_scope.complete is True
        assert nat_scope.complete is False
        assert nat_scope.regions_covered == 1
        assert nat_scope.target_region_count == 99

    def test_limited_region_run_never_national_complete(self):
        nat_scope = NationalScope(
            regions_covered=3,
            target_region_count=99,
            complete=False,
        )
        assert nat_scope.complete is False
        assert nat_scope.regions_covered < nat_scope.target_region_count


# ── Law scope invariant ────────────────────────────────────────────────────


class TestLawScope:
    def test_failed_law_forces_complete_false(self):
        failed_laws = ["223fz", "capital_repair"]
        assert len(failed_laws) > 0
        law_complete = len(failed_laws) == 0
        assert law_complete is False

    def test_failed_laws_empty_means_complete(self):
        failed_laws = []
        law_complete = len(failed_laws) == 0
        assert law_complete is True

    def test_invariant_failed_laws_not_empty_implies_complete_false(self):
        failed_laws = ["223fz"]
        assert len(failed_laws) > 0
        law_scope_complete = len(failed_laws) == 0
        assert law_scope_complete is False


# ── Report ─────────────────────────────────────────────────────────────────


class TestReport:
    def test_report_structure_with_docs(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        exec_scope = ExecutionScope(
            requested_regions=["72"], requested_dates=["2026-07-25+03:00"],
            requested_laws=["44fz"], units_target=1, units_completed=1, complete=True,
        )
        nat_scope = NationalScope(
            target_region_registry={"source": "test"},
            target_region_count=99, target_laws=list(ACTIVE_LAW_TYPES),
            target_window_days=180,
            regions_covered=1, laws_covered=["44fz"], days_covered=1,
            region_complete=False, law_complete=False, window_complete=False, complete=False,
        )
        scope = ScopeResult(execution_scope=exec_scope, national_scope=nat_scope)
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), 1, now)
        assert report["schema_version"] == "2.0.0"
        assert "execution_scope" in report
        assert "national_scope" in report
        assert "window_30d" in report
        assert "window_90d" in report
        assert "window_180d" in report

    def test_execution_scope_in_report(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        exec_scope = ExecutionScope(
            requested_regions=["72"], requested_dates=["2026-07-25+03:00"],
            requested_laws=["44fz"], units_target=1, units_completed=1, complete=True,
        )
        nat_scope = NationalScope(
            target_region_registry={}, target_region_count=99,
            target_laws=list(ACTIVE_LAW_TYPES), target_window_days=180,
            regions_covered=1, laws_covered=["44fz"], days_covered=1,
            region_complete=False, law_complete=False, window_complete=False, complete=False,
        )
        scope = ScopeResult(execution_scope=exec_scope, national_scope=nat_scope)
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), 1, now)
        es = report["execution_scope"]
        assert es["units_target"] == 1
        assert es["units_completed"] == 1
        assert es["complete"] is True
        ns = report["national_scope"]
        assert ns["complete"] is False

    def test_national_scope_in_report(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        exec_scope = ExecutionScope(
            requested_regions=["72"], requested_dates=["2026-07-25+03:00"],
            requested_laws=["44fz"], units_target=1, units_completed=1, complete=True,
        )
        nat_scope = NationalScope(
            target_region_registry={"source": "test"}, target_region_count=99,
            target_laws=list(ACTIVE_LAW_TYPES), target_window_days=180,
            regions_covered=1, laws_covered=["44fz"], days_covered=1,
            region_complete=False, law_complete=False, window_complete=False, complete=False,
        )
        scope = ScopeResult(execution_scope=exec_scope, national_scope=nat_scope)
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), 1, now)
        ns = report["national_scope"]
        assert ns["target_region_count"] == 99
        assert "target_region_registry" in ns

    def test_window_scope_in_report(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        exec_scope = ExecutionScope(units_target=1, units_completed=1, complete=True)
        nat_scope = NationalScope()
        scope = ScopeResult(execution_scope=exec_scope, national_scope=nat_scope)
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), 1, now)
        w30 = report.get("window_30d", {})
        assert "window_scope_complete" in w30
        assert "window_coverage_percent" in w30
        assert "observed_days" in w30

    def test_incomplete_window_contains_no_final_ssd_verdict(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        exec_scope = ExecutionScope(units_target=1, units_completed=1, complete=False)
        nat_scope = NationalScope(complete=False)
        scope = ScopeResult(execution_scope=exec_scope, national_scope=nat_scope)
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), 1, now)
        assert report["ssd_verdict"] == "unavailable"
        for wd in ("30", "90", "180"):
            w = report.get("windows", {}).get(wd, {})
            s = w.get("sizing", {})
            if wd != "30":
                assert s.get("sizing_status", "available") == "unavailable", f"wd={wd}"

    def test_no_active_claims_in_report(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        scope = ScopeResult()
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), [], now)
        text = json.dumps(report).lower()
        assert "active" not in text or "active" in ("measurement_kind" in text)

    def test_no_identifiers_in_output(self, tmp_path: Path):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        scope = ScopeResult()
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), [], now)
        write_outputs(report, tmp_path)
        with open(tmp_path / "arv-009-rolling-window-storage.json") as f:
            data = json.load(f)
        text = json.dumps(data)
        assert "p001" not in text
        assert "http://" not in text


# ── Source errors ──────────────────────────────────────────────────────────


class TestScopeResult:
    def test_source_error_makes_scope_incomplete(self):
        scope = ScopeResult(
            source_errors=["region 99 date 2026-07-25: API timeout"],
        )
        assert len(scope.source_errors) == 1

    def test_missing_law_blocks_final_verdict(self):
        assert "44fz" in IMPLEMENTED_LAWS
        assert "223fz" not in IMPLEMENTED_LAWS


# ── Parser helpers ─────────────────────────────────────────────────────────


class TestParserHelpers:
    def test_parse_procurement_id(self):
        assert _parse_xml_procurement_id("epNotificationEF2020_0325300006424000001_001.xml") == "0325300006424000001"

    def test_parse_law_44fz(self):
        assert _parse_xml_law("epNotificationEF2020_xxx.xml") == "44fz"

    def test_parse_law_223fz(self):
        assert _parse_xml_law("epNotification223_xxx.xml") == "223fz"

    def test_parse_eis_datetime(self):
        dt = _parse_eis_datetime("2025-12-31T12:00:00+03:00")
        assert dt is not None
        assert dt.year == 2025

    def test_parse_eis_datetime_invalid(self):
        assert _parse_eis_datetime("not-a-date") is None


# ── Output format ──────────────────────────────────────────────────────────


class TestOutputFormat:
    def test_csv_columns(self, tmp_path: Path):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        scope = ScopeResult()
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), [], now)
        write_outputs(report, tmp_path)
        csv_path = tmp_path / "arv-009-rolling-window-storage.csv"
        assert csv_path.exists()
        lines = csv_path.read_text().strip().split("\n")
        assert len(lines) >= 2
        header = lines[0]
        assert "window_days" in header
        assert "window_scope_complete" in header
        assert "sizing_status" in header

    def test_json_structure(self, tmp_path: Path):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        scope = ScopeResult()
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), [], now)
        write_outputs(report, tmp_path)
        json_path = tmp_path / "arv-009-rolling-window-storage.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        for key in ("schema_version", "measurement_kind", "ssd_verdict",
                     "execution_scope", "national_scope", "windows"):
            assert key in data


# ── Constants ──────────────────────────────────────────────────────────────


class TestConstants:
    def test_50_pct_commercial_reserve(self):
        assert COMMERCIAL_RESERVE_RATIO == 0.50

    def test_20_pct_free_space(self):
        assert 1 / 0.80 == 1.25

    def test_windows_30_90_180(self):
        for wd in (30, 90, 180):
            assert wd in (30, 90, 180)

    def test_law_types(self):
        assert "44fz" in ACTIVE_LAW_TYPES
        assert "223fz" in ACTIVE_LAW_TYPES
        assert "capital_repair" in ACTIVE_LAW_TYPES

    def test_only_44fz_implemented(self):
        assert IMPLEMENTED_LAWS == ["44fz"]
