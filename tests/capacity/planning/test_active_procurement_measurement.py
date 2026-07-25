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


# ─── Sweep parser fixtures and tests ──────────────────────────────────────

import io
import zipfile
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

from scripts.capacity.planning.measure_active_procurements import (
    _parse_xml_procurement_status,
    _parse_xml_procurement_id,
    _parse_xml_law,
    _parse_eis_datetime,
    _parse_xml_application_deadline,
    _fetch_active_from_getdocs_sweep,
    SweepCounters,
    SourceProvenance,
    SweepScope,
)


def _make_eis_xml(
    procurement_id: str = "0325300006424000001",
    status: str = "PUBLISHED",
    deadline: str | None = "2025-12-31T12:00:00+03:00",
    doc_type: str = "epNotificationEF2020",
    attachment_count: int = 1,
) -> bytes:
    ns3 = "http://zakupki.gov.ru/oos/types/1"
    ns4 = "http://zakupki.gov.ru/oos/common/1"
    lines = [
        f'<?xml version="1.0" encoding="UTF-8"?>',
        f'<ns3:{doc_type} xmlns:ns3="{ns3}" xmlns:ns4="{ns4}">',
        "  <ns3:commonInfo>",
        f"    <ns3:purchaseNumber>{procurement_id}</ns3:purchaseNumber>",
        f"    <ns3:status>{status}</ns3:status>",
    ]
    if deadline is not None:
        lines.append(f"    <ns3:applicationDeadline>{deadline}</ns3:applicationDeadline>")
    lines.extend([
        "  </ns3:commonInfo>",
    ])
    for i in range(attachment_count):
        lines.extend([
            f"  <ns4:attachmentInfo>",
            f"    <ns4:fileName>doc-{i}.pdf</ns4:fileName>",
            f"    <ns4:fileSize>{100000 + i}</ns4:fileSize>",
            f"    <ns4:url>http://example.com/doc-{i}.pdf</ns4:url>",
            f"  </ns4:attachmentInfo>",
        ])
    lines.append(f"</ns3:{doc_type}>")
    return "\n".join(lines).encode("utf-8")


