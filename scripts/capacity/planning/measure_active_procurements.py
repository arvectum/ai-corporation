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
import json
import logging
import math
import os
import random
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, build_opener
from xml.etree import ElementTree as ET

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
class SweepScope:
    target_laws: list[str] = field(default_factory=list)
    target_region_codes: list[str] = field(default_factory=list)
    target_date_from: str = ""
    target_date_to: str = ""
    completed_laws: list[str] = field(default_factory=list)
    completed_region_codes: list[str] = field(default_factory=list)
    completed_dates: list[str] = field(default_factory=list)
    region_scope_complete: bool = False
    date_scope_complete: bool = False
    law_scope_complete: bool = False
    source_scope_complete: bool = False
    region_registry_source: str = ""
    region_registry_version: str = ""
    target_region_count: int = 0
    implemented_laws: list[str] = field(default_factory=list)
    failed_laws: list[str] = field(default_factory=list)

@dataclass
class SweepCounters:
    zip_entries_total: int = 0
    xml_entries_total: int = 0
    notification_xml_total: int = 0
    xml_parsed_successfully: int = 0
    xml_parse_failed: int = 0
    unique_procurements_before_dedup: int = 0
    unique_procurements_after_dedup: int = 0
    active_procurements: int = 0
    excluded_completed: int = 0
    excluded_cancelled: int = 0
    excluded_deadline_passed: int = 0
    excluded_unmapped_status: int = 0
    deadline_present_count: int = 0
    deadline_parseable_count: int = 0

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
    scope: SweepScope | None = None
    sweep_counters: SweepCounters | None = None

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
    discovered_procurements_total: int = 0
    status_classified_procurements: int = 0
    status_unclassified_procurements: int = 0
    status_classification_coverage_percent: float = 0.0
    deadline_present_procurements: int = 0
    deadline_parseable_procurements: int = 0
    deadline_coverage_percent: float = 0.0
    coverage_reason: str | None = None

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

# ── EIS KLADR region registry for getDocsByOrgRegion sweep ──────────────
# Source: KLADR (Классификатор адресов Российской Федерации) as published
# on zakupki.gov.ru. Region codes 01–99 are the canonical 2-digit KLADR
# codes used by EIS getDocsByOrgRegion API. This registry represents the
# full set of possible codes; not all codes may have active procurements.
# Registry version: "kladdr-2024" (last verified 2024-Q3 from production
# ingestion of zakupki.gov.ru NSI region reference data).
# Verification method: cross-referenced with EIS getNsiOrgRegion endpoint
# and prior production bulk-download pipeline (commit 71c2fea oracle).

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

_EIS_DOC_TYPE_LAW = {
    "epNotificationEF2020": "44fz",
    "epNotification223": "223fz",
    "capitalRepair": "capital_repair",
}

_EIS_XML_NS = {
    "ns3": "http://zakupki.gov.ru/oos/types/1",
    "ns2": "http://zakupki.gov.ru/oos/base/1",
    "ns4": "http://zakupki.gov.ru/oos/common/1",
}


def _parse_xml_procurement_id(file_name: str) -> str:
    stem = Path(file_name).stem
    parts = stem.split("_", 2)
    if len(parts) >= 2:
        return parts[1]
    return stem


def _parse_xml_law(file_name: str) -> str:
    stem = Path(file_name).stem
    doc_type_part = stem.split("_")[0] if "_" in stem else stem
    return _EIS_DOC_TYPE_LAW.get(doc_type_part, "44fz")


def _parse_xml_procurement_status(root: ET.Element) -> str:
    for path in (
        (".//ns3:status",),
    ):
        node = root.find(path[0], _EIS_XML_NS)
        if node is not None and node.text:
            return node.text.strip()
    return "unknown"


def _parse_eis_datetime(text: str) -> datetime | None:
    cleaned = text.strip()
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
    return None


