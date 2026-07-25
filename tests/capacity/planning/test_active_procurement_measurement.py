"""
Tests for the active procurement measurement module (ARV-009C1).
"""

from __future__ import annotations

import json
import math
import os
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import urllib.error

from scripts.capacity.planning.measure_active_procurements import (
    MAX_PROCESSING_CONCURRENCY,
    ONE_GIB,
    PROCESSING_SPACE_MIN_BYTES,
    PERSISTENT_RESULTS_AND_LOGS_BYTES,
    SSD_CAPACITY_DECIMAL_BYTES,
    COMMERCIAL_RESERVE_RATIO,
    COVERAGE_THRESHOLD,
    ACTIVE_LAW_TYPES,
    EXCLUDED_STATUSES,
    GREEN_THRESHOLD_BYTES,
    YELLOW_THRESHOLD_BYTES,
    ActiveProcurement,
    ByLawType,
    CoverageReport,
    DocumentInfo,
    DocumentSizeProvenance,
    MeasurementProvenance,
    SizingResult,
    SnapshotStats,
    SourceProvenance,
    classify_eis_status,
    compute_coverage,
    compute_sizing,
    compute_statistics,
    compute_by_law,
    format_bytes,
    is_active_eis_status,
    make_incomplete_provenance,
    run_demo,
    _generate_synthetic_packages,
    _try_content_length,
    _try_content_range,
    _try_stream,
)


# ─── Helpers ──────────────────────────────────────────────────────────────

def _make_doc(size: int | None = 1_000_000) -> DocumentInfo:
    return DocumentInfo(
        size_bytes=size,
        provenance=DocumentSizeProvenance(method="eis_metadata"),
    )


def _make_pkg(
    docs: list | None = None,
    law: str = "44fz",
    status: str = "published",
    procurement_id: str = "test-0001",
) -> ActiveProcurement:
    docs = docs or [_make_doc()]
    return ActiveProcurement(
        procurement_id=procurement_id,
        law=law,
        status=status,
        documents=docs,
    )


# ─── Canonical status mapping ─────────────────────────────────────────────

class TestStatusMapping:
    def test_active_statuses(self):
        for s in ("published", "applying", "active", "submission", "open"):
            assert is_active_eis_status(s) is True, f"{s!r} should be active"
            assert classify_eis_status(s) == "active"

    def test_excluded_explicit(self):
        for s in EXCLUDED_STATUSES:
            assert is_active_eis_status(s) is False
            assert classify_eis_status(s) == "excluded_explicit"

    def test_unknown_status(self):
        assert is_active_eis_status("unknown_garbage") is False
        assert classify_eis_status("unknown_garbage") == "excluded_unmapped"

    def test_none_status(self):
        assert is_active_eis_status(None) is False
        assert classify_eis_status(None) == "excluded_missing"

    def test_case_insensitive(self):
        assert is_active_eis_status("PUBLISHED") is True
        assert is_active_eis_status("COMPLETED") is False

    def test_empty_status(self):
        assert is_active_eis_status("") is False


# ─── Coverage gate ────────────────────────────────────────────────────────

