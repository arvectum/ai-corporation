# ARV-009 — Storage Upper Bound: Decision Record

## Business objective

Determine whether a consumer-grade 4 TB external SSD provides adequate storage
headroom for the Mac mini pilot serving the first 5–10 clients, given the
expected volume of active procurement documents from ЕИС (44‑FZ, 223‑FZ,
capital repair) across 99 operational region codes used by the measurement.

## Measurement approach

A rolling-window EIS storage measurement was implemented in three iterations
(ARV-009C1.3A → ARV-009C1.3B). The final measurement swept:

- **Law:** 44‑FZ (`epNotificationEF2020`)
- **Regions:** 99 operational region codes used by the measurement
- **Window:** 30 consecutive days
- **Method:** per-region, per-date SOAP archive download, deduplicated by
  latest version, conservative union across procurements.
- **Reserve formula:** `base_required = documents × (1 + commercial_reserve) + processing_reserve + persistent_reserve`

## Measured result (30 days)

| Component | Bytes | GiB |
|-----------|-------|-----|
| Conservative union of unique documents | 188,675,255,282 | 175.7 |
| Commercial reserve (+50 %) | 94,337,627,641 | 87.9 |
| Processing reserve (150 GiB) | 161,061,273,600 | 150.0 |
| Persistent results and logs (50 GiB) | 53,687,091,200 | 50.0 |
| **Total base required (30 days)** | **497,761,247,723** | **463.6** |

**44‑FZ, 99 operational codes, 30 days → 12.4 % of 4 TB SSD.**

## Linear extrapolation (90 / 180 days)

Document volume scales linearly with days; processing and persistent reserves
are fixed overhead.

| Window | Estimated total (bytes) | Estimated total (TiB) | % of 4 TB |
|--------|------------------------|-----------------------|-----------|
| 90 days | 1,063,787,013,569 | 0.97 | 26.6 |
| 180 days | 1,912,825,662,338 | 1.74 | 47.8 |

> **Note:** 90‑ and 180‑day figures are **linear extrapolations**, not measured
> values. Real volume may diverge due to seasonal procurement cycles, changes in
> EIS document composition, or growth in active procurement volume.

## Decision

**4 TB SSD approved for pilot.**

- Scope: Mac mini, first 5–10 clients.
- The measured 30-day 44‑FZ volume (12.4 % of capacity) leaves substantial
  headroom for 223‑FZ, capital repair, and organic growth.
- Even the linear extrapolation to 180 days (47.8 %) stays safely below the
  warning threshold.
- Further measurement of 90-/180-day windows, 223‑FZ, or capital repair is
  **cancelled** as disproportionate to the research objective.

## Applicability

| Factor | Scope |
|--------|-------|
| Hardware | Mac mini, external 4 TB SSD |
| Client count | First 5–10 pilot clients |
| Laws | 44‑FZ measured; 223‑FZ and capital repair not yet measured |
| Regions | 99 operational codes used by the measurement |
| Window | 30 days measured; 90/180 estimated linearly |

## Limitations

1. **Single-law measurement.** Only 44‑FZ was measured. 223‑FZ and capital
   repair volumes are expected to increase total but were not quantified.
2. **Seasonality.** A 30-day window in late June–July may not reflect peak
   procurement periods (e.g., year-end).
3. **No multi-tenant scaling.** The measurement reflects a single Mac mini.
   Multi-client isolation overhead is not included.
4. **No 90/180 live data.** The linear model assumes steady-state daily volume.
5. **Archive format overhead.** EIS ZIP archives contain XML + attachments;
   actual on-disk consumption may differ slightly from extracted byte totals.

## Storage policy

| Category | Retention |
|----------|-----------|
| Raw active packages (archives) | Preserved |
| Raw inactive / completed packages | Deleted |
| Metadata, normalized data, provenance, hashes, reports | Preserved indefinitely |

## Operational thresholds

| Threshold | Level | Action |
|-----------|-------|--------|
| Warning | 70 % | Alert ops |
| Critical | 80 % | Prepare ingestion back-pressure |
| Ingestion protection | 90 % | Halt new sweeps until space freed |

## Markers

- ARV-009_COMPLETE
- ARV-009_4TB_PILOT_STORAGE_APPROVED
- ARV-009_FURTHER_MEASUREMENT_CANCELLED
