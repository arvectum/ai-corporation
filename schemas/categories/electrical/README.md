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

ARV-067A adds a shared registry of 156 attribute IDs, 21 units, five type-safe comparators and six verified value sets. Fifty-two attributes are verified by detailed profile contracts; 104 taxonomy discriminator definitions remain explicitly provisional.

ARV-067B adds an explicit category hierarchy with 166 stable nodes: one root, two registry groups, 28 source families and 135 source subcategories. Parent paths, attribute inheritance, role-specific overrides, routing aliases, provenance and lifecycle gates are validated across all files.

ARV-067C adds nine typed relations, three component roles, 25 explainable assertions and 26 relation fixtures for compatibility, completeness, replacement, approval and normative applicability.

ARV-067D adds 15 wave-1 detailed profiles, 11 category-promotion overlays and 180 fixture cases for cable accessories, surge protection, switching equipment, line hardware, insulators and low-voltage control/protection products.

ARV-067E adds separate manufacturer, series, model, execution, supplier-offer and evidence layers with stable IDs, articles, designation suffixes, replacement history and Rosseti-to-operator-approval mapping. The included records are synthetic contract fixtures and do not assert real product approval.

ARV-067F adds 22 clause/page-level requirements backed by two Rosseti registry snapshot hashes, explicit applicability conditions, source excerpts, priority/conflict gates and a fail-closed evaluator that never makes an automatic compliance decision.

ARV-067G adds a content-addressed provenance claim registry for categories, aliases, attributes, allowed values, relations and normative requirements. It includes immutable hash-chained review events, explicit conflict gates and a deterministic report of unverified, low-confidence, stale and production-blocked assertions.

ARV-067H adds a reproducible candidate truth-pack benchmark for all 15 wave-1 profiles: 2400 synthetic contract items, 1500 positive and 900 hard-negative cases, exact leakage checks, OCR and unseen-manufacturer slices, content-addressed pack roots, metrics and release gates. Independent human acceptance remains pending, so shadow-runtime promotion is blocked.

The package is an offline research contract and is intentionally not imported by the production resolver.

## Files

Detailed ontology:

- `electrical.v1.yaml` — four original detailed profiles and matching policy;
- `ontology.schema.json` — JSON Schema contract for the original ontology;
- `contract.py` — structural and cross-reference checks;
- `resolver.py` — deterministic synonym fixture resolver;
- `matcher.py` — explainable original fixture matching policy;
- `validate.py` — original detailed-profile validation entrypoint.

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
- `attributes/wave1_profiles.v1.yaml` — 30 verified ARV-067D profile attributes;
- `validate_attributes.py` — cross-file attribute, unit and profile-reference validator.

Category hierarchy:

- `category_tree.v1.yaml` — hierarchy, routing, lifecycle and fragment manifest;
- `category_tree.schema.json` — closed hierarchy manifest contract;
- `category_tree_fragment.schema.json` — closed node-fragment contract;
- `category_nodes/*.v1.yaml` — 166 explicit category nodes split into 16 reviewable fragments;
- `category_router.py` — deterministic offline routing for fixtures;
- `validate_category_tree.py` — graph, inheritance, source-coverage and routing validator.

Relation graph:

- `relation_graph.v1.yaml` — relation manifest and evaluation policy;
- `relation_types.v1.yaml` — nine typed relation contracts;
- `component_roles.v1.yaml` — abstract component-role endpoints;
- `relation_assertions/*.v1.yaml` — explainable relation assertions;
- `relation_evaluator.py` — deterministic offline relation evaluator;
- `validate_relations.py` — relation, evidence, conflict and cycle validator.

Wave-1 detailed profiles:

- `detailed_profiles_wave1.v1.yaml` — ARV-067D manifest;
- `detailed_profiles_wave1.schema.json` — closed profile manifest contract;
- `detailed_profile_fragment.schema.json` — closed profile-fragment contract;
- `profile_fragments/*.v1.yaml` — 15 detailed profiles in four reviewable domains;
- `wave1_category_bindings.v1.yaml` — offline `taxonomy_only → fixtures_ready` overlays;
- `wave1_category_bindings.schema.json` — closed binding contract;
- `wave1_profile_matcher.py` — deterministic five-outcome matcher;
- `validate_wave1_profiles.py` — schema, cross-reference, fixture and benchmark validator.

Product catalog entities:

- `product_catalog.v1.yaml` — ARV-067E entity manifest and governance;
- `product_catalog.schema.json` — closed manifest contract;
- `product_entity_fragment.schema.json` — closed contracts for six entity types;
- `product_entities/*.v1.yaml` — synthetic manufacturers, series, models, executions, offers and evidence;
- `product_catalog_contract_cases.schema.json` — negative/positive contract-case schema;
- `validate_product_catalog.py` — identifier, lifecycle, evidence and cross-reference validator.

Clause-level normative requirements:

- `normative_requirements.v1.yaml` — ARV-067F manifest, priorities and decision policy;
- `normative_document_editions.v1.yaml` — editions, dates, replacements and source-file hashes;
- `normative_requirement_fragments/*.v1.yaml` — 22 source-located requirements;
- `normative_requirement_evaluator.py` — fail-closed offline evaluator;
- `validate_normative_requirements.py` — source, cross-reference, conflict and fixture validator.

Provenance and expert review:

