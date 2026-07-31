# ARV-001 — golden-report quality gate

This package implements the repository side of R10.2. It evaluates a sanitized,
human-reviewed procurement report against a versioned fail-closed policy and can
create an immutable freeze manifest for one accepted real golden report.

## Boundaries

The package accepts only counts, booleans, stable reviewer aliases, defect
records, decision labels, and SHA-256 hashes. It does not read or store tender
text, customer names, procurement numbers, provider bodies, credentials,
private filesystem paths, PDFs, databases, or network resources.

It does not change the R9/R10.1 source graph, canonical producer, report renderer,
or artifact persistence. It does not perform external actions.

## Files

- `policy.json` — versioned thresholds, automatic-fail rules, severity and review policy;
- `review.schema.json` — closed sanitized case-review contract;
- `freeze_manifest.schema.json` — closed immutable freeze manifest contract;
- `evaluate.py` — dependency-free validation, evaluation and freeze CLI.

## Verdicts

- `PASS` — every safety gate and metric passes, required independent review is complete, no open Sev-1/Sev-2 remains, and the decision is agreed or adjudicated;
- `CONDITIONAL` — no automatic failure exists, but one or two open Sev-2 defects still require resolution or a reviewed waiver; release is blocked;
- `FAIL` — a safety gate, automatic-fail rule, quality threshold, open Sev-1, or excessive Sev-2 count fails;
- `NOT_READY` — the input is valid but required real evidence, truth, reviewer coverage, or adjudication is missing.

Synthetic fixtures can exercise the package but can never freeze ARV-001.

## Commands

```bash
python quality_gates/arv001/evaluate.py validate-package

python quality_gates/arv001/evaluate.py evaluate \
  /private/sanitized/arv001-review.json \
  --output /private/sanitized/arv001-evaluation.json

python quality_gates/arv001/evaluate.py freeze \
  /private/sanitized/arv001-review.json \
  --frozen-at 2026-07-31T12:00:00Z \
  --approval-id arv001-local-acceptance-001 \
  --output /private/sanitized/arv001-freeze-manifest.json
```

Exit codes:

- `0` — package validation passed, evaluation verdict is `PASS`, or freeze succeeded;
- `1` — malformed/unsafe input or package error;
- `2` — review is valid but verdict is `CONDITIONAL`, `FAIL`, or `NOT_READY`.

## Initial freeze gate

The first frozen manifest requires:

1. one approved real procurement report from the accepted controlled producer contour;
2. exact source-bundle, source-graph, canonical-output and report hashes;
3. a complete manual truth pack for critical requirements and risks;
4. two distinct independent completed reviewers;
5. decision agreement or a distinct adjudicator with rationale;
6. no automatic-fail defect, open Sev-1, open Sev-2, silent source loss, evidence mismatch or failed safety gate;
7. all versioned metric thresholds passed.

The source material and full review workbook remain outside Git. Only a separately
reviewed sanitized freeze manifest may be committed.

## Regression check

```bash
python -m pytest -q tests/quality/test_arv001_quality_gate.py
```

The broader default suite remains required before merge.
