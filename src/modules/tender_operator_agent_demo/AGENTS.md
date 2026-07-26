# Tender Operator Agent Demo instructions

These rules apply when working inside `src/modules/tender_operator_agent_demo`.

## Scope

- Preserve the three controlled demo modes: procurement search/intake, local upload and analyze, and synthetic walkthrough.
- Keep procurement discovery separate from documentation intake and analysis.
- Keep external calls behind adapters/services; routes and UI handlers must not own procurement business logic.
- Preserve RFQ-first workflow, source traceability, deterministic economics, calibrated risks, and explicit human review.

## Parsing and files

- Prefer deterministic document structure before fuzzy or LLM inference.
- Never invent missing tender facts.
- Preserve file/page/sheet/table/row provenance where available.
- Treat archive extraction, filenames, paths, content types, size limits, and generated HTML as security-sensitive.
- Use synthetic or sanitized fixtures only.

## Verification

Load the most relevant skill:

- `tender-parser-change` for extraction/normalization;
- `external-integration` for ЕИС/ЭТП/download adapters;
- `fastapi-feature` for routes and schemas;
- `browser-smoke` for end-to-end demo UI;
- `llm-schema-eval` for Hermes/LLM behavior.

Run focused tests under `tests/` matching the changed service or route before broader acceptance smoke. Compare generated report payloads for parser/economics changes.