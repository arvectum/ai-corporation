# Arvectum agent instructions

## Mission

Build and maintain the Arvectum procurement automation platform with small, reviewable, evidence-backed changes. Preserve human control, auditability, deterministic behavior where possible, and the current restricted-pilot boundaries.

## Architecture boundaries

- Keep procurement analysis, Hermes logic, parsers, calculations, reports, and state transitions in FastAPI/Python.
- Treat self-hosted n8n as an external orchestrator for schedules, webhooks, retries, waits, and notifications only.
- n8n and other integrations must use documented internal APIs; they must not write directly to product database tables.
- PostgreSQL is the production datastore. Preserve SQLAlchemy 2 and Alembic conventions.
- LLM-backed behavior must stay bounded, schema-validated, traceable, and human-reviewed.
- Do not add autonomous procurement submission, EDS/signature actions, supplier communication, or other external execution unless the task explicitly authorizes a separately reviewed control boundary.

## Default workflow

1. Restate the requested outcome and identify the smallest affected subsystem.
2. Load only the relevant skill from `.agents/skills`; do not load every skill.
3. Inspect the narrowest useful set of files, symbols, tests, and recent related changes.
4. Write a short implementation plan before editing when more than one file or subsystem is involved.
5. Make the smallest coherent diff. Do not refactor unrelated code.
6. Run targeted lint/tests first, then broaden verification only when the change justifies it.
7. Report changed files, decisions, assumptions, commands run, results, and remaining risks.

## Skill routing

Use the most specific matching skill. Common routes:

- bug or failing test: `bug-investigation`;
- normal implementation: `focused-change`;
- FastAPI route/schema/service: `fastapi-feature`;
- migration/model persistence: `alembic-migration`;
- tender extraction/normalization: `tender-parser-change`;
- ЕИС/ЭТП/SOAP/REST adapter: `external-integration`;
- Hermes/prompt/schema/model behavior: `llm-schema-eval`;
- regression coverage: `regression-test-design`;
- browser journey: `browser-smoke`;
- security analysis: `security-review` or `tender-safety-review`;
- diff/PR assessment: `pr-review`;
- release decision: `release-readiness`.

The full catalog and local setup guidance are in `docs/development/AI_AGENT_TOOLING.md`.

## Verification commands

Prefer the existing environment and lockfile. Typical commands:

```bash
uv sync --extra dev
uv run ruff check <changed-paths>
uv run pytest -q <targeted-test-files-or-nodeids>
uv run pytest -q
```

Use the full test suite for cross-cutting changes, shared models, migrations, routing, security boundaries, or before release. Do not claim a command passed unless it was actually run.

## Data and security rules

- Never commit `.env`, tokens, certificates, private keys, cookies, real tender archives, real partner data, generated pilot exports, or operator logs.
- Use synthetic or explicitly sanitized fixtures in tests and documentation.
- Preserve current `.gitignore` protections.
- Treat authentication, authorization, secrets, file extraction, archive handling, HTML rendering, and external network calls as security-sensitive.
- Require human review for findings that touch authentication, secrets, personal data, EDS, external side effects, or destructive migrations.

## Context and token budget

- Do not read the entire README or large documentation trees by default.
- Start with file names, symbols, imports, targeted grep, and related tests; open only the relevant ranges.
- Prefer concise summaries over pasting large files into the conversation.
- Use current external documentation through a documentation tool/MCP instead of copying whole manuals into prompts.
- Put recurring procedures in skills, subagents, commands, or scripts rather than repeating long prompts.
- Keep unrelated MCP servers disabled.
- Start a fresh task/session when the goal changes materially instead of carrying unrelated history.

## Source-of-truth loading

Load these only when relevant:

- Product scope and non-goals: `docs/product/MVP_v1_Scope.md`, `docs/product/MVP_v1_Non_Goals.md`
- Human-control policy: `docs/product/Human_Control_Policy_v2.md`
- Tender operator workflow: `docs/product/Tender_Operator_Pilot_Runbook.md`, `docs/product/Tender_Operator_RFQ_Workflow.md`
- Launch restrictions and gates: `docs/10_launch/Launch_L1_Restrictions.md`, `docs/10_launch/Launch_L1_Control_Gates.md`
- Canonical module registry: `docs/99_governance/canonical_module_registry_locked.md`

## Code review rules

Review changes for:

- broken human-control or restricted-pilot boundaries;
- direct database writes from external orchestration;
- missing traceability, schema validation, or deterministic fallbacks;
- unsafe archive/file handling, injection, XSS, secret or personal-data exposure;
- migration compatibility and rollback risk;
- behavior changes without focused regression tests;
- duplicated business logic or accidental divergence from canonical modules.

Prioritize concrete defects and regressions over style preferences.