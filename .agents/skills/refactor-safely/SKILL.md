---
name: refactor-safely
description: Refactor Arvectum code without changing observable behavior, using characterization tests and small reversible steps. Use for duplication, complexity, module extraction, or legacy cleanup; do not combine with unrelated feature work.
---

# Safe refactor

1. Define what must remain behaviorally identical and what structural outcome is desired.
2. Find existing tests that characterize the boundary. Add focused characterization tests before editing when coverage is weak.
3. Map callers, imports, public types, persistence assumptions, and configuration dependencies.
4. Split the work into reversible steps that keep tests passing.
5. Move or rename before rewriting. Avoid simultaneous semantic and structural changes.
6. Preserve public APIs or provide an explicit compatibility layer.
7. Do not change database schema, thresholds, prompt behavior, or report fields unless separately authorized.
8. Remove dead code only after proving there are no runtime, CLI, template, migration, or dynamic-import references.
9. Run targeted tests after each meaningful step, then the full suite for shared modules.
10. Compare outputs for representative tender fixtures when refactoring parsers, scoring, economics, or reports.

## Completion evidence

Report the behavior-preserving evidence, reduced complexity/duplication, changed public surfaces if any, and rollback path.