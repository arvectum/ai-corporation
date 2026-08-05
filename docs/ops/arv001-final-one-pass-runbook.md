# ARV-001 full pre-provider runbook

The canonical zero-generation entrypoint is `python -m scripts.arv001.full_pre_provider_canonical`.
It is the only supported preparation path; direct invocation of the base runner
`python -m scripts.arv001.full_pre_provider` is deprecated for operator use.
The command accepts no secrets and publishes only a sanitized report. A PASS
does not authorize `--execute-provider`.

## Root-cause matrix

| previous failure | root cause | repository protection | regression test | remaining local dependency |
| --- | --- | --- | --- | --- |
| entrypoint/root/module | ad-hoc invocation | module CLI checks | runtime doctor context | correct checkout |
| Python/dependencies/imports | incomplete interpreter | import phase | doctor import tests | Python 3.11 env |
| symbols/schemas | guessed contracts | typed service boundary | service tests | none |
| split roots/hash/legacy docs | wrong layout or bytes | split adapter/contract | split-root tests | source corpus |
| env/export/prefix/settings | shell-only or stale names | allow-listed dotenv | aggregated env tests | private env |
| tokenizer/GGUF/binary/runtime identity | unverified local runtime | closed identity contract | runtime-contract tests | attested local assets |
| persistence/snapshot/source graph/Gate 5 | static-only boundary | prepare-only | workflow tests | isolated SQLite |
| controlled handoff | transport coupled to validation | preflight-only | zero-call test | loopback runtime |
| report grounding/privacy | internal projection leakage | existing report validators | report fixture tests | none |
| cleanup/mutation counters | partial-stage ambiguity | finally/forensic report | failure-injection tests | local filesystem |

| previous symptom | actual root cause | repository protection | regression test | remaining external dependency |
| --- | --- | --- | --- | --- |
| base entrypoint | split roots were not materialized together | split-root adapter only | split-root view tests | approved roots |
| launch outside checkout | relative modules resolve unpredictably | exact-root preflight | runtime doctor context test | none |
| missing `redis` | incomplete interpreter | aggregated import check | dependency-complete interpreter test | Python environment |
| missing source-graph serializer | guessed API name | typed import contract | static contract test | none |
| `stored_name` assumption | incompatible document descriptor | metadata projection | controlled metadata test | none |
| `analysis_mode` assumption | obsolete schema field | current typed payloads | static contract test | none |
| corpus profile mismatch | volatile fields entered hash | bound profile resolver | hash-profile tests | approved corpus |
| legacy `.doc` | extractor coverage gap | supported legacy extraction | legacy extraction test | converter availability |
| logical count post-check | physical and logical units conflated | exact inventory contract | 10-to-6 test | approved corpus |
| tokenizer not exported | dotenv did not reach child process | explicit child environment | environment propagation test | private environment |
| unprefixed settings | legacy variable names | allow-list and stable code | unprefixed test | private environment |
| old runtime env absent | obsolete manifest assumed required | current discovery/profile | discovery test | local runtime assets |
| GGUF ambiguity | multiple candidates | bounded no-symlink discovery | ambiguity test | local runtime assets |
| binary identity changed | local executable hash was incorrectly attributed to the official release | release, archive, executable and bundle hashes in one contract | official-runtime identity tests | intact official bundle |
| substituted runtime dylib | executable-only hashing did not bind adjacent libraries | deterministic full-bundle tree hash | bundle substitution regression | intact official bundle |
| generic exception | raw diagnostics leaked | sanitized terminal boundary | failure-injection test | none |
| pre-stage boundary | static validation ended too early | `--prepare-only` | prepare-only test | isolated SQLite |
| application persistence | direct SQL would bypass contracts | ORM/services only | persistence workflow test | isolated SQLite |
| Gate 5 | persisted counts/binding unverified | post-persistence preflight | Gate 5 test | isolated SQLite |
| controlled transport | preflight and generation coupled | `--preflight-only` stop | transport-spy test | loopback runtime |
| report/privacy | fixture projections leaked internal fields | report scanner/projection validation | privacy fixture test | none |
| merged-main guard | development branch was hard-coded after merge | exact main/detached validator | exact-main repository tests | clean exact checkout |

## Approved local runtime identity

`config/arv001/approved_local_runtime.json` is the single source of truth. The
canonical runner loads and validates that closed contract; runtime hashes are no
longer duplicated in executable code.

The approved server runtime is the official `ggml-org/llama.cpp` release:

- tag: `b10240`;
- commit: `0b14b87d7c20cb753b94b96854dd7b45306fc696`;
- asset: `llama-b10240-bin-macos-arm64.tar.gz`;
- asset SHA-256: `771a9a9bc7c9c62d5a3e891f2b64c89bca4850a7c9aeaf0b0e3f26be216fd8c7`;
- extracted `llama-server` SHA-256: `ff0e2445d93e2d6305c44cce6386db1020385194261dc184deaf0f37c7148d85`;
- complete runtime-bundle tree SHA-256: `9fb4db02070f78327f8f57ee40a906ef7d13d13444cf0c491aec4ba22b413740`;
- bundle shape: `43` regular files and `18` safe symlinks.

`ARV001_LLAMA_SERVER_PATH` must point to `llama-server` inside the complete,
unmodified extracted official bundle. Do not copy the executable into a separate
directory: the validator hashes its parent directory as the runtime bundle and
rejects missing, added, changed or escaping entries. Quarantine xattrs are not
part of the tree identity, but file bytes, permission bits and safe symlink
targets are bound.

A pre-existing `llama-server` process is not authorization to reuse it. The
managed acceptance runtime starts from the exact supplied bundle, binds only to
loopback, and must be cleaned up by the runner.

## Canonical invocation

Run from the repository root with static corpus/policy inputs only. The
orchestrator creates the local credential, dynamic loopback URLs and tokenizer
identity; `--private-env` is optional and never supplies those dynamic values.
The supported execution checkout is either a clean `main` at the exact expected
SHA or a clean detached worktree at that exact SHA. Dirty tracked or untracked
state remains fail-closed.

```bash
ARV001_CANDIDATE_ROOT=... \
ARV001_INTAKE_ROOT=... \
ARV001_APPROVED_POLICY=... \
ARV001_PRIVATE_RUNTIME_DIR=... \
ARV001_CORPUS_SHA=... \
ARV001_POLICY_SHA=... \
ARV001_GGUF_PATH=... \
ARV001_LLAMA_SERVER_PATH=... \
make arv001-full-pre-provider
```

Use the exact GGUF path and the exact `llama-server` path inside the intact
official bundle. Generic asset-root discovery is not an approval mechanism for
this acceptance contour. Never set an API key, runtime URL or tokenizer identity
in this command.

The command starts no generation endpoint and must report
`provider_generation_calls=0`. A failure is fail-closed and sanitized; do not
substitute older ARV-003 artifacts or use `--execute-provider`.

The runtime doctor and preparation runner reject unsafe or ambiguous local
inputs. Their reports intentionally omit local paths, credentials, source text,
customer identifiers and provider response bodies.
