---
name: regression-test-design
description: Design and implement focused pytest coverage for an Arvectum behavior change or bug. Use when acceptance criteria need executable evidence; avoid broad coverage expansion unrelated to the task.
---

# Regression test design

1. Express the behavior as Given/When/Then before writing test code.
2. Choose the narrowest test level that proves the contract:
   - unit for deterministic parsing, normalization, scoring, and calculations;
   - service test for orchestration and state transitions;
   - API test for request/response and authorization boundaries;
   - integration marker only when a real dependency is essential.
3. Reuse existing fixtures and factories before adding new ones.
4. Use synthetic or sanitized tender data. Keep fixtures minimal and readable.
5. Test externally observable behavior, not private implementation details.
6. Include the main success case and the highest-risk failure/edge case.
7. For bug fixes, verify the test fails against the old behavior when feasible.
8. Keep network tests opt-in under existing markers; default tests must remain offline-safe.
9. Run the new node IDs, then the nearest module, then broader tests only if shared code changed.
10. Report exactly what contract the tests now protect.

## Common Arvectum assertions

- normalized tender item fields and source traceability;
- stable risk/economics decisions at threshold boundaries;
- no external side effects in demo or restricted-pilot flows;
- schema validation and deterministic fallback for LLM output;
- safe archive extraction and rejected invalid files;
- API response status, payload shape, and redaction behavior.