# Repository Maintenance Audit — 2026-07-30

## Scope

Repository: `arutyunoveth/ai-corporation`  
Audited branch: `main` at `3dcb9e9c0e7d937aaf347f9de488d18968aa6570`  
Goal: distinguish genuinely incomplete work from stale tracking, remove low-risk structural debt, and define the local-only verification sequence.

## Current verified state

- The latest merged code PR, #80, completed the GitHub Actions `CI` workflow successfully.
- The repository CI runs the full default pytest suite through `make test`, plus migration, secret-scan, Redis, PostgreSQL and R8 acceptance jobs.
- No currently failing GitHub Actions run or reproducible red test was found during this audit.
- The recent ARV-003 sequence closed multiple concrete defects around compact-wire grounding, llama.cpp schema constraints, disabled reasoning, exact request measurement and sanitized diagnostics.
- Real-provider and Mac mini checks remain local-only because they require private policy files, exact tokenizer configuration, local model infrastructure or customer/operator access.

This means the remaining “error tail” is primarily verification and maintainability debt, not a confirmed broken default test suite.

## Incomplete work that remains active

### External or local execution required

1. **ARV-072 / #41 — competitive benchmark**
   - The repository contract and comparison methodology exist.
   - Completion still requires live access to competitor products and controlled runs on identical real procurements.

2. **ARV-073 / #71 — ODS, Mac mini and Hermes infrastructure**
   - This is an infrastructure migration, inventory and rollback task.
   - It cannot be completed safely from GitHub alone because it changes the local Mac mini runtime and may remove duplicated local services.

3. **ARV-074 / #73 — Obsidian Mind pilot**
   - The pilot must use a separate local/private vault and MCP configuration.
   - It must finish with an explicit `ADOPT / ADOPT WITH LIMITS / REJECT` decision and must not enter product runtime implicitly.

4. **First real restricted-pilot evidence**
   - The repository package and controlled runners are present.
   - Completion requires a real operator/customer run, feedback, measured outcome and a sanitized evidence record.

### Product backlog items still open

- remaining absolute local links in historical documentation;
- richer partner-facing exports where demanded by a customer scenario;
- reusable versioned supplier-request templates;
- richer quote attachment parsing and validation;
- safe status-engine synchronization for commercial workspace actions;
- provider-abstraction and schema-family hardening without opening broad autonomy.

## Stale tracking found

- **#13 — Bootstrap canonical issue tracker and status sync** is functionally complete: issue-backed work, labels, backlog references and repository execution summaries now exist.
- **#14 — Prepare controlled paid pilot under restrictions** is functionally complete at repository-package level: the runbook, data policy, templates, checklist, folder runner and tender-operator refinement are already recorded as complete.
- **PR #72** is a documentation-only ARV-073 backlog update based on an old `main`. Its intended line is included in the maintenance branch, so the old PR should be closed as superseded rather than force-merged.

## Maintenance changes in this branch

1. Extracted the large router import/registration block from `src/main.py` into `src/shared/api/router_registry.py`.
   - Router order is preserved because order can affect conflict resolution.
   - `src/main.py` is reduced to application composition, middleware, health endpoints and site mounting.

2. Broadened the existing `make check` contour.
   - `compileall` now covers `src` and `scripts`.
   - Ruff now checks the application composition files in addition to the previously selected safety-sensitive paths.

3. Synchronized `docs/product/Product_Backlog.md` with active issues #41, #71 and #73.

## Technical debt intentionally not changed in this maintenance PR

These areas are real candidates but are too risky for a blind tree-wide rewrite without a local test loop:

1. **Private-method patch chain in production LLM analysis**
   - `llama_schema_constraint.py` and reasoning control wrap or patch private provider methods.
   - The probe and tests also call the provider request-body builder directly.
   - Replace this only through a dedicated adapter/profile interface with parity tests for request body, schema, response rewriting and diagnostic codes.

2. **Compressed parsing code in `openai_compatible.py`**
   - The compact response branch contains dense one-line statements and should be formatted and decomposed into named helpers.
   - Behavior is security-sensitive, so refactoring requires focused tests plus the complete default suite.

