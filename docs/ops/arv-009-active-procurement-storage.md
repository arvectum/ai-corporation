# ARV-009: Active Procurement Storage

## Objective

Demonstrate that 2 TB SSD is sufficient to store the documentation of all active procurements in EIS, with an additional 50% reserve for commercial procurements.

## Main Goal

Determine whether 2 TB SSD is sufficient for storing all active procurement documents in EIS, including a 50% reserve for commercial procurements.

## Steps

1. **Remove synthetic verdicts**

   - Synthetic data is only allowed for unit tests and CLI examples.
   - Synthetic data is **not** allowed for:
     - Total number of active procurements
     - Total size of EIS
     - Percentiles
     - Heavy tail
     - Verdict (GREEN/YELLOW/RED)
     - SSD recommendation

   Add top-level provenance:
   ```json
   {
     "measurement_kind": "real"
   }
   ```

   Only `measurement_kind: real` can be used for SSD verdict.

2. **Remove synthetic results**

   Remove current synthetic values from committed outputs:
   - 2800
   - 3100
   - 3480
   - 286.1 GiB
   - Future snapshot dates
   - Synthetic percentiles
   - GREEN verdict

   Before the real measurement, committed result should contain:
   ```json
   {
     "measurement_kind": "incomplete",
     "ssd_verdict": "unavailable",
     "reason": "real EIS snapshot not completed"
   }
   ```

   Do not save synthetic example as production summary.

   If needed, name the synthetic example explicitly:
   ```
   arv-009-active-snapshot.synthetic-example.json
   ```

   Prefer not to commit it.

3. **No implicit fallback**

   In real mode, remove fallback behavior:
   - No active procurements → run_demo()

   Instead, use:
   - Non-zero exit code
   - Clear error message
   - No output created
   - Marker: `ARV-009C1_REAL_MEASUREMENT_BLOCKED`

   Synthetic mode is only triggered with explicit `--demo` flag.

4. **Source of Active Procurements**

   Get the full list of active procurements through a connected real EIS.

   Priority:
   - SOAP/machine-readable EIS data
   - Existing live intake, if proven to provide a complete and up-to-date set
   - Local DB only if pre-populated with a full and current EIS snapshot

   Do not consider current rows in the local DB as a complete EIS without coverage verification.

   Document:
   - `source_type`
   - `query_started_at`
   - `query_completed_at`
   - `laws_requested`
   - `statuses_requested`
   - `records_received`
   - `unique_procurements`
   - `pagination_complete`
   - `source_errors`

   Do not commit tokens and procurement IDs.

5. **Definition of Active**

   Do not use the rule "any unknown status is active".

   Use canonical status mapping from the project.

   Include a procurement if:
   - Tender submission is open
   - Tender period has not yet ended
   - Procurement is in another explicitly allowed active status

   Exclude:
   - Completed
   - Cancelled
   - Archived
   - Outcome
   - Deadline passed
   - Unknown status

   Unknown status should be considered:
   - `excluded_unmapped`

   Include in coverage report.

6. **Documents List**

   For each active procurement, get the complete document manifest:
   - Document identifier
   - File name
   - URL in private temporary manifest
   - Declared size
   - Content type
   - Archive flag

   Committed aggregate should not contain identifiers, URLs, and names.

7. **Size Determination**

   For each document, use methods in strict order:
   - EIS metadata size
   - HTTP HEAD Content-Length
   - Range `bytes=0-0` and size from Content-Range
   - Streamed download with byte count and immediate deletion

   Implement B, C, and D methods.

   Do not document methods that are not implemented.

   For each document, document private provenance:
   ```json
   {
     "size_method": "eis_metadata" | "content_length" | "content_range" | "streamed" | "unavailable"
   }
   ```

8. **Coverage Gate**

   Calculate:
   - `active_procurements_total`
   - `active_procurements_with_document_manifest`
   - `documents_total`
   - `documents_with_known_size`
   - `documents_with_unknown_size`
   - `known_size_coverage_percent`
   - `procurement_coverage_percent`

   Conditions for final verdict:
   - `procurement_coverage_percent >= 95%`
   - `known_size_coverage_percent >= 95%`
   - `pagination_complete = true`
   - `measurement_kind = real`

   If any condition is not met:
   ```json
   {
     "ssd_verdict": "unavailable",
     "measurement_kind": "incomplete"
   }
   ```

   Do not extrapolate unknown data.

   Allow explicit upper-bound scenario for unknown documents, but do not present it as measured total.

9. **Single Real Snapshot Now**

   Make one real snapshot with a real UTC timestamp.

   Do not create dates in the future.

   Do not claim three snapshots.

   Fields:
   - `snapshot_started_at_utc`
   - `snapshot_completed_at_utc`
   - `snapshot_date`
   - `measurement_kind = real`

   Repeat snapshots on days 4 and 7 will be a separate next task.

10. **Aggregates**

   Calculate by the real snapshot:
   - Active procurements
   - Documents
   - Known bytes
   - Unknown documents
   - Mean
   - P50
   - P75
   - P90
   - P95
   - P99
   - Max

   Counts:
   - >100 MB
   - >250 MB
   - >500 MB
   - >1 GiB

   Heavy-tail:
   - Top 1%
   - Top 5%
   - Top 10%

   Breakdown:
   - 44-FZ
   - 223-FZ
   - Capital repair
   - Other supported EIS contours

