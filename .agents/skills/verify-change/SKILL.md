---
name: verify-change
description: Select and run the smallest sufficient verification for an Arvectum change before reporting completion or opening a pull request. Use after editing code, configuration, migrations, UI, or documentation.
---

# Change verification

Choose verification from the actual change surface. Never claim a command passed unless it was run.

## Verification matrix

### Documentation or agent instructions only

- Inspect the diff for broken paths, contradictory instructions, accidental secrets, and stale commands.
- No Python test run is required unless executable examples or generated artifacts changed.

### Python business logic

```bash
uv run ruff check <changed-python-paths> <related-tests>
uv run pytest -q <related-test-files-or-nodeids>
```

Run the full suite when shared models, routing, scoring, export contracts, or cross-module behavior changed.

### API routes or schemas

- Run focused route/service tests.
- Verify status codes, response schemas, negative paths, and authorization/human-control boundaries.
- Run the full suite for shared request/response models or middleware changes.

### Database models or migrations

- Inspect upgrade and downgrade paths.
- Verify compatibility with existing data and application startup.
- Run PostgreSQL-marked tests when the environment is available.
- Do not silently use SQLite as proof that a PostgreSQL migration is safe.

### Browser/UI behavior

- Run focused backend/UI tests first.
- Use Playwright MCP or the existing browser test contour for the smallest critical flow affected.
- Capture the exact route, scenario, and observed result.

### External integrations

- Run deterministic offline tests with fixtures first.
- Live checks must be explicit, bounded, read-only where possible, and must not expose credentials or partner data.
- Record timeout, retry, and failure behavior.

### Security-sensitive changes

Also load `tender-safety-review` and treat authentication, secrets, archive extraction, HTML rendering, personal data, EDS, and external side effects as requiring human review.

## Completion report

Report:

- commands actually run;
- passed, failed, skipped, and unavailable checks;
- why the selected scope was sufficient;
- remaining uncertainty and any required local-only verification.
