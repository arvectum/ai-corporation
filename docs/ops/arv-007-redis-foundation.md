# ARV-007: Redis Foundation

## Role of Redis

Redis provides ephemeral coordination primitives:
- **Distributed lock** for safety-critical run start coordination
- **In-flight idempotency claim** to reject duplicate concurrent requests
- **TTL cache** (adapter; not production-integrated in ARV-007)
- **Atomic rate-limit** (adapter; not production-integrated in ARV-007)
- **Queue contract/envelope** for ARV-008 (adapter only; no worker)

## PostgreSQL is the Source of Truth

- `TenderAnalysisRun` with `UniqueConstraint("procurement_case_id", "idempotency_key")` is canonical.
- Redis lock/idempotency TTL expiry does NOT create or confirm a run.
- Redis outage does NOT produce duplicate `TenderAnalysisRun` rows.
- Completed runs are returned from PostgreSQL even if Redis key is lost.

## Key Format & Tenant Isolation

Keys follow the pattern:
```
{namespace}:{environment}:{component}:{tenant}:{customer}:{project}:{case}:{operation}:{sha256(idempotency_key)}
```

All keys include tenant/customer dimensions. Cross-tenant isolation is enforced by the key structure.

## Raw Key Hashing Policy

User-controlled or potentially sensitive dimensions (idempotency key) are SHA-256 hashed before inclusion in Redis keys. Raw idempotency keys never appear in Redis keys, values, or diagnostics.

## TTL Policy

| Primitive     | Default TTL | Notes                                |
|---------------|-------------|--------------------------------------|
| Lock          | 30s         | Abandonded lock auto-expires         |
| Idempotency   | 3600s       | In-flight claim TTL                  |
| Cache         | 300s        | Fail-open on outage                  |
| Rate-limit    | window+60s  | Window expiry                        |
| Queue lease   | 300s        | Visibility timeout (ARV-008)         |

## Fail Semantics

| Operation        | Outage Behavior            |
|------------------|----------------------------|
| Lock acquire     | Fail closed (503)          |
| Idempotency claim| Fail closed (503)          |
| Cache get/set    | Fail open (cache miss)     |
| Rate-limit check | Fail closed                |

## Ownership-Safe Primitives

Both lock and idempotency use token-based ownership:

- **Lock**: `acquire()` returns a random token; `release(key, token)` uses Lua compare-and-delete — only the owner can release.
- **Idempotency**: `claim()` returns a random token (`str | None`); `release(key, token)` uses the same Lua compare-and-delete pattern. A foreign token cannot release or overwrite the claim.

## Single-Instance Lease Limitations

Redis lock uses a single-instance SET NX PX with Lua compare-and-delete release. Redlock/multi-node consensus is not implemented. The lock provides best-effort mutual exclusion within TTL bounds. If the Redis instance fails, the lock is released when TTL expires.

## Local Start / Ping / Stop

```bash
# Start Redis (test profile, loopback port 16379)
make redis-start

# Start Redis (no public port, with mandatory password)
export ARVECTUM_REDIS_PASSWORD='your-password'
docker compose -f docker-compose.redis.yml up -d

# Ping
make redis-ping

# Stop
make redis-stop

# Clean (remove volume)
make redis-clean
```

## Test-Only Loopback Exposure

`docker-compose.redis-test.yml` binds `127.0.0.1:16379:6379` for integration tests. Production `docker-compose.redis.yml` has no public port by default.

## Health Diagnostics

`GET /health/ready` includes:
```json
{
  "status": "ok|degraded",
  "redis": {
    "enabled": true,
    "status": "healthy|disabled|unavailable",
    "latency_ms": 1.2,
    "error_category": null
  },
  "feature_readiness": {
    "customer_pilot_run_start": "ready|blocked"
  }
}
```

The overall `status` is `degraded` when Redis is enabled but unavailable, even if storage is healthy. Redis URL, password, and raw exceptions are never exposed in health responses.

## Troubleshooting

- Check Redis connectivity: `redis-cli ping`
- Verify key isolation: `redis-cli --scan --pattern '{namespace}:*'`
- Check lock exists: `redis-cli get '<key>'`
- Clear dev keys safely: `redis-cli --scan --pattern 'arvectum:dev:*' | xargs redis-cli del` (never FLUSHALL/FLUSHDB in shared environments)
- Use `delete_test_namespace(client, namespace)` from test code for namespace-scoped cleanup:
  ```python
  from tests.integration.conftest import delete_test_namespace
  delete_test_namespace(redis_client, "test-arv007-<uuid>")
  ```
  The function refuses `arvectum`, `production`, and empty namespace prefixes.

## Security Boundary

- Redis URL is read from environment only (`.env.local` or CI secrets).
- Redis password is never committed to tracked files.
- Secrets, credentials, and raw idempotency keys are never written to Redis keys or values.
- Cache adapter rejects payloads with secret-like key prefixes (recursive check for nested dicts/lists).
- Error messages never include raw exception class names — only sanitized category labels.
- Raw exceptions, connection strings, and keys are never logged.

## Namespace Scoped Cleanup

Test code must never call `FLUSHDB` or `FLUSHALL`. Instead, each integration test operates within a unique prefix namespace (`test-arv007-<uuid>:`) and cleanup uses `delete_test_namespace()`:

1. Each test creates keys under `{test_namespace}:{component}:{test_label}`.
2. The `cleanup_keys` pytest fixture calls `delete_test_namespace(client, test_namespace)` after each test.
3. `delete_test_namespace()` uses `SCAN` to find matching keys and `DEL` to remove them in batches of 100.
4. The function refuses to operate on namespaces matching `arvectum`, `production`, or empty string, preventing accidental data loss.

This ensures zero cross-test pollution and safe parallel execution without global Redis state mutation.

## Test State Reset

When tests modify environment variables (e.g. to simulate Redis outage), the correct reset sequence is:

```python
# After monkeypatching env vars:
from src.shared.redis.client import reset_redis_runtime
reset_redis_runtime()  # clears settings cache + closes Redis client
```

`reset_redis_runtime()` calls `invalidate_settings_cache()` (clears `@lru_cache` on `get_settings()`) then `close_client()`. There is no direct access to private `_client_instance`/`_client_disabled` globals from test code.

## CI History

The quality job (`make check`) failed consistently across all commits until `894ca57`. The root cause was **ruff 0.16.0** adding new default rules:
- `PIE810`: merge `startswith` calls with a tuple
- `RUF022`: sort `__all__` entries
- `S110` / `BLE001`: suppress blind `except Exception:` / `try-except-pass` where intentional
- `I001`: sort import blocks
- `UP017`: use `datetime.UTC` alias (Python 3.11+)
- `C408`: use dict literal instead of `dict()` call

The redis-integration job originally used `2>&1 | tee` which masked the pytest exit code, producing false-green results. Fixed by redirecting output to a file first, capturing the status, then catting and returning the real exit code. The Makefile target now propagates the actual test outcome.

## Deferred to ARV-008

- Worker process / consumer
- Queue enqueue/dequeue runtime
- ThreadPoolExecutor migration
- Batch runtime
- Pause/cancel/resume
- Celery, RQ, Dramatiq
