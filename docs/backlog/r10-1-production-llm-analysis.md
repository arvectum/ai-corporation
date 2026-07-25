# R10.1 / ARV-003 — Production LLM Analysis

Status: `R10_1_GATE_4_VERSIONED_CANONICAL_PRODUCER_COMPLETE_GATE_5_READY`.

Canonical base: annotated tag `r9-operational-hardening-2026-07-24`, peeled commit `58bef2da2342bff1e6f63215ee2697e96fefe6f7`.

Architecture contract: `docs/reviews/r10-1-production-llm-architecture-audit-20260725.md`.

## Definition of done

ARV-003 is complete when one configured real provider processes a controlled real procurement through a versioned R10.1 producer and produces a canonical-compatible result where:

- every accepted factual conclusion resolves to evidence owned by the procurement;
- confidence is assigned by a deterministic grounding policy rather than trusted from the provider;
- provider/model/prompt/schema/evidence/policy identities are recorded;
- provider failure, invalid output, unsupported claims and budget exhaustion fail closed;
- token, latency and cost measurements are captured against explicit budgets;
- no stub or synthetic positive output is used in production mode;
- R8/R9 snapshot, hash, ownership, idempotency and final-PDF contracts remain unchanged;
- sanitized executable evidence is published for the real run.

## Gates

### Gate 0 — R9 closure

- [x] PR #16 synchronized with `main`;
- [x] final PR CI green;
- [x] Draft removed;
- [x] merged to `main`;
- [x] post-merge CI green;
- [x] annotated release tag created and verified.

Status: `R9_OPERATIONAL_HARDENING_MERGED_TAGGED_AND_POST_MERGE_VERIFIED`.

### Gate 1 — architecture audit and contract

- [x] inventory current controlled-provider, Hermes and RAG paths;
- [x] identify the frozen R9 canonical integration boundary;
- [x] define provider-neutral request/result/evidence contracts;
- [x] define fail-closed rules;
- [x] define confidence ownership;
- [x] define token/latency/cost budget requirements;
- [x] define ordered implementation plan and acceptance matrix.

Status: `R10_1_GATE_1_ARCHITECTURE_AUDIT_COMPLETE`.

### Gate 2 — offline production contract

- [x] add `src/modules/production_llm_analysis/schemas.py`;
- [x] add deterministic evidence-packet builder with stable SHA-256 identity;
- [x] add claim-level grounding validator;
- [x] validate exact quote and locator against current-procurement evidence;
- [x] separate provider-reported and validator-derived confidence;
- [x] add budget preflight and usage/latency/cost result models;
- [x] add sanitized failure result contract;
- [x] add fake providers only for tests;
- [x] prove no network is used in this gate;
- [x] prove no source graph, canonical persistence, artifact, UI, deployment or 223-FZ change.

Executable evidence:

- PR: `#23`;
- verified code head: `73d5f1ce1af0bb3694e1f061ed8cc7b44318adac`;
- CI workflow: `30130910676`;
- Gate 2 focused tests added: `27`;
- full suite: `1707 passed, 188 skipped, 150 warnings` in `420.17s`;
- `make check`: PASS;
- migrations: PASS;
- security scan: PASS;
- R8 PostgreSQL integration: PASS;
- R8 acceptance integration: PASS;
- evidence artifact: `r9-operational-hardening-evidence-73d5f1ce1af0bb3694e1f061ed8cc7b44318adac`;
- artifact digest: `sha256:93ee679cc99cf8c7127cbcbc76fc0b7cc413931e0e73d69509f61268557351d6`.

Status: `R10_1_GATE_2_OFFLINE_CONTRACT_COMPLETE`.

### Gate 3 — one transport behind the contract

- [x] add one OpenAI-compatible JSON provider transport behind `ProductionLLMProvider`;
- [x] retain credential and raw-response safety defaults;
- [x] send only the allow-listed evidence packet and versioned request metadata;
- [x] capture provider request ID, token usage, retry count, attempt/total latency and response SHA-256;
- [x] enforce per-attempt timeout, bounded retries and analysis-wide latency/cost budgets;
- [x] retry only timeout, 429 and transient 5xx classes;
- [x] reject permanent 4xx responses without retry;
- [x] validate the provider envelope, JSON content and claim schema before grounding;
- [x] test through an injected mocked HTTP boundary only;
- [x] prove no live network, real credential or real procurement is used;
- [x] prohibit production fallback to stub.

Executable evidence:

