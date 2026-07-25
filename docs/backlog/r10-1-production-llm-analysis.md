# R10.1 / ARV-003 — Production LLM Analysis

Status: `R10_1_GATE_3_OPENAI_COMPATIBLE_TRANSPORT_COMPLETE_GATE_4_READY`.

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

- [ ] add a producer beside `produce_frozen_canonical_analysis()` rather than changing the frozen producer in place;
- [ ] pass only validated supported claims into the existing canonical output builder;
- [ ] preserve source graph construction and R8/R9 persistence formats;
- [ ] preserve immutable ownership, hashes, idempotency and final-PDF verification;
- [ ] make R9 versus R10.1 mode selection explicit and fail closed;
- [ ] reject canonical publication when grounding validation fails.

Gate 4 may use the Gate 3 transport only through mocked HTTP or a fake provider. A real provider, real credential and real procurement call remain prohibited until Gate 5.

### Gate 5 — controlled real-provider evidence

- [ ] run one approved real procurement through the configured provider;
- [ ] verify every accepted claim against owned evidence;
- [ ] record evidence coverage and rejected claims;
- [ ] record provider/model/prompt/schema/policy versions;
- [ ] record token usage, latency and cost;
- [ ] prove budget compliance;
- [ ] publish sanitized CI/runtime evidence without raw tender data or credentials;
- [ ] repeat the same input to verify stable identities and non-conflicting publication.

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

Gate 4 is the only authorized implementation slice. It must add a versioned R10.1 producer beside the frozen R9 producer, preserve every existing canonical persistence and artifact invariant, and remain fully offline through fake-provider or mocked-HTTP tests.