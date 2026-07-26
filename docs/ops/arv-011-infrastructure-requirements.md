# ARV-011A — Runtime Topology Audit and Infrastructure Requirements

## 1. Executive decision

The current Mac Mini contour is **sufficient for the pilot phase** (5–10 restricted clients) with the following caveats:

- It is **not production-grade**: single hardware failure, no TLS termination, no off-device backup verification, no reverse proxy, no rate limiting, no 24/7 operational coverage.
- **Production readiness requires**: dedicated server or VPS with TLS, reverse proxy, automated off-device backup with verified restore, isolated PostgreSQL, and documented DR procedure.
- **Provider selection is premature**: actual resource profiles (CPU, RAM, disk IO) for the pilot workload have not been measured. Choosing a provider now would commit to unbounded cost without empirical data.

## 2. Scope and exclusions

### In scope
- Audit of runtime topology as reflected in the repository at the base SHA
- Service map of observed, documented, planned, and proposed components
- Infrastructure requirements for future application, database, storage, and networking nodes
- Security requirements classified by pilot/production/legal urgency
- Migration triggers from the Mac Mini contour to server-based infrastructure
- Three architecture options without brand/provider selection

### Out of scope (ARV-011A hard constraints)
- Provider selection, pricing research, server purchase, deployment
- Redis, n8n, Sentry, Prometheus, Grafana, reverse proxy, rate limiter introduction
- Runtime topology, Docker Compose, Dockerfile, application code, or configuration changes
- New storage measurements, load tests, EIS/SOAP capacity sweeps
- Alembic migration creation or modification
- Any file changes outside `docs/ops/arv-011-infrastructure-requirements.md` and `samples/ops/arv-011-infrastructure-requirements.json`

## 3. Repository baseline

- **Base SHA**: `da7b0785db82eb3728552081e70a8beccacc2e43`
- **Branch**: `opencode/arv-011-infrastructure-requirements`
- **Origin/main SHA**: `da7b0785db82eb3728552081e70a8beccacc2e43` (matches local HEAD)
- **Working tree**: clean, no uncommitted changes

## 4. Audit methodology

Each assertion carries one of five statuses:

| Status | Meaning |
|--------|---------|
| **observed** | Confirmed by code or runtime configuration at the base SHA |
| **documented** | Described in current documentation but not confirmed by runtime code |
| **planned** | Explicitly planned but not implemented |
| **proposed** | Suggested as a requirement for the future contour |
| **unknown** | Insufficient data in the repository |

Every substantive finding includes an evidence reference (file path and line range).

## 5. Current runtime summary

The repository contains **multiple configuration variants** for different contours. The actual deployed runtime combination on the Mac Mini is **unknown** — it cannot be fully reconstructed from the repository alone. Runtime process state is not verifiable from committed files.

### A. Main container definitions (Dockerfile + docker-compose.yml)

- **Dockerfile**: FastAPI/Uvicorn, `CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]`
- **docker-compose.yml**: only PostgreSQL `postgres:16-alpine`, host bind `5432:5432`, named volume `postgres_data`
- **pgvector** is NOT available in this compose image — it is `postgres:16-alpine` without pgvector

### B. Dedicated PostgreSQL compose (docker-compose.postgres.yml)

- **Image**: `pgvector/pgvector:pg17`
- **Port**: `127.0.0.1:55432:5432`
- **Volume**: separate named volume `arvectum_postgres_data`
- **Healthcheck**: `pg_isready` with configured credentials
- **Status**: observed configuration — not a proven running service in any specific contour

### C. R8 acceptance compose (docker-compose.r8-acceptance.yml)

- **Image**: `pgvector/pgvector:pg16`
- **Port**: `127.0.0.1:15432:5432`
- **Status**: test/acceptance contour — not pilot production runtime

### D. Mac Mini env examples

- **`.env.macmini.example`**: PostgreSQL `127.0.0.1:5432`, local OpenAI-compatible LLM at `localhost:8088`, SOAP disabled
- **`.env.runtime.example`**: PostgreSQL `127.0.0.1:55432`, backend port `8001`, Ollama-compatible endpoint at `127.0.0.1:11434`, embeddings endpoint at `127.0.0.1:8090`, Hermes disabled
- **Status**: documented configuration examples — not proof of actual running processes

### Summary

| Aspect | Status |
|--------|--------|
| Deployed runtime combination | unknown — cannot be reconstructed from repo alone |
| Runtime process state | not verifiable from repository |
| Available configuration variants | main Docker + Compose, dedicated pgvector Compose, r8-acceptance Compose, macmini env example, runtime env example |
| No reverse proxy (main deployment) | observed — nginx exists only in site-pilot compose |
| No TLS termination | observed — none configured in any deployment variant |
| No background worker process | observed — ThreadPoolExecutor is in-process |
| No message queue | observed — in-process `_FUTURES` dict |
| No monitoring stack | observed — no Prometheus/Grafana/Sentry config in repo |

## 6. Current service map