class TestCoverageGate:
    def test_full_coverage_accepted(self):
        pkgs = [_make_pkg(law="44fz") for _ in range(20)]
        cov = compute_coverage(pkgs)
        assert cov.procurement_coverage_percent == 100.0
        assert cov.known_size_coverage_percent == 100.0

    def test_95_percent_accepted(self):
        pkgs = [_make_pkg(status="published") for _ in range(19)]
        pkgs.append(_make_pkg(docs=[_make_doc(size=None)], status="published"))
        cov = compute_coverage(pkgs)
        assert cov.procurement_coverage_percent == 100.0
        assert cov.documents_with_unknown_size == 1
        assert cov.documents_total == sum(p.doc_count for p in pkgs)
        known_ratio = cov.documents_with_known_size / cov.documents_total * 100
        assert known_ratio >= COVERAGE_THRESHOLD

    def test_9499_percent_rejected(self):
        total_docs = 200
        unknown = 11
        known = total_docs - unknown
        ratio = known / total_docs * 100
        assert ratio < COVERAGE_THRESHOLD, f"{ratio} should be < {COVERAGE_THRESHOLD}"

        pkgs = [
            _make_pkg(docs=[_make_doc(size=1_000_000) for _ in range(known // 5 + 1)])
            for _ in range(5)
        ]
        pkgs.append(_make_pkg(docs=[_make_doc(size=None) for _ in range(unknown)]))
        cov = compute_coverage(pkgs)
        assert cov.known_size_coverage_percent < COVERAGE_THRESHOLD

    def test_empty_passthrough(self):
        cov = compute_coverage([])
        assert cov.procurement_coverage_percent == 100.0
        assert cov.known_size_coverage_percent == 100.0

    def test_excluded_counts(self):
        cov = compute_coverage([], excluded_unmapped=5, excluded_explicit=10)
        assert cov.excluded_unmapped_status == 5
        assert cov.excluded_explicit == 10


# ─── Statistics ───────────────────────────────────────────────────────────

class TestStatistics:
    def test_empty(self):
        stats = compute_statistics([])
        assert stats.active_procurements == 0
        assert stats.known_bytes == 0
        assert stats.mean_bytes == 0.0

    def test_single_procurement(self):
        pkg = _make_pkg(docs=[_make_doc(1_000_000)])
        stats = compute_statistics([pkg])
        assert stats.active_procurements == 1
        assert stats.known_bytes == 1_000_000
        assert stats.p50_bytes == 1_000_000
        assert stats.p99_bytes == 1_000_000

    def test_percentiles(self):
        rng = random.Random(42)
        pkgs = [_make_pkg(docs=[_make_doc(rng.randint(100_000, 50_000_000))]) for _ in range(100)]
        stats = compute_statistics(pkgs)
        sizes = sorted([p.known_bytes for p in pkgs])
        n = len(sizes)
        p50_idx = max(0, min(n - 1, int(math.ceil(50 * n / 100) - 1)))
        p95_idx = max(0, min(n - 1, int(math.ceil(95 * n / 100) - 1)))
        assert stats.p50_bytes == sizes[p50_idx]
        assert stats.p95_bytes == sizes[p95_idx]

    def test_unknown_docs_not_counted_in_known(self):
        docs = [_make_doc(1_000_000), _make_doc(size=None)]
        pkg = _make_pkg(docs=docs)
        stats = compute_statistics([pkg])
        assert stats.known_bytes == 1_000_000
        assert stats.unknown_documents == 1

    def test_zero_known_not_in_percentile(self):
        pkg = _make_pkg(docs=[_make_doc(size=None)])
        stats = compute_statistics([pkg])
        assert stats.known_bytes == 0
        assert stats.p50_bytes == 0


# ─── Heavy tail ───────────────────────────────────────────────────────────

class TestHeavyTail:
    def test_monotonic(self):
        pkgs = [_make_pkg(docs=[_make_doc(random.randint(10_000, 100_000_000))]) for _ in range(50)]
        stats = compute_statistics(pkgs)
        assert stats.heavy_tail_top_1_pct <= stats.heavy_tail_top_5_pct
        assert stats.heavy_tail_top_5_pct <= stats.heavy_tail_top_10_pct

    def test_empty(self):
        stats = compute_statistics([])
        assert stats.heavy_tail_top_1_pct == 0.0


# ─── Large packages ───────────────────────────────────────────────────────

class TestLargePackages:
    def test_over_100mb(self):
        sizes = [50_000_000, 150_000_000, 200_000_000]
        pkgs = [_make_pkg(docs=[_make_doc(s)]) for s in sizes]
        stats = compute_statistics(pkgs)
        assert stats.packages_over_100mb == 2

    def test_over_1gib(self):
        sizes = [500_000_000, ONE_GIB + 1]
        pkgs = [_make_pkg(docs=[_make_doc(s)]) for s in sizes]
        stats = compute_statistics(pkgs)
        assert stats.packages_over_1gib == 1


# ─── By law type ──────────────────────────────────────────────────────────

class TestByLawType:
    def test_grouping(self):
        pkgs = [
            _make_pkg(law="44fz", docs=[_make_doc(100)]),
            _make_pkg(law="44fz", docs=[_make_doc(200)]),
            _make_pkg(law="223fz", docs=[_make_doc(300)]),
        ]
        by_law = compute_by_law(pkgs)
        law_map = {b.law_type: b for b in by_law}
        assert law_map["44fz"].tenders == 2
        assert law_map["44fz"].known_bytes == 300
        assert law_map["223fz"].tenders == 1
        assert law_map["223fz"].known_bytes == 300


# ─── Sizing ───────────────────────────────────────────────────────────────

class TestSizing:
    def _make_coverage(self) -> CoverageReport:
        return CoverageReport(
            active_procurements_total=100,
            active_procurements_with_document_manifest=100,
            procurement_coverage_percent=100.0,
            documents_total=500,
            documents_with_known_size=500,
            documents_with_unknown_size=0,
            known_size_coverage_percent=100.0,
        )

    def test_50_pct_commercial_reserve(self):
        stats = SnapshotStats(known_bytes=100_000_000_000, p99_bytes=1_000_000_000,
                              active_procurements=50, documents=200)
        sizing = compute_sizing(stats, self._make_coverage())
        expected = int(100_000_000_000 * COMMERCIAL_RESERVE_RATIO)
        assert sizing.commercial_reserve_bytes == expected

    def test_green(self):
        stats = SnapshotStats(known_bytes=500_000_000_000, p99_bytes=5_000_000_000,
                              active_procurements=100, documents=300)
        sizing = compute_sizing(stats, self._make_coverage())
        assert sizing.classification == "GREEN"

    def test_yellow(self):
        total = 900_000_000_000
        stats = SnapshotStats(known_bytes=total, p99_bytes=50_000_000_000,
                              active_procurements=100, documents=300)
        sizing = compute_sizing(stats, self._make_coverage())
        assert sizing.classification == "YELLOW"
        assert sizing.base_required_bytes > GREEN_THRESHOLD_BYTES
        assert sizing.base_required_bytes <= YELLOW_THRESHOLD_BYTES

    def test_red(self):
        total = 1_800_000_000_000
        stats = SnapshotStats(known_bytes=total, p99_bytes=50_000_000_000,
                              active_procurements=100, documents=300)
        sizing = compute_sizing(stats, self._make_coverage())
        assert sizing.classification == "RED"

    def test_unavailable_when_zero(self):
        stats = SnapshotStats(known_bytes=0, p99_bytes=0,
                              active_procurements=0, documents=0)
        sizing = compute_sizing(stats, self._make_coverage())
        assert sizing.classification == "unavailable"

    def test_processing_space_min(self):
        stats = SnapshotStats(known_bytes=1_000, p99_bytes=1_000,
                              active_procurements=1, documents=1)
        sizing = compute_sizing(stats, self._make_coverage())
        assert sizing.processing_space_bytes == PROCESSING_SPACE_MIN_BYTES

    def test_processing_space_p99(self):
        p99 = 100_000_000_000
        stats = SnapshotStats(known_bytes=1_000, p99_bytes=p99,
                              active_procurements=1, documents=1)
        sizing = compute_sizing(stats, self._make_coverage())
        expected = p99 * MAX_PROCESSING_CONCURRENCY
        assert sizing.processing_space_bytes == expected

    def test_persistent_constant(self):
        stats = SnapshotStats(known_bytes=0, p99_bytes=0,
                              active_procurements=0, documents=0)
        sizing = compute_sizing(stats, self._make_coverage())
        assert sizing.persistent_results_and_logs_bytes == PERSISTENT_RESULTS_AND_LOGS_BYTES

    def test_minimum_disk_80pct(self):
        stats = SnapshotStats(known_bytes=500_000_000_000, p99_bytes=5_000_000_000,
                              active_procurements=100, documents=300)
        sizing = compute_sizing(stats, self._make_coverage())
        base = sizing.base_required_bytes
        expected_min = int(base / 0.80)
        assert sizing.minimum_disk_bytes == expected_min
        assert sizing.minimum_disk_bytes >= base

    def test_disk_class_1tb(self):
        sizing = compute_sizing(SnapshotStats(
            known_bytes=100_000_000_000, p99_bytes=5_000_000_000,
            active_procurements=100, documents=300,
        ), self._make_coverage())
        assert sizing.next_practical_disk_class == "1 TB"

    def test_disk_class_2tb(self):
        sizing = compute_sizing(SnapshotStats(
            known_bytes=500_000_000_000, p99_bytes=5_000_000_000,
            active_procurements=100, documents=300,
        ), self._make_coverage())
        assert sizing.next_practical_disk_class == "2 TB"

    def test_disk_class_4tb(self):
        sizing = compute_sizing(SnapshotStats(
            known_bytes=1_500_000_000_000, p99_bytes=5_000_000_000,
            active_procurements=100, documents=300,
        ), self._make_coverage())
        assert sizing.next_practical_disk_class == "4 TB"

    def test_disk_class_8tb(self):
        sizing = compute_sizing(SnapshotStats(
            known_bytes=3_500_000_000_000, p99_bytes=5_000_000_000,
            active_procurements=100, documents=300,
        ), self._make_coverage())
        assert sizing.next_practical_disk_class == "8 TB"

    def test_ssd_capacity_gib_not_mixed(self):
        stats = SnapshotStats(known_bytes=500_000_000_000, p99_bytes=5_000_000_000,
                              active_procurements=100, documents=300)
        sizing = compute_sizing(stats, self._make_coverage())
        assert sizing.ssd_capacity_decimal_bytes == SSD_CAPACITY_DECIMAL_BYTES
        assert sizing.ssd_capacity_gib == pytest.approx(
            SSD_CAPACITY_DECIMAL_BYTES / ONE_GIB, rel=1e-6
        )

    def test_remaining_never_negative(self):
        stats = SnapshotStats(known_bytes=5_000_000_000_000, p99_bytes=50_000_000_000,
                              active_procurements=100, documents=300)
        sizing = compute_sizing(stats, self._make_coverage())
        assert sizing.remaining_bytes >= 0


# ─── Demo mode ────────────────────────────────────────────────────────────

class TestDemoMode:
    def test_synthetic_verdict_unavailable(self, tmp_path: Path):
        run_demo(tmp_path)
        with open(tmp_path / "arv-009-active-snapshot-summary.json") as f:
            data = json.load(f)
        assert data["measurement_kind"] == "synthetic"
        assert data["ssd_verdict"] == "unavailable"

    def test_synthetic_reason_present(self, tmp_path: Path):
        run_demo(tmp_path)
        with open(tmp_path / "arv-009-active-snapshot-summary.json") as f:
            data = json.load(f)
        assert "synthetic" in data["measurement_provenance"]["reason"].lower()

    def test_files_created(self, tmp_path: Path):
        run_demo(tmp_path)
        assert (tmp_path / "arv-009-active-snapshot-summary.json").exists()
        assert (tmp_path / "arv-009-active-snapshot-summary.csv").exists()


# ─── Real mode (no fallback) ─────────────────────────────────────────────

class TestRealModeNoFallback:
    def test_no_fallback_to_demo(self):
        with pytest.raises(SystemExit) as exc:
            from scripts.capacity.planning.measure_active_procurements import run_real
            import tempfile
            run_real(Path(tempfile.mkdtemp()))
        assert exc.value.code == 1

    def test_no_fallback_error_marker(self, tmp_path: Path):
        from scripts.capacity.planning.measure_active_procurements import run_real
        with pytest.raises(SystemExit):
            run_real(tmp_path)
        json_file = tmp_path / "arv-009-active-snapshot-summary.json"
        assert json_file.exists()
        with open(json_file) as f:
            data = json.load(f)
        assert data["measurement_kind"] == "incomplete"
        assert data["ssd_verdict"] == "unavailable"
        assert "ARV-009C1_REAL_MEASUREMENT_BLOCKED" in (data["measurement_provenance"]["reason"] or "") or \
               "BLOCKED" in data["measurement_provenance"]["reason"]


# ─── Privacy ──────────────────────────────────────────────────────────────

class TestPrivacy:
    def test_no_procurement_ids_in_committed(self, tmp_path: Path):
        run_demo(tmp_path)
        with open(tmp_path / "arv-009-active-snapshot-summary.json") as f:
            data = json.load(f)
        text = json.dumps(data)
        assert "demo-" not in text
        assert "test-" not in text

    def test_no_urls_in_committed(self, tmp_path: Path):
        run_demo(tmp_path)
        with open(tmp_path / "arv-009-active-snapshot-summary.json") as f:
            data = json.load(f)
        assert "file_name" not in json.dumps(data)
        assert "url" not in json.dumps(data)


# ─── Determinism ──────────────────────────────────────────────────────────

class TestDeterminism:
    def test_synthetic_json_deterministic(self, tmp_path: Path):
        p1 = tmp_path / "run1"
        p2 = tmp_path / "run2"
        run_demo(p1)
        run_demo(p2)
        with open(p1 / "arv-009-active-snapshot-summary.json") as f:
            d1 = json.load(f)
        with open(p2 / "arv-009-active-snapshot-summary.json") as f:
            d2 = json.load(f)
        d1["meta"]["generated_at"] = ""
        d1["measurement_provenance"]["snapshot_started_at_utc"] = ""
        d1["measurement_provenance"]["snapshot_completed_at_utc"] = ""
        d1["measurement_provenance"]["source"]["query_started_at"] = ""
        d1["measurement_provenance"]["source"]["query_completed_at"] = ""
        d2["meta"]["generated_at"] = ""
        d2["measurement_provenance"]["snapshot_started_at_utc"] = ""
        d2["measurement_provenance"]["snapshot_completed_at_utc"] = ""
        d2["measurement_provenance"]["source"]["query_started_at"] = ""
        d2["measurement_provenance"]["source"]["query_completed_at"] = ""
        assert d1 == d2


# ─── Incomplete provenance ────────────────────────────────────────────────

class TestIncompleteProvenance:
    def test_marker(self):
        prov = make_incomplete_provenance("test reason")
        assert prov.measurement_kind == "incomplete"
        assert prov.ssd_verdict == "unavailable"
        assert prov.reason == "test reason"
        assert prov.snapshot_date is not None

    def test_future_date_rejected(self):
        before = datetime.now(UTC)
        prov = make_incomplete_provenance("test")
        parsed = datetime.fromisoformat(prov.snapshot_started_at_utc)
        assert before - parsed <= timedelta(seconds=1)


# ─── Format helper ────────────────────────────────────────────────────────

class TestFormatBytes:
    def test_gib(self):
        assert "GiB" in format_bytes(ONE_GIB)

    def test_mb(self):
        assert "MB" in format_bytes(1_500_000)

    def test_bytes(self):
        assert format_bytes(500) == "500 B"


# ─── Size resolution (unit only — no network) ────────────────────────────

class TestSizeResolution:
    def test_content_length_no_network_url_fails(self):
        with pytest.raises((urllib.error.URLError, OSError)):
            _try_content_length("http://localhost:1/nonexistent")

    def test_content_range_no_network_url_fails(self):
        with pytest.raises((urllib.error.URLError, OSError)):
            _try_content_range("http://localhost:1/nonexistent")


# ─── Streamed size resolution ─────────────────────────────────────────────

class TestStreamedSize:
    def test_stream_fails_on_unreachable(self):
        with pytest.raises((urllib.error.URLError, OSError)):
            _try_stream("http://localhost:1/nonexistent")

    def test_stream_temp_file_cleaned_on_error(self):
        tmp_files_before = {
            f for f in os.listdir("/tmp") if f.startswith("tmp")
        } if os.path.isdir("/tmp") else set()
        try:
            _try_stream("http://localhost:1/nonexistent")
        except (urllib.error.URLError, OSError):
            pass
        tmp_files_after = {
            f for f in os.listdir("/tmp") if f.startswith("tmp")
        } if os.path.isdir("/tmp") else set()
        assert tmp_files_after == tmp_files_before or len(tmp_files_after - tmp_files_before) == 0
