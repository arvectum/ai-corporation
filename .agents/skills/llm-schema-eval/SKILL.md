---
name: llm-schema-eval
description: Change Hermes or another LLM-backed Arvectum workflow with structured output, deterministic fallbacks, traceability, and evaluation evidence. Use for prompts, schemas, routing, model changes, and quality regressions.
---

# LLM schema and evaluation workflow

1. State the user-visible behavior, current failure mode, and success criteria.
2. Identify the structured output schema, prompt/template, model settings, fallback, and downstream consumers.
3. Change one major variable at a time when possible: prompt, schema, model, retrieval context, or post-processing.
4. Keep prompts compact; move stable domain rules into versioned references, code, or schemas instead of repeating them.
5. Require schema validation. Reject or repair malformed output through a bounded, logged path.
6. Preserve raw model metadata and trace identifiers without storing secrets or unnecessary personal data.
7. Keep a deterministic fallback for critical calculations, thresholds, and control decisions.
8. Add or update a small golden set covering normal, ambiguous, adversarial, and missing-data cases.
9. Score correctness, faithfulness to source, naturalness where relevant, safety, latency, and estimated cost.
10. Compare baseline versus candidate and document regressions, not only average improvement.

## Arvectum non-negotiables

- LLM output is advisory unless an approved workflow explicitly says otherwise.
- No fabricated tender facts.
- No external action from unreviewed model output.
- Human review remains visible in the trace and final report.