# R10.1 Gate 5 — controlled real-provider evidence

Status: `RUNNER_READY_LIVE_EVIDENCE_NOT_EXECUTED`.

This runbook is only for one explicitly approved provider, model, pricing policy and customer-owned procurement run. It is not a general rollout procedure.

## Safety boundary

- Run only on the operator-controlled host that already contains the approved database and extracted procurement documents.
- Keep the provider credential in the existing `AI_CORP_...` settings boundary. Never pass a credential as a command-line argument, commit it, paste it into an approval policy, or upload it with evidence.
- The runner supports the existing OpenAI-compatible `openai` and `cloudru` configuration paths only.
- Do not upload `execution-1/` or `execution-2/`: they contain customer canonical outputs. Only `controlled-evidence.manifest.json` is designed for sanitized review.
- A timeout, unavailable provider, malformed response, unsupported claim, grounding failure, budget failure or repeat semantic mismatch leaves no published target directory.

## 1. Approve one provider and model

Set exactly one of the existing secret boundaries locally:

```bash
export AI_CORP_LLM_PROVIDER=openai
export AI_CORP_LLM_MODEL='<approved-model-id>'
export AI_CORP_OPENAI_API_KEY='<set-locally-do-not-copy>'
```

or:

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
  "provider": "<openai-or-cloudru>",
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
      "currency": "USD",
      "pricing_table_version": "<provider-tariff-version-or-date>"
    }
  }
}
```

Replace every placeholder and every zero limit with an explicitly approved positive value. The schema rejects unknown fields, including `api_key`.

## 3. Approve one existing customer-owned run

Record outside the repository:

- exact `run_id`;
- exact registry number;
- customer, project and procurement-case ownership confirmation;
- confirmation that persisted procurement documents and extracted chunks belong to that registry number.

The runner independently re-checks these identities and refuses a non-customer-owned or mismatched run.

## 4. Execute

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

The controlled run is rejected when grounded claim semantics differ between executions, even when both individual provider calls otherwise succeed.

## Explicit non-goals

- no automatic switch of customer runs to R10.1;
- no CI-held provider credential;
- no provider benchmark;
- no source-graph, R8/R9 snapshot or final-PDF contract change;
- no UI, deployment, 223-FZ, ARV-001, ARV-004 or ARV-005 work.
