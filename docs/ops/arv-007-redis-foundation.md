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

## Single-Instance Lease Limitations

Redis lock uses a single-instance SET NX PX with Lua compare-and-delete release. Redlock/multi-node consensus is not implemented. The lock provides best-effort mutual exclusion within TTL bounds. If the Redis instance fails, the lock is released when TTL expires.

## Local Start / Ping / Stop

```bash
# Start Redis (test profile, loopback port 16379)
make redis-start

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

Redis URL, password, and raw exceptions are never exposed in health responses.

## Troubleshooting

- Check Redis connectivity: `redis-cli -a <password> ping`
- Verify key isolation: `redis-cli keys '{namespace}:*'`
- Check lock exists: `redis-cli get '<key>'`
- Clear dev keys safely: `redis-cli --scan --pattern 'arvectum:dev:*' | xargs redis-cli del` (never FLUSHALL/FLUSHDB in shared environments)

## Security Boundary

- Redis URL is read from environment only (`.env.local` or CI secrets).
- Redis password is never committed to tracked files.
- Secrets, credentials, and raw idempotency keys are never written to Redis keys or values.
- Cache adapter rejects payloads with secret-like key prefixes.

## Deferred to ARV-008

- Worker process / consumer
- Queue enqueue/dequeue runtime
- ThreadPoolExecutor migration
- Batch runtime
- Pause/cancel/resume
- Celery, RQ, Dramatiq
