# AI agent tooling for Arvectum

This document defines the recommended division of work between ChatGPT in the browser, Codex, and OpenCode, plus the repository-standard skills, subagents, commands, MCP policy, permissions, and token-efficiency rules.

## Recommended priority

### P0 — Shared repository instructions and coding skills

Status: implemented in this branch.

- `AGENTS.md` is the compact source of shared project instructions.
- Reusable task procedures live in `.agents/skills/<name>/SKILL.md`.
- Codex scans repository `.agents/skills` directories and loads a matching skill body only when selected.
- OpenCode also discovers `.agents/skills`, so the same skill library is shared rather than copied into two prompt systems.
- Stable architecture, safety, verification, and context-budget rules belong in Git; task-specific details stay in the issue or prompt.

#### Core navigation and implementation skills

- `focused-change` — smallest coherent implementation with targeted verification;
- `large-repo-navigation` — progressive repository exploration under a context budget;
- `bug-investigation` — reproduce, localize, fix, and add regression evidence;
- `refactor-safely` — behavior-preserving refactoring through characterization tests;
- `verify-change` — select the smallest sufficient lint/test/smoke contour.

#### Backend and persistence skills

- `fastapi-feature` — routes, schemas, services, compatibility, and API tests;
- `alembic-migration` — SQLAlchemy 2/Alembic schema and data migration safety;
- `dependency-upgrade` — controlled package/toolchain upgrades;
- `performance-debug` — measured profiling and optimization.

#### Procurement and AI-specific skills

- `tender-parser-change` — deterministic document extraction and normalization;
- `external-integration` — ЕИС, ЭТП, SOAP, REST, webhooks, and storage adapters;
- `llm-schema-eval` — Hermes/LLM schema, prompt, routing, fallback, and golden-set evaluation;
- `tender-safety-review` — procurement, EDS, data, LLM, and external-action controls.

#### Quality and delivery skills

- `regression-test-design` — focused pytest coverage and offline-safe fixtures;
- `browser-smoke` — scenario-specific UI verification;
- `security-review` — evidence-based application security review;
- `pr-review` — concrete regression and architecture review;
- `docs-sync` — synchronize canonical docs after verified behavior changes;
- `release-readiness` — evidence-based GO / conditional GO / NO-GO.

Use explicit skill invocation for high-risk or uncommon work. Typical Codex examples:

```text
$bug-investigation Fix the duplicate line-item defect in the tender parser.
$alembic-migration Add the supplier profile index with a safe downgrade.
$security-review Review the archive upload diff.
$release-readiness Assess this branch before merging to main.
```

### P0 — OpenCode subagents and commands

Status: implemented in this branch.

Project subagents in `.opencode/agents`:

- `reviewer` — read-only PR/diff review;
- `security-auditor` — read-only focused security audit;
- `test-engineer` — focused regression tests and verification;
- `integration-auditor` — read-only external-contract audit.

Project commands in `.opencode/commands`:

```text
/fix <bug description>
/review <branch, PR, or changed area>
/test-targeted <behavior or module>
/security-check <diff or subsystem>
/smoke-demo <journey or screen>
/integration-check <adapter or protocol>
/release-check <branch or release candidate>
```

Commands with `subtask: true` run the specialist in a separate context. This keeps large review, security, and testing transcripts out of the main implementation session.

### P1 — Current documentation: OpenAI Developer Docs MCP and Context7

Use documentation tools only for external APIs and libraries; inspect repository code first.

Preferred split:

- OpenAI Developer Docs MCP for Codex/OpenAI API/Agents SDK questions;
- Context7 for FastAPI, SQLAlchemy, Alembic, Pydantic, pytest, Ruff, PostgreSQL client libraries, and other third-party documentation;
- authoritative WSDL/specification/vendor documentation for procurement integrations.

Codex Context7 setup:

```bash
codex mcp add context7 -- npx -y @upstash/context7-mcp
codex mcp list
```

Benefits:

- avoids copying full manuals into prompts;
- reduces stale API guesses;
- retrieves only relevant documentation fragments;
- is useful before dependency additions or unfamiliar API usage.

### P1 — Browser verification: prefer Playwright CLI + skill; enable MCP only when useful

For normal repeatable coding-agent smoke tests, prefer a Playwright CLI/test workflow guided by `browser-smoke`. It is usually more token-efficient because the agent receives concise command output instead of a large browser tool schema and repeated accessibility trees.

Use Playwright MCP for exploratory debugging, persistent browser sessions, or workflows that genuinely need iterative page introspection.

Codex MCP setup when needed:

```bash
codex mcp add playwright npx "@playwright/mcp@latest"
```

OpenCode configuration syntax varies between major versions. For current OpenCode V2, use the `mcp.servers` shape from the installed version's official documentation. Keep Playwright disabled until a browser task needs it.

Priority browser journeys:

- `/demo/tender-agent` search, intake, upload, analysis, and report;
- unified site-to-pilot entrypoint;
- invalid archive/document handling;
- operator review and human-control checkpoints;
- generated HTML reports and downloads.

### P1 — GitHub plugin/connector

Use ChatGPT browser GitHub access for work that does not require the local runtime:

- inspect repository files, commits, issues, PRs, and diffs;
- create branches and documentation/configuration changes;
- maintain repository skills and instructions;
- prepare or review pull requests;
- perform remote triage.

Use Codex/OpenCode locally when execution depends on Docker, PostgreSQL, certificates, browser runtime, uncommitted files, or the Mac mini environment.

### P1 — Codex Security plugin when available

