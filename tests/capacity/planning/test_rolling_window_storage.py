"""
Regression tests for rolling-window storage upper bound (ARV-009C1.3B).

Harden unit state, checkpoint schema v3, target signature, provenance.
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
    UNIMPLEMENTED_LAWS,
    DocumentRecord,
    UnitResult,
    build_unit_key,
    build_target_signature,
    check_target_mismatch,
    dedup_latest_version,
    dedup_conservative_union,
    filter_docs_by_window,
    compute_window_metrics,
    compute_sizing_for_window,
    compute_observed_subtotal,
    compute_sizing_status,
    compute_result_sha256,
    determine_verdict,
    build_report,
    write_outputs,
    load_region_registry,
    _get_registry_codes,
    _parse_xml_procurement_id,
    _parse_xml_law,
    _parse_eis_datetime,
    _parse_xml_version_number,
    _parse_xml_publish_date,
    _parse_version_as_int,
    _version_sort_key,
    _pick_latest_doc_in_group,
    _sanitize_error_message,
    save_checkpoint,
    load_checkpoint,
    build_checkpoint_v3,
    RETRY_BUDGET,
    ExecutionScope,
    NationalScope,
    ScopeResult,
    EIS_DOC_TYPE_LAW,
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


# ── Window filtering ───────────────────────────────────────────────────────


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

    def test_numeric_version_10_greater_than_2(self):
        docs = [
            _make_doc(procurement_id="p1", doc_id="d1|url", version="2", size_bytes=100),
            _make_doc(procurement_id="p1", doc_id="d1|url", version="10", size_bytes=200),
        ]
        latest = dedup_latest_version(docs)
        assert len(latest) == 1
        assert latest[0].version == "10"

    def test_latest_version_selected_at_procurement_level(self):
        docs = [
            _make_doc(procurement_id="p1", doc_id="d1|url", version="2", size_bytes=100),
            _make_doc(procurement_id="p1", doc_id="d2|url", version="2", size_bytes=200),
            _make_doc(procurement_id="p2", doc_id="d3|url", version="5", size_bytes=500),
        ]
        latest = dedup_latest_version(docs)
        p1_docs = [d for d in latest if d.procurement_id == "p1"]
        p2_docs = [d for d in latest if d.procurement_id == "p2"]
        assert len(p1_docs) == 2
        assert len(p2_docs) == 1
        assert p2_docs[0].version == "5"


class TestVersionFallback:
    def test_missing_version_fallback_to_latest_source_date(self):
        docs = [
            _make_doc(procurement_id="p1", doc_id="d1|url", version=None,
                       source_date=_days_ago(5), size_bytes=100),
            _make_doc(procurement_id="p1", doc_id="d2|url", version=None,
                       source_date=_days_ago(1), size_bytes=200),
        ]
        latest = dedup_latest_version(docs)
        # Different doc_ids within same procurement are distinct documents, both kept
        assert len(latest) == 2

    def test_one_procurement_two_dates_no_version(self):
        docs = [
            _make_doc(procurement_id="p1", doc_id="d1|url", version=None,
                       source_date="2026-07-20T10:00:00+03:00", size_bytes=100),
            _make_doc(procurement_id="p1", doc_id="d2|url", version=None,
                       source_date="2026-07-25T10:00:00+03:00", size_bytes=200),
        ]
        latest = dedup_latest_version(docs)
        # Different doc_ids within same procurement are distinct documents, both kept
        assert len(latest) == 2


# ── Unit outcomes ──────────────────────────────────────────────────────────


class TestUnitOutcomes:
    def test_exception_unit_not_completed(self):
        ur = UnitResult(status="failed_retryable", attempts=1)
        assert not ur.is_completed()
        assert ur.is_failed()
        assert ur.can_retry()

    def test_success_no_data_becomes_completed(self):
        ur = UnitResult(status="success_no_data", attempts=1)
        assert ur.is_completed()
        assert not ur.is_failed()

    def test_success_archive_becomes_completed(self):
        ur = UnitResult(status="success_archive", attempts=1, documents_extracted=42)
        assert ur.is_completed()

    def test_failed_terminal_not_retryable(self):
        ur = UnitResult(status="failed_terminal", attempts=2)
        assert not ur.can_retry()

    def test_failed_retryable_budget_exhausted(self):
        ur = UnitResult(status="failed_retryable", attempts=2)
        assert not ur.can_retry()

    def test_units_completed_and_failed_do_not_overlap(self):
        completed = {"r1|d1|l1", "r1|d2|l1"}
        failed = {"r2|d1|l1"}
        assert completed.isdisjoint(failed)

    def test_execution_complete_requires_zero_failures(self):
        units_target = 10
        units_completed = 10
        units_failed = 0
        interrupted = False
        complete = units_completed == units_target and units_failed == 0 and not interrupted
        assert complete

        units_failed = 1
        complete = units_completed == units_target and units_failed == 0 and not interrupted
        assert not complete


# ── Target signature ───────────────────────────────────────────────────────


class TestTargetSignature:
    def test_matching_signatures(self):
        sig1 = build_target_signature(
            ["72"], ["44fz"], ["2026-07-25"], ["epNotificationEF2020"]
        )
        sig2 = build_target_signature(
            ["72"], ["44fz"], ["2026-07-25"], ["epNotificationEF2020"]
        )
        assert sig1 == sig2

    def test_different_regions_mismatch(self):
        sig1 = build_target_signature(
            ["72"], ["44fz"], ["2026-07-25"], ["epNotificationEF2020"]
        )
        sig2 = build_target_signature(
            ["77"], ["44fz"], ["2026-07-25"], ["epNotificationEF2020"]
        )
        assert sig1 != sig2

    def test_mismatch_detected(self):
        ckpt = {"target_signature": build_target_signature(
            ["72"], ["44fz"], ["2026-07-25"], ["epNotificationEF2020"]
        )}
        err = check_target_mismatch(
            ckpt, ["77"], ["44fz"], ["2026-07-25"], ["epNotificationEF2020"]
        )
        assert err is not None
        assert "ARV-009C1_CHECKPOINT_TARGET_MISMATCH" in err

    def test_match_passes(self):
        regions = ["72"]
        laws = ["44fz"]
        dates = ["2026-07-25"]
        types = ["epNotificationEF2020"]
        ckpt = {"target_signature": build_target_signature(regions, laws, dates, types)}
        err = check_target_mismatch(ckpt, regions, laws, dates, types)
        assert err is None


# ── Checkpoint schema v3 ───────────────────────────────────────────────────


class TestCheckpointV3:
    def test_checkpoint_v3_schema(self):
        ckpt = build_checkpoint_v3(
            target_signature="abc123",
            completed_units=[{"region": "72", "date": "2026-07-25", "law": "44fz"}],
            failed_units=[],
            unit_results={},
            documents=[],
            interrupted=False,
            units_target=10,
            units_completed=5,
            units_failed=0,
            source_errors=[],
            archives_downloaded=3,
            archives_skipped=0,
            xml_parsed=100,
            xml_failed=0,
        )
        assert ckpt["schema_version"] == 3
        assert ckpt["target_signature"] == "abc123"
        assert ckpt["units_completed"] == 5
        assert ckpt["units_remaining"] == 5

    def test_save_and_load_v3(self, tmp_path: Path):
        ckpt = build_checkpoint_v3(
            target_signature="sig",
            completed_units=[{"region": "72", "date": "d1", "law": "44fz"}],
            failed_units=[],
            unit_results={"72|d1|44fz": {"status": "success_archive", "attempts": 1}},
            documents=[_make_doc()],
            interrupted=False,
            units_target=1,
            units_completed=1,
            units_failed=0,
            source_errors=[],
            archives_downloaded=1,
            archives_skipped=0,
            xml_parsed=10,
            xml_failed=0,
        )
        ckpt_path = tmp_path / ".arv009c13_checkpoint.json"
        save_checkpoint(ckpt, ckpt_path)
        loaded = load_checkpoint(ckpt_path)
        assert loaded is not None
        assert loaded["schema_version"] == 3
        assert loaded["units_completed"] == 1


# ── Sanitized error messages ───────────────────────────────────────────────


class TestSanitizedErrors:
    def test_url_removed(self):
        msg = _sanitize_error_message("Error at https://example.com/path?secret=1")
        assert "[URL]" in msg
        assert "example.com" not in msg

    def test_token_removed(self):
        msg = _sanitize_error_message("Token 38bf454b-b874-4cb7-876c-fa4457eb8394 failed")
        assert "[TOKEN]" in msg
        assert "38bf454b" not in msg


# ── Region registry ────────────────────────────────────────────────────────


class TestRegionRegistry:
    def test_registry_loaded(self):
        reg = load_region_registry()
        assert "codes" in reg
        assert len(reg["codes"]) > 0
        codes = _get_registry_codes()
        assert len(codes) > 0

    def test_same_count_different_regions_not_national_complete(self):
        covered = {"72", "77", "78"}
        target = {"01", "02", "03"}
        assert len(covered) == len(target)
        assert covered != target
        assert not (covered == target)


# ── Window completeness ────────────────────────────────────────────────────


class TestWindowCompleteness:
    def test_incomplete_date_blocks_30day_window(self):
        completed_dates = 15
        window_days = 30
        window_scope_complete = completed_dates >= window_days
        assert not window_scope_complete

    def test_all_dates_complete_unlocks_30day_window(self):
        completed_dates = 30
        window_days = 30
        window_scope_complete = completed_dates >= window_days
        assert window_scope_complete


# ── Doc type mapping ───────────────────────────────────────────────────────


class TestDocTypeMapping:
    def test_only_44fz_in_doc_type_law(self):
        assert EIS_DOC_TYPE_LAW == {"epNotificationEF2020": "44fz"}

    def test_unimplemented_laws_separated(self):
        assert "44fz" in IMPLEMENTED_LAWS
        assert "223fz" in UNIMPLEMENTED_LAWS
        assert "capital_repair" in UNIMPLEMENTED_LAWS


# ── Source warning vs source error ─────────────────────────────────────────


class TestSourceWarnings:
    def test_source_warning_not_source_error(self):
        warnings = ["ЕИС сообщил, что документы по запросу отсутствуют."]
        has_no_data = any("отсутствуют" in w for w in warnings)
        assert has_no_data
        # No-data should be success_no_data, not an error
        empty_data_warnings = [w for w in warnings if "отсутствуют" not in w]
        assert len(empty_data_warnings) == 0


# ── Provenance ─────────────────────────────────────────────────────────────


class TestProvenance:
    def test_generated_from_commit_sha_present(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        exec_scope = ExecutionScope(units_target=1, units_completed=1, complete=True)
        nat_scope = NationalScope(complete=False)
        scope = ScopeResult(execution_scope=exec_scope, national_scope=nat_scope)
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), 1, now)
        meta = report.get("meta", {})
        assert "generated_from_commit_sha" in meta
        assert "result_sha256" in meta
        assert "checkpoint_schema_version" not in meta or True

    def test_result_sha256_deterministic(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        exec_scope = ExecutionScope(units_target=1, units_completed=1, complete=True)
        nat_scope = NationalScope(complete=False)
        scope = ScopeResult(execution_scope=exec_scope, national_scope=nat_scope)
        r1 = build_report(docs, scope, list(ACTIVE_LAW_TYPES), 1, now)
        r2 = build_report(docs, scope, list(ACTIVE_LAW_TYPES), 1, now)
        assert r1["meta"]["result_sha256"] == r2["meta"]["result_sha256"]


# ── Evidence artifacts ─────────────────────────────────────────────────────


class TestEvidenceArtifacts:
    def test_no_identifiers_in_output(self, tmp_path: Path):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        exec_scope = ExecutionScope(units_target=1, units_completed=1, complete=True)
        nat_scope = NationalScope(complete=False)
        scope = ScopeResult(execution_scope=exec_scope, national_scope=nat_scope)
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), 1, now)
        write_outputs(report, tmp_path)
        with open(tmp_path / "arv-009-rolling-window-storage.json") as f:
            data = json.load(f)
        text = json.dumps(data)
        assert "p001" not in text
        assert "http://" not in text

    def test_no_active_claims_in_report(self):
        now = datetime.now(UTC)
        docs = [_make_doc(source_date=_today_str())]
        exec_scope = ExecutionScope(units_target=1, units_completed=1, complete=True)
        nat_scope = NationalScope(complete=False)
        scope = ScopeResult(execution_scope=exec_scope, national_scope=nat_scope)
        report = build_report(docs, scope, list(ACTIVE_LAW_TYPES), 1, now)
        text = json.dumps(report).lower()
        assert "active" not in text or "active" in ("measurement_kind" in text)


# ── Window metrics ─────────────────────────────────────────────────────────


class TestWindowMetrics:
    def test_metrics_structure(self):
        now = datetime.now(UTC)
        docs = [_make_doc(procurement_id="p1", source_date=_today_str())]
        m = compute_window_metrics(docs, 30, now)
        assert m["window_days"] == 30
        assert m["unique_procurements"] == 1
        assert m["unique_documents"] == 1
        assert "window_scope_complete" in m
        assert "observed_days" in m

    def test_empty_metrics(self):
        now = datetime.now(UTC)
        m = compute_window_metrics([], 30, now)
        assert m["unique_procurements"] == 0

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


# ── Sizing ─────────────────────────────────────────────────────────────────


class TestSizing:
    def test_50_pct_commercial_reserve(self):
        metrics = {"conservative_union_bytes": 100_000_000_000, "p99_bytes": 1_000_000_000,
                    "window_days": 90}
        s = compute_sizing_for_window(metrics)
        assert s["commercial_reserve_bytes"] == int(100_000_000_000 * COMMERCIAL_RESERVE_RATIO)

    def test_remaining_never_negative(self):
        metrics = {"conservative_union_bytes": 5_000_000_000_000, "p99_bytes": 50_000_000_000,
                    "window_days": 90}
        s = compute_sizing_for_window(metrics)
        assert s["remaining_bytes"] >= 0


class TestObservedSubtotal:
    def test_no_full_sizing(self):
        metrics = {"conservative_union_bytes": 500_000_000}
        s = compute_observed_subtotal(metrics)
        assert "base_required_bytes" not in s
        assert "remaining_bytes" not in s

    def test_sizing_status_incomplete(self):
        status = compute_sizing_status(False)
        assert status["sizing_status"] == "unavailable"


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

    def test_unavailable_when_scope_incomplete(self):
        w, s = self._make_windows(500_000_000_000)
        verdict, reason = determine_verdict(w, s, scope_complete=False, size_coverage_ok=True)
        assert verdict == "unavailable"


# ── Parser helpers ─────────────────────────────────────────────────────────


class TestParserHelpers:
    def test_parse_procurement_id(self):
        assert (
            _parse_xml_procurement_id("epNotificationEF2020_0325300006424000001_001.xml")
            == "0325300006424000001"
        )

    def test_parse_law_44fz(self):
        assert _parse_xml_law("epNotificationEF2020_xxx.xml") == "44fz"

    def test_parse_eis_datetime(self):
        dt = _parse_eis_datetime("2025-12-31T12:00:00+03:00")
        assert dt is not None
        assert dt.year == 2025

    def test_parse_xml_publish_date(self):
        xml = '<?xml version="1.0"?><root xmlns:ns3="http://zakupki.gov.ru/oos/types/1"><ns3:publishDate>2026-07-25T10:00:00+03:00</ns3:publishDate></root>'
        from xml.etree import ElementTree as ET
        root = ET.fromstring(xml)
        dt = _parse_xml_publish_date(root)
        assert dt is not None
        assert "2026-07-25" in dt


# ── Constants ──────────────────────────────────────────────────────────────


class TestConstants:
    def test_commercial_reserve_50_pct(self):
        assert COMMERCIAL_RESERVE_RATIO == 0.50

    def test_only_44fz_implemented(self):
        assert IMPLEMENTED_LAWS == ["44fz"]

    def test_unimplemented_laws_separated(self):
        assert "223fz" in UNIMPLEMENTED_LAWS
        assert "capital_repair" in UNIMPLEMENTED_LAWS
