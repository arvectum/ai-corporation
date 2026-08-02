# ARV-001 complete-corpus one-shot acceptance

This command is the repository-owned local acceptance path for the full EIS corpus of procurement `0388100001826000047`.

It replaces private orchestration harnesses. The Mac mini operator supplies only local paths and the already approved local runtime environment; application contracts are resolved from the repository itself.

## Safety boundary

The runner:

- requires a clean worktree at an explicitly approved commit;
- accepts only a fresh isolated SQLite database outside the repository;
- can initialize that database and migrate it to the repository head;
- validates the six logical documents and all ten physical files before the first application-data mutation;
- verifies exact file names, sizes, SHA-256 values and the canonical corpus hash;
- uses the current customer registry, pilot, tender repository, source-graph, snapshot and Gate 5 APIs;
- creates only a clearly named internal acceptance customer;
- requires the exact approved provider policy, model alias and persistent tokenizer;
- invokes the existing controlled R10.1 runner at most once;
- uses two canonical executions with zero retries;
- never writes to the production database or changes the accepted ARV-003 run, snapshot or bundle;
- writes generated acceptance artifacts outside Git.

## Required local inputs

- complete-corpus candidate directory containing:
  - `physical-files.json`;
  - `metadata.json`;
  - `logical-documents.json`;
  - `document-set-summary.json`;
  - `deterministic-parse-summary.json`;
  - `intake-summary.json`;
- the intake files referenced by `metadata.json`;
- exact approved provider policy;
- local Settings secret boundary;
- exact tokenizer environment;
- accepted loopback llama.cpp server exposing only `arvectum-gemma4-12b-q4km`;
- dependency-complete Python environment.

## Static preflight

Use the command without `--execute-provider` to verify every contract and input without creating application records or calling the provider.

```bash
python -m scripts.arv001.run_complete_corpus_acceptance \
  --candidate-root "$CANDIDATE_ROOT" \
  --intake-root "$INTAKE_ROOT" \
  --database-path "$RUN_ROOT/arv001.sqlite3" \
  --initialize-database \
  --data-dir "$RUN_ROOT/data" \
  --approved-policy "$POLICY" \
  --output-root "$RUN_ROOT/output" \
  --expected-head "$EXPECTED_HEAD"
```

A successful static preflight returns `status=static_preflight_complete`. It does not create customer, project, case, tender, document, chunk, run or snapshot records and does not call the provider.

## One controlled execution

After operator authorization, repeat the same command on a new empty `RUN_ROOT` and append `--execute-provider`.

```bash
python -m scripts.arv001.run_complete_corpus_acceptance \
  --candidate-root "$CANDIDATE_ROOT" \
  --intake-root "$INTAKE_ROOT" \
  --database-path "$RUN_ROOT/arv001.sqlite3" \
  --initialize-database \
  --data-dir "$RUN_ROOT/data" \
  --approved-policy "$POLICY" \
  --output-root "$RUN_ROOT/output" \
  --expected-head "$EXPECTED_HEAD" \
  --execute-provider
```

The controlled execution stops fail-closed on the first failed preflight. It never performs a second controlled invocation or automatic retry.

Success marker:

```text
ARV-001_COMPLETE_CORPUS_REPORT_READY_FOR_PRODUCT_OWNER_REVIEW
```

## Output

A successful run writes:

- `static-preflight.json`;
- `application-data-summary.json`;
- `post-persistence-preflight.json`;
- `controlled-invocation-summary.json`;
- `controlled-run-manifest.json`;
- `canonical-output.json`;
- `customer-report.html`;
- `upload-ready-report.html`;
- `report-content-check.json`;
- `artifact-hashes.json`;
- `README.md`;
- the original controlled-evidence directory with both canonical executions.

The two HTML files must be byte-identical. The report validator rejects internal IDs, hashes, private paths, stale statements that the technical specification or contract draft are missing, and omission of the required complete-corpus content.

## Acceptance sequence after generation

The generated `upload-ready-report.html` must first be reviewed by the product owner. Only after that review is it sent, together with the source documents, to two independent human reviewers. Issue ARV-001 remains open until the two reviews, truth pack, deterministic PASS and freeze manifest are complete.