3. **README mixes current status with a long historical ledger**
   - Move the historical phase-by-phase narrative and ID inventory to versioned governance/reference documents.
   - Keep the root README focused on current product status, setup, architecture, safety boundaries and primary commands.

4. **No committed `uv.lock` on `main`**
   - Generate and validate the lockfile locally with the supported `uv` version before committing it.
   - Do not commit a lockfile generated in an unknown environment without `uv sync --frozen` and CI verification.

5. **Historical absolute local paths and old synchronization statements**
   - Rewrite only navigation links and current-state assertions; preserve historical decisions as historical records.

## Local execution plan

### Phase 1 — verify the maintenance PR

```bash
cd /Users/master/Documents/AI-Corporation-live
git fetch origin
git switch chore/repository-maintenance-2026-07-30
git pull --ff-only

python3.11 -m venv .venv-maintenance
source .venv-maintenance/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'

make check
python -m pytest -q tests/production_llm_analysis/test_llama_batch_probe_token_shaping.py
make test
alembic heads
python scripts/ops/secret_scan.py
```

Notes:

- Do not hide failures from `make check`, the focused llama test, `make test`, `alembic heads` or the secret scan.
- Confirm that application import succeeds and that the route count and route paths remain stable with an existing app smoke test or a small local comparison against `origin/main`.

### Phase 2 — finish the ARV-003 local verification tail

Use only the existing private provider settings, approved provider policy and exact persistent tokenizer environment.

```bash
source <PRIVATE_PROVIDER_ENV>
source .venv-maintenance/bin/activate
python -m scripts.r10_1.probe_llama_batch_shape \
  --approved-policy '<PRIVATE_POLICY_PATH>'
```

Acceptance:

- exit code `0`;
- exactly one provider call;
- zero retries;
- `reasoning_enabled=false`;
- non-negative context headroom;
- one to three server-grounded claims;
- no provider body, prompt, customer data or credential printed.

When the synthetic probe passes, run the existing controlled customer-data Gate 5 command exactly once under the repository runbook. Do not retry automatically and do not loosen schema, grounding, budget or human-review gates to make the run pass.

### Phase 3 — controlled pilot evidence

1. Select one approved real tender folder and one named operator.
2. Copy the folder into the approved local pilot input location; do not commit it.
3. Run the documented partner tender folder/tender-operator command.
4. Review redaction output before export.
5. Record operator time, corrections, missing requirements, false positives, report usefulness and final bid-decision impact.
6. Store only a sanitized summary in the repository; keep source documents, logs and partner exports outside Git.
7. Update the pilot evidence backlog item only after the sanitized evidence record is reviewed.

### Phase 4 — refactor the LLM provider boundary

Give OpenCode the following bounded task after Phase 1 and Phase 2 are green:

1. Introduce a public request-profile abstraction for canonical OpenAI-compatible, llama schema-constrained and llama non-reasoning request construction.
2. Preserve byte-for-byte canonical request bodies for existing profiles, except for explicitly versioned changes.
3. Move compact response validation and evidence-reference expansion into named pure helpers.
4. Remove direct calls to `_build_request_body` from probes/tests and remove global monkeypatch leakage.
5. Add parity tests for all safe invalid-response codes, server-owned grounding, schema flattening, disabled reasoning and exact token measurement.
6. Run focused tests, `make check`, full pytest, Alembic heads and secret scan.
7. Deliver as a separate PR; do not combine with product features or provider policy changes.

### Phase 5 — tree and documentation cleanup

1. Generate an inventory with `git ls-files`, file sizes and last-change dates.
2. Classify large documentation as `current`, `historical`, `generated`, or `local-only`.
3. Move historical README material into `docs/99_governance/history/` without deleting decision records.
4. Replace absolute links only where they are intended as active navigation.
5. Generate `uv.lock`, run `uv sync --frozen --extra dev`, then compare `make check` and `make test` results with the pip-based CI environment.
6. Remove stale merged branches only after confirming they contain no unique commits.
7. Submit documentation/tree cleanup separately from runtime refactoring.

### Phase 6 — local infrastructure tasks

Execute ARV-073 and ARV-074 independently. Each must begin with an inventory and backup, define rollback before deletion or migration, and produce a written acceptance decision. Do not combine ODS migration, Hermes changes, Obsidian Mind evaluation and product runtime changes into one deployment.
