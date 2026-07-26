---
name: focused-change
description: Implement a feature, bug fix, or refactor in Arvectum with the smallest coherent diff and targeted verification. Use for normal coding tasks; do not use for read-only review.
---

# Focused change workflow

1. State the requested behavior in one or two sentences.
2. Identify the owning module, its public boundary, and the nearest tests.
3. Inspect only the relevant symbols and call sites. Avoid broad repository scans unless ownership is unclear.
4. Check whether the change affects a documented product, launch, data, or human-control boundary.
5. Propose a short plan when multiple files or layers are involved.
6. Implement the minimum coherent change without unrelated cleanup.
7. Add or update focused regression tests for externally visible behavior.
8. Run targeted Ruff and pytest commands first. Run the full suite only for cross-cutting changes or before release.
9. Summarize changed files, behavior, commands, results, assumptions, and residual risk.

## Stop and ask before proceeding

Stop when the change would:

- add a production dependency without a clear need;
- change a public API or database schema without migration/compatibility work;
- enable autonomous procurement submission, EDS, supplier messages, or another external side effect;
- require real partner data, credentials, or secrets;
- conflict with canonical module or launch-control documentation.
