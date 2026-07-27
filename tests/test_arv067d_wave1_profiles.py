from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "schemas" / "categories" / "electrical"
FIXTURES = ROOT / "fixtures" / "ontology" / "electrical"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_profiles() -> tuple[dict, list[dict]]:
    manifest = yaml.safe_load(
        (DIRECTORY / "detailed_profiles_wave1.v1.yaml").read_text(encoding="utf-8")
    )
    profiles = [
        profile
        for relative_path in manifest["profile_files"]
        for profile in yaml.safe_load(
            (DIRECTORY / relative_path).read_text(encoding="utf-8")
        )["profiles"]
    ]
    return manifest, profiles


def _load_fixture_document() -> dict:
    return yaml.safe_load(
        (FIXTURES / "wave1_profile_cases.yaml").read_text(encoding="utf-8")
    )


def _load_fixture_blocks() -> dict[str, dict]:
    fixtures = _load_fixture_document()
    return {block["profile_id"]: block for block in fixtures["profiles"]}


def test_arv067d_wave1_profiles_validate() -> None:
    validator = _load_module(
        DIRECTORY / "validate_wave1_profiles.py",
        "arv067d_validate_wave1_profiles",
    )
    assert validator.main() == 0


def test_arv067d_has_full_wave1_profile_and_fixture_coverage() -> None:
    _, profiles = _load_profiles()
    fixtures = yaml.safe_load(
        (FIXTURES / "wave1_profile_cases.yaml").read_text(encoding="utf-8")
    )
    assert len(profiles) == 15
    assert fixtures["case_count"] == 180
    assert len(fixtures["profiles"]) == 15
    counts = {block["profile_id"]: len(block["cases"]) for block in fixtures["profiles"]}
    assert set(counts) == {profile["id"] for profile in profiles}
    assert set(counts.values()) == {12}


def test_arv067d_each_profile_has_positive_negative_and_uncertain_cases() -> None:
    _, profiles = _load_profiles()
    fixtures = _load_fixture_document()
    templates = {row["id"]: row for row in fixtures["case_templates"]}
    blocks = {block["profile_id"]: block for block in fixtures["profiles"]}
    for profile in profiles:
        statuses = Counter(
            templates[case["template_id"]]["expected_status"]
            for case in blocks[profile["id"]]["cases"]
        )
        assert statuses == {
            "EXACT": 2,
            "LIKELY_ANALOG": 3,
            "PARTIAL": 3,
            "UNCERTAIN": 2,
            "NO_MATCH": 2,
        }


def test_arv067d_matcher_is_explainable_and_fail_closed() -> None:
    matcher = _load_module(
        DIRECTORY / "wave1_profile_matcher.py",
        "arv067d_wave1_profile_matcher",
    )
    validator = _load_module(
        DIRECTORY / "validate_wave1_profiles.py",
        "arv067d_wave1_materializer",
    )
    _, profiles = _load_profiles()
    by_id = {profile["id"]: profile for profile in profiles}
    fixtures = _load_fixture_document()
    templates = {row["id"]: row for row in fixtures["case_templates"]}
    blocks = {block["profile_id"]: block for block in fixtures["profiles"]}

    exact_block = blocks["cable_joint"]
    exact_template = templates["exact_all"]
    exact_candidate = validator.materialize_candidate(
        by_id["cable_joint"], exact_block, exact_template
    )
    result = matcher.evaluate_profile(
        by_id["cable_joint"],
        exact_block["requested"],
        exact_candidate,
        evidence_confirmed=exact_template["evidence_confirmed"],
    )
    assert result["status"] == "EXACT"
    assert exact_template["expected_generic_reason"] in result["reason_codes"]

    mismatch_block = blocks["surge_arrester"]
    mismatch_template = templates["no_match_critical_first"]
    mismatch_candidate = validator.materialize_candidate(
        by_id["surge_arrester"], mismatch_block, mismatch_template
    )
    result = matcher.evaluate_profile(
        by_id["surge_arrester"],
        mismatch_block["requested"],
        mismatch_candidate,
        evidence_confirmed=mismatch_template["evidence_confirmed"],
    )
    assert result["status"] == "NO_MATCH"
    assert "CRITICAL_ATTRIBUTE_MISMATCH" in result["reason_codes"]
    assert result["requires_review"] is True

    evidence_block = blocks["control_relay"]
    evidence_template = templates["uncertain_evidence_missing"]
    evidence_candidate = validator.materialize_candidate(
        by_id["control_relay"], evidence_block, evidence_template
    )
    result = matcher.evaluate_profile(
        by_id["control_relay"],
        evidence_block["requested"],
        evidence_candidate,
        evidence_confirmed=evidence_template["evidence_confirmed"],
    )
    assert result["status"] == "UNCERTAIN"
    assert "PROFILE_EVIDENCE_MISSING" in result["reason_codes"]


def test_arv067d_bindings_are_offline_overlays() -> None:
    bindings = yaml.safe_load(
        (DIRECTORY / "wave1_category_bindings.v1.yaml").read_text(encoding="utf-8")
    )
    assert len(bindings["bindings"]) == 11
    assert all(row["base_lifecycle_status"] == "taxonomy_only" for row in bindings["bindings"])
    assert all(row["effective_lifecycle_status"] == "fixtures_ready" for row in bindings["bindings"])
    assert all(row["human_review_gate"] is True for row in bindings["bindings"])
    assert all(row["production_active"] is False for row in bindings["bindings"])
    assert bindings["governance"]["base_tree_is_immutable_snapshot"] is True


def test_arv067d_production_runtime_is_not_wired() -> None:
    forbidden = {
        "detailed_profiles_wave1.v1.yaml",
        "ARV-067D-ELECTRICAL-DETAILED-PROFILES-WAVE1",
        "wave1_profile_matcher",
    }
    hits: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                hits.append(f"{path.relative_to(ROOT)}:{token}")
    assert hits == []
