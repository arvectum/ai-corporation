# R10.1 llama.cpp batch-shaped compatibility probe

This probe is a precondition for another customer-data Gate 5 invocation after
`provider_response_invalid_json` on a real batch.

It is intentionally:

- database-free;
- customer-data-free;
- snapshot-free;
- one provider call only;
- zero automatic retries;
- deterministic synthetic input;
- approximately real 32K map-batch shape;
- template-level thinking disabled.

Run only with the existing private provider Settings profile and approved local
provider policy:

```bash
.venv/bin/python -m scripts.r10_1.probe_llama_batch_shape \
  --approved-policy '<PRIVATE POLICY PATH>'
```

Acceptance requires:

- exit code `0`;
- status `batch_shaped_compact_contract_passed`;
- provider call count `1`;
- retry count `0`;
- `reasoning_enabled=false`;
- `server_owned_grounding=true`;
- one to three grounded claims;
- one canonical evidence reference per claim.

Do not run the controlled customer runner when this probe fails.
