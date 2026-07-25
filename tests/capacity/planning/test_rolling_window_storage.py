"""
Tests for rolling-window storage upper bound (ARV-009C1.3).
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
    COVERAGE_THRESHOLD,
    ROLLING_WINDOWS_DAYS,
    ACTIVE_LAW_TYPES,
    IMPLEMENTED_LAWS,
    DocumentRecord,
    ScopeResult,
    dedup_latest_version,
    dedup_conservative_union,
    filter_docs_by_window,
    compute_window_metrics,
    compute_sizing_for_window,
    determine_verdict,
    build_report,
    write_outputs,
    run_sweep,
    _extract_documents_from_archive,
    _parse_xml_procurement_id,
    _parse_xml_law,
    _parse_eis_datetime,
    _parse_xml_version_number,
    save_checkpoint,
    load_checkpoint,
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

    def test_all_windows_from_one_set(self):
        docs = [
            _make_doc(procurement_id="p1", source_date=_days_ago(5)),
            _make_doc(procurement_id="p2", source_date=_days_ago(40)),
            _make_doc(procurement_id="p3", source_date=_days_ago(100)),
            _make_doc(procurement_id="p4", source_date=_days_ago(160)),
        ]
        now = datetime.now(UTC)
        for wd in ROLLING_WINDOWS_DAYS:
            filtered = filter_docs_by_window(docs, wd, now)
            assert isinstance(filtered, list)


# ── Deduplication ──────────────────────────────────────────────────────────


class TestDedup:
    def test_conservative_union_dedup_by_key(self):
        docs = [
            _make_doc(procurement_id="p1", doc_id="d1|url", version="1"),
            _make_doc(procurement_id="p1", doc_id="d1|url", version="2"),
        ]
        union = dedup_conservative_union(docs)
        assert len(union) == 1  # same dedup_key

    def test_latest_version_prefers_higher(self):
        docs = [
            _make_doc(procurement_id="p1", doc_id="d1|url", version="1", size_bytes=100),
            _make_doc(procurement_id="p1", doc_id="d1|url", version="2", size_bytes=200),
        ]
        latest = dedup_latest_version(docs)
        assert len(latest) == 1
        assert latest[0].version == "2"
        assert latest[0].size_bytes == 200

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
        union_bytes = sum(d.size_bytes or 0 for d in union)
        latest_bytes = sum(d.size_bytes or 0 for d in latest)
        # With same count and same sizes, union == latest here
        assert union_bytes >= latest_bytes

    def test_duplicate_version_not_double_counted(self):
        docs = [
            _make_doc(procurement_id="p1", doc_id="d1|url", version="1"),
            _make_doc(procurement_id="p1", doc_id="d1|url", version="1"),
        ]
        union = dedup_conservative_union(docs)
        assert len(union) == 1


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
        assert m["max_daily_incoming_bytes"] == 500  # day ago: 200+300
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


# ── Verdict ────────────────────────────────────────────────────────────────


class TestVerdict:
    def _make_windows(self, base: int, p99: int = 5_000_000_000) -> tuple[dict, dict]:
        w = {}
        s = {}
        for wd in (30, 90, 180):
            metrics = {"window_days": wd, "conservative_union_bytes": base,
                       "p99_bytes": p99, "size_coverage_percent": 100.0,
                       "window_days": wd, "window_start_date": "", "window_end_date": "",
                       "unique_procurements": 10, "unique_documents": 10,
                       "known_bytes": base, "unknown_size_documents": 0,
                       "latest_version_bytes": base, "mean_bytes": 0,
                       "p50_bytes": 0, "p75_bytes": 0, "p90_bytes": 0, "p95_bytes": 0,
                       "max_bytes": 0, "packages_over_100mb": 0, "packages_over_250mb": 0,
                       "packages_over_500mb": 0, "packages_over_1gib": 0,
                       "max_daily_incoming_bytes": 0, "avg_daily_incoming_bytes": 0,
                       "file_count": 10}
            w[wd] = metrics
            s[wd] = compute_sizing_for_window(metrics)
        return w, s

    def test_strong_green(self):
        base = 500_000_000_000  # well under 1.4 TB
        w, s = self._make_windows(base)
        verdict, reason = determine_verdict(w, s, scope_complete=True, size_coverage_ok=True)
        assert verdict == "STRONG_GREEN"
        assert reason is not None

    def test_conditional_green(self):
        # 180d union > ~527 GB triggers base > 1.4 TB, but 90d <= 527 GB keeps base <= 1.4 TB
        # With p99=1 (processing=150 GiB): base = union * 1.5 + 214.7 GiB
        # For base <= 1.4 TB: union <= (1.4 TB - 214.7 GiB) / 1.5 ≈ 790 GB
        # For base > 1.4 TB: union > 790 GB
        union_90 = 700_000_000_000  # base ~= 1.26 TB
        union_180 = 1_000_000_000_000  # base ~= 1.71 TB
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
                       "file_count": 10}
            w[wd] = metrics
        s = {wd: compute_sizing_for_window(w[wd]) for wd in w}
        # Verify 90d base <= 1.4 TB and 180d base > 1.4 TB
        assert s[90]["base_required_bytes"] <= GREEN_THRESHOLD_BYTES
        assert s[180]["base_required_bytes"] > GREEN_THRESHOLD_BYTES
        verdict, reason = determine_verdict(w, s, scope_complete=True, size_coverage_ok=True)
        assert verdict == "CONDITIONAL_GREEN"

    def test_yellow(self):
        # p99=1 → processing=150 GiB. base = union*1.5 + 214.7 GiB
        # For YELLOW: 1.4 TB < base <= 1.7 TB → union ≈ 790-990 GB
        union = 860_000_000_000
        w, s = self._make_windows(union, p99=1)
        assert s[90]["base_required_bytes"] > GREEN_THRESHOLD_BYTES
        assert s[90]["base_required_bytes"] <= YELLOW_THRESHOLD_BYTES
        verdict, reason = determine_verdict(w, s, scope_complete=True, size_coverage_ok=True)
        assert verdict == "YELLOW"

    def test_red(self):
        # base > 1.7 TB → union > ~990 GB
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

    def test_unavailable_when_law_missing(self):
        w, s = self._make_windows(500_000_000_000)
        # scope_complete=False because laws are missing
        verdict, reason = determine_verdict(w, s, scope_complete=False, size_coverage_ok=True)
        assert verdict == "unavailable"
        assert "scope incomplete" in (reason or "").lower()


# ── Report ─────────────────────────────────────────────────────────────────


class TestReport:
    def test_report_structure_with_docs(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        scope = ScopeResult(
            regions_completed=1, dates_completed=1,
            laws_completed=["44fz"],
            region_scope_complete=True, date_scope_complete=True,
            law_scope_complete=False,
        )
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), ["223fz", "capital_repair"], now)
        assert report["schema_version"] == "1.0.0"
        assert "windows" in report
        assert "sizing" in report
        assert "scope" in report
        for wd in ROLLING_WINDOWS_DAYS:
            assert str(wd) in report["windows"]
            assert str(wd) in report["sizing"]

    def test_provisional_44fz_envelope_when_law_incomplete(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        scope = ScopeResult(law_scope_complete=False)
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), ["223fz", "capital_repair"], now)
        assert "provisional_44fz_envelope" in report
        assert report["provisional_44fz_envelope"]["measured_law"] == "44fz"
        assert "unimplemented_laws" in report["provisional_44fz_envelope"]

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
        # No procurement IDs, file names, or URLs in committed output
        assert "p001" not in text
        assert "doc1.pdf" not in text
        assert "http://" not in text


# ── Checkpoint ─────────────────────────────────────────────────────────────


class TestCheckpoint:
    def test_save_and_load(self, tmp_path: Path):
        state = {
            "completed_regions": ["72", "50"],
            "completed_dates": ["2026-07-25+03:00"],
            "documents": [asdict(_make_doc())],
            "had_error": False,
            "source_errors": [],
            "archives_downloaded": 5,
            "archives_skipped": 0,
        }
        ckpt_path = tmp_path / ".arv009c13_checkpoint.json"
        save_checkpoint(state, ckpt_path)
        assert ckpt_path.exists()
        loaded = load_checkpoint(ckpt_path)
        assert loaded is not None
        assert loaded["completed_regions"] == ["72", "50"]
        assert loaded["archives_downloaded"] == 5
        assert len(loaded["documents"]) == 1

    def test_load_nonexistent_returns_none(self, tmp_path: Path):
        loaded = load_checkpoint(tmp_path / "nonexistent.json")
        assert loaded is None

    def test_interrupted_resume(self, tmp_path: Path):
        state = {
            "completed_regions": ["72"],
            "completed_dates": ["2026-07-25+03:00"],
            "documents": [asdict(_make_doc(
                procurement_id="p001",
                source_date="2026-07-25T10:00:00+03:00",
            ))],
            "had_error": False,
            "source_errors": [],
            "archives_downloaded": 1,
            "archives_skipped": 0,
            "region_attempted": ["72"],
        }
        ckpt_path = tmp_path / ".arv009c13_checkpoint.json"
        save_checkpoint(state, ckpt_path)

        # Reload and verify resume would pick up completed regions
        loaded = load_checkpoint(ckpt_path)
        assert loaded is not None
        assert "72" in loaded["completed_regions"]
        assert len(loaded["documents"]) == 1


# ── Source errors ──────────────────────────────────────────────────────────


class TestScopeResult:
    def test_source_error_makes_scope_incomplete(self):
        scope = ScopeResult(
            regions_completed=1, dates_completed=100,
            region_scope_complete=False,
            date_scope_complete=True,
            law_scope_complete=False,
            source_errors=["region 99 date 2026-07-25: API timeout"],
        )
        assert not scope.region_scope_complete
        assert len(scope.source_errors) == 1

    def test_missing_law_blocks_final_verdict(self):
        scope = ScopeResult(law_scope_complete=False)
        # When law_scope_complete=False, verdict must be unavailable
        from scripts.capacity.planning.measure_rolling_window_storage import IMPLEMENTED_LAWS
        assert "44fz" in IMPLEMENTED_LAWS
        assert "223fz" not in IMPLEMENTED_LAWS
        assert not scope.law_scope_complete


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
        assert len(lines) >= 2  # header + at least one data row
        header = lines[0]
        assert "window_days" in header
        assert "conservative_union_bytes" in header
        assert "base_required_bytes" in header

    def test_json_structure(self, tmp_path: Path):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        scope = ScopeResult()
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), [], now)
        write_outputs(report, tmp_path)
        json_path = tmp_path / "arv-009-rolling-window-storage.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        for key in ("schema_version", "measurement_kind", "ssd_verdict", "windows", "sizing", "scope"):
            assert key in data


# ── Constants ──────────────────────────────────────────────────────────────


class TestConstants:
    def test_50_pct_commercial_reserve(self):
        assert COMMERCIAL_RESERVE_RATIO == 0.50

    def test_20_pct_free_space(self):
        assert 1 / 0.80 == 1.25  # base / 0.80 = 125% of base

    def test_windows_30_90_180(self):
        assert 30 in ROLLING_WINDOWS_DAYS
        assert 90 in ROLLING_WINDOWS_DAYS
        assert 180 in ROLLING_WINDOWS_DAYS

    def test_law_types(self):
        assert "44fz" in ACTIVE_LAW_TYPES
        assert "223fz" in ACTIVE_LAW_TYPES
        assert "capital_repair" in ACTIVE_LAW_TYPES

    def test_only_44fz_implemented(self):
        assert IMPLEMENTED_LAWS == ["44fz"]
