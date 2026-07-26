---
name: performance-debug
description: Diagnose and improve a measured Arvectum performance problem in parsing, database access, API latency, report generation, or batch processing. Use only with a reproducible slowdown or resource issue.
---

# Performance debugging

1. Define the measured symptom, workload, baseline, target, and environment.
2. Reproduce with a representative sanitized input before optimizing.
3. Measure the main phases separately: I/O, parsing, database, LLM/network, rendering, and serialization.
4. Profile before changing code; avoid guessing from code appearance.
5. Check repeated file reads, N+1 queries, unbounded collections, duplicate parsing, blocking calls in async paths, and unnecessary model/context size.
6. Prefer algorithmic or query improvements before concurrency and caching.
7. Add caching only with explicit key, invalidation, size, persistence, and privacy rules.
8. Preserve correctness and traceability; compare output before and after.
9. Add a benchmark or bounded performance assertion when stable and valuable.
10. Report baseline, bottleneck evidence, improvement, resource tradeoffs, and remaining limits.

## Token/LLM checks

For LLM-backed paths, measure prompt size, repeated context, output length, retries, and whether deterministic preprocessing can replace model input.