| Component | Status | Purpose | Stateful |
|-----------|--------|---------|----------|
| FastAPI application code | observed | Business API server | No |
| Uvicorn Docker CMD | observed | Container entrypoint on 0.0.0.0:8000 | No |
| SQLAlchemy / Alembic | observed | ORM and schema migration management | No |
| Background ThreadPoolExecutor | observed | In-process executor for RAG prepare/analyze | No (ephemeral) |
| PostgreSQL Compose definitions | observed | postgres:16-alpine and pgvector/pgvector variants | Yes |
| pgvector extension | observed (migrations, diagnostics, dedicated/test Compose) | Vector similarity search for RAG | Yes |
| pgvector in main Compose | gap — NOT available in postgres:16-alpine | N/A | N/A |
| LLM/embedding/Hermes HTTP clients | observed | Configurable provider abstractions in code | No |
| EIS/SOAP client | observed | Zakupki.gov.ru SOAP client | No |
| Document storage code | observed | Filesystem download/extract/store logic | No (code) |
| Storage capacity guardrails | observed | ARV-010: warning 70%, critical 80%, ingestion protection 90% | No |
| Health/readiness endpoints | observed | `/health`, `/health/ready` | No |
| Reverse proxy (site-pilot demo) | observed | nginx for site-pilot demo Compose | No |
| Local LLM process | documented/configurable — runtime status unknown | Inference via local endpoint | No (process) |
| Embedding server process | documented/configurable — runtime status unknown | Embedding generation | No (process) |
| Hermes agent process | documented/configurable, default disabled — runtime status unknown | Optional analysis sidecar | No (process) |
| External 4 TB SSD | documented/approved (ARV-009) — actual mounted state unknown | Document storage | Yes |
| Backup storage | unknown — no backup scripts or infra found | Database and document backups | N/A |
| Monitoring | unknown — no Prometheus/Grafana/Sentry config | Observability | N/A |

### Evidence references

- **FastAPI application**: `Dockerfile:22` — `CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]`
- **PostgreSQL**: `docker-compose.yml:3` — `image: postgres:16-alpine`, `docker-compose.r8-acceptance.yml:3` — `image: pgvector/pgvector:pg16`
- **Background job executor**: `src/tender_research/rag/job_runner.py:26` — `ThreadPoolExecutor(max_workers=2, ...)`, in-process, ephemeral queue
- **Document storage root**: `src/shared/config/settings.py:44` — `arvectum_data_dir: str = "./data"`
- **Hermes base URL**: `src/shared/config/settings.py:102` — `hermes_base_url: str = "http://127.0.0.1:8099"`
- **Local LLM base URL**: `src/shared/config/settings.py:80` — `local_llm_base_url: str = "http://127.0.0.1:8088/v1"`
- **Embedding base URL**: `src/shared/config/settings.py:72` — `rag_embeddings_base_url: str = "http://127.0.0.1:8090/v1"`
- **Storage guardrails**: `src/shared/config/settings.py:114-125` — warning=70, critical=80, ingestion_protected=90
- **Health endpoint**: `src/main.py:206-208` — `/health` returns `{"status": "ok"}`
- **Readiness endpoint**: `src/main.py:211-222` — `/health/ready` checks data_dir writable + storage gate

## 7. Runtime startup and process model

- **Single Uvicorn process** on `0.0.0.0:8000` with `--proxy-headers`
- **No startup hooks** that run background workers or schema migrations automatically
- **Alembic migrations are NOT applied automatically** on startup — they require explicit `alembic upgrade head`
- **No `lifespan` or `startup/shutdown` events** in `src/main.py`
- **Docker HEALTHCHECK** defined only in `deploy/pilot/Dockerfile:16` (not in the main `Dockerfile`)
- **No process manager** (no supervisor, no systemd unit in repo)

Evidence: `src/main.py:107-225`, `Dockerfile:22`

## 8. Database and pgvector

### Observed facts
- **Default SQLite** for local dev: `sqlite:///./ai_corporation.db` (`settings.py:11`)
- **PostgreSQL** for server/macmini: `postgresql+psycopg://...` (via `AI_CORP_DATABASE_URL`)
- **Docker Compose (r8-acceptance)**: `pgvector/pgvector:pg16` image, port 15432 bound to 127.0.0.1
- **Docker Compose (postgres)**: `pgvector/pgvector:pg17` image, port 55432 bound to 127.0.0.1
- **Docker Compose (main)**: `postgres:16-alpine` image, port 5432 publicly bound
- **pgvector extension**: checked at runtime via `src/shared/db/diagnostics.py:44-49` — queries `pg_extension` for `vector`
- **SQLAlchemy engine**: single engine with `check_same_thread=False` for SQLite, no connection pooling configured for PostgreSQL
- **No persistent volume for app data** in the main docker-compose (only `postgres_data` named volume)
- **Named volume `postgres_data`** for PostgreSQL data persistence

### Risks
- Main `docker-compose.yml` uses `postgres:16-alpine` **without pgvector** — the r8-acceptance and postgres compose files use the correct `pgvector/pgvector` image
- PostgreSQL in main compose is publicly bound (`5432:5432`), not restricted to `127.0.0.1`
- No connection pooling (e.g., PgBouncer) configured
- No backup mechanism evident in the repository

Evidence: `docker-compose.yml:3,9`, `docker-compose.r8-acceptance.yml:3-5`, `docker-compose.postgres.yml:4,13`, `src/shared/config/settings.py:11`, `src/shared/db/session.py:10-14`

## 9. Background processing

### Observed model
- **In-process ThreadPoolExecutor** with `max_workers=2` in `src/tender_research/rag/job_runner.py:26`
- **Two job types**: `prepare` (document ingestion, chunking, embedding) and `analyze` (RAG-based tender analysis)
- **Database-persisted job state** via `TenderAnalysisJob` model (PostgreSQL/SQLite)
- **Job lifecycle**: queued → running → completed/failed/cancelled
- **No separate worker process** — jobs run inside the FastAPI process
- **Queue is ephemeral**: in-memory `_FUTURES` dict (`job_runner.py:27-28`) — lost on process restart
- **No retry mechanism** for failed jobs (jobs fail permanently)
- **No scheduling mechanism** — jobs are submitted via API endpoints

