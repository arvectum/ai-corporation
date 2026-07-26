# ARV-072 competitive benchmark

This directory contains the versioned, provider-neutral benchmark contract for
comparing Arvectum with procurement-analysis products on identical real
procurements.

## Files

- `rubric.json` — dimensions, weights, comparability and automatic-fail rules;
- `benchmark.json` — exact product identities, public-surface snapshot, access
  gates, protocol and current blockers;
- `live_result.schema.json` — redacted evidence contract for one product/case/run;
- `report_template.md` — final report structure;
- `validate.py` — dependency-free consistency check.

The five selected procurement cases are stored in
`fixtures/competitive/arv072/cases.json`.

## Important separation

The dated public-surface snapshot records only what vendors publish. It does not
prove accuracy, evidence quality, speed or cost in a controlled run and must
never be converted into rubric scores.

Live scoring starts only after:

1. all five source bundles and truth packs are frozen and hashed;
2. Arvectum's accepted ARV-003 producer and ARV-001 quality gate are available;
3. authorized access exists for at least five comparable products;
4. retained evidence is stored outside Git and represented here only by hashes
   and redacted observations.

## Validation

```bash
python benchmarks/competitive/arv072/validate.py
```

A successful run prints `ARV-072 benchmark package: OK`.
