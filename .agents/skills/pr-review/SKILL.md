---
name: pr-review
description: Review an Arvectum pull request or local diff for bugs, regressions, missing tests, architectural drift, and unsafe control changes. Use for read-only code review; prioritize concrete defects over style.
---

# Pull request review

1. Read the PR goal, changed-file list, and diff before opening unrelated files.
2. Reconstruct the changed behavior and affected public/internal contracts.
3. Inspect nearby callers, schemas, migrations, and tests only where needed to validate a concern.
4. Look for:
   - incorrect logic or edge cases;
   - compatibility and migration defects;
   - missing authorization, validation, redaction, or traceability;
   - external side effects or weakened human-control gates;
   - parser overfitting or fabricated defaults;
   - duplicated business logic and divergence from canonical modules;
   - tests that pass without exercising the changed path.
5. Distinguish blocking defects, non-blocking risks, and optional cleanup.
6. Avoid comments about formatting already enforced by Ruff or other CI.
7. For every finding, cite exact file/line or diff location and explain a realistic failure scenario.
8. Confirm whether the claimed verification matches the change scope.
9. State explicitly when no blocking finding is supported by evidence.

## Review summary

Return findings ordered by severity, then open questions, then a concise verification assessment.