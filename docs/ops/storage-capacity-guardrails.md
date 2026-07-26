# Storage Capacity Guardrails

## ARV-010 — Ingestion Protection

### Context

ARV-009 established a 4 TB external SSD as the pilot storage root and defined
operational thresholds (70 % warning, 80 % critical, 90 % ingestion protection).
ARV-010 implements runtime enforcement of those thresholds.

### Storage root resolution

The storage root is configured via environment variables:

**Canonical name** (highest priority):
- `ARVECTUM_STORAGE_ROOT`

**Compatibility name** (lower priority, backward compatibility):
- `AI_CORP_ARVECTUM_STORAGE_ROOT`

When both are set, the canonical name takes priority (determined by `AliasChoices` order).

```
ARVECTUM_STORAGE_ROOT
```

If this variable is:

- unset or empty;
- pointing to a path that does not exist;
- pointing to a path that is not a directory;
- pointing to the system disk (`stat.st_dev` matches `/`);

the storage state is reported as `storage_unknown` and ingestion is blocked
(fail-closed behaviour).

### Thresholds

| Условие | State | Canonical env | Compatibility env |
|---------|-------|---------------|-------------------|
| used < warning | `normal` | — | — |
| warning <= used < critical | `warning` | `ARVECTUM_STORAGE_WARNING_PERCENT` | `AI_CORP_ARVECTUM_STORAGE_WARNING_PERCENT` |
| critical <= used < protected | `critical` | `ARVECTUM_STORAGE_CRITICAL_PERCENT` | `AI_CORP_ARVECTUM_STORAGE_CRITICAL_PERCENT` |
| used >= protected | `ingestion_protected` | `ARVECTUM_STORAGE_INGESTION_PROTECTED_PERCENT` | `AI_CORP_ARVECTUM_STORAGE_INGESTION_PROTECTED_PERCENT` |
| N/A | `storage_unknown` | — | — |

All threshold env vars accept integer values (0–100).

**Defaults:** `warning = 70`, `critical = 80`, `protected = 90`.

Thresholds are validated at config load time:

```
0 <= warning < critical < ingestion_protected <= 100
```

### Storage snapshot

The `get_storage_snapshot()` function returns:

- `storage_root` — resolved path (not exposed in public API)
- `filesystem_total_bytes`
- `filesystem_used_bytes`
- `filesystem_free_bytes`
- `used_percent`
- `state` — one of the states above
- `checked_at` — ISO-8601 UTC timestamp (always filled)
- `mount_verified` — whether `st_dev` differs from the system root
- `reason` — safe public reason code (no absolute paths)

A public-facing variant (`PublicStorageSnapshot`) omits `storage_root` and
uses safe reason codes.

### Public reason codes

| Reason | Meaning |
|--------|---------|
| `storage_root_not_configured` | `ARVECTUM_STORAGE_ROOT` not set |
| `storage_root_missing` | Path does not exist |
| `storage_root_not_directory` | Path is not a directory |
| `storage_mount_not_verified` | Path resolves to system disk |
| `storage_usage_unavailable` | `disk_usage()` call failed |
| `threshold_normal` | Used space < warning threshold |
| `threshold_warning` | Used space >= warning threshold |
| `threshold_critical` | Used space >= critical threshold |
| `threshold_ingestion_protected` | Used space >= ingestion protected threshold |

### Blocked operations

When the state is `ingestion_protected` or `storage_unknown`, ingestion is blocked
on the following entrypoints (fail-closed):

- `prepare_tender_for_analysis()` — service-level gate
- `POST /api/tender-research/prepare` — synchronous API
- `POST /api/tender-research/jobs/prepare` — background job submission
- `run_prepare_job()` — background worker recheck (job is `fail_job`'d with `current_step="gate_check"`)
- Any function explicitly decorated with `@mass_ingestion`

The gate returns machine-readable errors with HTTP 503:

- `STORAGE_CAPACITY_PROTECTION_ACTIVE` — capacity threshold exceeded
- `STORAGE_STATE_UNKNOWN` — storage root cannot be resolved or verified

### Implemented enforcement

1. **Service-level gate** — `prepare_tender_for_analysis()` checks
   `check_ingestion_allowed()` before any download or file creation.
2. **Synchronous prepare API** — `POST /api/tender-research/prepare` returns
   HTTP 503 with machine-readable error code.
3. **Background prepare submission** — `POST /api/tender-research/jobs/prepare`
   checks gate before creating the job record.
4. **Background worker recheck** — `run_prepare_job()` rechecks storage
   immediately before `mark_job_running()`, catching jobs queued when space
   was adequate but filled before execution.

### Not yet covered

The following do NOT have a storage gate yet and must be protected when implemented:

- Future national SOAP sweep runtime
- Future bulk procurement download endpoints
- Future batch file-ingestion flow (e.g., DocumentSet creation)

### Permitted operations

The following continue to function regardless of storage state:

- Reading existing data (GET endpoints, list queries)
- Report generation and export
- Completing an already-running atomic operation
- Cleanup tasks
- Backup / restore
- Health diagnostics
- Metadata-only operations (e.g., `create_document_ingestion_run()`)

### Error semantics

`IngestionBlockedError` uses HTTP 503 (Service Unavailable) because the
request is valid but temporarily blocked by operational state. This
distinguishes it from client errors (4xx) and server bugs (5xx).

### Fail-closed rationale

If storage state cannot be determined, the system assumes the worst case and
blocks mass ingestion. This prevents data loss or corruption from an
unexpectedly full or misconfigured storage device.

### Manual recovery procedure

1. Free space on the storage root (delete unused archives, move old data).
2. Verify the root is accessible and mounted correctly.
3. The next `get_storage_snapshot()` call will reflect the improved state.
4. Mass ingestion resumes automatically when state returns to `normal`,
   `warning`, or `critical`.

No manual override flag is provided. If the operator needs to force ingestion
in a degraded state, the threshold env vars can be temporarily adjusted (e.g.,
raising `ARVECTUM_STORAGE_INGESTION_PROTECTED_PERCENT` (canonical) or `AI_CORP_ARVECTUM_STORAGE_INGESTION_PROTECTED_PERCENT` (compatibility) to 99).

### Health endpoint

The existing `/health/ready` endpoint includes a `storage` section with:

- `storage_total_bytes`
- `storage_free_bytes`
- `storage_used_percent`
- `storage_state`
- `ingestion_allowed`

### Metrics

The `storage_metrics_dict()` function returns a dictionary for the `/health/ready` endpoint with these fields:

- `storage_total_bytes`
- `storage_free_bytes`
- `storage_used_percent`
- `storage_state`
- `ingestion_allowed`

No additional observability stack is required. Existing log aggregation
captures these fields.

### Markers

- ARV-010_STORAGE_GUARDRAILS_IMPLEMENTED
- ARV-010_STORAGE_INGESTION_PROTECTION_VERIFIED
- ARV-010_STORAGE_GATE_CONNECTED_TO_REAL_INGESTION
- ARV-010_BACKGROUND_JOB_RECHECK_VERIFIED
