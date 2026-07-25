"""
ARV-009C1 — Active EIS procurement storage measurement.

Connects to the EIS SOAP API to obtain the full list of active
procurements across all supported legal frameworks (44-FZ, 223-FZ,
capital repair, etc.), retrieves document manifests, determines
document sizes via the most reliable method (EIS metadata →
Content-Length → Content-Range → streaming byte count), and
produces a sizing summary against the 2 TB external SSD target.

Usage:
    # Real mode (requires configured EIS SOAP API)
    python scripts/capacity/planning/measure_active_procurements.py \
        --output-dir /tmp/arv009c1

    # Demo mode (synthetic data for tests / CLI examples)
    python scripts/capacity/planning/measure_active_procurements.py \
        --demo \
        --output-dir /tmp/arv009c1

Exits non-zero if real mode cannot reach or complete the EIS snapshot.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import random
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, build_opener

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

SSD_CAPACITY_DECIMAL_BYTES = 2_000_000_000_000
ONE_GIB = 1_073_741_824
GREEN_THRESHOLD_BYTES = 1_400_000_000_000
YELLOW_THRESHOLD_BYTES = 1_700_000_000_000
PROCESSING_SPACE_MIN_BYTES = 150 * ONE_GIB
PERSISTENT_RESULTS_AND_LOGS_BYTES = 50 * ONE_GIB
COMMERCIAL_RESERVE_RATIO = 0.50
MAX_PROCESSING_CONCURRENCY = 4
COVERAGE_THRESHOLD = 95.0

ACTIVE_LAW_TYPES = ("44fz", "223fz", "capital_repair")

EXCLUDED_STATUSES = frozenset({
    "completed", "cancelled", "canceled", "archived", "outcome",
})

# ── Data structures ───────────────────────────────────────────────────────

@dataclass
class DocumentSizeProvenance:
    method: str  # eis_metadata | content_length | content_range | streamed | unavailable
    retrieved_at: str | None = None
    url: str | None = field(default=None, repr=False)
    error: str | None = None

@dataclass
class DocumentInfo:
    file_name: str | None = field(default=None, repr=False)
    url: str | None = field(default=None, repr=False)
    content_type: str | None = None
    size_bytes: int | None = None
    is_archive: bool = False
    provenance: DocumentSizeProvenance | None = None
    tender_id: str | None = field(default=None, repr=False)

@dataclass
class EisProcurementRef:
    procurement_id: str
    law: str | None
    status: str | None
    source: str

@dataclass
class ActiveProcurement:
    procurement_id: str = field(repr=False)
    law: str | None
    status: str
    registry_number: str | None = field(default=None, repr=False)
    documents: list[DocumentInfo] = field(default_factory=list)
    total_bytes: int = 0
    doc_count: int = 0
    known_bytes: int = 0
    unknown_docs: int = 0

    def __post_init__(self):
        self.total_bytes = sum(d.size_bytes or 0 for d in self.documents)
        self.doc_count = len(self.documents)
        self.known_bytes = sum(d.size_bytes or 0 for d in self.documents if d.size_bytes is not None)
        self.unknown_docs = sum(1 for d in self.documents if d.size_bytes is None)

@dataclass
class SourceProvenance:
    source_type: str  # eis_soap | demo
    query_started_at: str
    query_completed_at: str
    laws_requested: list[str]
    statuses_requested: list[str]
    records_received: int
    unique_procurements: int
    pagination_complete: bool
    source_errors: list[str]
    using_fallback: bool = False

@dataclass
class CoverageReport:
    active_procurements_total: int
    active_procurements_with_document_manifest: int
    procurement_coverage_percent: float
    documents_total: int
    documents_with_known_size: int
    documents_with_unknown_size: int
    known_size_coverage_percent: float
    excluded_unmapped_status: int = 0
    excluded_deadline_passed: int = 0
    excluded_explicit: int = 0

@dataclass
class MeasurementProvenance:
    measurement_kind: str  # real | synthetic | incomplete
    snapshot_started_at_utc: str
    snapshot_completed_at_utc: str
    snapshot_date: str
    source: SourceProvenance | None = None
    coverage: CoverageReport | None = None
    ssd_verdict: str = "unavailable"
    reason: str | None = None

@dataclass
class SnapshotStats:
    active_procurements: int = 0
    documents: int = 0
    known_bytes: int = 0
    unknown_documents: int = 0
    mean_bytes: float = 0.0
    p50_bytes: int = 0
    p75_bytes: int = 0
    p90_bytes: int = 0
    p95_bytes: int = 0
    p99_bytes: int = 0
    max_bytes: int = 0
    packages_over_100mb: int = 0
    packages_over_250mb: int = 0
    packages_over_500mb: int = 0
    packages_over_1gib: int = 0
    heavy_tail_top_1_pct: float = 0.0
    heavy_tail_top_5_pct: float = 0.0
    heavy_tail_top_10_pct: float = 0.0

@dataclass
class ByLawType:
    law_type: str
    tenders: int = 0
    documents: int = 0
    known_bytes: int = 0

@dataclass
class SizingResult:
    eis_active_bytes: int = 0
    commercial_reserve_bytes: int = 0
    p99_package_bytes: int = 0
    max_processing_concurrency: int = MAX_PROCESSING_CONCURRENCY
    processing_space_bytes: int = 0
    persistent_results_and_logs_bytes: int = PERSISTENT_RESULTS_AND_LOGS_BYTES
    base_required_bytes: int = 0
    ssd_capacity_decimal_bytes: int = SSD_CAPACITY_DECIMAL_BYTES
    ssd_capacity_gib: float = 0.0
    remaining_bytes: int = 0
    used_percent: float = 0.0
    classification: str = "unavailable"
    minimum_disk_bytes: int = 0
    minimum_disk_decimal_tb: float = 0.0
    minimum_disk_gib: float = 0.0
    next_practical_disk_class: str = ""

# ── Canonical status mapping ─────────────────────────────────────────────

def is_active_eis_status(eis_status: str | None) -> bool:
    if not eis_status:
        return False
    s = eis_status.strip().lower()
    if s in EXCLUDED_STATUSES:
        return False
    known_active = {"published", "applying", "active", "submission", "open"}
    if s in known_active:
        return True
    return False

def classify_eis_status(eis_status: str | None) -> str:
    if not eis_status:
        return "excluded_missing"
    s = eis_status.strip().lower()
    if s in EXCLUDED_STATUSES:
        return "excluded_explicit"
    if is_active_eis_status(s):
        return "active"
    return "excluded_unmapped"

# ── Real-mode EIS SOAP source ────────────────────────────────────────────

def _fetch_active_from_eis_soap() -> tuple[list[ActiveProcurement], SourceProvenance]:
    from src.modules.tender_operator_agent_demo.settings import get_zakupki_soap_settings
    from src.modules.tender_operator_agent_demo.zakupki_soap_client import ZakupkiSoapClient
    from src.modules.tender_operator_agent_demo.procurement_schemas import ProcurementSearchRequest

    started_at = datetime.now(UTC)
    settings = get_zakupki_soap_settings()
    if not settings.configured:
        if not settings.enabled:
            raise RuntimeError(
                "ARV-009C1_REAL_MEASUREMENT_BLOCKED: EIS SOAP API not enabled. "
                "Set ZAKUPKI_GOV_RU_SOAP_ENABLED=1."
            )
        if not settings.token_configured:
            raise RuntimeError(
                "ARV-009C1_REAL_MEASUREMENT_BLOCKED: EIS SOAP token not configured. "
                "Set ZAKUPKI_GOV_RU_SOAP_TOKEN or ZAKUPKI_GOV_RU_SOAP_TOKEN_FILE."
            )

        client = ZakupkiSoapClient(settings)

        # Probe connectivity to both endpoints
        try:
            probe = client.probe_xsd()
            getdocs_reachable = probe.get("status") == "ok"
        except Exception:
            getdocs_reachable = False

        try:
            req = ProcurementSearchRequest(
                source="zakupki_gov_ru_soap_legacy",
                law="44fz",
                max_results=1,
            )
            client.search_procurements(req)
            search_reachable = True
        except Exception:
            search_reachable = False

        if not search_reachable:
            raise RuntimeError(
                "ARV-009C1_REAL_SNAPSHOT_BLOCKED_NO_SYNTHETIC_FALLBACK: "
                f"EIS SOAP search API ({settings.base_url}) unreachable. "
                f"getDocsIP endpoint reachable={getdocs_reachable}. "
                "Cannot retrieve active procurement list without search API. "
                "No implicit fallback to synthetic data."
            )

    client = ZakupkiSoapClient(settings)

    seen_ids: set[str] = set()
    procurements: list[ActiveProcurement] = []
    source_errors: list[str] = []
    laws_tried: list[str] = []
    statuses_tried: list[str] = ["active", "published", "applying"]
    total_raw = 0

    for law in ACTIVE_LAW_TYPES:
        laws_tried.append(law)
        try:
            req = ProcurementSearchRequest(
                source="zakupki_gov_ru_soap_legacy",
                law=law,
                max_results=settings.max_results,
            )
            results = client.search_procurements(req)
            total_raw += len(results)

            for r in results:
                if r.procurement_id in seen_ids:
                    continue
                seen_ids.add(r.procurement_id)

                status_class = classify_eis_status(r.status)
                if status_class != "active":
                    continue

                docs = _fetch_documents_for_procurement(client, r.procurement_id, source_errors)
                procurements.append(ActiveProcurement(
                    procurement_id=r.procurement_id,
                    law=r.law,
                    status=r.status or "unknown",
                    registry_number=r.registry_number,
                    documents=docs,
                ))

        except Exception as e:
            msg = f"{law}: {e}"
            source_errors.append(msg)
            logger.warning("Failed to query law=%s: %s", law, e)

    completed_at = datetime.now(UTC)
    provenance = SourceProvenance(
        source_type="eis_soap",
        query_started_at=started_at.isoformat(),
        query_completed_at=completed_at.isoformat(),
        laws_requested=laws_tried,
        statuses_requested=statuses_tried,
        records_received=total_raw,
        unique_procurements=len(seen_ids),
        pagination_complete=bool(settings.max_results >= 50),
        source_errors=source_errors,
    )
    return procurements, provenance

def _fetch_documents_for_procurement(
    client: Any, procurement_id: str, errors: list[str]
) -> list[DocumentInfo]:
    docs: list[DocumentInfo] = []
    try:
        attachments = client.list_attachments(procurement_id)
        for att in attachments:
            size = att.size_bytes
            method = "eis_metadata" if size is not None else "unavailable"
            if size is None and att.url:
                size, method = _resolve_document_size(att.url, errors)
            archive_exts = frozenset({".zip", ".rar", ".7z", ".gz", ".tar", ".bz2"})
            ext = (Path(att.name).suffix or "").lower()
            docs.append(DocumentInfo(
                file_name=att.name,
                url=att.url,
                content_type=att.content_type,
                size_bytes=size,
                is_archive=ext in archive_exts,
                provenance=DocumentSizeProvenance(
                    method=method,
                    retrieved_at=datetime.now(UTC).isoformat(),
                    url=att.url,
                ),
                tender_id=procurement_id,
            ))
    except Exception as e:
        errors.append(f"documents for {procurement_id}: {e}")
    return docs

def _resolve_document_size(url: str, errors: list[str]) -> tuple[int | None, str]:
    try:
        size, method = _try_content_length(url)
        if size is not None:
            return size, method
    except Exception as e:
        errors.append(f"HEAD {url}: {e}")

    try:
        size = _try_content_range(url)
        if size is not None:
            return size, "content_range"
    except Exception as e:
        errors.append(f"Range {url}: {e}")

    try:
        size = _try_stream(url)
        if size is not None:
            return size, "streamed"
    except Exception as e:
        errors.append(f"stream {url}: {e}")

    return None, "unavailable"

def _try_content_length(url: str) -> tuple[int | None, str]:
    req = Request(url, method="HEAD")
    req.add_header("User-Agent", "ArvectumTenderAgent/0.1")
    opener = build_opener()
    with opener.open(req, timeout=15) as resp:
        cl = resp.headers.get("Content-Length")
        if cl:
            return int(cl), "content_length"
    return None, "content_length"

def _try_content_range(url: str) -> int | None:
    req = Request(url)
    req.add_header("Range", "bytes=0-0")
    req.add_header("User-Agent", "ArvectumTenderAgent/0.1")
    opener = build_opener()
    with opener.open(req, timeout=15) as resp:
        cr = resp.headers.get("Content-Range")
        if cr and "/" in cr:
            total = cr.split("/")[-1].strip()
            try:
                return int(total)
            except ValueError:
                pass
    return None

def _try_stream(url: str, max_bytes: int = 200 * 1024 * 1024) -> int | None:
    tmp = tempfile.NamedTemporaryFile(delete=False)
    try:
        req = Request(url)
        req.add_header("User-Agent", "ArvectumTenderAgent/0.1")
        opener = build_opener()
        total = 0
        with opener.open(req, timeout=30) as resp:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    return None
        return total
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

# ── Demo mode (synthetic data for tests only) ─────────────────────────────

_DEMO_LAW_TYPES = ["44fz", "44fz", "44fz", "44fz", "223fz", "223fz"]
_DOC_SIZES = [
    (10_000, 100_000, 0.08),
    (100_000, 500_000, 0.22),
    (500_000, 2_000_000, 0.30),
    (2_000_000, 10_000_000, 0.22),
    (10_000_000, 50_000_000, 0.12),
    (50_000_000, 200_000_000, 0.05),
    (200_000_000, 500_000_000, 0.01),
]
_DOC_EXTS = [
    ("pdf", 0.35), ("docx", 0.15), ("doc", 0.10), ("xlsx", 0.08),
    ("xls", 0.05), ("zip", 0.10), ("rar", 0.05), ("7z", 0.02),
]
_ARCHIVE_EXTS = frozenset({"zip", "rar", "7z", "gz", "tar", "bz2"})

def _synthetic_size(rng: random.Random) -> int:
    r = rng.random()
    cum = 0.0
    for lo, hi, wt in _DOC_SIZES:
        cum += wt
        if r <= cum:
            return rng.randint(lo, hi)
    return rng.randint(50_000, 500_000)

def _synthetic_ext(rng: random.Random) -> str:
    r = rng.random()
    cum = 0.0
    for ext, wt in _DOC_EXTS:
        cum += wt
        if r <= cum:
            return ext
    return "pdf"

def _generate_synthetic_packages(count: int, *, rng: random.Random) -> list[ActiveProcurement]:
    pkgs: list[ActiveProcurement] = []
    for i in range(count):
        doc_count = rng.choices(
            [2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 30],
            weights=[15, 15, 15, 12, 10, 8, 6, 5, 4, 3, 2, 2, 1],
        )[0]
        docs: list[DocumentInfo] = []
        for d in range(doc_count):
            ext = _synthetic_ext(rng)
            size = _synthetic_size(rng)
            docs.append(DocumentInfo(
                size_bytes=size,
                content_type=f"application/{ext}",
                is_archive=ext in _ARCHIVE_EXTS,
                provenance=DocumentSizeProvenance(method="synthetic"),
            ))
        status = rng.choice(["published", "applying"])
        law = rng.choice(_DEMO_LAW_TYPES)
        pkgs.append(ActiveProcurement(
            procurement_id=f"demo-{i:06d}",
            law=law,
            status=status,
            documents=docs,
        ))
    pkgs.sort(key=lambda p: p.known_bytes, reverse=True)
    return pkgs

# ── Statistics ────────────────────────────────────────────────────────────

def compute_statistics(procurements: list[ActiveProcurement]) -> SnapshotStats:
    if not procurements:
        return SnapshotStats()

    known_sizes = sorted([p.known_bytes for p in procurements])
    n = len(known_sizes)
    total_known = sum(known_sizes)
    total_docs = sum(p.doc_count for p in procurements)
    unknown_docs = sum(p.unknown_docs for p in procurements)

    def pct(rank: float) -> int:
        idx = max(0, min(n - 1, int(math.ceil(rank * n / 100) - 1)))
        return known_sizes[idx]

    over_100mb = sum(1 for s in known_sizes if s > 100_000_000)
    over_250mb = sum(1 for s in known_sizes if s > 250_000_000)
    over_500mb = sum(1 for s in known_sizes if s > 500_000_000)
    over_1gib = sum(1 for s in known_sizes if s > ONE_GIB)

    rev = list(reversed(known_sizes))

    def heavy_tail(top_pct: float) -> float:
        cnt = max(1, int(n * top_pct / 100))
        return sum(rev[:cnt]) / total_known if total_known > 0 else 0.0

    return SnapshotStats(
        active_procurements=n,
        documents=total_docs,
        known_bytes=total_known,
        unknown_documents=unknown_docs,
        mean_bytes=total_known / n if n > 0 else 0.0,
        p50_bytes=pct(50),
        p75_bytes=pct(75),
        p90_bytes=pct(90),
        p95_bytes=pct(95),
        p99_bytes=pct(99),
        max_bytes=known_sizes[-1] if known_sizes else 0,
        packages_over_100mb=over_100mb,
        packages_over_250mb=over_250mb,
        packages_over_500mb=over_500mb,
        packages_over_1gib=over_1gib,
        heavy_tail_top_1_pct=heavy_tail(1),
        heavy_tail_top_5_pct=heavy_tail(5),
        heavy_tail_top_10_pct=heavy_tail(10),
    )

def compute_by_law(procurements: list[ActiveProcurement]) -> list[ByLawType]:
    groups: dict[str, ByLawType] = {}
    for p in procurements:
        lt = (p.law or "unknown").lower()
        if lt not in groups:
            groups[lt] = ByLawType(law_type=lt)
        groups[lt].tenders += 1
        groups[lt].documents += p.doc_count
        groups[lt].known_bytes += p.known_bytes
    result = sorted(groups.values(), key=lambda g: g.known_bytes, reverse=True)
    for g in result:
        g.law_type = g.law_type
    return result

# ── Coverage ──────────────────────────────────────────────────────────────

def compute_coverage(
    procurements: list[ActiveProcurement],
    excluded_unmapped: int = 0,
    excluded_deadline_passed: int = 0,
    excluded_explicit: int = 0,
) -> CoverageReport:
    total_active = len(procurements)
    with_manifest = sum(1 for p in procurements if p.doc_count > 0)
    proc_cov = (with_manifest / total_active * 100.0) if total_active > 0 else 100.0

    all_docs = sum(p.doc_count for p in procurements)
    known = sum(p.doc_count - p.unknown_docs for p in procurements)
    unknown = sum(p.unknown_docs for p in procurements)
    doc_cov = (known / all_docs * 100.0) if all_docs > 0 else 100.0

    return CoverageReport(
        active_procurements_total=total_active,
        active_procurements_with_document_manifest=with_manifest,
        procurement_coverage_percent=proc_cov,
        documents_total=all_docs,
        documents_with_known_size=known,
        documents_with_unknown_size=unknown,
        known_size_coverage_percent=doc_cov,
        excluded_unmapped_status=excluded_unmapped,
        excluded_deadline_passed=excluded_deadline_passed,
        excluded_explicit=excluded_explicit,
    )

# ── Sizing ────────────────────────────────────────────────────────────────

def compute_sizing(stats: SnapshotStats, coverage: CoverageReport) -> SizingResult:
    eis_active = stats.known_bytes
    commercial_reserve = int(eis_active * COMMERCIAL_RESERVE_RATIO)
    p99 = stats.p99_bytes
    processing = max(PROCESSING_SPACE_MIN_BYTES, p99 * MAX_PROCESSING_CONCURRENCY)
    base = eis_active + commercial_reserve + processing + PERSISTENT_RESULTS_AND_LOGS_BYTES
    remaining = SSD_CAPACITY_DECIMAL_BYTES - base
    used_pct = base / SSD_CAPACITY_DECIMAL_BYTES * 100.0 if SSD_CAPACITY_DECIMAL_BYTES > 0 else 0.0

    if eis_active == 0:
        classification = "unavailable"
    elif base <= GREEN_THRESHOLD_BYTES:
        classification = "GREEN"
    elif base <= YELLOW_THRESHOLD_BYTES:
        classification = "YELLOW"
    else:
        classification = "RED"

    min_disk = int(base / 0.80)
    min_disk_tb = min_disk / 1_000_000_000_000
    min_disk_gib = min_disk / ONE_GIB

    if min_disk <= 1_000_000_000_000:
        disk_class = "1 TB"
    elif min_disk <= 2_000_000_000_000:
        disk_class = "2 TB"
    elif min_disk <= 4_000_000_000_000:
        disk_class = "4 TB"
    else:
        disk_class = "8 TB"

    return SizingResult(
        eis_active_bytes=eis_active,
        commercial_reserve_bytes=commercial_reserve,
        p99_package_bytes=p99,
        processing_space_bytes=processing,
        base_required_bytes=base,
        ssd_capacity_decimal_bytes=SSD_CAPACITY_DECIMAL_BYTES,
        ssd_capacity_gib=SSD_CAPACITY_DECIMAL_BYTES / ONE_GIB,
        remaining_bytes=max(remaining, 0),
        used_percent=used_pct,
        classification=classification,
        minimum_disk_bytes=min_disk,
        minimum_disk_decimal_tb=min_disk_tb,
        minimum_disk_gib=min_disk_gib,
        next_practical_disk_class=disk_class,
    )

# ── Provenance helpers ────────────────────────────────────────────────────

def make_incomplete_provenance(reason: str) -> MeasurementProvenance:
    now = datetime.now(UTC)
    return MeasurementProvenance(
        measurement_kind="incomplete",
        snapshot_started_at_utc=now.isoformat(),
        snapshot_completed_at_utc=now.isoformat(),
        snapshot_date=now.strftime("%Y-%m-%d"),
        ssd_verdict="unavailable",
        reason=reason,
    )

# ── Output ────────────────────────────────────────────────────────────────

def _provenance_to_dict(p: MeasurementProvenance) -> dict[str, Any]:
    return asdict(p)

def _stats_to_dict(s: SnapshotStats) -> dict[str, Any]:
    return asdict(s)

def _sizing_to_dict(s: SizingResult) -> dict[str, Any]:
    return asdict(s)

def write_json_output(
    path: Path,
    provenance: MeasurementProvenance,
    stats: SnapshotStats | None = None,
    sizing: SizingResult | None = None,
    by_law: list[ByLawType] | None = None,
    limitations: list[dict[str, str]] | None = None,
) -> None:
    output: dict[str, Any] = {
        "schema_version": "1.0.0",
        "measurement_kind": provenance.measurement_kind,
        "ssd_verdict": provenance.ssd_verdict,
        "meta": {
            "tool": "measure_active_procurements.py",
            "version": "1.0.0",
            "generated_at": datetime.now(UTC).isoformat(),
        },
        "measurement_provenance": _provenance_to_dict(provenance),
    }

    if provenance.coverage:
        output["coverage"] = asdict(provenance.coverage)
    if stats is not None:
        output["snapshot"] = _stats_to_dict(stats)
        output["size_statistics"] = {
            "mean_bytes": stats.mean_bytes,
            "p50_bytes": stats.p50_bytes,
            "p75_bytes": stats.p75_bytes,
            "p90_bytes": stats.p90_bytes,
            "p95_bytes": stats.p95_bytes,
            "p99_bytes": stats.p99_bytes,
            "max_bytes": stats.max_bytes,
            "packages_over_100mb": stats.packages_over_100mb,
            "packages_over_250mb": stats.packages_over_250mb,
            "packages_over_500mb": stats.packages_over_500mb,
            "packages_over_1gib": stats.packages_over_1gib,
        }
        output["heavy_tail"] = {
            "top_1_pct": stats.heavy_tail_top_1_pct,
            "top_5_pct": stats.heavy_tail_top_5_pct,
            "top_10_pct": stats.heavy_tail_top_10_pct,
        }
    if by_law is not None:
        output["by_law_type"] = [asdict(b) for b in by_law]
    if sizing is not None:
        output["sizing"] = _sizing_to_dict(sizing)
    if limitations is not None:
        output["limitations"] = limitations

    path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n")

def write_csv_output(
    path: Path,
    provenance: MeasurementProvenance,
    stats: SnapshotStats | None = None,
) -> None:
    header = (
        "schema_version,measurement_kind,ssd_verdict,reason,"
        "active_procurements_total,documents_total,known_bytes,unknown_documents"
    )
    if stats is None:
        path.write_text(header + "\n")
        return
    reason = (provenance.reason or "").replace(",", ";")
    path.write_text(
        f"1.0.0,{provenance.measurement_kind},{provenance.ssd_verdict},{reason},"
        f"{stats.active_procurements},{stats.documents},{stats.known_bytes},"
        f"{stats.unknown_documents}\n"
    )

def format_bytes(b: int) -> str:
    if b >= ONE_GIB:
        return f"{b / ONE_GIB:.1f} GiB"
    if b >= 1_000_000:
        return f"{b / 1_000_000:.1f} MB"
    return f"{b} B"

# ── Demo mode runner (for unit tests only) ────────────────────────────────

def run_demo(output_dir: Path) -> MeasurementProvenance:
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(42)
    packages = _generate_synthetic_packages(50, rng=rng)
    stats = compute_statistics(packages)
    by_law = compute_by_law(packages)

    now = datetime.now(UTC)
    provenance = MeasurementProvenance(
        measurement_kind="synthetic",
        snapshot_started_at_utc=now.isoformat(),
        snapshot_completed_at_utc=now.isoformat(),
        snapshot_date=now.strftime("%Y-%m-%d"),
        source=SourceProvenance(
            source_type="demo",
            query_started_at=now.isoformat(),
            query_completed_at=now.isoformat(),
            laws_requested=list(ACTIVE_LAW_TYPES),
            statuses_requested=["published", "applying"],
            records_received=50,
            unique_procurements=50,
            pagination_complete=True,
            source_errors=[],
        ),
        coverage=compute_coverage(packages),
        ssd_verdict="unavailable",
        reason="Synthetic data — not a real measurement.",
    )
    sizing = compute_sizing(stats, provenance.coverage)

    limitations = [
        {"category": "synthetic", "description": "All data is synthetic. Not suitable for SSD verdict."},
    ]

    write_json_output(
        output_dir / "arv-009-active-snapshot-summary.json",
        provenance, stats=stats, sizing=sizing, by_law=by_law,
        limitations=limitations,
    )
    write_csv_output(output_dir / "arv-009-active-snapshot-summary.csv", provenance, stats)

    print(f"\n  ARV-009C1 — DEMO MODE (synthetic data, not real)")
    print(f"  Synthetic tenders: {stats.active_procurements}")
    print(f"  Synthetic bytes:   {format_bytes(stats.known_bytes)}")
    print(f"  Verdict:           unavailable (synthetic)")
    print(f"  Output files in:   {output_dir}\n")
    return provenance

# ── Real mode runner ──────────────────────────────────────────────────────

def run_real(output_dir: Path) -> MeasurementProvenance:
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC)

    try:
        procurements, source_prov = _fetch_active_from_eis_soap()
    except RuntimeError as e:
        reason = str(e)
        logger.error(reason)
        provenance = make_incomplete_provenance(reason)
        provenance.source = SourceProvenance(
            source_type="eis_soap",
            query_started_at=started_at.isoformat(),
            query_completed_at=datetime.now(UTC).isoformat(),
            laws_requested=list(ACTIVE_LAW_TYPES),
            statuses_requested=["active", "published", "applying"],
            records_received=0,
            unique_procurements=0,
            pagination_complete=False,
            source_errors=[reason],
        )

        write_json_output(output_dir / "arv-009-active-snapshot-summary.json", provenance)
        write_csv_output(output_dir / "arv-009-active-snapshot-summary.csv", provenance)

        print(f"\n  ARV-009C1 — REAL SNAPSHOT INCOMPLETE")
        print(f"  Source: EIS SOAP API")
        print(f"  Status: BLOCKED")
        print(f"  Reason: {reason}")
        print(f"  Output files in:   {output_dir}\n")
        sys.exit(1)

    # Filter to active only
    excluded_unmapped = 0
    excluded_explicit_total = 0
    active: list[ActiveProcurement] = []
    for p in procurements:
        status_class = classify_eis_status(p.status)
        if status_class == "active":
            active.append(p)
        elif status_class == "excluded_unmapped":
            excluded_unmapped += 1
        elif status_class == "excluded_explicit":
            excluded_explicit_total += 1

    coverage = compute_coverage(
        active,
        excluded_unmapped=excluded_unmapped,
        excluded_explicit=excluded_explicit_total,
    )

    not_active = excluded_unmapped + excluded_explicit_total
    source_prov.unique_procurements = len(active) + not_active
    source_prov.records_received = len(active) + not_active

    stats = compute_statistics(active)
    by_law = compute_by_law(active)
    sizing = compute_sizing(stats, coverage)

    gate_pass = (
        coverage.procurement_coverage_percent >= COVERAGE_THRESHOLD
        and coverage.known_size_coverage_percent >= COVERAGE_THRESHOLD
        and source_prov.pagination_complete
    )

    if gate_pass:
        measurement_kind = "real"
        ssd_verdict = sizing.classification
        reason = None
    else:
        measurement_kind = "incomplete"
        ssd_verdict = "unavailable"
        gate_fails = []
        if coverage.procurement_coverage_percent < COVERAGE_THRESHOLD:
            gate_fails.append(
                f"procurement coverage {coverage.procurement_coverage_percent:.1f}% < {COVERAGE_THRESHOLD}%"
            )
        if coverage.known_size_coverage_percent < COVERAGE_THRESHOLD:
            gate_fails.append(
                f"document size coverage {coverage.known_size_coverage_percent:.1f}% < {COVERAGE_THRESHOLD}%"
            )
        if not source_prov.pagination_complete:
            gate_fails.append("pagination incomplete")
        reason = "Coverage gate not passed: " + "; ".join(gate_fails)

    completed_at = datetime.now(UTC)
    provenance = MeasurementProvenance(
        measurement_kind=measurement_kind,
        snapshot_started_at_utc=started_at.isoformat(),
        snapshot_completed_at_utc=completed_at.isoformat(),
        snapshot_date=completed_at.strftime("%Y-%m-%d"),
        source=source_prov,
        coverage=coverage,
        ssd_verdict=ssd_verdict,
        reason=reason,
    )

    limitations = []
    if not gate_pass:
        limitations.append({
            "category": "coverage",
            "description": "Coverage gate not passed. Results are not representative.",
        })
    if coverage.documents_with_unknown_size > 0:
        limitations.append({
            "category": "unknown_sizes",
            "description": f"{coverage.documents_with_unknown_size} documents have unknown size. "
                           "These bytes are NOT included in eis_active_bytes.",
        })

    write_json_output(
        output_dir / "arv-009-active-snapshot-summary.json",
        provenance, stats=stats, sizing=sizing, by_law=by_law,
        limitations=limitations or None,
    )
    write_csv_output(output_dir / "arv-009-active-snapshot-summary.csv", provenance, stats)

    print(f"\n  ARV-009C1 — REAL SNAPSHOT")
    print(f"  Source:            EIS SOAP API")
    print(f"  Started:           {started_at.isoformat()}")
    print(f"  Completed:         {completed_at.isoformat()}")
    print(f"  Active procurements: {stats.active_procurements}")
    print(f"  Documents:         {stats.documents}")
    print(f"  Known bytes:       {format_bytes(stats.known_bytes)}")
    print(f"  Coverage:          proc={coverage.procurement_coverage_percent:.1f}% "
          f"doc_size={coverage.known_size_coverage_percent:.1f}%")
    print(f"  Measurement:       {measurement_kind}")
    print(f"  Verdict:           {ssd_verdict}")
    if reason:
        print(f"  Reason:            {reason}")
    print(f"  Output files in:   {output_dir}\n")

    return provenance

# ── CLI ───────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="ARV-009C1 — Measure active EIS procurement storage."
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Use synthetic data for unit tests / CLI examples only.",
    )
    parser.add_argument(
        "--output-dir",
        default="/tmp/arv009c1",
        help="Output directory for generated files.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    output_dir = Path(args.output_dir)

    if args.demo:
        run_demo(output_dir)
    else:
        run_real(output_dir)


if __name__ == "__main__":
    main()
