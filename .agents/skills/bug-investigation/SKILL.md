---
name: bug-investigation
description: Reproduce, localize, and fix a concrete Arvectum bug with evidence and a minimal regression test. Use for exceptions, wrong outputs, failed tests, or customer-reported behavior; do not use for broad refactoring.
---

# Bug investigation

1. Restate the observed behavior, expected behavior, and known reproduction input.
2. Reproduce before editing whenever possible. Record the exact command, request, or fixture.
3. Trace from the failing boundary inward: route/CLI -> service -> parser/model -> persistence/export.
4. Inspect only relevant symbols, call sites, tests, and recent commits. Do not read large documentation trees by default.
5. Form at most three ranked hypotheses and test the cheapest one first.
6. Identify the root cause, not only the line that raised the error.
7. Add a regression test that fails for the original behavior and passes after the fix.
8. Make the smallest coherent change; avoid opportunistic cleanup.
9. Run the failing test first, then the nearest test module, Ruff on changed paths, and broader tests only if the boundary is shared.
10. Report reproduction, root cause, changed files, verification, and remaining uncertainty.

## Arvectum checks

- Distinguish source-document ambiguity from parser defects.
- Preserve human-control and restricted-pilot boundaries.
- Do not use real partner data in a committed regression fixture.
- For archive/download bugs, check path traversal, size limits, MIME/content mismatch, retries, and partial files.
- For economics/risk defects, verify deterministic formulas and explain changed decision thresholds.