### Limitations
- Jobs block the API process under load (same process, shared GIL for Python code)
- Queue is lost on restart — running/queued jobs disappear
- No deduplication — same registry number can be submitted multiple times
- `max_workers=2` limits concurrent analysis even on multi-core hardware
- No monitoring of queue depth or job age

Evidence: `src/tender_research/rag/job_runner.py:26-28,384-391`, `src/tender_research/rag/job_schemas.py`, `src/tender_research/rag/job_service.py`, `src/tender_research/models.py:309`

## 10. Persistent data and storage paths

| Path | Config source | Type | Persistent | Backed up | Loss tolerable | Contains customer data |
|------|---------------|------|------------|-----------|----------------|----------------------|
| `./data/` (configurable via `AI_CORP_ARVECTUM_DATA_DIR`) | `settings.py:44` | Filesystem root | Yes (SSD) | Unknown | No | Yes |
| `./data/tenders/` | Implicit in document_store.py | Tender documents | Yes | Unknown | No | Yes |
| `./data/eis_seed/registry_numbers.txt` | `settings.py:49` | Seed data | Yes | Unknown | Partial (regenerable) | No |
| `postgres_data` (Docker volume) | `docker-compose.yml:11` | PostgreSQL data | Yes | Unknown | No | Yes |
| `./ai_corporation.db` (SQLite) | `settings.py:11` | SQLite DB file | Yes | Unknown | No | Yes |
| Reports/artifacts in `./data/` | code | Generated reports | Yes | Unknown | Partial (regenerable) | Yes |
| Company agent runs | `company_agent_runs/` | Agent output | Yes | Unknown | Partial | Yes |
| Local pilot runs | `local_pilot_runs/` | Pilot data | Yes | Unknown | Partial | Yes |

### ARV-009 compliance
- External 4 TB SSD is accepted for pilot and first 5–10 clients
- Raw packages of active procurements are preserved
- Raw packages of completed/inactive procurements are removed
- Metadata, normalized results, provenance, hashes, URLs, reports, and audit history are preserved
- No separate cumulative archive of all historical raw packages

### ARV-010 compliance
- Storage guardrails observed: warning 70%, critical 80%, ingestion protection 90%
- `storage_unknown` is fail-closed (ingestion blocked)
- Storage root must be on a separate filesystem (enforced in code via `_mount_verified()`)
- Ingestion is blocked at `STORAGE_UNKNOWN` or `INGESTION_PROTECTED` states

Evidence: `src/shared/storage/capacity.py:62-77,81-88,122-125`, `src/shared/storage/gate.py:39-44`

## 11. Backup and restore

### Observed status
- **No backup scripts** exist in the repository
- **No documented backup procedure** in ops docs
- **No restore procedure** documented
- **No off-device copy mechanism** configured
- **No backup schedule** defined
- **Backup retention policy**: not documented
- **Data/raw retention policy**: exists per ARV-009 (raw packages of active procurements preserved, completed/inactive removed; metadata/results/reports preserved)
- **No verified restore** ever performed (no evidence in repo)

### Gap analysis
- PostgreSQL `pg_dump` could be used but is not scripted or scheduled
- Document storage (4 TB SSD) has no backup strategy
- No mechanism ensures consistency between database state and filesystem documents at backup time
- No RPO/RTO defined

Evidence: searched `scripts/`, `docs/ops/`, `Makefile` — no backup or restore targets found

## 12. Health, readiness and observability

### Observed endpoints
- **`GET /health`**: returns `{"status": "ok"}` — no dependencies checked, always returns 200 if process is alive. Suitable for liveness only.
- **`GET /health/ready`**: checks `data_dir` writability and storage gate state. Returns `{"status": "ok"}` or `{"status": "degraded"}` with storage metrics. Suitable for readiness.
- **`GET /api/tender-research/health`**: checks database connectivity, migration head, pgvector availability, and table counts. More comprehensive but scoped to tender-research prefix.

### Observations
- No database connectivity check in the main `/health/ready` endpoint
- No LLM provider health check
- No EIS/SOAP endpoint health check
- No Hermes connectivity check
- No Prometheus metrics endpoint
- Standard Python logging is present; centralized structured log collection is not evidenced
- No audit log for health state transitions
- Readiness endpoint is behind pilot auth in production config (`.env.runtime.example:16` — `/health/ready` in protected prefixes)

Evidence: `src/main.py:206-222`, `src/tender_research/api.py:286-309`

## 13. Network exposure and public ingress

### Current exposure
- **FastAPI on 0.0.0.0:8000** — listens on all interfaces (`Dockerfile:22`)
- **PostgreSQL is bound to all host interfaces by the main Compose mapping `5432:5432`** and may be externally reachable depending on host firewall, NAT and network configuration. Actual public-internet reachability is unknown.
- **No TLS termination** at the application or infrastructure level
- **No reverse proxy** in the main deployment (only in `site-pilot` compose with nginx)
- **TrustedHostMiddleware**: configured via `AI_CORP_ALLOWED_HOSTS`; if the list is empty, the middleware is NOT installed and Host header filtering is absent.
- **CORSMiddleware**: configured via `AI_CORP_CORS_ALLOW_ORIGINS`; if the list is empty, the middleware is NOT installed and the application does not issue CORS permissions for cross-origin browser requests. Production origins must be set explicitly if browser access is required.
- **Basic auth** for pilot API paths — configurable via `AI_CORP_TENDER_PILOT_BASIC_AUTH_ENABLED` / `AI_CORP_PILOT_AUTH_ENABLED`
- **Proxy headers** enabled via `--proxy-headers` Uvicorn flag
- **No rate limiting**