- PR: `#24`;
- verified implementation head: `bb1bd7fef7d1ce191509da4fa5a04b03086482ca`;
- CI workflow: `30146130371`;
- Gate 3 focused test cases added: `24`;
- full suite: `1731 passed, 188 skipped, 150 warnings` in `401.00s`;
- `make check`: PASS;
- migrations: PASS;
- security scan: PASS;
- R8 PostgreSQL integration: PASS;
- R8 acceptance integration: PASS;
- evidence artifact: `r9-operational-hardening-evidence-bb1bd7fef7d1ce191509da4fa5a04b03086482ca`;
- artifact digest: `sha256:ebac6d46a3db72d5b3f79799f867acfbc69ec1ce14f72a3c1827e3ebef29c189`.

Status: `R10_1_GATE_3_OPENAI_COMPATIBLE_TRANSPORT_COMPLETE`.

### Gate 4 — versioned R10.1 canonical producer

- [x] add `produce_r10_1_canonical_analysis()` beside `produce_frozen_canonical_analysis()`;
- [x] keep the frozen producer unchanged and prove byte-equivalent explicit dispatch;
- [x] build deterministic evidence packets only from server-owned analyzed documents;
- [x] validate tenant/project/case/run/registry identities before provider invocation;
- [x] pass only validated supported claims through an explicit field-path allow-list;
- [x] reject unknown fields, malformed values, mixed supported/rejected results and provider-authored positive decisions;
- [x] preserve existing source graph construction, canonical serialization and verification;
- [x] record provider/model/prompt/schema/evidence/policy/budget provenance without raw response bodies;
- [x] make `frozen_r9` versus `production_llm_r10_1` mode selection explicit and prohibit fallback;
- [x] prove provider failure and grounding failure cannot publish canonical files;
- [x] remain fully offline through fake-provider tests.

Executable evidence:

- PR: `#25`;
- verified implementation head: `af6e02eb661292d9e4a5e6abd8efce6c913510f6`;
- CI workflow: `30147724651`;
- Gate 4 focused test cases added: `8`;
- full suite: `1739 passed, 188 skipped, 150 warnings` in `421.59s`;
- `make check`: PASS;
- migrations: PASS;
- security scan: PASS;
- R8 PostgreSQL integration: PASS;
- R8 acceptance integration: PASS;
- evidence artifact: `r9-operational-hardening-evidence-af6e02eb661292d9e4a5e6abd8efce6c913510f6`;
- artifact digest: `sha256:0a52d24fd2edab82e447ea1a68dfa549f3101b80ffb1d2ac5db551e44276c958`.

Status: `R10_1_GATE_4_VERSIONED_CANONICAL_PRODUCER_COMPLETE`.

### Gate 5 — controlled real-provider evidence

- [ ] select and approve one configured provider/model and versioned pricing policy;
- [ ] load credentials only from the existing secret/config boundary and prove they never enter requests, results, logs or artifacts;
- [ ] run one approved real procurement through `production_llm_r10_1`;
- [ ] verify every accepted claim against procurement-owned evidence and preserve rejected claims/limitations;
- [ ] record provider/model/request/prompt/schema/evidence/grounding/pricing identities;
- [ ] record token usage, retries, latency and cost against explicit budgets;
- [ ] prove provider, validation and budget failures remain fail closed without frozen or stub fallback;
- [ ] publish sanitized runtime evidence without raw tender text, raw provider bodies or credentials;
- [ ] repeat the same controlled input to verify stable local identities and non-conflicting publication.

Gate 5 is the first gate allowed to use one approved real credential, endpoint and procurement. It must be executed as a controlled evidence run, not as general customer rollout.

### Gate 6 — handoff to ARV-001

- [ ] provide R10.1 evidence to the golden report and release-gate work;
- [ ] do not begin ARV-004 self-improvement or ARV-005 pilot expansion before ARV-001 acceptance criteria are defined.

## Non-goals

- no source graph redesign;
- no R8/R9 artifact-contract redesign;
- no new UI;
- no deployment;
- no 223-FZ;
- no provider marketplace or multi-provider benchmark;
- no Hermes customer-scoped memory/self-improvement work;
- no autonomous external action;
- no broad refactor of unrelated historical LLM code.

## Immediate next slice

Gate 5 is the only authorized implementation slice. It must execute one approved real-provider procurement analysis through the versioned R10.1 producer, produce sanitized evidence for grounding and budgets, and preserve every fail-closed and immutable-publication invariant established in Gates 2–4.