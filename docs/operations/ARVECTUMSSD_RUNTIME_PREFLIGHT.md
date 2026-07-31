# ArvectumSSD runtime preflight

`scripts/ops/audit_external_runtime_paths.py` is the repository-side preflight
for the external runtime root. It is read-only: it does not create
directories, move data, change Docker or Colima, inspect client documents, or
contact Ollama, LM Studio, or another provider.

## Canonical contract

Set these variables in a local, untracked runtime env file:

- `ARVECTUM_STORAGE_ROOT` — canonical external storage root;
- `ARVECTUM_INTERNAL_RUNTIME_ROOT` — label-only comparison root;
- `ARVECTUM_DOCKER_CONTEXT` — expected Docker context;
- `ARVECTUM_DOCKER_CONTEXTS` — optional comma-separated allowlist for the live
  audit. Without it, every context returned by `docker context ls` is checked;
- `ARVECTUM_OLLAMA_REQUIRED` and `ARVECTUM_LMSTUDIO_REQUIRED` — optional dependency policy.

`ARVECTUM_STORAGE_ROOT` takes precedence over the compatibility alias
`AI_CORP_ARVECTUM_STORAGE_ROOT` used by application storage settings.

The root must be a real directory on a separate filesystem. The preflight does
not follow a root symlink and does not create missing subdirectories. Required
subroots are `data`, `artifacts`, `eis-archives`, `company-agent-runs`,
`backups`, `models`, `infrastructure`, and `temporary`.

## Safe commands

```bash
python scripts/ops/audit_external_runtime_paths.py --filesystem-only --json
```

Filesystem-only does not make claims about Docker, PostgreSQL, Redis, Ollama,
or LM Studio inventory. For a complete audit, provide a sanitized inventory:

```bash
python scripts/ops/audit_external_runtime_paths.py \
  --inventory-json <sanitized-inventory.json> --json
```

The inventory adapter accepts only `docker_contexts`,
`active_docker_context`, `postgres_instances`, `redis_instances`,
`ollama_available`, and `lmstudio_available`. Alternatively,
`--live-runtime` first lists Docker contexts, then runs
`docker --context <name> ps` for every discovered context (or every context in
`ARVECTUM_DOCKER_CONTEXTS`). This does not change the active context and does
not start, stop, or restart containers. PostgreSQL and Redis counts are
aggregated across unique daemons: each context is first checked with read-only
`docker --context <name> info`, and contexts with the same in-process daemon
identity share one `ps` query. The daemon identity is never emitted or stored
in the report. Classification uses only the Compose service label, image
repository, or narrowly tokenized container name; command text and arbitrary
label values are ignored. An unavailable daemon identity produces the
sanitized `docker_daemon_identity_unavailable` reason code; a later container
query failure produces `docker_context_unavailable`.

The JSON is sanitized: physical paths, Docker endpoints, credentials, model
paths, volume identifiers, and client identifiers are not emitted. Exit code
`0` means all mandatory checks for the selected mode passed; optional
dependencies use three states: `not_checked`, `available`, and `unavailable`.
Filesystem-only reports `not_checked` for Ollama and LM Studio; live runtime
checks report `available` or `unavailable`. Optional dependencies are only
fatal when explicitly required.

The filesystem device check is mandatory by default. The test-only environment
override `ARVECTUM_REQUIRE_SEPARATE_FILESYSTEM=false` is used only with
temporary same-device roots in unit tests and must not be used for production
runtime validation.

The command is intentionally not a replacement for an approved operational
restart or data migration. Do not remove old roots, Docker volumes, Colima
profiles, models, or backups based on this report alone.

## Current audit boundary

This repository change does not move data, download or delete models, mutate
PostgreSQL or Redis, restart Colima, execute a reboot, or perform provider/LLM
calls. Live Docker/Colima and model endpoint verification remains a separate
operator-approved read-only action.