### Risks
- PostgreSQL bound to all interfaces in main Compose (`5432:5432`) with default credentials (`ai_corporation:ai_corporation`); reachability depends on firewall and NAT
- No TLS means traffic is in plaintext, including basic auth credentials
- TrustedHostMiddleware is opt-in — if `AI_CORP_ALLOWED_HOSTS` is empty, no Host header filtering
- No reverse proxy means no request filtering, no WAF, no DDoS protection

Evidence: `Dockerfile:22`, `docker-compose.yml:9`, `src/shared/api/middleware.py:53-83`, `src/shared/config/settings.py:13-14`

## 14. ЕИС/SOAP dependencies

### Observed facts
- **SOAP client**: `src/modules/tender_operator_agent_demo/zakupki_soap_client.py`
- **Two endpoints**: individual (`getDocsIP`) and legacy legal-entity (`searchProcurements`)
- **Network requirements**:
  - Direct HTTPS to `zakupki.gov.ru` (proxy bypass configurable)
  - `NO_PROXY` domains: `zakupki.gov.ru,.zakupki.gov.ru,int.zakupki.gov.ru,int44.zakupki.gov.ru`
  - Russian egress is required for production use (zakupki.gov.ru may geo-block)
  - TLS trust policy configurable via `ARVECTUM_ETP_TLS_*` variables
- **Timeout**: 30 seconds configurable
- **Max results**: 10 per search
- **Max attachments**: 20
- **Max download**: 200 MB
- **Authentication**: individual person token via SOAP header
- **SSL verification**: truststore-based, fail-closed by default

### Risks
- No proxy serving as a controlled egress point
- Russian hosting may be required for reliable EIS access (zakupki.gov.ru may block foreign IPs)
- Individual token has limited access (only individual person mode is documented)

Evidence: `src/tender_research/eis_real_loader.py`, `.env.example:42-69`, `src/shared/config/settings.py:96-99`, `src/shared/network/etp_trust.py`

## 15. LLM and embedding dependencies

### Observed provider implementations
- **Local LLM** (llama.cpp/Ollama): configurable via `AI_CORP_LOCAL_LLM_BASE_URL`, default `http://127.0.0.1:8088/v1`
- **OpenAI-compatible**: `AI_CORP_OPENAI_BASE_URL` (default `https://api.openai.com/v1`)
- **Cloud.ru**: `AI_CORP_CLOUDRU_BASE_URL` (default `https://foundation-models.api.cloud.ru/v1`)
- **Yandex**: `AI_CORP_YANDEX_BASE_URL` (default `https://ai.api.cloud.yandex.net/v1`)
- **GigaChat**: `AI_CORP_GIGACHAT_BASE_URL` (default `https://gigachat.devices.sberbank.ru/api/v1`)
- **Stub**: default provider, returns canned responses for development
- **Embedding**: default `hashing` provider (local, no external call), configurable to remote OpenAI-compatible endpoint
- **Sentence Transformers**: optional, fallback path

### Network dependencies
- Local LLM: localhost (no external network)
- Cloud providers: require internet egress
- Russian providers (Cloud.ru, Yandex, GigaChat): require access to Russian API endpoints
- Embedding server: default localhost:8090

### Data handling
- `AI_CORP_LLM_ALLOW_RAW_PARTNER_DATA=false` — prevents sending raw procurement data to external LLMs
- `AI_CORP_LLM_STORE_RAW_RESPONSE=false` — prevents storing raw LLM responses
- Stub mode: no external data exposure

Evidence: `src/shared/config/settings.py:23-42,70-82`, `src/tender_research/rag/embeddings.py`, `src/tender_research/rag/llm.py`, `.env.example:16-41`

## 16. Environment variables and secrets

### Key variable groups
| Group | Variables | Required | Default | Security concern |
|-------|-----------|----------|---------|-----------------|
| Database | `AI_CORP_DATABASE_URL`, `ARVECTUM_POSTGRES_*` | Yes | SQLite / placeholder | Default PostgreSQL password `CHANGE_ME` |
| Auth | `AI_CORP_*_AUTH_*` | Conditional | Placeholders | Default credentials in `.env.example` |
| LLM | `AI_CORP_LLM_*`, `AI_CORP_OPENAI_*`, `AI_CORP_CLOUDRU_*`, `AI_CORP_YANDEX_*`, `AI_CORP_GIGACHAT_*` | Conditional | Stub / empty | API keys in env files |
| SOAP | `ZAKUPKI_GOV_RU_SOAP_*` | Conditional | Disabled | Token in env file |
| Storage | `ARVECTUM_STORAGE_*` (canonical), `AI_CORP_ARVECTUM_STORAGE_*` (compat) | Yes | 70/80/90 | Storage root path |
| Network | `AI_CORP_ALLOWED_HOSTS`, `AI_CORP_CORS_ALLOW_ORIGINS` | No | Empty | Empty = middleware not installed; no Host filtering; no CORS headers |
| Hermes | `AI_CORP_HERMES_ENABLED` | No | False | Internal sidecar |

### Risks
- `CHANGE_ME` defaults in `.env.example` for PostgreSQL password could be deployed as-is
- No `env_file` secret management documented for production
- `AI_CORP_DEBUG=false` by default — safe
- API keys for cloud providers stored in environment files, not in a secrets manager
- `.env.local` files excluded from git (not in repo), but no encryption mandated