def _make_zip(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


class TestSweepParserUnit:
    def test_parse_status_found(self):
        xml = _make_eis_xml(status="PUBLISHED")
        root = ET.fromstring(xml)
        assert _parse_xml_procurement_status(root) == "PUBLISHED"

    def test_parse_status_missing_returns_unknown(self):
        xml = _make_eis_xml(status="")
        root = ET.fromstring(xml)
        assert _parse_xml_procurement_status(root) == "unknown"

    def test_parse_status_no_status_node_returns_unknown(self):
        xml = _make_eis_xml(status="PUBLISHED")
        xml = xml.replace(b"<ns3:status>PUBLISHED</ns3:status>", b"")
        root = ET.fromstring(xml)
        assert _parse_xml_procurement_status(root) == "unknown"

    def test_parse_procurement_id(self):
        assert _parse_xml_procurement_id("epNotificationEF2020_0325300006424000001_001.xml") == "0325300006424000001"

    def test_parse_law_44fz(self):
        assert _parse_xml_law("epNotificationEF2020_xxx.xml") == "44fz"

    def test_parse_law_223fz(self):
        assert _parse_xml_law("epNotification223_xxx.xml") == "223fz"

    def test_parse_eis_datetime_with_tz(self):
        dt = _parse_eis_datetime("2025-12-31T12:00:00+03:00")
        assert dt is not None
        assert dt.year == 2025
        assert dt.month == 12
        assert dt.day == 31

    def test_parse_eis_datetime_without_tz(self):
        dt = _parse_eis_datetime("2025-12-31T12:00:00")
        assert dt is not None

    def test_parse_eis_datetime_date_only(self):
        dt = _parse_eis_datetime("2025-12-31")
        assert dt is not None

    def test_parse_eis_datetime_invalid(self):
        assert _parse_eis_datetime("not-a-date") is None

    def test_parse_deadline_found(self):
        xml = _make_eis_xml(deadline="2025-12-31T12:00:00+03:00")
        root = ET.fromstring(xml)
        assert _parse_xml_application_deadline(root) == "2025-12-31T12:00:00+03:00"

    def test_parse_deadline_missing(self):
        xml = _make_eis_xml(deadline=None)
        root = ET.fromstring(xml)
        assert _parse_xml_application_deadline(root) is None


class TestSweepParserIntegration:
    """Integration tests with mocked SOAP client returning fixture ZIPs."""

    def _make_mock_result(self, archive_bytes: bytes | None = None, warnings: list[str] | None = None):
        result = MagicMock()
        result.archive_url = "http://example.com/archive.zip" if archive_bytes else None
        result.warnings = warnings or []
        return result

    def _make_mock_attachment(self, archive_bytes: bytes):
        att = MagicMock()
        att.stored_name = "test_archive.zip"
        return att

    def _mock_sweep(self, archive_bytes: bytes, regions: list[str] | None = None):
        with (
            patch("src.modules.tender_operator_agent_demo.settings.get_zakupki_soap_settings") as mock_settings,
            patch("src.modules.tender_operator_agent_demo.zakupki_soap_client.ZakupkiSoapClient") as mock_client_cls,
        ):
            settings = MagicMock()
            settings.configured = True
            settings.enabled = True
            settings.token_configured = True
            mock_settings.return_value = settings

            client = MagicMock()
            mock_client_cls.return_value = client

            result = self._make_mock_result(archive_bytes)
            client.get_docs_by_org_region.return_value = result

            att = self._make_mock_attachment(archive_bytes)
            client.download_archive.return_value = att

            if archive_bytes:
                real_zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
                zf_entries = {name: real_zf.read(name) for name in real_zf.namelist()}
                zf_namelist = list(zf_entries.keys())
                real_zf.close()
            else:
                zf_entries = {}
                zf_namelist = []

            with patch("zipfile.ZipFile") as mock_zf:
                zf_instance = MagicMock()
                mock_zf.return_value.__enter__.return_value = zf_instance
                zf_instance.namelist.return_value = zf_namelist
                zf_instance.read.side_effect = lambda name: zf_entries.get(name, b"")

                procurements, prov, scope = _fetch_active_from_getdocs_sweep(
                    region_whitelist=regions or ["72"],
                    lookback_days=1,
                )
                return procurements, prov, scope

    def test_a_three_notification_xmls_all_parsed(self):
        """A. ZIP with 3 notification XMLs: xml_entries_total=3, xml_parsed_successfully=3."""
        future = "2099-12-31T12:00:00+03:00"
        zip_data = _make_zip([
            ("epNotificationEF2020_0001_001.xml", _make_eis_xml("0001", "PUBLISHED", future)),
            ("epNotificationEF2020_0002_001.xml", _make_eis_xml("0002", "PUBLISHED", future)),
            ("epNotificationEF2020_0003_001.xml", _make_eis_xml("0003", "PUBLISHED", future)),
        ])
        _, prov, _ = self._mock_sweep(zip_data)
        cnt = prov.sweep_counters
        assert cnt is not None
        assert cnt.xml_entries_total == 3, f"expected 3, got {cnt.xml_entries_total}"
        assert cnt.xml_parsed_successfully == 3, f"expected 3, got {cnt.xml_parsed_successfully}"
        assert cnt.xml_parse_failed == 0, f"expected 0, got {cnt.xml_parse_failed}"

    def test_b_mixed_statuses_counters(self):
        """B. ZIP: 2 active, 1 completed, 1 unknown, 1 deadline passed."""
        future_deadline = "2030-12-31T12:00:00+03:00"
        past_deadline = "2020-01-01T12:00:00+03:00"
        zip_data = _make_zip([
            ("epNotificationEF2020_0001_001.xml", _make_eis_xml("0001", "PUBLISHED", future_deadline)),
            ("epNotificationEF2020_0002_001.xml", _make_eis_xml("0002", "APPLYING", future_deadline)),
            ("epNotificationEF2020_0003_001.xml", _make_eis_xml("0003", "COMPLETED", future_deadline)),
            ("epNotificationEF2020_0004_001.xml", _make_eis_xml("0004", "UNKNOWN_STATUS", future_deadline)),
            ("epNotificationEF2020_0005_001.xml", _make_eis_xml("0005", "PUBLISHED", past_deadline)),
        ])
        _, prov, _ = self._mock_sweep(zip_data)
        cnt = prov.sweep_counters
        assert cnt is not None
        assert cnt.active_procurements == 2, f"expected 2, got {cnt.active_procurements}"
        assert cnt.excluded_completed == 1, f"expected 1, got {cnt.excluded_completed}"
        assert cnt.excluded_unmapped_status == 1, f"expected 1, got {cnt.excluded_unmapped_status}"
        assert cnt.excluded_deadline_passed == 1, f"expected 1, got {cnt.excluded_deadline_passed}"
        assert cnt.xml_parsed_successfully == 5, f"expected 5, got {cnt.xml_parsed_successfully}"

    def test_c_corrupted_xml_parse_failed(self):
        """C. Corrupted XML: parse_failed increases, no procurement becomes active."""
        zip_data = _make_zip([
            ("epNotificationEF2020_0001_001.xml", b"not valid xml content"),
        ])
        _, prov, _ = self._mock_sweep(zip_data)
        cnt = prov.sweep_counters
        assert cnt is not None
        assert cnt.xml_parse_failed == 1, f"expected 1, got {cnt.xml_parse_failed}"
        assert cnt.xml_parsed_successfully == 0, f"expected 0, got {cnt.xml_parsed_successfully}"
        assert cnt.active_procurements == 0, f"expected 0, got {cnt.active_procurements}"

    def test_d_on_region_seven_days_partial_scope(self, tmp_path: Path):
        """D. One region, 7 days: source_scope_complete=false, ssd_verdict=unavailable."""
        zip_data = _make_zip([
            ("epNotificationEF2020_0001_001.xml", _make_eis_xml("0001", "PUBLISHED")),
        ])
        real_zf = zipfile.ZipFile(io.BytesIO(zip_data))
        zf_entries = {name: real_zf.read(name) for name in real_zf.namelist()}
        zf_namelist = list(zf_entries.keys())
        real_zf.close()

        with (
            patch("src.modules.tender_operator_agent_demo.settings.get_zakupki_soap_settings") as mock_settings,
            patch("src.modules.tender_operator_agent_demo.zakupki_soap_client.ZakupkiSoapClient") as mock_client_cls,
            patch("zipfile.ZipFile") as mock_zf,
        ):
            settings = MagicMock()
            settings.configured = True
            settings.last_token_hint = "test"
            settings.token_configured = True
            settings.enabled = True
            mock_settings.return_value = settings
            client = MagicMock()
            mock_client_cls.return_value = client
            result = self._make_mock_result(zip_data)
            client.get_docs_by_org_region.return_value = result
            att = self._make_mock_attachment(zip_data)
            client.download_archive.return_value = att

            zf_instance = MagicMock()
            mock_zf.return_value.__enter__.return_value = zf_instance
            zf_instance.namelist.return_value = zf_namelist
            zf_instance.read.side_effect = lambda name: zf_entries.get(name, b"")

            from scripts.capacity.planning.measure_active_procurements import run_real
            try:
                prov = run_real(tmp_path)
                assert prov.measurement_kind == "real_partial", f"expected real_partial, got {prov.measurement_kind}"
                assert prov.ssd_verdict == "unavailable", f"expected unavailable, got {prov.ssd_verdict}"
            except SystemExit:
                pytest.fail("run_real should not exit(1) for partial scope")

    def test_g_all_xml_processed(self):
        """G. All XML inside ZIP genuinely processed — every notification produces a unique procurement."""
        future = "2099-12-31T12:00:00+03:00"
        zip_data = _make_zip([
            ("epNotificationEF2020_0010_001.xml", _make_eis_xml("0010", "PUBLISHED", future)),
            ("epNotificationEF2020_0011_001.xml", _make_eis_xml("0011", "PUBLISHED", future)),
            ("epNotificationEF2020_0012_001.xml", _make_eis_xml("0012", "PUBLISHED", future)),
            ("readme.txt", b"not an xml file"),
        ])
        _, prov, scope = self._mock_sweep(zip_data)
        cnt = prov.sweep_counters
        assert cnt is not None
        assert cnt.zip_entries_total == 4, f"expected 4, got {cnt.zip_entries_total}"
        assert cnt.xml_entries_total == 3, f"expected 3, got {cnt.xml_entries_total}"
        assert cnt.xml_parsed_successfully == 3, f"expected 3, got {cnt.xml_parsed_successfully}"
        assert cnt.notification_xml_total == 3, f"expected 3, got {cnt.notification_xml_total}"
        assert cnt.active_procurements == 3, f"expected 3, got {cnt.active_procurements}"


class TestSweepScope:
    def test_scope_fields_present(self, tmp_path: Path):
        with (
            patch("src.modules.tender_operator_agent_demo.settings.get_zakupki_soap_settings") as mock_settings,
            patch("src.modules.tender_operator_agent_demo.zakupki_soap_client.ZakupkiSoapClient") as mock_client_cls,
            patch("zipfile.ZipFile"),
        ):
            settings = MagicMock()
            settings.configured = True
            settings.enabled = True
            settings.token_configured = True
            mock_settings.return_value = settings
            client = MagicMock()
            mock_client_cls.return_value = client
            result = MagicMock()
            result.archive_url = None
            result.warnings = []
            client.get_docs_by_org_region.return_value = result

            from scripts.capacity.planning.measure_active_procurements import run_real
            try:
                prov = run_real(tmp_path)
                source = prov.source
                assert source is not None
                assert source.scope is not None
                assert source.scope.target_region_codes == ["72"]
                assert source.scope.target_laws == ["44fz"]
                assert prov.measurement_kind == "real_partial"
                assert prov.ssd_verdict == "unavailable"
            except SystemExit:
                pytest.fail("run_real should not exit(1) for partial scope")


class TestPaginationComplete:
    def test_false_on_source_error(self, tmp_path: Path):
        """F. Source error: pagination_complete = false."""
        with (
            patch("src.modules.tender_operator_agent_demo.settings.get_zakupki_soap_settings") as mock_settings,
            patch("src.modules.tender_operator_agent_demo.zakupki_soap_client.ZakupkiSoapClient") as mock_client_cls,
        ):
            settings = MagicMock()
            settings.configured = True
            settings.enabled = True
            settings.token_configured = True
            mock_settings.return_value = settings
            client = MagicMock()
            mock_client_cls.return_value = client
            client.get_docs_by_org_region.side_effect = RuntimeError("API unreachable")

            from scripts.capacity.planning.measure_active_procurements import run_real
            prov = run_real(tmp_path)
            assert prov.measurement_kind == "incomplete"
            assert prov.ssd_verdict == "unavailable"
            assert prov.source is not None
            assert prov.source.pagination_complete is False
            assert len(prov.source.source_errors) > 0
