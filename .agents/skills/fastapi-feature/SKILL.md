---
name: fastapi-feature
description: Add or change an Arvectum FastAPI route, request/response schema, or service-backed feature while preserving layered architecture and compatibility. Use for API and web-handler coding tasks.
---

# FastAPI feature workflow

1. Identify the public contract: method, path, request model, response model, status codes, authorization, and side effects.
2. Find the owning router and service. Keep business rules out of the route handler.
3. Reuse existing Pydantic schemas or add narrowly scoped schemas with explicit validation.
4. Preserve dependency-injection, session, configuration, and error-handling patterns already used nearby.
5. Define compatibility behavior for missing fields, old clients, and existing stored data.
6. Reject invalid input at the boundary; do not silently coerce procurement-critical values.
7. Keep external calls behind service adapters and preserve timeouts, retries, and traceability.
8. Add focused API tests plus service tests for non-trivial rules.
9. Run Ruff on changed paths and targeted pytest modules; broaden when shared schemas, middleware, auth, or routing changed.
10. Document a public/internal API change when another component or n8n will call it.

## Arvectum constraints

- n8n may call documented internal APIs but must not write directly to product tables.
- No procurement submission, EDS action, or supplier message without an explicitly approved control boundary.
- LLM results must be schema-validated, traceable, and reviewable.
- Keep demo routes offline-safe where current behavior promises it.