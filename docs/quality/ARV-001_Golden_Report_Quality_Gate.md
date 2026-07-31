# ARV-001 / R10.2 — golden report and quality gate

## Decision

Arvectum must not enter ARV-005 pilot expansion on the basis of a working demo,
a provider success response, or a visually plausible PDF. R10.2 is complete only
when one real report is independently reviewed, passes the versioned gate, and is
bound to immutable source/canonical/report hashes in a freeze manifest.

The repository implementation is deterministic. The real source bundle, report,
review workbook, provider/runtime logs, and customer data remain outside Git.

## Quality axes

### Evidence

Every material claim must be tied to server-owned evidence. At least 95% of all
material claims must be supported, and 100% of supported material claims must
have valid locators. Any confirmed evidence mismatch is an automatic failure.

### Critical requirements and risks

The manual truth pack is authoritative for the gate. Critical-requirement recall
must be at least 90%; critical-risk recall must be at least 85%. Missing truth is
recorded as missing and yields `NOT_READY`; it is never converted to zero.

### Source completeness

All mandatory documents must be processed. Silent loss of any mandatory
document, table, attachment, or version is an automatic failure. An explicit
limitation may keep the case in `NOT_READY`, but it cannot be hidden by a score.

### Decision quality

The system and reviewed decision must agree. A distinct adjudicator may resolve
a disagreement with a written rationale. A positive decision without supported
qualification and commercial inputs is an automatic failure.

### Safety and artifact integrity

All safety gates are mandatory:

- completed human review;
- verified tenant/customer/project/case/run boundary;
- verified immutable artifact binding;
- verified source graph;
- zero external execution.

Any failed safety gate blocks release regardless of aggregate metrics.

## Defect severity

- `Sev-1` — can change the participation decision, cause rejection or material financial/contractual harm, leak data, or bind the wrong evidence/artifact. It blocks release and cannot be accepted as risk.
- `Sev-2` — material inaccuracy or omission requiring correction, an explicit limitation, or a reviewed waiver. One or two open Sev-2 defects yield `CONDITIONAL`; more than two fail.
- `Sev-3` — non-critical wording/usability issue. It may remain open with an owner and due date.

An automatic-fail rule always wins over severity aggregation.

## Stop rules

Stop the case and do not retry by loosening the gate when any of the following is
confirmed:

- wrong procurement, lot, customer, project, case or run identity;
- fabricated material fact or source attribution;
- evidence mismatch;
- silent source loss;
- unsupported positive decision;
- tenant/data boundary breach;
- unsafe external action;
- unresolved Sev-1.

A corrective attempt must create a new run and new immutable artifacts. Existing
canonical outputs and PDFs are not edited in place.

## Freeze stages

### Initial freeze

One real accepted report, at least two independent reviewers, all metrics passed,
no open Sev-1/Sev-2, and exact hashes are sufficient to freeze policy v1 for the
first controlled pilot wave.

### Pilot aggregate

At least three real truth-backed cases, each with two independent reviewers, are
required before aggregate quality claims are made. ARV-005 remains responsible
for the wider 10–20 case commercial pilot evidence.

## Relationship to existing work

- ARV-003/R10.1 supplies the controlled real provider output; ARV-001 does not change its provider or source-graph contracts.
- ARV-042 pilot KPI definitions remain the business/pilot layer; ARV-001 is the report-level release gate.
- ARV-052 expert review supplies the human escalation workflow; ARV-001 records only sanitized reviewer/adjudication outcomes.
- ARV-067 and ARV-072 may use the frozen gate but must not weaken or tune it from their own results.

## Completion markers

Repository implementation:

```text
ARV-001_REPOSITORY_GATE_IMPLEMENTED
ARV-001_POLICY_VERSION=1.0.0
ARV-001_SYNTHETIC_CANNOT_FREEZE
ARV-001_SOURCE_GRAPH_UNCHANGED
```

Final local acceptance:

```text
ARV-001_REAL_GOLDEN_REPORT_ACCEPTED
ARV-001_FREEZE_MANIFEST_CREATED
ARV-001_GATE_FROZEN
```

The final markers must not be used until the local real-case procedure succeeds.
