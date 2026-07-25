"""
ARV-009C1.3 — Rolling-window EIS storage upper bound.

Sweeps EIS getDocsByOrgRegion across all regions for up to 180 lookback
days, collects document metadata from export ZIP XMLs, deduplicates,
and computes storage upper bounds for 30/90/180-day rolling windows.

Usage:
    python scripts/capacity/planning/measure_rolling_window_storage.py \
        --output-dir /tmp/arv009c1

    # Resume from checkpoint
    python scripts/capacity/planning/measure_rolling_window_storage.py \
        --output-dir /tmp/arv009c1 --resume

    # Limited regions for testing
    python scripts/capacity/planning/measure_rolling_window_storage.py \
        --output-dir /tmp/arv009c1 --max-regions 3 --lookback-days 7

    # Custom delay between SOAP requests
    python scripts/capacity/planning/measure_rolling_window_storage.py \
        --output-dir /tmp/arv009c1 --delay 0.5
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import sys
import time
import zipfile
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

SSD_CAPACITY_DECIMAL_BYTES = 2_000_000_000_000
ONE_GIB = 1_073_741_824
ONE_TIB = 1_099_511_627_776
GREEN_THRESHOLD_BYTES = 1_400_000_000_000
YELLOW_THRESHOLD_BYTES = 1_700_000_000_000
PROCESSING_SPACE_MIN_BYTES = 150 * ONE_GIB
PERSISTENT_RESULTS_AND_LOGS_BYTES = 50 * ONE_GIB
COMMERCIAL_RESERVE_RATIO = 0.50
MAX_PROCESSING_CONCURRENCY = 4
COVERAGE_THRESHOLD = 95.0
ROLLING_WINDOWS_DAYS = (30, 90, 180)

ACTIVE_LAW_TYPES = ("44fz", "223fz", "capital_repair")
IMPLEMENTED_LAWS = ["44fz"]

EIS_DOC_TYPE_LAW = {
    "epNotificationEF2020": "44fz",
    "epNotification223": "223fz",
    "capitalRepair": "capital_repair",
}

EIS_REGION_REGISTRY = {
    "source": "KLADR canonical, verified via EIS NSI getNsiOrgRegion",
    "version": "kladdr-2024",
    "codes": [
        "01", "02", "03", "04", "05", "06", "07", "08", "09", "10",
        "11", "12", "13", "14", "15", "16", "17", "18", "19", "20",
        "21", "22", "23", "24", "25", "26", "27", "28", "29", "30",
        "31", "32", "33", "34", "35", "36", "37", "38", "39", "40",
        "41", "42", "43", "44", "45", "46", "47", "48", "49", "50",
        "51", "52", "53", "54", "55", "56", "57", "58", "59", "60",
        "61", "62", "63", "64", "65", "66", "67", "68", "69", "70",
        "71", "72", "73", "74", "75", "76", "77", "78", "79", "80",
        "81", "82", "83", "84", "85", "86", "87", "88", "89", "90",
        "91", "92", "93", "94", "95", "96", "97", "98", "99",
    ],
}

_RUSSIAN_REGIONS = EIS_REGION_REGISTRY["codes"]

DEFAULT_DELAY_SECONDS = 0.0
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0

EIS_XML_NS = {
    "ns3": "http://zakupki.gov.ru/oos/types/1",
    "ns2": "http://zakupki.gov.ru/oos/base/1",
    "ns4": "http://zakupki.gov.ru/oos/common/1",
}

CHECKPOINT_FILENAME = ".arv009c13_checkpoint.json"
OUTPUT_JSON = "arv-009-rolling-window-storage.json"
OUTPUT_CSV = "arv-009-rolling-window-storage.csv"

_interrupted = False


def _handle_sigint(signum: int, frame: Any) -> None:
    global _interrupted
    if _interrupted:
        logger.warning("Second SIGINT — forcing exit.")
        sys.exit(1)
    logger.info("SIGINT received. Will stop after current operation.")
    _interrupted = True


# ── EIS helpers ────────────────────────────────────────────────────────────


def _parse_eis_datetime(text: str) -> datetime | None:
    cleaned = text.strip()
    # Strip trailing timezone offset like +03:00 or -05:00 for date-only formats
    import re as _re
    cleaned_no_tz = _re.sub(r"[+-]\d{2}:\d{2}$", "", cleaned)

    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    # Try date-only without timezone suffix (e.g. "2026-07-25+03:00" -> "2026-07-25")
    try:
        return datetime.strptime(cleaned_no_tz, "%Y-%m-%d")
    except ValueError:
        pass
    return None


def _parse_xml_procurement_id(file_name: str) -> str:
    stem = Path(file_name).stem
    parts = stem.split("_", 2)
    if len(parts) >= 2:
        return parts[1]
    return stem


def _parse_xml_law(file_name: str) -> str:
    stem = Path(file_name).stem
    doc_type_part = stem.split("_")[0] if "_" in stem else stem
    return EIS_DOC_TYPE_LAW.get(doc_type_part, "44fz")


def _parse_xml_version_number(root: ET.Element) -> str | None:
    node = root.find(".//ns3:versionNumber", EIS_XML_NS)
    if node is not None and node.text:
        return node.text.strip()
    return None


# ── Document extraction ────────────────────────────────────────────────


@dataclass
class DocumentRecord:
    procurement_id: str = field(repr=False)
    law: str
    doc_id: str
    file_name: str = field(repr=False)
    url: str = field(repr=False)
    size_bytes: int | None = None
    version: str | None = None
    source_date: str = ""
    source_region: str = ""

    def dedup_key(self) -> str:
        return f"{self.law}|{self.procurement_id}|{self.doc_id}"


def _extract_documents_from_archive(
    archive_path: Path,
    region: str,
    date_str: str,
) -> list[DocumentRecord]:
    docs: list[DocumentRecord] = []
    try:
        with zipfile.ZipFile(archive_path) as zf:
            for name in zf.namelist():
                if not name.endswith(".xml"):
                    continue
                try:
                    raw_xml = zf.read(name)
                    root = ET.fromstring(raw_xml)
                except Exception:
                    continue
                doc_type_key = name.split("_")[0] if "_" in name else Path(name).stem
                if doc_type_key not in EIS_DOC_TYPE_LAW:
                    continue
                procurement_id = _parse_xml_procurement_id(name)
                law = _parse_xml_law(name)
                version = _parse_xml_version_number(root)
                try:
                    for att_node in root.findall(".//ns4:attachmentInfo", EIS_XML_NS):
                        fn_node = att_node.find("ns4:fileName", EIS_XML_NS)
                        fs_node = att_node.find("ns4:fileSize", EIS_XML_NS)
                        fu_node = att_node.find("ns4:url", EIS_XML_NS)
                        fname = fn_node.text.strip() if fn_node is not None and fn_node.text else ""
                        fsize_txt = fs_node.text.strip() if fs_node is not None and fs_node.text else "0"
                        furl = fu_node.text.strip() if fu_node is not None and fu_node.text else ""
                        if not fname:
                            continue
                        fsize = int(fsize_txt) if fsize_txt.isdigit() else None
                        doc_id = f"{fname}|{furl}"
                        docs.append(DocumentRecord(
                            procurement_id=procurement_id,
                            law=law,
                            doc_id=doc_id,
                            file_name=fname,
                            url=furl,
                            size_bytes=fsize,
                            version=version,
                            source_date=date_str,
                            source_region=region,
                        ))
                except Exception:
                    continue
    except Exception:
        logger.warning("Failed to read archive %s", archive_path, exc_info=True)
    return docs


# ── Deduplication ──────────────────────────────────────────────────────────


def dedup_latest_version(docs: list[DocumentRecord]) -> list[DocumentRecord]:
    seen: dict[str, DocumentRecord] = {}
    for d in docs:
        key = d.dedup_key()
        existing = seen.get(key)
        if existing is None:
            seen[key] = d
        elif (d.version or "") > (existing.version or ""):
            seen[key] = d
    return list(seen.values())


def dedup_conservative_union(docs: list[DocumentRecord]) -> list[DocumentRecord]:
    seen: dict[str, DocumentRecord] = {}
    for d in docs:
        seen[d.dedup_key()] = d
    return list(seen.values())


# ── Scope result ───────────────────────────────────────────────────────────


@dataclass
class ScopeResult:
    regions_completed: int = 0
    dates_completed: int = 0
    laws_completed: list[str] = field(default_factory=list)
    region_scope_complete: bool = False
    date_scope_complete: bool = False
    law_scope_complete: bool = False
    source_errors: list[str] = field(default_factory=list)


# ── Window computation ─────────────────────────────────────────────────────


def filter_docs_by_window(
    docs: list[DocumentRecord],
    window_days: int,
    snapshot_date: datetime,
) -> list[DocumentRecord]:
    cutoff = snapshot_date - timedelta(days=window_days)
    return [d for d in docs if d.source_date and _date_in_window(d.source_date, cutoff)]


def _date_in_window(date_str: str, cutoff: datetime) -> bool:
    dt = _parse_eis_datetime(date_str)
    if dt is None:
        return False
    if dt.tzinfo is not None:
        return dt >= cutoff
    # Naive datetime (date-only) — compare as dates
    return dt.date() >= cutoff.date()


def compute_window_metrics(
    docs: list[DocumentRecord],
    window_days: int,
    snapshot_date: datetime,
) -> dict[str, Any]:
    window_docs = filter_docs_by_window(docs, window_days, snapshot_date)

    window_start = (snapshot_date - timedelta(days=window_days)).strftime("%Y-%m-%d")

    latest_docs = dedup_latest_version(window_docs)
    union_docs = dedup_conservative_union(window_docs)

    union_bytes = sum(d.size_bytes or 0 for d in union_docs)
    latest_bytes = sum(d.size_bytes or 0 for d in latest_docs)

    union_unknown = sum(1 for d in union_docs if d.size_bytes is None)
    total_union = len(union_docs)
    known_union = total_union - union_unknown
    size_cov = (known_union / total_union * 100.0) if total_union > 0 else 0.0

    procurements = sorted({d.procurement_id for d in union_docs})

    pkg_bytes_map: dict[str, int] = defaultdict(int)
    for d in union_docs:
        pkg_bytes_map[d.procurement_id] += d.size_bytes or 0
    pkg_sizes = sorted(pkg_bytes_map.values())
    n = len(pkg_sizes)
    total_pkg_bytes = sum(pkg_sizes)

    def pct(rank: float) -> int:
        if n == 0:
            return 0
        idx = max(0, min(n - 1, int(math.ceil(rank * n / 100) - 1)))
        return pkg_sizes[idx]

    over_100mb = sum(1 for s in pkg_sizes if s > 100_000_000)
    over_250mb = sum(1 for s in pkg_sizes if s > 250_000_000)
    over_500mb = sum(1 for s in pkg_sizes if s > 500_000_000)
    over_1gib = sum(1 for s in pkg_sizes if s > ONE_GIB)

    daily_bytes: dict[str, int] = defaultdict(int)
    for d in union_docs:
        dt = _parse_eis_datetime(d.source_date)
        if dt is not None:
            daily_bytes[dt.strftime("%Y-%m-%d")] += d.size_bytes or 0
    daily_values = list(daily_bytes.values())
    max_daily = max(daily_values) if daily_values else 0
    avg_daily = (sum(daily_values) / len(daily_values)) if daily_values else 0.0

    return {
        "window_days": window_days,
        "window_start_date": window_start,
        "window_end_date": snapshot_date.strftime("%Y-%m-%d"),
        "unique_procurements": len(procurements),
        "unique_documents": total_union,
        "known_bytes": union_bytes,
        "unknown_size_documents": union_unknown,
        "size_coverage_percent": round(size_cov, 1),
        "latest_version_bytes": latest_bytes,
        "conservative_union_bytes": union_bytes,
        "mean_bytes": round(total_pkg_bytes / n, 2) if n > 0 else 0.0,
        "p50_bytes": pct(50),
        "p75_bytes": pct(75),
        "p90_bytes": pct(90),
        "p95_bytes": pct(95),
        "p99_bytes": pct(99),
        "max_bytes": pct(100) if pkg_sizes else 0,
        "packages_over_100mb": over_100mb,
        "packages_over_250mb": over_250mb,
        "packages_over_500mb": over_500mb,
        "packages_over_1gib": over_1gib,
        "max_daily_incoming_bytes": max_daily,
        "avg_daily_incoming_bytes": round(avg_daily, 1),
        "file_count": len(window_docs),
    }


# ── Sizing ─────────────────────────────────────────────────────────────────


def compute_sizing_for_window(window_metrics: dict[str, Any]) -> dict[str, Any]:
    eis_window = window_metrics["conservative_union_bytes"]
    commercial = int(eis_window * COMMERCIAL_RESERVE_RATIO)
    p99 = window_metrics.get("p99_bytes", 0)
    processing = max(PROCESSING_SPACE_MIN_BYTES, p99 * MAX_PROCESSING_CONCURRENCY)
    base = eis_window + commercial + processing + PERSISTENT_RESULTS_AND_LOGS_BYTES
    remaining = SSD_CAPACITY_DECIMAL_BYTES - base
    used_pct = base / SSD_CAPACITY_DECIMAL_BYTES * 100.0 if SSD_CAPACITY_DECIMAL_BYTES > 0 else 0.0
    min_disk = int(math.ceil(base / 0.80))
    return {
        "window_days": window_metrics["window_days"],
        "eis_window_bytes": eis_window,
        "commercial_reserve_bytes": commercial,
        "p99_package_bytes": p99,
        "max_processing_concurrency": MAX_PROCESSING_CONCURRENCY,
        "processing_space_bytes": processing,
        "persistent_results_and_logs_bytes": PERSISTENT_RESULTS_AND_LOGS_BYTES,
        "base_required_bytes": base,
        "ssd_capacity_decimal_bytes": SSD_CAPACITY_DECIMAL_BYTES,
        "ssd_capacity_gib": round(SSD_CAPACITY_DECIMAL_BYTES / ONE_GIB, 1),
        "remaining_bytes": max(remaining, 0),
        "used_percent": round(used_pct, 1),
        "minimum_disk_bytes": min_disk,
        "minimum_disk_decimal_tb": round(min_disk / 1_000_000_000_000, 2),
        "minimum_disk_gib": round(min_disk / ONE_GIB, 1),
    }


# ── Verdict ────────────────────────────────────────────────────────────────


def determine_verdict(
    windows: dict[int, dict[str, Any]],
    sizing: dict[int, dict[str, Any]],
    scope_complete: bool,
    size_coverage_ok: bool,
) -> tuple[str, str | None]:
    if not scope_complete:
        return "unavailable", "Scope incomplete: full regional and law coverage required."
    if not size_coverage_ok:
        return "unavailable", "Document size coverage below 95% threshold."
    s180 = sizing.get(180)
    s90 = sizing.get(90)
    if s180 is None:
        return "unavailable", "180-day window data not available."
    b180 = s180["base_required_bytes"]
    b90 = s90["base_required_bytes"] if s90 else b180
    if b180 <= GREEN_THRESHOLD_BYTES:
        return "STRONG_GREEN", (
            f"180-day base required {_fmt_bytes(b180)} <= 1.4 TB. "
            "2 TB has sufficient margin."
        )
    if b90 <= GREEN_THRESHOLD_BYTES:
        return "CONDITIONAL_GREEN", (
            f"90-day base required {_fmt_bytes(b90)} <= 1.4 TB, "
            f"180-day {_fmt_bytes(b180)} > 1.4 TB. "
            "2 TB sufficient with cleanup and limited retention."
        )
    if b90 <= YELLOW_THRESHOLD_BYTES:
        return "YELLOW", (
            f"90-day base required {_fmt_bytes(b90)} in 1.4-1.7 TB range. "
            "2 TB may be insufficient."
        )
    return "RED", (
        f"90-day base required {_fmt_bytes(b90)} > 1.7 TB. 2 TB insufficient."
    )


def _fmt_bytes(b: int) -> str:
    if b >= ONE_TIB:
        return f"{b / ONE_TIB:.1f} TiB"
    if b >= ONE_GIB:
        return f"{b / ONE_GIB:.1f} GiB"
    if b >= 1_000_000:
        return f"{b / 1_000_000:.1f} MB"
    return f"{b} B"


# ── Checkpoint ─────────────────────────────────────────────────────────────


def save_checkpoint(state: dict[str, Any], path: Path) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def load_checkpoint(path: Path) -> dict[str, Any] | None:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Failed to load checkpoint at %s", path)
    return None


# ── Sweep ──────────────────────────────────────────────────────────────────


def run_sweep(
    output_dir: Path,
    lookback_days: int = 180,
    max_regions: int | None = None,
    region_whitelist: list[str] | None = None,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    resume: bool = False,
) -> tuple[list[DocumentRecord], ScopeResult, list[str], list[str]]:
    from src.modules.tender_operator_agent_demo.settings import (
        get_zakupki_soap_settings,
    )
    from src.modules.tender_operator_agent_demo.zakupki_soap_client import (
        ZakupkiSoapClient,
    )
    from src.tender_research.sync.eis_params import (
        format_eis_exact_date,
    )

    settings = get_zakupki_soap_settings()
    if not settings.configured:
        if not settings.enabled:
            raise RuntimeError("ARV-009C1_REAL_MEASUREMENT_BLOCKED: EIS SOAP API not enabled.")
        if not settings.token_configured:
            raise RuntimeError("ARV-009C1_REAL_MEASUREMENT_BLOCKED: EIS SOAP token not configured.")
    client = ZakupkiSoapClient(settings)

    if region_whitelist:
        regions = region_whitelist
    elif max_regions:
        regions = _RUSSIAN_REGIONS[:max_regions]
    else:
        regions = list(_RUSSIAN_REGIONS)

    today = datetime.now(UTC)
    dates_to_scan: list[str] = [
        format_eis_exact_date(today - timedelta(days=i), timezone="Europe/Moscow")
        for i in range(lookback_days)
    ]

    target_laws = list(ACTIVE_LAW_TYPES)
    implemented_laws = list(IMPLEMENTED_LAWS)

    archive_dir = output_dir / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / CHECKPOINT_FILENAME

    # Load checkpoint state
    completed_regions: set[str] = set()
    completed_dates: set[str] = set()
    documents: list[DocumentRecord] = []
    had_error = False
    source_errors: list[str] = []
    archives_downloaded = 0
    archives_skipped = 0
    region_attempted: set[str] = set()

    if resume:
        ckpt = load_checkpoint(checkpoint_path)
        if ckpt:
            completed_regions = set(ckpt.get("completed_regions", []))
            completed_dates = set(ckpt.get("completed_dates", []))
            doc_dicts = ckpt.get("documents", [])
            documents = [DocumentRecord(**d) for d in doc_dicts]
            had_error = ckpt.get("had_error", False)
            source_errors = ckpt.get("source_errors", [])
            archives_downloaded = ckpt.get("archives_downloaded", 0)
            archives_skipped = ckpt.get("archives_skipped", 0)
            region_attempted = set(ckpt.get("region_attempted", []))
            logger.info(
                "Resumed: %d regions, %d dates, %d docs, %d errors",
                len(completed_regions), len(completed_dates), len(documents),
                len(source_errors),
            )

    signal.signal(signal.SIGINT, _handle_sigint)

    for region in regions:
        if _interrupted:
            break
        if region in completed_regions:
            continue
        region_attempted.add(region)

        for exact_date in dates_to_scan:
            if _interrupted:
                break
            if exact_date in completed_dates:
                continue

            for law in implemented_laws:
                if _interrupted:
                    break

                doc_type = "epNotificationEF2020"
                try:
                    if delay_seconds > 0 and (archives_downloaded > 0 or archives_skipped > 0):
                        time.sleep(delay_seconds)

                    result = client.get_docs_by_org_region(
                        org_region=region,
                        exact_date=exact_date,
                        document_type44=doc_type,
                    )
                    if result.archive_url:
                        attached = client.download_archive(result.archive_url, archive_dir)
                        archives_downloaded += 1
                        archive_path = archive_dir / attached.stored_name
                        new_docs = _extract_documents_from_archive(archive_path, region, exact_date)
                        documents.extend(new_docs)
                    elif result.warnings:
                        for w in result.warnings:
                            source_errors.append(f"{region} {exact_date} {law}: {w}")
                except Exception as e:
                    source_errors.append(f"{region} {exact_date} {law}: {e}")
                    had_error = True

                # Checkpoint after each (region, date, law) combo
                ckpt_data = {
                    "completed_regions": list(completed_regions),
                    "completed_dates": list(completed_dates),
                    "documents": [asdict(d) for d in documents],
                    "had_error": had_error,
                    "source_errors": source_errors,
                    "archives_downloaded": archives_downloaded,
                    "archives_skipped": archives_skipped,
                    "region_attempted": list(region_attempted),
                }
                save_checkpoint(ckpt_data, checkpoint_path)

            completed_dates.add(exact_date)

        completed_regions.add(region)
        # Region done — update checkpoint with region completion
        ckpt_data = {
            "completed_regions": list(completed_regions),
            "completed_dates": list(completed_dates),
            "documents": [asdict(d) for d in documents],
            "had_error": had_error,
            "source_errors": source_errors,
            "archives_downloaded": archives_downloaded,
            "archives_skipped": archives_skipped,
            "region_attempted": list(region_attempted),
        }
        save_checkpoint(ckpt_data, checkpoint_path)

    if _interrupted:
        logger.info("Sweep interrupted. %d regions, %d dates, %d docs collected.",
                     len(completed_regions), len(completed_dates), len(documents))

    failed_laws = [l for l in target_laws if l not in implemented_laws]
    law_scope_complete = not failed_laws
    region_scope_complete = len(completed_regions) == len(regions) and not had_error
    date_scope_complete = len(completed_dates) == len(dates_to_scan) and not had_error

    scope = ScopeResult(
        regions_completed=len(completed_regions),
        dates_completed=len(completed_dates),
        laws_completed=list(implemented_laws),
        region_scope_complete=region_scope_complete,
        date_scope_complete=date_scope_complete,
        law_scope_complete=law_scope_complete,
        source_errors=source_errors,
    )

    # Clean up archives
    for f in archive_dir.iterdir():
        try:
            if f.is_file():
                f.unlink()
        except OSError:
            pass

    return documents, scope, target_laws, failed_laws


# ── Output ─────────────────────────────────────────────────────────────────


def build_report(
    documents: list[DocumentRecord],
    scope: ScopeResult,
    target_laws: list[str],
    failed_laws: list[str],
    generated_at: datetime,
) -> dict[str, Any]:
    snapshot_date = datetime.now(UTC)
    windows: dict[int, dict[str, Any]] = {}
    sizing: dict[int, dict[str, Any]] = {}

    for wd in ROLLING_WINDOWS_DAYS:
        wm = compute_window_metrics(documents, wd, snapshot_date)
        windows[wd] = wm
        sizing[wd] = compute_sizing_for_window(wm)

    scope_complete = (
        scope.region_scope_complete
        and scope.date_scope_complete
        and scope.law_scope_complete
        and not failed_laws
    )

    windows_ok = all(
        w.get("size_coverage_percent", 0) >= COVERAGE_THRESHOLD
        for w in windows.values()
    )

    verdict, reason = determine_verdict(windows, sizing, scope_complete, windows_ok)

    # Provisional 44-FZ envelope when law scope is incomplete
    provisional = None
    if not scope.law_scope_complete:
        w180 = windows.get(180, {})
        s180 = sizing.get(180, {})
        remaining = s180.get("remaining_bytes", 0) if s180 else 0
        remaining_fmt = _fmt_bytes(remaining) if remaining else "N/A"
        provisional = {
            "measured_law": "44fz",
            "unimplemented_laws": failed_laws,
            "window_days": w180.get("window_days", 180),
            "measured_bytes": w180.get("conservative_union_bytes", 0),
            "remaining_on_2tb_after_44fz": remaining_fmt,
        }

    report = {
        "schema_version": "1.0.0",
        "measurement_kind": "incomplete" if not scope_complete else "real_partial",
        "ssd_verdict": verdict,
        "verdict_reason": reason,
        "meta": {
            "tool": "measure_rolling_window_storage.py",
            "version": "1.0.0",
            "generated_at": generated_at.isoformat(),
        },
        "scope": {
            "target_laws": target_laws,
            "failed_laws": failed_laws,
            "completed_laws": scope.laws_completed,
            "regions_completed": scope.regions_completed,
            "dates_completed": scope.dates_completed,
            "region_scope_complete": scope.region_scope_complete,
            "date_scope_complete": scope.date_scope_complete,
            "law_scope_complete": scope.law_scope_complete,
            "source_errors": scope.source_errors,
        },
        "windows": {str(k): v for k, v in sorted(windows.items())},
        "sizing": {str(k): v for k, v in sorted(sizing.items())},
    }

    if provisional is not None:
        report["provisional_44fz_envelope"] = provisional

    return report


def write_outputs(report: dict[str, Any], output_dir: Path) -> None:
    json_path = output_dir / OUTPUT_JSON
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    logger.info("Wrote %s", json_path)

    csv_path = output_dir / OUTPUT_CSV
    lines = [
        "window_days,unique_procurements,unique_documents,known_bytes,unknown_size_documents,"
        "size_coverage_pct,latest_version_bytes,conservative_union_bytes,"
        "p50_bytes,p95_bytes,p99_bytes,base_required_bytes,remaining_bytes,verdict"
    ]
    windows = report.get("windows", {})
    sizing = report.get("sizing", {})
    verdict = report.get("ssd_verdict", "unavailable")
    for wd, wm in sorted(windows.items(), key=lambda x: int(x[0])):
        s = sizing.get(wd, {})
        lines.append(
            f"{wd},{wm.get('unique_procurements',0)},{wm.get('unique_documents',0)},"
            f"{wm.get('known_bytes',0)},{wm.get('unknown_size_documents',0)},"
            f"{wm.get('size_coverage_percent',0)},{wm.get('latest_version_bytes',0)},"
            f"{wm.get('conservative_union_bytes',0)},"
            f"{wm.get('p50_bytes',0)},{wm.get('p95_bytes',0)},{wm.get('p99_bytes',0)},"
            f"{s.get('base_required_bytes',0)},{s.get('remaining_bytes',0)},{verdict}"
        )
    csv_path.write_text("\n".join(lines) + "\n")
    logger.info("Wrote %s", csv_path)


# ── Main ───────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ARV-009C1.3 — Rolling-window EIS storage upper bound."
    )
    parser.add_argument("--output-dir", default="/tmp/arv009c1")
    parser.add_argument("--lookback-days", type=int, default=180)
    parser.add_argument("--max-regions", type=int, default=None)
    parser.add_argument("--region-whitelist", nargs="*", default=None)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC)

    try:
        documents, scope, target_laws, failed_laws = run_sweep(
            output_dir=output_dir,
            lookback_days=args.lookback_days,
            max_regions=args.max_regions,
            region_whitelist=args.region_whitelist,
            delay_seconds=args.delay,
            resume=args.resume,
        )
    except RuntimeError as e:
        logger.error("Sweep blocked: %s", e)
        report = {
            "schema_version": "1.0.0",
            "measurement_kind": "incomplete",
            "ssd_verdict": "unavailable",
            "verdict_reason": str(e),
            "meta": {
                "tool": "measure_rolling_window_storage.py",
                "version": "1.0.0",
                "generated_at": generated_at.isoformat(),
            },
        }
        write_outputs(report, output_dir)
        sys.exit(1)

    logger.info(
        "Sweep complete: regions=%d dates=%d docs=%d errors=%d",
        scope.regions_completed, scope.dates_completed,
        len(documents), len(scope.source_errors),
    )

    report = build_report(documents, scope, target_laws, failed_laws, generated_at)
    write_outputs(report, output_dir)

    print(f"\n  ARV-009C1.3 — ROLLING WINDOW STORAGE UPPER BOUND")
    print(f"  Regions completed: {scope.regions_completed}")
    print(f"  Dates completed:   {scope.dates_completed}")
    print(f"  Laws completed:    {scope.laws_completed}")
    print(f"  Total documents:   {len(documents)}")
    print(f"  Scope complete:    region={scope.region_scope_complete} "
          f"date={scope.date_scope_complete} law={scope.law_scope_complete}")
    print(f"  Verdict:           {report.get('ssd_verdict', 'unavailable')}")
    if report.get("verdict_reason"):
        print(f"  Reason:            {report['verdict_reason']}")
    for wd, wm in sorted(report.get("windows", {}).items(), key=lambda x: int(x[0])):
        s = report.get("sizing", {}).get(wd, {})
        print(f"  Window {wd}d: {wm.get('unique_procurements',0)} procurements, "
              f"{_fmt_bytes(wm.get('conservative_union_bytes',0))} union, "
              f"base required {_fmt_bytes(s.get('base_required_bytes',0))}")
    print(f"  Output files in:   {output_dir}\n")


if __name__ == "__main__":
    main()
