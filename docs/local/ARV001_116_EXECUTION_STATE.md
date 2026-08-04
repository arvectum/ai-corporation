# ARV-001 Issue 116 Execution State

## Status

IN_PROGRESS — the repository-owned zero-generation pre-provider contour is implemented. The previously accepted executable head passed local acceptance and the full repository suite, but the documentation closeout head exposed one repository-owned macOS compatibility defect in canonical private-root creation. The defect is fixed and covered by regression tests; exact-head CI and one fresh Mac zero-generation acceptance are required before merge.

Provider execution remains outside this task and is not authorized.

## Branch and pull request

- Branch: `fix/arv001-final-one-pass`
- Pull request: #117
- Base commit: `89741e0d93af92239320c9c84806db985452804c`
- Previously accepted executable head: `949865acaec90534af882522e4ef32ac46148e1f`
- Documentation closeout head that exposed the defect: `e5cffaceae412325628e0b8a92e2ef36714b64bf`
- Compatibility fix commit: `9025adb56792417f96af19e3ddedbf99e1e0fceb`
- Regression test commit: `13c7c5350f98d956e38fccbf0a90797cc7c6b5cc`

The exact acceptance head is always the current PR head returned by Git. Do not copy an older SHA into the acceptance command.

## Completed repository scope

- Repository-owned runtime doctor and canonical full-pre-provider entrypoint.
- Exact approved GGUF and arm64 `llama-server` identity binding.
- Managed loopback runtime startup and zero-generation `/v1/models` and tokenizer probes.
- Split-root corpus validation with exact `10 physical -> 6 logical` inventory.
- Isolated application persistence through supported services and ORM boundaries.
- Exact-run ownership, tender, ordered-document, immutable snapshot, source-graph, and corpus verification.
- Post-persistence Gate 5 verification.
- Controlled runner preflight-only handoff that stops before provider construction and transport.
- Transactional retained-state publication with exact file set, 0700/0600 modes, fsync, atomic rename, post-rename verification, no-overwrite, and cleanup/quarantine on failure.
- Closed sanitized phase reporting and stable repository-owned prepared-state reason codes.
- Privacy scanning for local paths, credentials, database URLs, UUIDs, registry identity, and private descriptor identities.
- Runtime lifecycle, failure-injection, same-registry binding, zero-transport, publication, and strict prepared-state regression coverage.

## macOS private-root compatibility defect

The canonical closeout run used a fresh directory created through the normal macOS temporary path. macOS exposes `/tmp` as a symlink to `/private/tmp`. The legacy private-root guard rejected every symlinked ancestor before resolving the path, so it rejected a valid `mktemp` directory before runtime startup.

The canonical entrypoint now:

- rejects a symlink at the supplied private-root leaf;
- resolves existing system symlink ancestors to one canonical destination;
- performs all creation and publication only on that canonical destination;
- rejects a canonical destination inside the repository;
- preserves the existing no-overwrite and mode boundaries.

Regression coverage proves that a symlinked ancestor outside the repository is accepted, a symlinked private-root leaf is rejected, and an alias resolving inside the repository remains rejected.

## Previously accepted Mac zero-generation result

Accepted executable head: `949865acaec90534af882522e4ef32ac46148e1f`

- Exit code: `0`
- Status: `PASS`
- All 20 phases: PASS
- Physical documents: `10`
- Logical documents: `6`
- Extracted documents: `10`
- Prepared chunks: `233`
- Application prepared: `true`
- Post-persistence Gate 5 ready: `true`
- Controlled preflight-only: `true`

## Previously completed repository verification

- Focused ARV-001 tests: `55 passed`, `1 warning`
- `make check`: PASS
- `make test`: PASS
- Full suite: `2300 passed`, `230 skipped`, `0 failed`, `194 warnings`
- Full-suite duration: `172.30s`
- Exact-head CI #1738 on `949865acaec90534af882522e4ef32ac46148e1f`: SUCCESS
- Documentation-only CI #1739 on `e5cffaceae412325628e0b8a92e2ef36714b64bf`: SUCCESS

The R9 backup/restore fixture uses PostgreSQL 16 server (`pgvector/pgvector:pg16`), so local full-suite restore checks must place PostgreSQL 16 client tools first in `PATH`.

## Safety boundary

Required for every closeout run:

- `controlled_preflight_invocations=1` only on a complete PASS run;
- `controlled_provider_invocations=0`;
- `provider_generation_calls=0`;
- `production_db_mutations=0`;
- `old_arv003_mutations=0`;
- `git_data_leaks=0`;
- provider construction, transport, and generation remain unused.

## Remaining closeout actions

1. Verify exact-head CI for the current PR head containing the compatibility fix and regression tests.
2. Run one fresh canonical zero-generation acceptance bound to that exact PR head.
3. Complete review and merge PR #117.
4. Close #116 as completed and post the exact merged-head result to #87.

## Boundary after merge

The only next engineering action is a separately authorized one-shot controlled provider run on the exact merged head. This task does not authorize `--execute-provider`, provider construction, transport, generation, product-owner review, human-review freeze, or closure of #87.
