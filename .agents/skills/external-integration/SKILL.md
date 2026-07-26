---
name: external-integration
description: Implement or audit an Arvectum integration with ЕИС, ЭТП, SOAP, REST, webhooks, storage, or another external system. Use for adapters and contracts; do not put procurement business logic into the integration layer.
---

# External integration workflow

1. Capture the authoritative contract: protocol, endpoint/WSDL, authentication, request/response examples, limits, and failure semantics.
2. Distinguish verified facts from assumptions. Do not infer undocumented fields or behavior.
3. Build a narrow adapter interface so business logic remains in FastAPI/Python services.
4. Define timeouts, retries with backoff, idempotency, pagination/cursors, rate limiting, and partial-failure behavior.
5. Preserve raw identifiers and source metadata for traceability while normalizing only at the domain boundary.
6. Validate response shape and content type; handle malformed XML/JSON/archives defensively.
7. Redact secrets, certificates, tokens, cookies, and personal data from logs and fixtures.
8. Add contract fixtures from sanitized examples and offline tests. Mark real network smoke tests explicitly.
9. For SOAP, verify namespaces, operation binding, service URL, TLS/client-certificate behavior, and fault payloads.
10. Report what is proven locally, what requires live credentials, and what remains unknown.

## Stop conditions

Stop before enabling external submission, EDS/signature, supplier communication, destructive synchronization, or production credentials unless the task explicitly authorizes the boundary and review process.