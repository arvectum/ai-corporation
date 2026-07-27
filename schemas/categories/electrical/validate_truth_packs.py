#!/usr/bin/env python3
"""Validate ARV-067H truth-pack contracts, generated items and release gates."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
FIXTURE_DIR = REPO_ROOT / "fixtures" / "ontology" / "electrical"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from truth_pack_generator import generate_all_packs, iter_items, load_profiles  # noqa: E402
from truth_pack_runner import run_benchmark  # noqa: E402

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
OUTCOMES = {"EXACT", "LIKELY_ANALOG", "PARTIAL", "UNCERTAIN", "NO_MATCH"}


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be object")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be object")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _error(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def load_contract() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = _load_yaml(HERE / "truth_pack_manifest.v1.yaml")
    seed = _load_yaml(HERE / str(manifest["seed_contract_file"]))
    acceptance = _load_yaml(HERE / str(manifest["acceptance_file"]))
    fixtures = _load_yaml(REPO_ROOT / str(manifest["contract_fixture_file"]))
    return manifest, seed, acceptance, fixtures


def validate_schemas(
    manifest: dict[str, Any],
    seed: dict[str, Any],
    acceptance: dict[str, Any],
    fixtures: dict[str, Any],
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    pairs = [
        ("truth_pack_manifest.schema.json", manifest),
        ("truth_pack_seed_contract.schema.json", seed),
        ("truth_pack_acceptance.schema.json", acceptance),
        ("truth_pack_contract_cases.schema.json", fixtures),
    ]
    for schema_name, document in pairs:
        schema = _load_json(HERE / schema_name)
        try:
            Draft202012Validator.check_schema(schema)
            Draft202012Validator(schema).validate(document)
        except Exception as exc:  # jsonschema gives precise path in the message
            errors.append(_error("TP_SCHEMA_VALIDATION_FAILED", f"{schema_name}: {exc}"))
    return errors


def _acceptance_errors(
    manifest: dict[str, Any],
    seed: dict[str, Any],
    acceptance: dict[str, Any],
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    rows = acceptance["profiles"]
    profile_ids = [str(row["profile_id"]) for row in rows]
    if set(profile_ids) != set(str(value) for value in seed["profiles"]):
        errors.append(_error("TP_ACCEPTANCE_PROFILE_SET_MISMATCH", "acceptance profile set differs from seed"))
    if len(profile_ids) != len(set(profile_ids)):
        errors.append(_error("TP_ACCEPTANCE_PROFILE_SET_MISMATCH", "duplicate acceptance profile"))

    statuses = Counter(str(row["acceptance_status"]) for row in rows)
    summary = acceptance["summary"]
    if int(summary["profile_count"]) != len(rows):
        errors.append(_error("TP_ACCEPTANCE_SUMMARY_MISMATCH", "profile_count"))
    for key, status in (
        ("accepted_profiles", "accepted"),
        ("pending_profiles", "pending"),
        ("rejected_profiles", "rejected"),
    ):
        if int(summary[key]) != statuses.get(status, 0):
            errors.append(_error("TP_ACCEPTANCE_SUMMARY_MISMATCH", key))

    all_accepted = bool(rows) and all(row["acceptance_status"] == "accepted" for row in rows)
    if bool(summary["independent_acceptance_complete"]) != all_accepted:
        errors.append(_error("TP_ACCEPTANCE_SUMMARY_MISMATCH", "independent_acceptance_complete"))

    policy = manifest["acceptance_policy"]
    machine_actor = str(acceptance["machine_generator_actor_id"])
    for row in rows:
        profile_id = str(row["profile_id"])
        status = str(row["acceptance_status"])
        primary = row.get("primary_annotator_id")
        acceptor = row.get("acceptance_annotator_id")
        if status != "accepted":
            continue
        if not primary or not acceptor or primary == acceptor:
            errors.append(_error("TP_ACCEPTANCE_ANNOTATORS_NOT_INDEPENDENT", profile_id))
        if primary == machine_actor or acceptor == machine_actor:
            errors.append(_error("TP_MACHINE_SELF_ACCEPTANCE_FORBIDDEN", profile_id))
        if not row.get("accepted_at"):
            errors.append(_error("TP_ACCEPTANCE_DATE_REQUIRED", profile_id))
        if not SHA256_RE.fullmatch(str(row.get("acceptance_hash") or "")):
            errors.append(_error("TP_ACCEPTANCE_HASH_REQUIRED", profile_id))
        if int(row.get("audited_item_count", 0)) < int(policy["accepted_pack_requires_minimum_audit_sample"]):
            errors.append(_error("TP_ACCEPTANCE_AUDIT_SAMPLE_TOO_SMALL", profile_id))
        disagreement = row.get("disagreement_rate")
        if disagreement is None or float(disagreement) > float(policy["accepted_pack_max_disagreement_rate"]):
            errors.append(_error("TP_ACCEPTANCE_DISAGREEMENT_TOO_HIGH", profile_id))
        if not str(row.get("rationale") or "").strip():
            errors.append(_error("TP_ACCEPTANCE_RATIONALE_REQUIRED", profile_id))
    return errors


def validate_dataset(
    manifest: dict[str, Any],
    seed: dict[str, Any],
    acceptance: dict[str, Any],
    packs: list[dict[str, Any]],
) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    profiles = load_profiles(seed)
    expected_profile_ids = [str(value) for value in seed["profiles"]]
    pack_ids = [str(pack["profile_id"]) for pack in packs]

    if manifest.get("runtime_import") is not False:
        errors.append(_error("TP_RUNTIME_IMPORT_FORBIDDEN", "runtime_import must be false"))
    if manifest["governance"].get("production_accuracy_claims_forbidden_before_independent_acceptance") is not True:
        errors.append(_error("TP_PRODUCTION_ACCURACY_CLAIM_FORBIDDEN", "claim gate disabled"))
    if len(packs) != int(manifest["profile_count"]):
        errors.append(_error("TP_PROFILE_COUNT_MISMATCH", str(len(packs))))
    if set(pack_ids) != set(expected_profile_ids) or len(pack_ids) != len(set(pack_ids)):
        errors.append(_error("TP_PROFILE_SET_MISMATCH", "pack profile set differs from seed"))

    all_items = list(iter_items(packs))
    if len(all_items) != int(manifest["total_items"]):
        errors.append(_error("TP_TOTAL_ITEM_COUNT_MISMATCH", str(len(all_items))))

    item_ids: list[str] = []
    item_hashes: list[str] = []
    source_ids: list[str] = []
    surface_texts: list[str] = []
    manufacturers: dict[str, set[str]] = {"train": set(), "dev": set(), "test": set()}

    for pack in packs:
        profile_id = str(pack["profile_id"])
        if profile_id not in profiles:
            errors.append(_error("TP_UNKNOWN_PROFILE", profile_id))
            continue
        items = list(pack["items"])
        positive_count = sum(bool(item["truth"]["positive"]) for item in items)
        hard_negative_count = sum(bool(item["truth"]["hard_negative"]) for item in items)
        if positive_count < int(manifest["positive_items_per_profile"]):
            errors.append(_error("TP_POSITIVE_MINIMUM_NOT_MET", profile_id))
        if hard_negative_count < int(manifest["hard_negative_items_per_profile"]):
            errors.append(_error("TP_HARD_NEGATIVE_MINIMUM_NOT_MET", profile_id))
        if len(items) != int(manifest["items_per_profile"]):
            errors.append(_error("TP_PROFILE_ITEM_COUNT_MISMATCH", profile_id))
        outcome_counts = Counter(str(item["truth"]["expected_outcome"]) for item in items)
        if dict(outcome_counts) != dict(manifest["outcome_targets_per_profile"]):
            errors.append(_error("TP_OUTCOME_DISTRIBUTION_MISMATCH", profile_id))
        split_counts = Counter(str(item["split"]) for item in items)
        expected_splits = {
            split: int(value["items_per_profile"])
            for split, value in manifest["split_policy"].items()
        }
        if dict(split_counts) != expected_splits:
            errors.append(_error("TP_SPLIT_DISTRIBUTION_MISMATCH", profile_id))
        present_slices = {str(label) for item in items for label in item["slice_labels"]}
        for required_slice in manifest["required_slices"]:
            if required_slice not in present_slices:
                errors.append(_error("TP_REQUIRED_SLICE_MISSING", f"{profile_id}:{required_slice}"))

        recomputed_hashes: list[str] = []
        for item in items:
            item_id = str(item["item_id"])
            item_ids.append(item_id)
            item_hash = str(item["item_hash"])
            item_hashes.append(item_hash)
            source_ids.append(str(item["source_record_id"]))
            surface_texts.append(str(item["surface_text"]))
            split = str(item["split"])
            manufacturers.setdefault(split, set()).add(str(item["manufacturer_id"]))
            if str(item["profile_id"]) != profile_id:
                errors.append(_error("TP_ITEM_PROFILE_MISMATCH", item_id))
            if str(item["truth"]["expected_outcome"]) not in OUTCOMES:
                errors.append(_error("TP_UNKNOWN_OUTCOME", item_id))
            trace = item["trace"]
            if (
                trace.get("benchmark_id") != manifest["benchmark_id"]
                or trace.get("benchmark_version") != manifest["version"]
                or trace.get("ontology_registry_id") != manifest["ontology_registry_id"]
                or trace.get("ontology_version") != manifest["ontology_version"]
            ):
                errors.append(_error("TP_ONTOLOGY_TRACE_MISMATCH", item_id))
            payload = copy.deepcopy(item)
            payload.pop("item_hash", None)
            recomputed = _sha256_text(_canonical_json(payload))
            recomputed_hashes.append(recomputed)
            if item_hash != recomputed:
                errors.append(_error("TP_ITEM_HASH_MISMATCH", item_id))
        expected_root = _sha256_text("\n".join(sorted(recomputed_hashes)))
        if str(pack.get("pack_root_hash")) != expected_root:
            errors.append(_error("TP_PACK_ROOT_HASH_MISMATCH", profile_id))

    if len(item_ids) != len(set(item_ids)):
        errors.append(_error("TP_DUPLICATE_ITEM_ID", "duplicate item id"))
    if len(item_hashes) != len(set(item_hashes)):
        errors.append(_error("TP_DUPLICATE_ITEM_HASH", "duplicate item hash"))
    if len(source_ids) != len(set(source_ids)):
        errors.append(_error("TP_DUPLICATE_SOURCE_RECORD", "duplicate source record"))
    if len(surface_texts) != len(set(surface_texts)):
        errors.append(_error("TP_DUPLICATE_SURFACE_TEXT", "duplicate surface text"))
    if manufacturers.get("test", set()) & (
        manufacturers.get("train", set()) | manufacturers.get("dev", set())
    ):
        errors.append(_error("TP_TEST_MANUFACTURER_LEAKAGE", "test manufacturer appears in train/dev"))

    errors.extend(_acceptance_errors(manifest, seed, acceptance))
    return errors


def _set_valid_acceptance(row: dict[str, Any]) -> None:
    row["acceptance_status"] = "accepted"
    row["primary_annotator_id"] = "expert.primary"
    row["acceptance_annotator_id"] = "expert.acceptance"
    row["accepted_at"] = "2026-07-27T12:00:00Z"
    row["acceptance_hash"] = "a" * 64
    row["audited_item_count"] = 32
    row["disagreement_rate"] = 0.01
    row["rationale"] = "Independent acceptance fixture."


def apply_mutation(
    manifest: dict[str, Any],
    seed: dict[str, Any],
    acceptance: dict[str, Any],
    packs: list[dict[str, Any]],
    case: dict[str, Any],
) -> None:
    mutation = str(case["mutation"])
    target_id = case.get("target_id")
    if mutation == "none":
        return
    pack = next((row for row in packs if row["profile_id"] == target_id), packs[0])
    acceptance_row = next(
        (row for row in acceptance["profiles"] if row["profile_id"] == target_id),
        acceptance["profiles"][0],
    )
    if mutation == "wrong_profile_count":
        manifest["profile_count"] += 1
    elif mutation == "insufficient_positive":
        pack["items"] = [item for item in pack["items"] if not item["truth"]["positive"]] + [
            item for item in pack["items"] if item["truth"]["positive"]
        ][:99]
    elif mutation == "insufficient_hard_negative":
        negatives = [item for item in pack["items"] if item["truth"]["hard_negative"]][:49]
        positives = [item for item in pack["items"] if item["truth"]["positive"]]
        pack["items"] = positives + negatives
    elif mutation == "wrong_total_items":
        manifest["total_items"] += 1
    elif mutation == "unknown_profile":
        pack["profile_id"] = "unknown_profile"
    elif mutation == "duplicate_item_id":
        pack["items"][1]["item_id"] = pack["items"][0]["item_id"]
    elif mutation == "duplicate_item_hash":
        pack["items"][1]["item_hash"] = pack["items"][0]["item_hash"]
    elif mutation == "duplicate_source_record":
        pack["items"][1]["source_record_id"] = pack["items"][0]["source_record_id"]
    elif mutation == "duplicate_surface_text":
        pack["items"][1]["surface_text"] = pack["items"][0]["surface_text"]
    elif mutation == "test_manufacturer_leak":
        test_item = next(item for item in pack["items"] if item["split"] == "test")
        train_item = next(item for item in pack["items"] if item["split"] == "train")
        test_item["manufacturer_id"] = train_item["manufacturer_id"]
    elif mutation == "bad_item_hash":
        pack["items"][0]["item_hash"] = "0" * 64
    elif mutation == "bad_pack_root":
        pack["pack_root_hash"] = "0" * 64
    elif mutation == "missing_ocr_slice":
        for item in pack["items"]:
            item["slice_labels"] = [label for label in item["slice_labels"] if label != "ocr"]
    elif mutation == "missing_unseen_slice":
        for item in pack["items"]:
            item["slice_labels"] = [
                label for label in item["slice_labels"] if label != "unseen_manufacturer"
            ]
    elif mutation == "wrong_ontology_trace":
        pack["items"][0]["trace"]["ontology_version"] = "0.0.0"
    elif mutation == "outcome_distribution_mismatch":
        pack["items"][0]["truth"]["expected_outcome"] = "LIKELY_ANALOG"
    elif mutation == "acceptance_profile_mismatch":
        acceptance_row["profile_id"] = "unknown_profile"
    elif mutation == "accepted_same_annotator":
        _set_valid_acceptance(acceptance_row)
        acceptance_row["acceptance_annotator_id"] = acceptance_row["primary_annotator_id"]
    elif mutation == "accepted_missing_hash":
        _set_valid_acceptance(acceptance_row)
        acceptance_row["acceptance_hash"] = None
    elif mutation == "accepted_small_audit":
        _set_valid_acceptance(acceptance_row)
        acceptance_row["audited_item_count"] = 31
    elif mutation == "accepted_high_disagreement":
        _set_valid_acceptance(acceptance_row)
        acceptance_row["disagreement_rate"] = 0.03
    elif mutation == "machine_self_acceptance":
        _set_valid_acceptance(acceptance_row)
        acceptance_row["primary_annotator_id"] = acceptance["machine_generator_actor_id"]
    elif mutation == "production_claims_enabled":
        manifest["governance"]["production_accuracy_claims_forbidden_before_independent_acceptance"] = False
    elif mutation == "runtime_import_enabled":
        manifest["runtime_import"] = True
    else:
        raise ValueError(f"unknown mutation: {mutation}")


def main() -> int:
    try:
        manifest, seed, acceptance, fixtures = load_contract()
        schema_errors = validate_schemas(manifest, seed, acceptance, fixtures)
        if schema_errors:
            raise AssertionError(schema_errors)
        first = generate_all_packs()
        second = generate_all_packs()
        first_roots = [pack["pack_root_hash"] for pack in first]
        second_roots = [pack["pack_root_hash"] for pack in second]
        if first_roots != second_roots:
            raise AssertionError("truth-pack generation is not deterministic")
        errors = validate_dataset(manifest, seed, acceptance, first)
        if errors:
            raise AssertionError(errors)
        report = run_benchmark(first)
        if not report["gates"]["metrics_passed"]:
            raise AssertionError("synthetic contract metric gates must pass")
        if not report["gates"]["leakage_passed"]:
            raise AssertionError("exact leakage gates must pass")
        if report["gates"]["independent_acceptance_passed"]:
            raise AssertionError("independent acceptance must remain pending")
        if report["status"] != "RELEASE_BLOCKED":
            raise AssertionError("release must remain blocked before acceptance")
        for case in fixtures["cases"]:
            m = copy.deepcopy(manifest)
            s = copy.deepcopy(seed)
            a = copy.deepcopy(acceptance)
            p = copy.deepcopy(first)
            apply_mutation(m, s, a, p, case)
            codes = {row["code"] for row in validate_dataset(m, s, a, p)}
            expected = set(case["expected_error_codes"])
            if not expected <= codes:
                raise AssertionError(
                    f"{case['id']}: expected {sorted(expected)}, got {sorted(codes)}"
                )
            if not expected and codes:
                raise AssertionError(f"{case['id']}: unexpected {sorted(codes)}")
        items = list(iter_items(first))
        ocr_count = sum("ocr" in item["slice_labels"] for item in items)
        unseen_count = sum("unseen_manufacturer" in item["slice_labels"] for item in items)
        print(
            "ARV-067H truth packs: OK "
            f"(profiles={len(first)}, items={len(items)}, positive={report['positive_count']}, "
            f"hard_negative={report['hard_negative_count']}, ocr={ocr_count}, "
            f"unseen_manufacturer={unseen_count}, fixture_cases={len(fixtures['cases'])}, "
            "independent_acceptance=false, release=BLOCKED, runtime_import=false)"
        )
        return 0
    except Exception as exc:
        print(f"ARV-067H truth packs: FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