- `provenance_registry.v1.yaml` — ARV-067G manifest, confidence and production gates;
- `provenance_sources.v1.yaml` — immutable content-addressed source revisions;
- `provenance_claims/*.v1.yaml` — 24 audit claims across six assertion types;
- `provenance_review_events.v1.yaml` — append-only hash-chained review history;
- `provenance_conflicts.v1.yaml` — explicit unresolved/resolved conflict registry;
- `provenance_audit_report.v1.yaml` — deterministic report of review and freshness gaps;
- `generate_provenance_report.py` — report generator;
- `validate_provenance.py` — source, claim, review-event, conflict and report validator.

Truth packs and benchmark:

- `truth_pack_manifest.v1.yaml` — ARV-067H counts, slices, metrics and release gates;
- `truth_pack_seed_contract.v1.yaml` — deterministic split, manufacturer, surface and mutation contract;
- `truth_pack_acceptance.v1.yaml` — per-profile independent-acceptance registry;
- `truth_pack_generator.py` — reproducible 2400-item JSONL materializer and pack-root generator;
- `truth_pack_runner.py` — outcome, category, attribute, hard-negative and slice benchmark runner;
- `truth_pack_release_report.v1.yaml` — committed blocked-release snapshot without production claims;
- `validate_truth_packs.py` — schema, counts, hash, leakage, acceptance and release-gate validator.

Fixtures live in `fixtures/ontology/electrical/`.

## Validation

```bash
python schemas/categories/electrical/validate.py
python schemas/categories/electrical/validate_catalog.py
python schemas/categories/electrical/validate_attributes.py
python schemas/categories/electrical/validate_category_tree.py
python schemas/categories/electrical/validate_relations.py
python schemas/categories/electrical/validate_wave1_profiles.py
python schemas/categories/electrical/validate_product_catalog.py
python schemas/categories/electrical/validate_normative_requirements.py
python schemas/categories/electrical/validate_provenance.py
python schemas/categories/electrical/validate_truth_packs.py
python schemas/categories/electrical/truth_pack_runner.py
python -m pytest -q tests/test_arv067_electrical_ontology.py
python -m pytest -q tests/test_arv067_electrical_catalog_expansion.py
python -m pytest -q tests/test_arv067a_attribute_registry.py
python -m pytest -q tests/test_arv067b_category_tree.py
python -m pytest -q tests/test_arv067c_relations.py
python -m pytest -q tests/test_arv067d_wave1_profiles.py
python -m pytest -q tests/test_arv067e_product_catalog.py
python -m pytest -q tests/test_arv067f_normative_requirements.py
python -m pytest -q tests/test_arv067g_provenance.py
python -m pytest -q tests/test_arv067h_truth_packs.py
python -m pytest -q tests/test_arv067h_release_report.py
```

Successful validator markers:

```text
ARV-067 electrical ontology: OK (categories=4, synonyms=8, matches=13, runtime_import=false)
ARV-067 expanded electrical catalog: OK (sections=28, normative_documents=42, runtime_import=false)
ARV-067A attribute registry: OK (..., runtime_import=false)
ARV-067B category tree: OK (nodes=166, families=28, subcategories=135, detailed_profiles=4, routing_cases=24, runtime_import=false)
ARV-067C relation graph: OK (types=9, components=3, assertions=25, fixtures=26, runtime_import=false)
ARV-067D wave1 profiles: OK (profiles=15, bindings=11, fixtures=180, attributes=..., runtime_import=false)
ARV-067E product catalog: OK (manufacturers=3, series=4, models=5, executions=6, offers=5, evidence=8, contract_cases=15, runtime_import=false)
ARV-067F normative requirements: OK (document_editions=2, requirements=22, fixture_cases=20, runtime_import=false)
ARV-067G provenance: OK (sources=6, claims=24, review_events=24, conflicts=0, fixture_cases=24, production_ready=0, runtime_import=false)
ARV-067H truth packs: OK (profiles=15, items=2400, positive=1500, hard_negative=900, ocr=510, unseen_manufacturer=600, fixture_cases=25, independent_acceptance=false, release=BLOCKED, runtime_import=false)
```

## Safety boundary

- no database model, migration or production resolver is changed;
- taxonomy-only families, provisional attributes and category nodes are not production matchers;
- ARV-067D promotions are overlays and do not rewrite the source category snapshot;
- ARV-067E records are synthetic fixtures and do not assert real products or approvals;
- ARV-067F stores only hashes, locators and short excerpts; edition currency and compliance are never inferred automatically;
- ARV-067G does not convert machine-extracted or legacy source-verified assertions into expert-verified claims;
- provenance source revisions and review events are append-only; changed source content requires a new revision;
- low-confidence claims and unresolved conflicts remain review-required and production-blocked;
- ARV-067H synthetic truth items validate the matcher contract, not real-procurement accuracy;
- exact train/dev/test leakage and test-manufacturer leakage are blocked, while shared-generator bias remains disclosed;
- OCR-like inputs do not measure OCR extraction accuracy because structured values are supplied downstream;
- independent acceptance is pending for all 15 profiles, so shadow-runtime promotion remains blocked;
- manufacturers, series, models and supplier SKUs never become canonical categories;
- series ranges are not proof of a concrete execution's parameters;
- Rosseti registry rows map to operator-approval evidence, not categories or certificates;
- broad family matches and cross-branch ambiguity require review;
- compatibility, completeness and equivalence remain separate concepts;
- role-specific voltage and current fields are not merged automatically;
- the ontology and normative registry are not certification or legal-compliance evidence;
- operator registry inclusion is not universal product equivalence proof;
- standards require scope, edition and amendment verification for each procurement;
- unlicensed full texts, protected source content and secrets are not committed;
- required unknowns remain uncertain rather than guessed;
- live accuracy and compliance claims remain blocked until real truth packs and independent human review are accepted.
