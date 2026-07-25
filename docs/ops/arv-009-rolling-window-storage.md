# ARV-009C1.3: Rolling-Window Storage Upper Bound

## Objective

Measure the total document volume of all EIS procurements published or modified
within a rolling 30/90/180-day window, as a conservative upper bound for local
storage on a 2 TB SSD — without requiring exact active-status classification.

## Result B (Status Unavailable in Export XML)

Confirmed: export ZIP XMLs obtained via `getDocsByOrgRegion` / `getDocsIP` do
not contain procurement lifecycle status. Schema inventory (38 XMLs, 27 677
elements) found zero status-related elements in any namespace.

**Marker:** `ARV-009C1_EXACT_ACTIVE_SET_REQUIRES_EXTERNAL_STATUS_SOURCE`

## Method

### Sweep

- **Transport**: EIS SOAP `getDocsByOrgRegion` (authenticated via DOCUMENT_EXPORT_TOKEN)
- **Regions**: All 99 KLADR codes (configurable for testing)
- **Laws**: 44-FZ (confirmed, `epNotificationEF2020`). 223-FZ and capital_repair
  are defined but not yet implemented.
- **Lookback**: Up to 180 days. 30d, 90d, and 180d windows are all computed
  from a single sweep (no separate runs needed).
- **Rate limiting**: Configurable delay between sequential requests. Retries with
  bounded exponential backoff.
- **Checkpoint/resume**: State saved after every (region, date, law) tuple.
  SIGINT-safe: `SIGINT` stops after current operation; second `SIGINT` forces exit.
- **Raw XML/archives deleted** after processing (not committed).

### Deduplication

Two modes:
1. **`latest_version_bytes`** — only the latest version of each document per
   procurement, identified by `versionNumber` from XML.
2. **`conservative_union_bytes`** — union of all unique documents per
   procurement within the window, deduplicated by `(law, procurement_id, doc_id)`.

SSD calculation always uses `conservative_union_bytes`.

### Metrics per Window

For each window (30, 90, 180 days):
- Regions, laws, dates completed
- Unique procurements and documents
- Known bytes and unknown-size documents
- Size coverage percentage
- Latest-version bytes and conservative-union bytes
- Percentiles: p50, p75, p90, p95, p99, max
- Counts of packages > 100 MB, 250 MB, 500 MB, 1 GiB
- Maximum and average daily incoming bytes

No procurement is labelled "active".

## Scope Completeness

| Condition | Gate |
|-----------|------|
| Regions complete (99/99) | `region_scope_complete` |
| Dates complete (180/180) | `date_scope_complete` |
| Laws complete (3/3) | `law_scope_complete` |
| No source errors | `pagination_complete` |
| Size coverage ≥ 95% | `size_coverage_ok` |

If any condition fails → `ssd_verdict = "unavailable"`.

## SSD Calculation

```
eis_window_bytes = conservative_union_bytes
commercial_reserve = eis_window_bytes × 0.50
processing_space = max(150 GiB, p99_package_bytes × 4)
persistent = 50 GiB
base_required = eis_window + commercial + processing + persistent
minimum_disk = base_required / 0.80
```

SSD capacity: 2 000 000 000 000 decimal bytes (≈ 1 862.6 GiB).

## Decision Matrix

| Verdict | Condition | Meaning |
|---------|-----------|---------|
| STRONG GREEN | 180d base ≤ 1.4 TB | 2 TB has sufficient margin |
| CONDITIONAL GREEN | 90d base ≤ 1.4 TB, 180d > 1.4 TB | 2 TB OK with cleanup & limited retention |
| YELLOW | 90d base 1.4-1.7 TB | 2 TB may be insufficient |
| RED | 90d base > 1.7 TB | 2 TB insufficient |
| unavailable | scope incomplete or coverage < 95% | Cannot produce verdict |

When law scope is incomplete, a **provisional 44-FZ envelope** shows remaining
capacity on 2 TB after the measured 44-FZ component.

## Current Result (Tyumen / 7d)

| Metric | Value |
|--------|-------|
| Regions completed | 1 / 99 |
| Dates completed | 7 / 180 |
| Laws completed | 1 / 3 (44-FZ) |
| Total documents | 833 |
| Unique procurements | 178 |
| Conservative union bytes | 598.3 MB |
| Size coverage | 100.0 % |
| p50 | 418 KB |
| p95 | 7.1 MB |
| p99 | 79.1 MB |
| max | 301.7 MB |
| Packages > 100 MB | 1 |
| Verdict | unavailable (scope incomplete) |

## 44-FZ Provisional Envelope

With law scope incomplete (223-FZ and capital_repair unimplemented), the
provisional 44-FZ component for 180 days shows measured 598 MB with
≈ 1 862 GiB remaining on the 2 TB SSD after the 44-FZ envelope.

## Outputs

- `samples/capacity/arv-009-rolling-window-storage.json`
- `samples/capacity/arv-009-rolling-window-storage.csv`
- `docs/ops/arv-009-rolling-window-storage.md`

## Tests

48 new rolling-window tests pass (271 total capacity tests), covering:
- Window filtering (30/90/180 from one set)
- Dedup: conservative union ≥ latest version
- Dedup: duplicate version not double-counted
- Metrics structure and edge cases (empty set, partial size coverage)
- Percentile monotonicity
- Sizing: 50% commercial reserve, 20% free-space floor
- Verdict: all 6 cases (STRONG_GREEN through unavailable)
- No active claims in output
- No identifiers/URLs in committed output
- Checkpoint save/load and interrupted resume
- Source error makes scope incomplete
- Missing law blocks final verdict
- CSV and JSON output format

## CI

- `make check` — all checks pass
- Secret scan — clean
- Alembic — single head

## Markers

- `ARV-009C1_RESULT_B_FINALIZED`
- `ARV-009C1_ROLLING_WINDOW_UPPER_BOUND_READY`