Use for deeper authorized scans, change review, vulnerability backlog triage, and proposed hardening. Keep human review for authentication, secrets, personal data, archive/file handling, EDS, external actions, and destructive migrations.

The repository `security-review` skill remains useful even without the plugin because it encodes Arvectum-specific trust boundaries and expected regression evidence.

### P2 — Google Drive connector in ChatGPT

Use for company materials that should not be copied into the repository:

- customer interview notes and pilot feedback;
- commercial documents;
- brand assets and presentations;
- research and working spreadsheets.

Keep product architecture, executable runbooks, schemas, migrations, API contracts, and development decisions in Git. Drive must not be the only source of truth for code behavior.

### P2 — GitHub Issues or Jira/Confluence, not competing backlogs

Recommended current default: GitHub Issues and pull requests because implementation history already lives next to the code. Add Atlassian only when an enterprise customer or external process requires it.

Do not duplicate acceptance criteria and status across systems; duplication increases stale context and token use.

### P2 — Sentry after a deployed pilot exists

Use a read-only connector/MCP for grouped errors and traces, then invoke `bug-investigation` to create a focused fix and regression test.

Do not paste entire log bundles into prompts. Retrieve the relevant event, stack, request metadata, and trace segment only. Sentry does not replace product audit logs or Hermes/operator traceability.

### P3 — Read-only PostgreSQL diagnostics

Add database tooling only when recurring diagnosis justifies it.

Rules:

- separate read-only credentials;
- no production writes;
- schema/table allowlist where supported;
- row and result limits;
- no personal or partner data copied into model context;
- migrations only through Alembic, never ad-hoc agent writes.

### P3 — Package stable workflows as an Arvectum plugin

Create a distributable plugin after the skill catalog and connector choices stabilize. A future plugin can bundle selected skills, presentation metadata, and dependencies on approved GitHub, documentation, Sentry, or other tools.

Repository-scoped skills remain the best format while workflows are still changing quickly because every edit is reviewable in the same PR process as code.

## Surface matrix

| Capability | ChatGPT browser | Codex | OpenCode |
|---|---|---|---|
| GitHub repository/PR/issue work | Primary connector | CLI/MCP/plugin optional | CLI/MCP optional |
| Company docs and connected data | Primary | Only when local execution needs a file | Only when local execution needs a file |
| Repository instructions | Project context | `AGENTS.md` | `AGENTS.md` |
| Shared coding workflows | Maintains them in Git | `.agents/skills` | `.agents/skills` |
| Specialist coding roles | Separate chat/task | Built-in/subagents/skills | `.opencode/agents` |
| Repetitive commands | Prompt/skill | `$skill`, CLI scripts | `.opencode/commands` and skills |
| Current dependency docs | Web/official sources | Docs MCP / Context7 | Context7 / MCP |
| Local edits and tests | Remote-safe edits only | Primary | Primary/alternative |
| Browser smoke | Browser capability when available | Playwright CLI/skill; MCP when needed | Playwright CLI/skill; MCP when needed |
| Docker/PostgreSQL/certificates | Not local | Primary | Primary |
| Deployed error diagnosis | Sentry connector/plugin | Sentry MCP/plugin | Sentry MCP |

## Token-efficiency policy

1. Use progressive disclosure. The agent initially sees skill names and descriptions, then loads only the matching `SKILL.md`.
2. Keep descriptions short, specific, and non-overlapping so the correct skill triggers without loading several bodies.
3. Keep `AGENTS.md` compact and link to canonical documents instead of embedding them.
4. Search paths, symbols, imports, and nearest tests before reading large files or documentation trees.
5. Open targeted line ranges and summarize; do not paste whole READMEs, logs, reports, WSDLs, or manuals.
6. Prefer deterministic scripts and CLI commands for repeated transformations and verification.
7. Use OpenCode `subtask: true` commands for review/security/testing so their transcripts do not pollute the main context.
8. Keep MCP servers disabled unless the current task needs them. Every enabled tool adds schemas and selection noise.
9. Prefer Playwright CLI/tests over Playwright MCP for repeatable smoke checks; use MCP for exploratory persistent sessions.
10. Run targeted Ruff/pytest first. Full-suite runs are for shared boundaries, migrations, releases, or explicit confidence checks.
11. Separate planning, implementation, review, and release assessment for large changes.
12. Use a cheaper/faster model for navigation, formatting, simple tests, and documentation; reserve stronger reasoning for architecture, complex defects, security, and ambiguous procurement extraction.
13. Start a new session when the objective changes materially.
14. Store stable decisions, commands, and constraints in Git instead of re-explaining them in every prompt.
15. Do not ask Codex and OpenCode to independently implement the same large task by default. Use one implementer and one narrow independent reviewer.

## Recommended operating pattern

```text
1. ChatGPT browser: research, architecture, issue/PR preparation, connected company data, and remote-safe GitHub work.
2. Codex: primary local implementation and verified execution.
3. OpenCode: alternative model routing, targeted subagents, or a clean independent review pass.
4. GitHub PR: canonical diff, checks, review discussion, and merge history.
```

## Permission defaults

- Exploration and review: read-only.
- Normal implementation: workspace write access only.
- Network access: only for a named documentation source, dependency, or read-only integration.
- Browser access: isolated profile and only named local/test origins when possible.
- Production databases, destructive commands, external messages, EDS, procurement submissions, and irreversible actions: explicit human approval.

## Local setup still required

Repository skills, OpenCode agents, and commands are portable and implemented here. MCP installation and runtime verification must be performed on the development machine because they modify local Codex/OpenCode configuration and require Node, browser, Docker, certificates, or local credentials.