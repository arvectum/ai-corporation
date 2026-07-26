# ARV-067 electrical ontology

This directory contains the first versioned vertical ontology asset for Arvectum.
It covers four electrical procurement categories:

1. low-voltage power cable;
2. self-supporting insulated wire (SIP);
3. miniature circuit breaker;
4. electromechanical contactor.

The package defines category aliases, canonical marks, attribute synonyms,
required/optional fields, comparison semantics, match labels and stable mismatch
reason codes. It is an offline research contract and is intentionally not
imported by the production resolver.

## Files

- `electrical.v1.yaml` — ontology and matching policy;
- `ontology.schema.json` — versioned JSON Schema contract for the YAML document;
- `contract.py` — structural and cross-reference checks;
- `resolver.py` — deterministic synonym fixture resolver;
- `matcher.py` — explainable fixture matching policy;
- `validate.py` — validation entrypoint and fixture gate.

Fixtures live in `fixtures/ontology/electrical/`.

## Validation

```bash
python schemas/categories/electrical/validate.py
python -m pytest -q tests/test_arv067_electrical_ontology.py
```

A successful validator run prints:

```text
ARV-067 electrical ontology: OK (categories=4, synonyms=8, matches=13, runtime_import=false)
```

## Safety boundary

- no database model, migration or production resolver is changed;
- the ontology is not certification or legal-compliance evidence;
- a higher numeric capability is accepted only for fields explicitly marked
  `minimum`;
- required unknowns produce `UNCERTAIN`, not a guessed match;
- live accuracy claims remain blocked until controlled truth packs are accepted
  under ARV-001/ARV-005.
