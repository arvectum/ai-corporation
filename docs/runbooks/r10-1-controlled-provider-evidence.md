# R10.1 Gate 5 — controlled real-provider evidence

Status: `RUNNER_READY_LIVE_EVIDENCE_NOT_EXECUTED`.

This runbook is only for one explicitly approved provider, model, pricing policy and customer-owned procurement run. It is not a general rollout procedure.

R10.1 uses deterministic map batches. Persisted source chunks are the atomic
units: a chunk is never split, batches are packed in stable source order, and
each batch carries the plan hash, batch hash, ordinal and corpus evidence hash.
The approved exact tokenizer must be configured for the provider/model before
any live execution; character estimates are not an acceptance basis.

The current planner identity is `arv003-map-plan-v6`. Its provider wire contract is
`compact-safe-v1`: the provider receives only fragment identity, source order, chunk
index and text; server-side expansion restores canonical provenance before grounding.
It recalculates payload
capacity after every exact request measurement and reserves enough rough-token
capacity for the remaining batch slots. The 32K/64K batch limits (32/18) and
HTTP tokenizer budgets (80/48) remain fixed. A locally fitting batch is not
accepted if it makes the remaining corpus infeasible; the planner performs at
most one deterministic grow attempt and bounded payload-domain shrink before
returning a sanitized fail-closed code.

## Safety boundary

- Run only on the operator-controlled host that already contains the approved database and extracted procurement documents.
- Keep the provider credential in the existing `AI_CORP_...` settings boundary. Never pass a credential as a command-line argument, commit it, paste it into an approval policy, or upload it with evidence.
- The runner supports the existing OpenAI-compatible `openai`, `openai_compatible` and `cloudru` configuration paths only.
- Do not upload `execution-1/` or `execution-2/`: they contain customer canonical outputs. Only `controlled-evidence.manifest.json` is designed for sanitized review.
- A timeout, unavailable provider, malformed response, unsupported claim, grounding failure, budget failure or repeat semantic mismatch leaves no published target directory.

## 1. Approve one provider and model

Set exactly one of the existing secret boundaries locally:

```bash
export AI_CORP_LLM_PROVIDER=openai_compatible
export AI_CORP_LLM_MODEL='<approved-model-id>'
export AI_CORP_OPENAI_API_KEY='<set-locally-do-not-copy>'
```

`openai` is also accepted as the configured provider name for the same secret boundary.

Or use Cloud.ru:

```bash
export AI_CORP_LLM_PROVIDER=cloudru
export AI_CORP_LLM_MODEL='<approved-model-id>'
export AI_CORP_CLOUDRU_API_KEY='<set-locally-do-not-copy>'
```

The base URL may be overridden through the corresponding existing setting only when the approved endpoint differs from the repository default.

## 2. Create a non-secret approval policy outside the repository

The pricing values must come from the provider's approved current tariff. The policy must not contain an API key, credential reference, endpoint token or raw procurement data.

```json
{
  "policy_version": "<approval-id-and-date>",
  "provider": "<openai-compatible-alias-or-cloudru>",
  "model": "<exact-approved-model-id>",
  "budget": {
    "limits": {
      "max_input_tokens": 0,
      "max_output_tokens": 0,
      "timeout_ms": 0,
      "max_retries": 0,
      "max_total_latency_ms": 0,
      "max_estimated_cost": 0.0,
      "chars_per_token_estimate": 4
    },
    "pricing": {
      "input_cost_per_1k_tokens": 0.0,
      "output_cost_per_1k_tokens": 0.0,
      "currency": "<three-letter-currency-code>",
      "pricing_table_version": "<provider-tariff-version-or-date>"
    }
  }
}
```

Replace every placeholder and every zero limit with an explicitly approved positive value. The policy provider and model must exactly match `AI_CORP_LLM_PROVIDER` and `AI_CORP_LLM_MODEL`. The schema rejects unknown fields, including `api_key`.

## 3. Approve one existing customer-owned run

Record outside the repository:

- exact `run_id`;
- exact registry number;
- customer, project and procurement-case ownership confirmation;
- confirmation that persisted procurement documents and extracted chunks belong to that registry number.

The runner independently re-checks these identities and refuses a non-customer-owned or mismatched run.

## 4. Execute

Before execution, record the approved tokenizer identity and context budget in
the non-secret policy/approval record. The runner executes batches sequentially
and merges claims by stable `(claim_id, field_path, canonical claim hash)` order.
Any missing, duplicate or oversized chunk fails closed; no partial target is
published.

For the local Gemma runtime, reuse the already loaded loopback `llama-server`
model through its non-generating `/tokenize` endpoint. This avoids loading the
7.6 GB GGUF in a new `llama-tokenize` process for every planner measurement.
The adapter accepts evidence only through stdin, permits only an explicit
`127.0.0.1` or `::1` HTTP endpoint ending in `/tokenize`, and never calls a
chat/completion route.

```bash
export ARV003_LLAMA_TOKENIZER_URL='http://127.0.0.1:8081/tokenize'
export ARV003_EXACT_TOKENIZER_COMMAND='python scripts/r10_1/tokenize_via_llama_server.py'
export ARV003_TOKENIZER_IDENTITY='<llama-build>-<gguf-sha256>-server-tokenize-v1'
```

A direct command that reloads the GGUF on every invocation is not approved for
the 1266-chunk controlled corpus because its planning time is not bounded in
practice. The controlled runner refuses to start when the tokenizer command or
identity is absent. Tokenizer failures are surfaced only as sanitized repository
codes.

```bash
python scripts/r10_1/run_controlled_provider_evidence.py \
  --run-id '<approved-run-id>' \
  --expected-registry-number '<approved-registry-number>' \
  --approved-policy '/absolute/local/path/approved-policy.json'
```

The default target is created under:

```text
<ARVECTUM_DATA_DIR>/r10-1-controlled-evidence/<run-id>-<provider>-<model>/
```

An existing target is never overwritten.

## 5. Acceptance evidence

A successful controlled run must contain:

- `execution-1/` and `execution-2/` with locally verified canonical outputs;
- `controlled-evidence.manifest.json` with `repeat_identity_verified=true`;
- identical request, evidence-packet, grounded-claim, source-graph and production-model identities across both executions;
- separate per-execution provider request ID, usage, latency, retries, cost, raw-response hash and canonical file hashes;
- `raw_response_stored=false`;
- no credential, raw provider body, evidence quote, raw tender text or local path in the sanitized manifest.

The controlled run is rejected when grounded claim semantics differ between executions, even when both individual provider calls otherwise succeed. Provider request IDs, provider confidence, usage, timing, cost and raw-response hashes are retained per execution but are not treated as stable identities.

## Explicit non-goals

- no automatic switch of customer runs to R10.1;
- no CI-held provider credential;
- no provider benchmark;
- no source-graph, R8/R9 snapshot or final-PDF contract change;
- no UI, deployment, 223-FZ, ARV-001, ARV-004 or ARV-005 work.
