# ARV-067 electrical ontology

This directory contains versioned electrical procurement data assets for Arvectum.

The detailed v1 ontology covers four deterministic matching profiles:

1. low-voltage power cable;
2. self-supporting insulated wire (SIP);
3. miniature circuit breaker;
4. electromechanical contactor.

The expanded catalog adds section-level coverage of both PJSC Rosseti approved-equipment registries:

- 21 primary-equipment sections;
- 7 secondary-equipment sections;
- 28 total families with aliases, subcategories and discriminator fields;
- a 42-document metadata-only normative registry covering GOST/PUE, Rosseti, Rosatom and RusHydro sources.

ARV-067A adds a shared registry of 126 attribute IDs, 21 units, five type-safe comparators and six verified value sets. Twenty-two attributes preserve the exact contract of the four detailed profiles; 104 taxonomy discriminator definitions remain explicitly provisional.

ARV-067B adds an explicit category hierarchy with 166 stable nodes: one root, two registry groups, 28 source families and 135 source subcategories. Parent paths, attribute inheritance, role-specific overrides, routing aliases, provenance and lifecycle gates are validated across all files.

ARV-067C adds a separate typed relation graph with nine relation types, three abstract component roles, 25 explainable assertions and 26 evaluation fixtures. Compatibility, completeness, replacement, approval and normative applicability remain separate semantics; missing evidence returns `UNCERTAIN`.

The package is an offline research contract and is intentionally not imported by the production resolver.

## Files

Detailed ontology:

- `electrical.v1.yaml` — four detailed profiles and matching policy;
- `ontology.schema.json` — JSON Schema contract for the detailed ontology;
- `contract.py` — structural and cross-reference checks;
- `resolver.py` — deterministic synonym fixture resolver;
- `matcher.py` — explainable fixture matching policy;
- `validate.py` — detailed-profile validation entrypoint.

Expanded nomenclature and norms:

- `nomenclature.v1.yaml` — 28 Rosseti-derived section families;
- `nomenclature.schema.json` — closed catalog contract;
- `normative_registry.v1.yaml` — metadata-only normative source registry;
- `normative_registry.schema.json` — closed normative contract;
- `validate_catalog.py` — cross-file catalog and normative validator.

Shared attribute registry:

- `attribute_registry.v1.yaml` — units, comparators, value sets and fragment manifest;
- `attribute_registry.schema.json` — closed manifest contract;
- `attribute_fragment.schema.json` — closed fragment contract;
- `attributes/*.v1.yaml` — verified and provisional attribute definitions by domain;
- `validate_attributes.py` — cross-file attribute, unit and profile-reference validator.

Category hierarchy:

- `category_tree.v1.yaml` — hierarchy, routing, lifecycle and fragment manifest;
- `category_tree.schema.json` — closed hierarchy manifest contract;
- `category_tree_fragment.schema.json` — closed node-fragment contract;
- `category_nodes/*.v1.yaml` — 166 explicit category nodes split into 16 reviewable fragments;
- `category_router.py` — deterministic offline routing for fixtures;
- `validate_category_tree.py` — graph, inheritance, source-coverage and routing validator.

Relation graph:

- `relation_graph.v1.yaml` — relation manifest, evaluation policy and safety governance;
- `relation_graph.schema.json` — closed graph manifest contract;
- `relation_types.v1.yaml` — nine non-equivalence relation semantics;
- `relation_type_registry.schema.json` — direction, symmetry, transitivity and cardinality contract;
- `component_roles.v1.yaml` — abstract component roles, not manufacturer models;
- `component_role_registry.schema.json` — component-role contract;
- `relation_assertions/*.v1.yaml` — cable, overhead-line, switching and RZA/measurement assertions;
- `relation_assertion_fragment.schema.json` — assertion/evidence/provenance contract;
- `relation_evaluator.py` — deterministic offline evaluation;
- `relation_validation_contract.py`, `relation_validation_assertions.py` — validation helpers;
- `validate_relations.py` — relation graph validation entrypoint.

Fixtures live in `fixtures/ontology/electrical/`.

## Validation

```bash
python schemas/categories/electrical/validate.py
python schemas/categories/electrical/validate_catalog.py
python schemas/categories/electrical/validate_attributes.py
python schemas/categories/electrical/validate_category_tree.py
python schemas/categories/electrical/validate_relations.py
python -m pytest -q tests/test_arv067_electrical_ontology.py
python -m pytest -q tests/test_arv067_electrical_catalog_expansion.py
python -m pytest -q tests/test_arv067a_attribute_registry.py
python -m pytest -q tests/test_arv067b_category_tree.py
python -m pytest -q tests/test_arv067c_relations.py
```

Successful validator markers:

```text
ARV-067 electrical ontology: OK (categories=4, synonyms=8, matches=13, runtime_import=false)
ARV-067 expanded electrical catalog: OK (sections=28, normative_documents=42, runtime_import=false)
ARV-067A attribute registry: OK (..., runtime_import=false)
ARV-067B category tree: OK (nodes=166, families=28, subcategories=135, detailed_profiles=4, routing_cases=24, runtime_import=false)
ARV-067C relation graph: OK (types=9, components=3, assertions=25, fixtures=26, runtime_import=false)
```

## Safety boundary

- no database model, migration or production resolver is changed;
- taxonomy-only families, provisional attributes, category nodes and relation templates are not production matchers;
- broad family matches and cross-branch ambiguity require review;
- compatibility is not equivalence, and completeness is not compatibility;
- category-level compatibility never rises above `CONDITIONAL`;
- missing relation evidence returns `UNCERTAIN` rather than a guess;
- `replaces` and `approved_for` instances remain blocked until ARV-067E provides catalog entities;
- role-specific voltage and current fields are not merged automatically;
- the ontology and normative registry are not certification or legal-compliance evidence;
- operator registry inclusion is not universal product equivalence proof;
- standards require scope, edition and amendment verification for each procurement;
- unlicensed full texts are not committed;
- live accuracy and compliance claims remain blocked until controlled truth packs and human review are accepted.
