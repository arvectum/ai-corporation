---
name: tender-parser-change
description: Change tender document extraction, item normalization, specification parsing, or source selection in Arvectum. Use for PDF/DOCX/XLSX/XML/HTML procurement parsing and extraction defects.
---

# Tender parser change

1. Define the target field or item behavior and provide a minimal sanitized source example.
2. Identify the extraction stage: file selection, format reader, table/text segmentation, candidate detection, normalization, merge/deduplication, or report rendering.
3. Preserve source traceability: file, page/sheet/table/row, raw text, and confidence/reason where available.
4. Prefer deterministic structural signals before fuzzy or LLM-based inference.
5. Do not overfit to one tender number, supplier, layout, or wording.
6. Add positive, negative, and ambiguity fixtures. Include multi-row headers, merged cells, units, decimal separators, repeated sections, and irrelevant attachments when applicable.
7. Keep unknown values unknown; do not invent quantities, standards, prices, addresses, or deadlines.
8. Preserve equivalent/variant distinctions and prevent duplicate line items.
9. Compare before/after extraction on focused fixtures and inspect the generated report payload, not only intermediate objects.
10. Run focused parser tests and any acceptance smoke tied to the affected tender flow.

## Quality gate

Report precision risks, recall risks, fallback behavior, and which document layouts remain unsupported.