Evidence: `.env.example`, `.env.macmini.example`, `.env.runtime.example`, `src/shared/config/settings.py`

## 17. Security requirements

| # | Requirement | Classification | Rationale |
|---|-------------|---------------|-----------|
| 1 | TLS termination for all public HTTP traffic | required_before_production | No encryption currently; basic auth credentials transmitted in plaintext |
| 2 | PostgreSQL not publicly accessible | required_before_production | Currently published on 5432:5432 with default credentials |
| 3 | Protected operator access | required_for_pilot | Pilot auth is configurable but basic auth over HTTP is weak |
| 4 | Secrets outside Git | required_for_pilot | `.env.local` convention exists but no enforcement |
| 5 | Production credentials different from defaults | required_before_production | `CHANGE_ME` values must never reach production |
| 6 | Secret rotation process | recommended_later | No rotation mechanism documented |
| 7 | Off-device backup with verified restore | required_before_production | No backup infrastructure exists |
| 8 | Audit logs | required_before_production | No audit trail for operations |
| 9 | Rate limiting | required_before_production | API is unprotected against abuse |
| 10 | Allowed hosts validation | required_for_pilot | TrustedHostMiddleware not installed when ALLOWED_HOSTS is empty |
| 11 | CORS restriction | required_before_production | CORSMiddleware not installed when CORS_ALLOW_ORIGINS is empty |
| 12 | Safe proxy headers handling | required_for_pilot | `--proxy-headers` is set but no reverse proxy validates forwarded headers |
| 13 | Minimized public port exposure | required_before_production | Only port 8000 (with TLS) should be public |
| 14 | Controlled egress to EIS and LLM providers | required_before_production | No controlled egress point for Russian endpoints |
| 15 | Raw procurement/customer document protection | required_for_pilot | Encryption-at-rest configuration is not evidenced in the repository; status: unknown/gap |
| 16 | Customer data isolation (tenant) | required_for_pilot | Database has tenant isolation via registry_number/customer_inn but no hard tenant boundary |
| 17 | Data localization where applicable law requires it | requires_legal_validation | Personal/customer data classes require legal classification |
| 18 | Documented retention and deletion policy | required_before_production | ARV-009 defines data/raw retention; backup retention not documented |

### Data localization note
The following data classes require separate legal classification before an infrastructure decision can be made:
- **Procurement data sourced from EIS/SOAP** — legal status of intermediate cached data
- **Customer-supplied documents** — may contain personal or commercially sensitive data
- **LLM prompts/responses** — legal status depends on whether they contain personal or commercially sensitive data

Infrastructure requirement: "Hosting in the Russian Federation where applicable law and contractual obligations require it." This is not a legal conclusion; legal validation is required for each data class.

## 18. Future infrastructure node requirements

### 18.1 Application node

- **Purpose**: Run the FastAPI application, serve API requests, handle background job submission
- **Pilot requirement** (proposed_initial_envelope, requires validation): Single node, 2–4 vCPU, 8–16 GB RAM, 50 GB system disk
- **Production requirement** (proposed_initial_envelope, requires validation): 2+ nodes behind load balancer, 4–8 vCPU, 16–32 GB RAM each
- **Functional requirements**:
  - Python 3.11+ runtime
  - Uvicorn with multiple workers
  - Proxy headers trust (already configured)
  - HEALTHCHECK for orchestrator
  - Graceful shutdown handling
- **Persistence**: None (stateless)
- **Network access**: Inbound on port 8000 (via reverse proxy), outbound to PostgreSQL, LLM providers, EIS, Hermes
- **Security boundary**: Internal network, no direct public access (reverse proxy only)
- **Backup**: Not required (stateless node, can be rebuilt from image)
- **Observability**: `/health`, `/health/ready` endpoints; application-level logging to stdout
- **Scaling direction**: Horizontal (add nodes behind load balancer)
- **Dependencies**: PostgreSQL, LLM provider (local or cloud), document storage (filesystem or object storage)
- **Unresolved**: CPU/RAM requirements not measured — proposed envelope based on typical Python web app

### 18.2 PostgreSQL/pgvector node

- **Purpose**: Primary database with pgvector extension for vector similarity search
- **Pilot requirement** (proposed_initial_envelope, requires validation): 2 vCPU, 4 GB RAM, 50 GB SSD, pgvector/pgvector:pg16+
- **Production requirement** (proposed_initial_envelope, requires validation): 4+ vCPU, 8–16 GB RAM, 100–200 GB SSD, dedicated node
- **Functional requirements**:
  - pgvector extension enabled
  - Database connection pooler
  - Automated daily `pg_dump` to backup storage
  - Point-in-time recovery not required initially
  - Replication not required for pilot
- **Persistence**: Full (all application state)
- **Network access**: Application node(s) only (not publicly accessible)
- **Security boundary**: Bind to `127.0.0.1` or internal network, strong password, TLS for connections
- **Backup**: Daily `pg_dump` to backup storage; verified restore every 30 days
- **Observability**: Disk usage, connection count, query performance
- **Scaling direction**: Vertical (more RAM) for pilot; read replicas for production
- **Dependencies**: Persistent block storage with snapshots
- **Unresolved**: Actual database size not measured; vector index size depends on document volume

### 18.3 Document storage

