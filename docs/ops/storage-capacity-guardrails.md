# Storage Capacity Guardrails

## ARV-010 — Ingestion Protection

### Context

ARV-009 established a 4 TB external SSD as the pilot storage root and defined
operational thresholds (70 % warning, 80 % critical, 90 % ingestion protection).
ARV-010 implements runtime enforcement of those thresholds.

### Storage root resolution

The storage root is configured via the environment variable:

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

| Used % | State | Default env override |
|--------|-------|---------------------|
| < 70 % | `normal` | `ARVECTUM_STORAGE_WARNING_PERCENT` |
| 70–79 % | `warning` | `ARVECTUM_STORAGE_CRITICAL_PERCENT` |
| 80–89 % | `critical` | `ARVECTUM_STORAGE_INGESTION_PROTECTED_PERCENT` |
| ≥ 90 % | `ingestion_protected` | — |
| N/A | `storage_unknown` | — |

All threshold env vars accept integer values (0–100).

### Storage snapshot

The `get_storage_snapshot()` function returns:

- `storage_root` — resolved path (not exposed in public API)
- `filesystem_total_bytes`
- `filesystem_used_bytes`
- `filesystem_free_bytes`
- `used_percent`
- `state` — one of the states above
- `checked_at` — ISO-8601 UTC timestamp
- `mount_verified` — whether `st_dev` differs from the system root
- `reason` — human-readable explanation

A public-facing variant (`PublicStorageSnapshot`) omits `storage_root`.

### Blocked operations

When the state is `ingestion_protected` or `storage_unknown`, the following
mass operations are blocked (fail-closed):

- Full SOAP sweeps (EIS national sweeps)
- Mass download of procurement documents
- Batch document ingestion (`DocumentSet` creation)
- Any operation decorated with `@mass_ingestion`

The gate returns machine-readable errors:

- `STORAGE_CAPACITY_PROTECTION_ACTIVE` — capacity threshold exceeded
- `STORAGE_STATE_UNKNOWN` — storage root cannot be resolved or verified

### Permitted operations

The following continue to function regardless of storage state:

- Reading existing data (GET endpoints, list queries)
- Report generation and export
- Completing an already-running atomic operation
- Cleanup tasks
- Backup / restore
- Health diagnostics

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
raising `ARVECTUM_STORAGE_INGESTION_PROTECTED_PERCENT` to 99).

### Health endpoint

The existing `/health/ready` endpoint includes a `storage` section with:

- `storage_total_bytes`
- `storage_free_bytes`
- `storage_used_percent`
- `storage_state`
- `ingestion_allowed`

### Metrics

Structured logs (via `storage_metrics_dict()`) emit these fields:

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
