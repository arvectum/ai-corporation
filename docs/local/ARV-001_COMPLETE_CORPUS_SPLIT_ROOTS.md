# ARV-001 split candidate/intake roots

Use this repository-owned adapter when the immutable complete-corpus summaries and the original intake metadata/files are stored in different directories.

The adapter creates a temporary byte-identical view containing only the six JSON contract artifacts, delegates to the existing one-shot runner, and removes the view after completion. It does not copy or modify procurement source files, the candidate summaries, the intake run, the production database, or the accepted ARV-003 bundle.

## Static preflight

```bash
python -m scripts.arv001.run_complete_corpus_acceptance_split_roots \
  --candidate-root "$CANDIDATE_ROOT" \
  --intake-root "$INTAKE_ROOT" \
  --database-path "$RUN_ROOT/arv001.sqlite3" \
  --initialize-database \
  --data-dir "$RUN_ROOT/data" \
  --approved-policy "$POLICY" \
  --output-root "$RUN_ROOT/output" \
  --expected-head "$EXPECTED_HEAD"
```

The adapter requires these files directly in `CANDIDATE_ROOT`:

- `physical-files.json`;
- `logical-documents.json`;
- `document-set-summary.json`;
- `deterministic-parse-summary.json`;
- `intake-summary.json`.

It requires `metadata.json` directly in `INTAKE_ROOT`. The stored procurement files referenced by that metadata remain in the same intake root and are validated by the existing one-shot runner.

If `metadata.json` also exists in the candidate root, both copies must be byte-identical. Missing, symlinked or conflicting artifacts fail closed before application workflow and provider execution.

## Controlled execution

Only after a successful static preflight, use a new empty run root and append `--execute-provider`. All existing one-shot safety rules remain unchanged: one controlled invocation, two executions, zero retries, isolated SQLite only, no production or historical ARV-003 mutations.