- **Purpose**: Store raw tender documents, extracted text, chunks, reports, exports
- **Pilot requirement**: External SSD (4 TB, accepted per ARV-009) or equivalent network storage
- **Production requirement**: Object-compatible document storage or dedicated NAS with >4 TB capacity
- **Functional requirements**:
  - POSIX-compatible filesystem or object storage API
  - Storage capacity guardrails (ARV-010) deployed on application side
  - Mount must be on a separate filesystem from system disk (enforced by `_mount_verified()`)
  - Warning at 70%, critical at 80%, ingestion protection at 90%
- **Persistence**: Full (customer documents and reports)
- **Network access**: Application node(s) only; for object storage, HTTPS with restricted IAM
- **Security boundary**: Encryption at rest is a proposed requirement for customer-sensitive documents
- **Backup**: Critical documents should be backed up; raw packages of completed procurements can be deleted per ARV-009
- **Observability**: Disk usage metrics, ingestion gate status
- **Scaling direction**: Vertical (larger disk) or migration to object storage
- **Dependencies**: PostgreSQL (for metadata <-> file consistency)
- **Unresolved**: Actual growth rate not measured; backup strategy not defined

### 18.4 Backup storage

- **Purpose**: Store database dumps and critical document backups
- **Pilot requirement**: External HDD or cloud storage, minimum 2x database + critical documents size
- **Production requirement**: Off-device/off-site storage with versioning, minimum 30-day retention
- **Functional requirements**:
  - Automated backup schedule (daily database, weekly documents)
  - Retention policy (30 days for pilot, 90+ days for production)
  - Verified restore (automated check every 30 days)
  - Off-device copy (different physical location or cloud region)
- **Persistence**: Medium-term (retention-based)
- **Network access**: Outbound from application/database node; inbound for restore operations
- **Security boundary**: Encrypted at rest and in transit
- **Backup**: Self-referential — backup storage is itself the backup destination
- **Observability**: Backup success/failure notifications, backup age monitoring
- **Scaling direction**: Increases with data volume
- **Dependencies**: Database node, document storage
- **Unresolved**: No backup infrastructure currently exists; tooling needs to be selected

### 18.5 Reverse proxy / public ingress

- **Purpose**: TLS termination, request routing, rate limiting, WAF, request filtering
- **Pilot requirement**: Reverse proxy / TLS termination service on the same or a small VPS; automated certificate management
- **Production requirement**: Managed or self-hosted load balancer / reverse proxy with HA
- **Functional requirements**:
  - TLS 1.2+ termination
  - Rate limiting per client IP (100 req/min suggested initial)
  - Request size limits (10 MB default)
  - Forwarded headers validation (remove external Forwarded/X-Forwarded-*)
  - Health check routing (liveness vs. readiness distinction)
- **Persistence**: None (stateless)
- **Network access**: Public inbound on port 443; forward to application node on port 8000
- **Security boundary**: Public-facing; must be hardened
- **Backup**: Configuration backup (git-ops)
- **Observability**: Access logs, error logs, metrics for 5xx rate
- **Scaling direction**: Can be combined with application node for pilot; separate for production
- **Dependencies**: Application node
- **Unresolved**: Shared vs. dedicated IP; DDoS protection needs

### 18.6 Background processing

- **Purpose**: Execute RAG prepare/analyze jobs outside the API process
- **Pilot requirement**: Same as application node (in-process executor is acceptable for pilot)
- **Production requirement**: Separate worker process/container with its own scaling
- **Functional requirements**:
  - Persistent job queue (database-backed, survives restart)
  - Configurable concurrency (4–8 workers)
  - Job retry with backoff (3 attempts, exponential backoff)
  - Job timeout handling (120s default)
  - Queue depth monitoring and alerting
  - Graceful shutdown (finish running jobs, drain queue)
- **Persistence**: Job state in PostgreSQL (already implemented via `TenderAnalysisJob`)
- **Network access**: Same as application node
- **Security boundary**: Internal network, same as application
- **Backup**: Job state is in database, covered by database backup
- **Observability**: Queue depth, job age, success/failure rate
- **Scaling direction**: Separate worker pool; can scale independently from API
- **Dependencies**: PostgreSQL, document storage, LLM provider
- **Unresolved**: Queue persistence and retry need implementation; current model is GAP

### 18.7 Optional local LLM

- **Purpose**: Run local LLM inference for analysis without sending data to external providers
- **Pilot requirement**: Not required (stub or cloud providers suffice for demo)
- **Production requirement**: GPU-accelerated node or CPU-based inference (slower)
- **Functional requirements**:
  - OpenAI-compatible API endpoint (already supported)
  - Configurable model
  - Configurable timeout (default 120s)
  - GPU recommended for acceptable latency
- **Persistence**: Model files (downloadable); no customer data persisted
- **Network access**: Localhost or internal network; no external access needed
- **Security boundary**: Isolated from public network
- **Backup**: Not required (model files are re-downloadable)
- **Observability**: Inference latency, token throughput
- **Scaling direction**: Vertical (better GPU); horizontal (multiple model instances) for production
- **Dependencies**: Application node (as HTTP client)
- **Unresolved**: Whether local LLM is needed at all vs. Russian cloud providers; GPU vs. CPU adequacy

## 19. Migration triggers

