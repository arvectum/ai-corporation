# ARV-009: Active Procurement Storage — Investigation Report

## Objective

Demonstrate that 2 TB SSD is sufficient to store the documentation of all active
procurements in EIS, with an additional 50% reserve for commercial procurements.

## Result

| Item | Value |
|------|-------|
| Measurement kind | `incomplete` |
| SSD verdict | `unavailable` |
| Reason | Coverage gate not passed: status population not classified |
| Active procurements | 0 (178/178 unmapped) |
| Documents | 0 (no active procurements to measure) |

## Investigation Summary

### C1.1 — First real snapshot (Tyumen / 7 days)

- **Data source**: EIS SOAP `getDocsByOrgRegion`
- **Laws requested**: 44-FZ, 223-FZ, capital_repair
- **Laws implemented**: 44-FZ only
- **Region**: Tyumen (code 72)
- **Date range**: 2026-07-19 — 2026-07-25
- **Archives**: 5/5 (100% archive coverage)
- **XML files**: 190
- **Parsed OK**: 190
- **Parsed failed**: 0
- **Unique procurements**: 178
- **Active**: 0 | **Completed**: 0 | **Cancelled**: 0 | **Deadline passed**: 0
- **Unmapped (unknown status)**: 178
- **Source errors**: 2 (date=2026-07-25, date=2026-07-19 — no documents by request for 223-FZ)

### C1.2A — XML parsing defect fixed

The original implementation parsed all ZIP entries but extracted status, deadline,
and attachments **after** the loop using only the last-entry values. This was fixed
so each XML is independently parsed and counted.

### C1.2B — Status element investigation (Result B)

**Conclusion**: Export ZIP XMLs do not contain lifecycle status. The schema
inventory (38 XMLs, 27 677 elements) found zero status-related elements in any
namespace. The status element exists in the EPtypes XSD but is never populated
in export ZIP content.

**Deadline** is `collectingInfo/endDT` at 100% coverage (38/38 XMLs).

**Document type**: Only `epNotificationEF2020` (44-ФЗ) confirmed. No cancellation,
completion, or protocol documents found in the sampled set.

**Marker**: `ARV-009C1_EXACT_ACTIVE_SET_REQUIRES_EXTERNAL_STATUS_SOURCE`

## Coverage Gate Status

| Condition | Status |
|-----------|--------|
| Region sweep complete | ✅ (1/1) |
| Date sweep complete | ✅ (7/7) |
| Law sweep complete | ❌ (1/3 — only 44-FZ implemented) |
| Pagination complete | ❌ (not attempted — no active to paginate) |
| Status classification ≥ 95% | ❌ (0%) |
| Procurement coverage ≥ 95% | ❌ (0% — no active procurements classified) |
| Document size coverage ≥ 95% | ❌ (0% — no active documents) |

## Limitations

1. **Status unavailable in export XML**: The EIS export format does not include
   lifecycle status. An external source (e.g., `getDocsIP` SOAP metadata) is
   required for active/completed/cancelled classification.
2. **Only 44-FZ implemented**: 223-FZ and capital_repair laws are defined but
   not yet implemented in the sweep. `law_scope_complete` is `False`.
3. **Tyumen only**: Single-region sample. Full EIS coverage requires all 89
   regions.
4. **7-day window**: Procurement lifecycle may extend beyond 7 days; a longer
   lookback (30, 60, 90 days) would capture more context.
5. **No cancellation/protocol documents found**: The sample may need a wider
   date range or additional document-type filters to detect non-notification
   documents.

## Recommendations

1. **Use external status source** — The SOAP `getDocsIP` response includes
   `<status>` in its metadata wrapper. Pre-classify procurements as
   active/completed/cancelled before feeding them into the storage-sizing
   pipeline.
2. **Rolling-window approach (implemented)** — See
   `docs/ops/arv-009-rolling-window-storage.md` and
   `scripts/capacity/planning/measure_rolling_window_storage.py`.
   Measures total document volume across 30/90/180-day windows as a
   conservative upper bound without requiring active-status classification.
3. **Implement remaining laws** — Add 223-FZ and capital_repair support to the
   sweep to complete law scope.
4. **Expand region coverage** — Run sweeps across all 99 KLADR regions to build
   representative national data.

## Outputs

- `samples/capacity/arv-009-active-snapshot-summary.json` — real snapshot JSON
- `samples/capacity/arv-009-active-snapshot-summary.csv` — CSV summary
- `samples/capacity/arv-009-eis-schema-inventory.json` — full element inventory
- `docs/ops/arv-009-eis-status-source-investigation.md` — Result B write-up

## Tests

83 capacity tests pass (53 existing + 30 new for coverage gates, status
classification, law scope independence, and schema inventory).

## CI

- `make check` — all checks pass
- Secret scan — clean
- Alembic — single head