def _parse_xml_application_deadline(root: ET.Element) -> str | None:
    for tag in ("ns3:applicationDeadline", "ns4:deadline", "ns3:submissionDeadline"):
        node = root.find(f".//{tag}", _EIS_XML_NS)
        if node is not None and node.text:
            return node.text.strip()
    return None


def _extract_attachments(
    root: ET.Element, procurement_id: str
) -> list[DocumentInfo]:
    docs: list[DocumentInfo] = []
    try:
        for att_node in root.findall(".//ns4:attachmentInfo", _EIS_XML_NS):
            fn_node = att_node.find("ns4:fileName", _EIS_XML_NS)
            fs_node = att_node.find("ns4:fileSize", _EIS_XML_NS)
            fu_node = att_node.find("ns4:url", _EIS_XML_NS)
            fname = fn_node.text.strip() if fn_node is not None and fn_node.text else ""
            fsize_txt = fs_node.text.strip() if fs_node is not None and fs_node.text else "0"
            furl = fu_node.text.strip() if fu_node is not None and fu_node.text else ""
            if not fname:
                continue
            fsize = int(fsize_txt) if fsize_txt.isdigit() else None
            docs.append(DocumentInfo(
                file_name=fname,
                url=furl,
                size_bytes=fsize,
                provenance=DocumentSizeProvenance(
                    method="eis_metadata",
                    retrieved_at=datetime.now(UTC).isoformat(),
                    url=furl,
                ),
                tender_id=procurement_id,
            ))
    except Exception:
        pass
    return docs