| # | Trigger | Metric | Threshold | Source | Severity | Status |
|---|---------|--------|-----------|--------|----------|--------|
| 1 | More than 10 active clients | Client count | >10 active client orgs | Operational | critical | proposed |
| 2 | Contractual SLA | SLA existence | Any committed SLA | Contract | critical | proposed |
| 3 | Sustained CPU pressure | CPU utilization | >70% over 15 min window | `top`/`htop` | warning | proposed |
| 4 | Sustained RAM pressure | RAM utilization | >80% over 15 min window | `free`/`vm_stat` | warning | proposed |
| 5 | Background queue growth | Job queue depth | >50 queued jobs sustained | API monitoring | warning | proposed |
| 6 | Excessive queue age | Oldest queued job age | >30 min | API monitoring | critical | proposed |
| 7 | Storage warning threshold | Disk usage | ≥70% | ARV-010 gate | warning | configured |
| 8 | Storage critical threshold | Disk usage | ≥80% | ARV-010 gate | critical | configured |
| 9 | Processing reserve exhaustion | Available workers | 0 free workers for >5 min | Application metric | critical | proposed |
| 10 | 24/7 operation requirement | Operational window | Required by client/contract | Requirement | critical | proposed |
| 11 | Backup exceeds RPO | Backup age | >24h since last successful backup | Operational check | warning | proposed |
| 12 | Restore exceeds RTO | Restore duration | >4h for full restore | DR test | critical | proposed |
| 13 | Single hardware failure unacceptable | HA requirement | Explicit requirement | Client/audit | critical | proposed |
| 14 | Client requires data center hosting | Requirement | Explicit request | Contract | critical | proposed |
| 15 | Client requires on-premise deployment | Requirement | Explicit request | Contract | critical | proposed |
| 16 | Internet/power inadequacy | Availability | Sustained downtime | Monitoring | critical | proposed |

**Note**: Only triggers 7, 8 have configured thresholds (ARV-010 storage guardrails). All others are proposed thresholds requiring validation through actual measurement.

## 20. Architecture option A — Mac Mini + small public ingress VPS

### Topology
```
Internet → VPS (nginx, TLS, rate limit) → Mac Mini (FastAPI, PostgreSQL, storage)
```

### Stateful components
- PostgreSQL data (Mac Mini)
- Document storage on external SSD (Mac Mini)
- Database backup (VPS storage or external)

### Network flow
- Public traffic → VPS port 443 (TLS) → port forwarding → Mac Mini port 8000
- PostgreSQL and storage remain on Mac Mini, no direct public access

### Advantages
- Preserves existing investment in Mac Mini and 4 TB SSD
- Minimal operational change — add VPS as TLS termination and public ingress
- Low additional cost (small VPS, 1–2 vCPU, 2–4 GB RAM)
- Quick to implement (days, not weeks)

### Limitations
- Single hardware failure still applies (Mac Mini is a single point)
- Bandwidth limited by home internet connection
- No data redundancy for storage
- Background jobs still run in-process on Mac Mini
- No failover capability

### Security considerations
- VPS must be hardened (SSH key only, firewall, automated brute-force protection)
- TLS termination on VPS exposes only HTTPS to the internet
- VPS-to-Mac Mini connection must be secured (authenticated encrypted private tunnel)
- PostgreSQL port must be firewalled from the internet

### Operational complexity
- Low — single VPS, single Mac Mini, SSH tunnel or VPN
- Backup requires additional scripting (scp/rsync from Mac Mini to VPS or cloud storage)

### Single points of failure
- Mac Mini hardware failure
- Home internet outage
- Power outage at Mac Mini location
- VPS provider availability

### When this option fits
- Pilot phase (5–10 clients)
- No contractual SLA
- Development/demo stage

### When it stops fitting
- Any contractual SLA
- Client requires 24/7 availability
- Backup RPO/RTO requirements formalized
- More than 10 active clients

### Migration path
- Option A → Option B: decommission Mac Mini, move all services to a single VPS/dedicated server
- Option A → Option C: incremental migration of stateful components

### Unresolved questions
- Is the Mac Mini's home internet connection reliable enough for daily business?
- Who is responsible for Mac Mini physical security and uptime?
- What is the actual upstream bandwidth and latency?

## 21. Architecture option B — Single VPS or dedicated server

### Topology
```
Internet → Single server (reverse proxy + FastAPI + PostgreSQL + storage)
```

### Stateful components
- PostgreSQL data (server system disk or attached volume)
- Document storage (attached block storage, 4 TB+)
- All on one server

### Network flow
- Public traffic → server port 443 (TLS) → reverse proxy → local port 8000
- PostgreSQL listens on 127.0.0.1 only
- All services on same machine

### Advantages
- Simple architecture — one machine to manage
- No inter-service network latency
- Full control over hardware (dedicated) or configurable (VPS)
- Single security perimeter

### Limitations
- No HA — single machine failure takes everything down
- Resource contention between application and database
- Scaling requires vertical upgrade (more CPU/RAM/disk) or migration to Option C
- Larger VPS/dedicated server cost

### Security considerations
- Firewall: only ports 443 (HTTPS) and 22 (SSH) open
- PostgreSQL on Unix socket or 127.0.0.1
- TLS with automated certificate management
- Automated brute-force protection on SSH
- Regular OS patching

### Operational complexity
- Medium — single server OS management, Docker or bare deployment
- Backup: `pg_dump` cron + rsync to backup storage

### Single points of failure
- Single server hardware failure
- Single VPS provider availability

### When this option fits
- After pilot scale-out is needed but before HA is required
- Single server cost is acceptable
- 10–20 active clients

### When it stops fitting
- HA requirement
- Database needs separate scaling
- Regulatory requirement for data isolation

### Migration path
- From Option A: migrate data from Mac Mini, shut down
- To Option C: split services across multiple nodes

### Unresolved questions
- Acceptable downtime for maintenance (OS updates, PostgreSQL restart)?
- Can database and application co-exist on the same node under load?
- What is the actual CPU/RAM requirement (not yet measured)?

## 22. Architecture option C — Split contour

