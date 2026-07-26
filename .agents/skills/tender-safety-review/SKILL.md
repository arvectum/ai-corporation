---
name: tender-safety-review
description: Review Arvectum changes that touch procurement data, EIS/ETP integrations, documents, Hermes/LLM decisions, partner exports, EDS, or external actions. Use before merging security- or domain-sensitive changes.
---

# Tender and control-boundary review

Review the proposed diff and report findings by severity with exact file/symbol references.

## Required checks

1. Human control
   - No autonomous submission, signature, supplier communication, or irreversible external action.
   - Recommendations remain distinguishable from verified facts and operator decisions.

2. Traceability
   - Inputs, extracted evidence, normalization, scoring, model output, and final decision remain attributable.
   - LLM output is schema-validated and has a deterministic or safe failure path.

3. Data handling
   - No real partner data, tokens, certificates, cookies, tender archives, or generated exports are committed.
   - Logs and API responses do not expose personal, commercial, or secret data.

4. File and network safety
   - Archive extraction prevents path traversal and unsafe file types.
   - HTML and report rendering avoids injection/XSS.
   - External calls have explicit timeouts, bounded retries, validation, and honest failure states.

5. Architecture
   - Business logic remains in FastAPI/Python.
   - n8n/integrations use documented internal APIs and do not write directly to product tables.
   - Database changes include Alembic migration and compatibility analysis.

6. Verification
   - Focused tests cover the changed control boundary and negative paths.
   - Existing restricted-pilot behavior is not silently widened.

## Output format

Return:

- blocking findings;
- non-blocking risks;
- missing tests/evidence;
- explicit statement when no issue was found.

Do not approve a sensitive change solely because tests pass.