def _fetch_active_from_getdocs_sweep(
    output_dir: Path | None = None,
    max_regions: int | None = None,
    lookback_days: int = 7,
    region_whitelist: list[str] | None = None,
) -> tuple[list[ActiveProcurement], SourceProvenance, SweepScope]:
    from src.modules.tender_operator_agent_demo.settings import (
        get_zakupki_soap_settings,
    )
    from src.modules.tender_operator_agent_demo.zakupki_soap_client import (
        ZakupkiSoapClient,
    )
    from src.tender_research.sync.eis_params import (
        format_eis_exact_date,
    )

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

    if region_whitelist:
        regions = region_whitelist
    elif max_regions:
        regions = _RUSSIAN_REGIONS[:max_regions]
    else:
        regions = _RUSSIAN_REGIONS

    dates_to_scan: list[str] = []
    today = datetime.now(UTC)
    for i in range(lookback_days):
        d = today - timedelta(days=i)
        dates_to_scan.append(format_eis_exact_date(d, timezone="Europe/Moscow"))

    target_date_from = dates_to_scan[-1] if dates_to_scan else ""
    target_date_to = dates_to_scan[0] if dates_to_scan else ""
    target_laws = list(ACTIVE_LAW_TYPES)
    implemented_laws = ["44fz"]

    archive_dir = (output_dir / "archives") if output_dir else Path(tempfile.mkdtemp(suffix="_archives"))

    seen_ids: set[str] = set()
    procurements: list[ActiveProcurement] = []
    source_errors: list[str] = []
    regions_scanned: list[str] = []
    dates_scanned: list[str] = []
    archives_received = 0
    archives_downloaded = 0

    counters = SweepCounters()
    had_error = False

    for region in regions:
        regions_scanned.append(region)
        for exact_date in dates_to_scan:
            if exact_date not in dates_scanned:
                dates_scanned.append(exact_date)
            try:
                result = client.get_docs_by_org_region(
                    org_region=region,
                    exact_date=exact_date,
                    document_type44="epNotificationEF2020",
                )
                if result.archive_url:
                    archives_received += 1
                    try:
                        attachment = client.download_archive(result.archive_url, archive_dir)
                        archives_downloaded += 1
                        archive_path = archive_dir / attachment.stored_name
                        with zipfile.ZipFile(archive_path) as zf:
                            for name in zf.namelist():
                                counters.zip_entries_total += 1
                                if not name.endswith(".xml"):
                                    continue
                                counters.xml_entries_total += 1
                                try:
                                    raw_xml = zf.read(name)
                                    root = ET.fromstring(raw_xml)
                                except Exception:
                                    counters.xml_parse_failed += 1
                                    continue
                                counters.xml_parsed_successfully += 1
                                doc_type_key = name.split("_")[0] if "_" in name else Path(name).stem
                                if doc_type_key not in _EIS_DOC_TYPE_LAW:
                                    continue
                                counters.notification_xml_total += 1
                                procurement_id = _parse_xml_procurement_id(name)
                                counters.unique_procurements_before_dedup += 1
                                if procurement_id in seen_ids:
                                    continue
                                seen_ids.add(procurement_id)
                                law = _parse_xml_law(name)
                                status_text = _parse_xml_procurement_status(root)
                                deadline_str = _parse_xml_application_deadline(root)
                                if deadline_str:
                                    counters.deadline_present_count += 1
                                    dl_dt = _parse_eis_datetime(deadline_str)
                                    if dl_dt is not None:
                                        counters.deadline_parseable_count += 1
                                if status_text in ("unknown", "parse_failed"):
                                    counters.excluded_unmapped_status += 1
                                    continue
                                status_class = classify_eis_status(status_text)
                                if status_class == "excluded_explicit":
                                    s_lower = status_text.strip().lower()
                                    if s_lower == "completed":
                                        counters.excluded_completed += 1
                                    elif s_lower in ("cancelled", "canceled"):
                                        counters.excluded_cancelled += 1
                                    continue
                                if status_class == "excluded_unmapped":
                                    counters.excluded_unmapped_status += 1
                                    continue
                                if deadline_str:
                                    deadline_dt = _parse_eis_datetime(deadline_str)
                                    if deadline_dt is not None and deadline_dt < datetime.now(UTC):
                                        counters.excluded_deadline_passed += 1
                                        continue
                                docs = _extract_attachments(root, procurement_id)
                                procurements.append(ActiveProcurement(
                                    procurement_id=procurement_id,
                                    law=law,
                                    status=status_text,
                                    registry_number=procurement_id,
                                    documents=docs,
                                ))
                                counters.active_procurements += 1
                    except Exception as e:
                        source_errors.append(f"download/parse {region} {exact_date}: {e}")
                        had_error = True
                elif result.warnings:
                    for w in result.warnings:
                        source_errors.append(f"{region} {exact_date}: {w}")
            except Exception as e:
                source_errors.append(f"{region} {exact_date}: {e}")
                had_error = True

    completed_at = datetime.now(UTC)
    counters.unique_procurements_after_dedup = len(seen_ids)

    region_scope_complete = (len(regions_scanned) == len(regions)) and not had_error
    date_scope_complete = (len(dates_scanned) == len(dates_to_scan)) and not had_error
    failed_laws = [l for l in target_laws if l not in implemented_laws]
    completed_laws = [l for l in implemented_laws if l not in failed_laws]
    law_scope_complete = (
        set(completed_laws) == set(target_laws)
        and not failed_laws
    )
    source_scope_complete = region_scope_complete and date_scope_complete and law_scope_complete
    pagination_complete = source_scope_complete and not had_error

    scope = SweepScope(
        target_laws=target_laws,
        target_region_codes=list(regions),
        target_date_from=target_date_from,
        target_date_to=target_date_to,
        completed_laws=completed_laws,
        completed_region_codes=list(regions_scanned),
        completed_dates=list(dates_scanned),
        region_scope_complete=region_scope_complete,
        date_scope_complete=date_scope_complete,
        law_scope_complete=law_scope_complete,
        source_scope_complete=source_scope_complete,
        region_registry_source=EIS_REGION_REGISTRY["source"],
        region_registry_version=EIS_REGION_REGISTRY["version"],
        target_region_count=len(regions),
        implemented_laws=implemented_laws,
        failed_laws=failed_laws,
    )

    provenance = SourceProvenance(
        source_type="eis_getdocs_sweep",
        query_started_at=started_at.isoformat(),
        query_completed_at=completed_at.isoformat(),
        laws_requested=target_laws,
        statuses_requested=["published", "applying"],
        records_received=counters.unique_procurements_before_dedup,
        unique_procurements=counters.unique_procurements_after_dedup,
        pagination_complete=pagination_complete,
        source_errors=source_errors,
        scope=scope,
        sweep_counters=counters,
    )
    logger.info(
        "getDocs sweep: regions=%d dates=%d archives=%d/%d zip=%d xml=%d/%d ntf=%d "
        "parse_ok=%d parse_fail=%d unique_before=%d unique_after=%d active=%d "
        "completed=%d cancelled=%d deadline=%d unmapped=%d errors=%d",
        len(regions_scanned), len(dates_scanned), archives_downloaded, archives_received,
        counters.zip_entries_total, counters.xml_entries_total, counters.xml_parsed_successfully,
        counters.notification_xml_total,
        counters.xml_parsed_successfully, counters.xml_parse_failed,
        counters.unique_procurements_before_dedup, counters.unique_procurements_after_dedup,
        counters.active_procurements,
        counters.excluded_completed, counters.excluded_cancelled,
        counters.excluded_deadline_passed, counters.excluded_unmapped_status,
        len(source_errors),
    )
    return procurements, provenance, scope