### Topology
```
Internet → Reverse proxy node → Application node(s) → PostgreSQL node
                                          ↓
                                    Document storage (NFS/S3)
                                          ↓
                                    Backup storage (off-device)
```

### Stateful components
- PostgreSQL node: database data (persistent block storage)
- Document storage: object storage or dedicated NAS (4 TB+)
- Backup storage: separate device or cloud storage
- Application node(s): stateless

### Network flow
- Public traffic → reverse proxy (TLS) → app node(s) (internal network) → PostgreSQL (internal)
- App node(s) → document storage (internal network or S3 API)
- App node(s) → EIS, LLM providers (outbound internet)
- Backup: PostgreSQL node → backup storage; document storage → backup storage

### Advantages
- Each component can scale independently
- No single point of failure (with redundancy per layer)
- Can meet regulatory requirements for data isolation
- Production-grade architecture

### Limitations
- Highest operational complexity
- Highest cost (multiple nodes/services)
- Requires container orchestration platform or manual coordination
- Over-engineered for pilot phase

### Security considerations
- Defense in depth: firewall per node, network segmentation
- TLS everywhere, including inter-node communication
- IAM for object storage access
- VPN for inter-node communication if across providers

### Operational complexity
- High — multiple services, network configuration, monitoring, backup coordination

### Single points of failure
- Mitigated with redundancy (multi-az, multi-node)

### When this option fits
- Production with contractual SLA
- Regulatory compliance (152-FZ, 44-FZ data localization)
- Large scale (20+ active clients)

### When it stops fitting
- Cost-sensitive early stage
- Small operational team

### Migration path
- From Option A or B: incremental migration per component
- Start with PostgreSQL separation, then document storage, then app scaling

### Unresolved questions
- What container orchestration platform to use?
- What object-compatible document storage backend to use?
- Is multi-region required for compliance?

## 23. Recommended sequencing

### Phase 0 (current pilot) — Option A
- Keep Mac Mini as application + database + storage server
- Add a small VPS for TLS termination and public ingress
- Implement basic backup (cron + scp to VPS or cloud storage)
- Measure actual CPU, RAM, disk, and network utilization

### Phase 1 (initial production) — Option B
- Single VPS or dedicated server with 8+ vCPU, 16+ GB RAM, 4 TB+ storage
- Docker-based deployment with docker-compose or single-server orchestration
- Automated daily backup with off-device copy
- Rate limiting and TLS on the reverse proxy

### Phase 2 (scaled production) — Option C
- Split application, database, and storage
- Object storage for documents
- Database replication for HA
- Background worker pool separate from API process
- Monitoring and alerting

### Provider selection
- **Not started** in this task. Must follow after:
  1. Actual resource utilization is measured during the pilot
  2. Compliance requirements are clarified (data localization, SLA)
  3. Budget constraints are known

## 24. Unknowns and validation backlog

| # | Unknown | Why it matters | How to resolve |
|---|---------|---------------|----------------|
| 1 | Actual CPU usage under pilot load | Capacity planning | Run `top`/`htop` during pilot operations |
| 2 | Actual RAM usage under pilot load | Capacity planning | Monitor RSS of all services during pilot |
| 3 | Actual disk growth rate | Storage sizing timeline | Track `data/` directory size weekly |
| 4 | Database size with 10 clients | PostgreSQL sizing | Measure `pg_database_size()` with production data |
| 5 | Vector index size | pgvector resource planning | Measure index size with actual embeddings |
| 6 | Background job average duration | Worker pool sizing | Log completion_time - start_time for each job |
| 7 | Background job peak concurrency | Worker pool sizing | Track concurrent `_FUTURES` during peak hours |
| 8 | Network throughput to EIS | Bandwidth planning | Measure actual download time for EIS documents |
| 9 | Network throughput to LLM providers | Bandwidth planning | Measure prompt/response latency |
| 10 | Backup size (database + documents) | Backup storage and transfer time | Run trial `pg_dump` and measure |
| 11 | Restore duration | RTO validation | Time a trial restore |
| 12 | Mac Mini upstream bandwidth | Public ingress adequacy | Run speed test during business hours |
| 13 | EIS geo-accessibility from foreign IPs | Hosting location requirement | Test zakupki.gov.ru reachability from non-Russian IP |
| 14 | Hermes resource usage | Combined node sizing | Monitor Hermes if enabled |
| 15 | Storage guardrail frequency | Operational stability | Track how often storage gate blocks ingestion |

## 25. Explicit non-decisions

The following decisions are explicitly **not made** in ARV-011A:

- Provider not selected (any VPS, cloud, or dedicated server vendor)
- Pricing not researched (no tariff comparison)
- Server not purchased or provisioned
- Deployment not performed (no runtime changes)
- Runtime topology not changed (Mac Mini contour remains as-is)
- Redis not introduced (no message queue selected)
- n8n not introduced (no workflow automation platform)
- Sentry not introduced (no error tracking)
- Prometheus/Grafana not introduced (no monitoring stack)
- Reverse proxy not deployed (nginx config exists for site-pilot demo only)
- No new storage measurement (existing ARV-009/ARV-010 numbers accepted)
- No load test performed
- No EIS/SOAP capacity sweep
- No local LLM model selected
- No migration created or modified
- No test written or modified
- No dependency added
- No source code changed

## 26. Markers

- ARV-011_RUNTIME_TOPOLOGY_AUDITED
- ARV-011_INFRASTRUCTURE_REQUIREMENTS_READY
- ARV-011_PROVIDER_SELECTION_NOT_STARTED
