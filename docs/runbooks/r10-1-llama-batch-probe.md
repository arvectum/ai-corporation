# R10.1 llama.cpp exact token-shaped compatibility probe

This probe is a precondition for another customer-data Gate 5 invocation after
`provider_response_invalid_json` on a real batch.

It is intentionally:

- database-free;
- customer-data-free;
- snapshot-free;
- one provider call only;
- zero automatic retries;
- deterministic synthetic input;
- shaped with the approved exact persistent tokenizer to the real 32K request
  boundary;
- measured over the complete serialized request envelope;
- template-level thinking disabled.

Run only with the existing private provider Settings profile, exact tokenizer
environment and approved local provider policy:

```bash
.venv/bin/python -m scripts.r10_1.probe_llama_batch_shape \
  --approved-policy '<PRIVATE POLICY PATH>'
```

Before the provider call, the probe binary-searches the deterministic synthetic
fragment count and verifies both controlled limits:

- serialized evidence tokens do not exceed the approved evidence budget;
- exact request tokens plus output reserve and safety margin do not exceed the
  32K context window.

Acceptance requires:

- exit code `0`;
- status `batch_shaped_compact_contract_passed`;
- provider call count `1`;
- retry count `0`;
- `reasoning_enabled=false`;
- `server_owned_grounding=true`;
- non-negative `context_headroom_tokens`;
- at least eight synthetic fragments after exact shaping;
- one to three grounded claims;
- one canonical evidence reference per claim.

A non-2xx response is reported only as a sanitized HTTP status code and optional
server error type. Provider response bodies, prompts and evidence text are never
printed.

Do not run the controlled customer runner when this probe fails.