11. **SSD Calculation**

   Use units without mixing.

   SSD:
   ```
   ssd_capacity_decimal_bytes = 2_000_000_000_000
   ```

   Also show:
   ```
   ssd_capacity_gib = ssd_capacity_decimal_bytes / 2^30
   ```

   Formula:
   ```
   eis_active_bytes = measured known document bytes
   commercial_reserve_bytes = eis_active_bytes * 0.50
   processing_space_bytes = max(150 GiB, p99_package_bytes * max_processing_concurrency)
   persistent_results_and_logs_bytes = 50 GiB
   base_required_bytes = eis_active_bytes + commercial_reserve_bytes + processing_space_bytes + persistent_results_and_logs_bytes
   remaining_bytes = ssd_capacity_decimal_bytes - base_required_bytes
   used_percent = base_required_bytes / ssd_capacity_decimal_bytes * 100
   ```

   Do not write "2,000 GiB".

12. **Verdict**

   GREEN:
   ```
   base_required_bytes <= 1_400_000_000_000
   ```

   YELLOW:
   ```
   1_400_000_000_000 < base_required_bytes <= 1_700_000_000_000
   ```

   RED:
   ```
   base_required_bytes > 1_700_000_000_000
   ```

   Verdict is only allowed after passing coverage gate.

13. **Safe Disk**

   Do not assign:
   ```
   safe_disk = TWO_TB
   ```

   Calculate separately:
   ```
   minimum_disk_bytes = base_required_bytes / 0.80
   ```

   This is the technical class of the disk, not a brand recommendation.

14. **Output**

   Update:
   - `samples/capacity/arv-009-active-snapshot-summary.json`
   - `samples/capacity/arv-009-active-snapshot-summary.csv`
   - `docs/ops/arv-009-active-procurement-storage.md`

   Committed JSON should contain:
   - `schema_version`
   - `measurement_kind`
   - `measurement_provenance`
   - `coverage`
   - `snapshot`
   - `size_statistics`
   - `heavy_tail`
   - `by_law_type`
   - `sizing`
   - `limitations`

15. **Documentation**

   Document should honestly separate:
   - Actually measured
   - Not measured
   - Assumption
   - User policy

   Commercial reserve 50% source: user policy

   Processing minimum 150 GiB source: planning assumption

   Persistent/results 50 GiB source: planning assumption

   Do not write "actual", "measured", or "full snapshot" for synthetic or incomplete data.

16. **Tests**

   Add tests:
   - Real mode never falls back to demo
   - Synthetic measurement cannot produce verdict
   - Future snapshot date rejected
   - Incomplete coverage cannot produce verdict
   - Coverage 94.99% rejected
   - Coverage 95% accepted
   - Unknown status excluded
   - Canonical status mapping used
   - Content-Length handling
   - Content-Range handling
   - Streamed byte count
   - Temporary streamed file removed
   - Decimal TB and GiB not mixed
   - Minimum disk calculated, not constant
   - Commercial reserve exactly 50%
   - Current timestamp real
   - No identifiers/URLs/tokens in committed outputs
   - Deterministic aggregate from fixed private input fixture

17. **Validation**

   Clean venv:
   ```
   python3.11 -m venv /tmp/arvectum-arv009-c11-venv
   source /tmp/arvectum-arv009-c11-venv/bin/activate
   ```
   ```
   python -m pip install --upgrade pip setuptools wheel
   python -m pip install -e '.[dev]'
   python -m pip check
   ```
   ```
   python -m compileall -q scripts/capacity tests/capacity
   python -m pytest -q tests/capacity
   make check
   make test
   python scripts/ops/secret_scan.py
   alembic heads
   git diff --check
   ```

18. **Commit**

   Create a new commit:
   ```
   fix(ops): replace synthetic active-storage evidence with real snapshot gate
   ```

   Do not amend.
   Do not force-push.

19. **PR #20**

   Update body:
   - Remove unconfirmed numbers
   - State real/incomplete status
   - State coverage
   - State actual source
   - State exact snapshot timestamp
   - State SSD verdict only if coverage >=95%.

   PR remain Draft.

20. **Final Report**

   1. Real source.
   2. Snapshot time.
   3. Pagination completeness.
   4. Active procurement count.
   5. Document count.
   6. Procurement coverage.
   7. Document-size coverage.
   8. Known bytes.
   9. Unknown documents.
   10. Percentiles.
   11. Heavy tail.
   12. Commercial reserve.
   13. Processing reserve.
   14. Base required.
   15. 2 TB capacity in decimal bytes and GiB.
   16. Remaining capacity.
   17. Minimum disk at 20% free-space rule.
   18. Verdict or unavailable reason.
   19. Tests.
   20. CI.
   21. Commit.
   22. PR status.
   23. Status markers:
   - `ARV-009C1_REAL_SNAPSHOT_COMPLETED`
   - If source is unavailable: `ARV-009C1_REAL_SNAPSHOT_BLOCKED_NO_SYNTHETIC_FALLBACK`

   Stop after the report.
