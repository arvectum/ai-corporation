# Test instructions

These rules apply when working under `tests/`.

- Load `regression-test-design` for new behavior coverage and `verify-change` for selecting commands.
- Prefer the lowest useful test level and focused node IDs.
- Reuse existing fixtures/factories before adding new ones.
- Use synthetic or explicitly sanitized procurement data.
- Keep the default suite offline-safe; live dependencies belong behind existing markers.
- Assert observable contracts rather than private implementation details.
- Do not weaken assertions or delete coverage merely to make a change pass.
- For parser defects, include positive, negative, and ambiguity cases.
- For thresholds/economics, test exact boundary values.
- For LLM-backed paths, test schema validation, traceability, and deterministic fallback without requiring a live model by default.
- Report the exact behavior protected and commands actually run.