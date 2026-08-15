# ARV-001 Execution State

## Current status

ARV-001 is recovered and its acceptance baseline is reproducible, but the
fresh exact-head zero-generation acceptance has not yet been run on the new
durable baseline. Provider execution remains unauthorized.

### Code recovery

- `ENG-002.2-A2`: **DONE 100%**.
- Canonical reconciliation PR: #3.
- Recovery merge: `1d4df81552c5d82e2f9f3637f48c643c3a30aaa8`.
- The canonical 20-file ARV-001 recovery is complete; meaningful residual code
  is none.

### Historical input recovery

- Historical descriptor identity: verified.
- Historical corpus SHA: `6557c0fa0dcc85bbab1a1e72a556505734c65eea6a29e649082eafbe80dc1d0a`.
- Historical local exact source-byte recovery: 5/10.
- Local archaeology: exhausted.

### Baseline re-acquisition

- `ENG-002.2-A3R.1`: **DONE 100%**.
- Baseline: `arv001-v2-6557c0fa0dcc`.
- Two independent fresh read-only EIS acquisitions: PASS.
- Physical documents: 10; logical documents: 6.
- Source identity reproducible: YES.
- Newly acquired corpus SHA: `6557c0fa0dcc85bbab1a1e72a556505734c65eea6a29e649082eafbe80dc1d0a`.
- The newly acquired corpus matches the historical approved corpus identity:
  YES.

The previously approved corpus identity is therefore fully reproducible again
from fresh repository-owned EIS intake.

### TLS and authentication

- macOS system-trust repository contour: PASS.
- Direct EIS route: PASS.
- A valid individual getDocsIP token is configured locally.
- Unauthenticated XSD GET: HTTP 403; it is not an authentication gate.
- Authenticated getDocsIP: PASS.

No token alias or token value is recorded here.

### Current ARV-001 status

| Boundary | Status |
| --- | --- |
| Code recovery | DONE |
| Acceptance baseline | REPRODUCIBLE / BOUND |
| Exact-head zero-generation A3.1 | PENDING on durable baseline v2 |
| Provider execution | NOT AUTHORIZED |
| Golden report | PENDING |
| Freeze | NOT PERFORMED |

## Next action

`ENG-002.2-A3.1-R` — run fresh exact-head zero-generation acceptance using the
durable reproducible baseline v2.

Do not claim ARV-001 complete until that action passes. This state does not
authorize `--execute-provider`.

## Historical implementation record

Issue #116 / PR #117 records an earlier implementation and compatibility-fix
phase. They are retained as forensic history only and do not describe the
current execution state. The earlier closeout required a fresh acceptance
before merge; that prerequisite has since been superseded by the completed
code recovery and fresh dual baseline re-acquisition recorded above.