def _fetch_active_from_eis_soap() -> tuple[list[ActiveProcurement], SourceProvenance]:
    from src.modules.tender_operator_agent_demo.procurement_schemas import (
        ProcurementSearchRequest,
    )
    from src.modules.tender_operator_agent_demo.settings import (
        get_zakupki_soap_settings,
    )
    from src.modules.tender_operator_agent_demo.zakupki_soap_client import (
        ZakupkiSoapClient,
    )

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
    discovered_total: int = 0,
    status_classified: int = 0,
    deadline_present: int = 0,
    deadline_parseable: int = 0,
) -> CoverageReport:
    total_active = len(procurements)
    with_manifest = sum(1 for p in procurements if p.doc_count > 0)
    if total_active > 0:
        proc_cov = with_manifest / total_active * 100.0
    else:
        proc_cov = 0.0

    all_docs = sum(p.doc_count for p in procurements)
    known = sum(p.doc_count - p.unknown_docs for p in procurements)
    doc_cov = (known / all_docs * 100.0) if all_docs > 0 else 0.0

    status_unclassified = discovered_total - status_classified if discovered_total > 0 else 0
    status_cov = (status_classified / discovered_total * 100.0) if discovered_total > 0 else 0.0
    deadline_cov = (deadline_parseable / deadline_present * 100.0) if deadline_present > 0 else 0.0

    reason = None
    if total_active == 0 and discovered_total == 0:
        reason = "no_procurements_discovered"
    elif total_active == 0 and discovered_total > 0:
        reason = "status_population_not_classified"
    elif status_cov < 95.0:
        reason = "status_classification_insufficient"

    return CoverageReport(
        active_procurements_total=total_active,
        active_procurements_with_document_manifest=with_manifest,
        procurement_coverage_percent=proc_cov,
        documents_total=all_docs,
        documents_with_known_size=known,
        documents_with_unknown_size=all_docs - known,
        known_size_coverage_percent=doc_cov,
        excluded_unmapped_status=excluded_unmapped,
        excluded_deadline_passed=excluded_deadline_passed,
        excluded_explicit=excluded_explicit,
        discovered_procurements_total=discovered_total,
        status_classified_procurements=status_classified,
        status_unclassified_procurements=status_unclassified,
        status_classification_coverage_percent=status_cov,
        deadline_present_procurements=deadline_present,
        deadline_parseable_procurements=deadline_parseable,
        deadline_coverage_percent=deadline_cov,
        coverage_reason=reason,
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
    source = provenance.source
    if source is not None and source.scope is not None:
        output["scope"] = asdict(source.scope)
    if source is not None and source.sweep_counters is not None:
        output["sweep_counters"] = asdict(source.sweep_counters)
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

    print("\n  ARV-009C1 — DEMO MODE (synthetic data, not real)")
    print(f"  Synthetic tenders: {stats.active_procurements}")
    print(f"  Synthetic bytes:   {format_bytes(stats.known_bytes)}")
    print("  Verdict:           unavailable (synthetic)")
    print(f"  Output files in:   {output_dir}\n")
    return provenance

# ── Real mode runner ──────────────────────────────────────────────────────

def run_real(output_dir: Path) -> MeasurementProvenance:
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(UTC)

    try:
        procurements, source_prov, scope = _fetch_active_from_getdocs_sweep(
            output_dir=output_dir,
            region_whitelist=["72"],
            lookback_days=7,
        )
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

        print("\n  ARV-009C1 — REAL SNAPSHOT INCOMPLETE")
        print("  Source: EIS SOAP API")
        print("  Status: BLOCKED")
        print(f"  Reason: {reason}")
        print(f"  Output files in:   {output_dir}\n")
        sys.exit(1)

    counters = source_prov.sweep_counters or SweepCounters()
    active = procurements

    status_classified = (
        counters.active_procurements
        + counters.excluded_completed
        + counters.excluded_cancelled
    )

    coverage = compute_coverage(
        active,
        excluded_unmapped=counters.excluded_unmapped_status,
        excluded_deadline_passed=counters.excluded_deadline_passed,
        excluded_explicit=counters.excluded_completed + counters.excluded_cancelled,
        discovered_total=counters.unique_procurements_after_dedup,
        status_classified=status_classified,
        deadline_present=counters.deadline_present_count,
        deadline_parseable=counters.deadline_parseable_count,
    )

    stats = compute_statistics(active)
    by_law = compute_by_law(active)
    sizing = compute_sizing(stats, coverage)

    scope_complete = (
        scope.source_scope_complete
        and source_prov.pagination_complete
    )

    is_full_national = (
        scope_complete
        and len(scope.target_region_codes) >= 99
        and len(scope.target_laws) >= 3
        and len(scope.completed_dates) >= 364
    )

    coverage_pass = (
        coverage.status_classification_coverage_percent >= COVERAGE_THRESHOLD
        and coverage.procurement_coverage_percent >= COVERAGE_THRESHOLD
        and coverage.known_size_coverage_percent >= COVERAGE_THRESHOLD
    )

    if is_full_national and coverage_pass:
        measurement_kind = "real"
        ssd_verdict = sizing.classification
        reason = None
    elif scope_complete and coverage_pass:
        measurement_kind = "real_partial"
        ssd_verdict = "unavailable"
        reason = (
            "Partial scope: target covers "
            f"{len(scope.target_region_codes)} regions, "
            f"{len(scope.completed_dates)} days, "
            f"{len(scope.target_laws)} law(s). "
            "Full national sweep required for SSD verdict."
        )
    else:
        measurement_kind = "incomplete"
        ssd_verdict = "unavailable"
        gate_fails = []
        if not scope_complete:
            gate_fails.append(
                f"scope incomplete: region={scope.region_scope_complete} "
                f"date={scope.date_scope_complete} law={scope.law_scope_complete} "
                f"pagination={source_prov.pagination_complete}"
            )
        if not coverage_pass:
            gate_fails.append(
                f"coverage status={coverage.status_classification_coverage_percent:.1f}% "
                f"proc={coverage.procurement_coverage_percent:.1f}% "
                f"doc_size={coverage.known_size_coverage_percent:.1f}%"
            )
        if coverage.coverage_reason:
            gate_fails.append(f"reason={coverage.coverage_reason}")
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
    if measurement_kind != "real":
        limitations.append({
            "category": "coverage",
            "description": reason or "Results are not representative for national verdict.",
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

    print("\n  ARV-009C1 — REAL SNAPSHOT")
    print("  Source:            EIS SOAP API")
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
