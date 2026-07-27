#!/usr/bin/env python3
"""Deterministically materialize ARV-067H synthetic candidate truth packs.

The generated packs test the matcher contract and release plumbing. They are not
independently accepted human truth and cannot support production accuracy claims.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import yaml

HERE = Path(__file__).resolve().parent


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be an object")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_seed() -> dict[str, Any]:
    return _load_yaml(HERE / "truth_pack_seed_contract.v1.yaml")


def load_profiles(seed: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    seed = seed or load_seed()
    registry = _load_yaml(HERE / str(seed["profile_registry_file"]))
    profiles: dict[str, dict[str, Any]] = {}
    for relative_path in registry["profile_files"]:
        fragment = _load_yaml(HERE / str(relative_path))
        for profile in fragment["profiles"]:
            profile_id = str(profile["id"])
            if profile_id in profiles:
                raise ValueError(f"duplicate profile: {profile_id}")
            profiles[profile_id] = profile
    return profiles


def _base_pair(rule: dict[str, Any]) -> tuple[Any, Any]:
    comparator = str(rule["comparator"])
    if comparator == "exact":
        return f"truth_{rule['id']}", f"truth_{rule['id']}"
    if comparator == "minimum":
        return 10, 10
    if comparator == "maximum":
        return 10, 10
    if comparator == "contains":
        return [f"truth_{rule['id']}"], [f"truth_{rule['id']}"]
    if comparator == "range_overlap":
        return [10, 20], [10, 20]
    raise ValueError(f"unsupported comparator: {comparator}")


def _mismatch_candidate(rule: dict[str, Any], requested: Any) -> Any:
    comparator = str(rule["comparator"])
    if comparator == "exact":
        return f"mismatch_{rule['id']}"
    if comparator == "minimum":
        return float(requested) - 1
    if comparator == "maximum":
        return float(requested) + 1
    if comparator == "contains":
        return [f"mismatch_{rule['id']}"]
    if comparator == "range_overlap":
        return [30, 40]
    raise ValueError(f"unsupported comparator: {comparator}")


def _select_rules(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    critical = [row for row in profile["attributes"] if bool(row["critical"])]
    required_noncritical = [
        row
        for row in profile["attributes"]
        if row["requirement"] == "required" and not bool(row["critical"])
    ]
    optional = [row for row in profile["attributes"] if row["requirement"] == "optional"]
    if not critical:
        raise ValueError(f"{profile['id']}: critical attribute required")
    if not required_noncritical:
        raise ValueError(f"{profile['id']}: required noncritical attribute required")
    if not optional:
        raise ValueError(f"{profile['id']}: optional attribute required")
    return critical[0], required_noncritical[0], optional[0]


def _structured_input(profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    requested: dict[str, Any] = {}
    candidate: dict[str, Any] = {}
    for rule in profile["attributes"]:
        required_value, candidate_value = _base_pair(rule)
        requested[str(rule["id"])] = required_value
        candidate[str(rule["id"])] = candidate_value
    return requested, candidate


def _apply_outcome(
    profile: dict[str, Any],
    outcome: str,
    ordinal: int,
    requested: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[bool, dict[str, list[str]]]:
    critical, required_noncritical, optional = _select_rules(profile)
    expected = {
        "critical_mismatch_attributes": [],
        "critical_missing_attributes": [],
        "required_issue_attributes": [],
        "optional_issue_attributes": [],
    }
    evidence_confirmed = True
    if outcome == "EXACT":
        return evidence_confirmed, expected
    if outcome == "LIKELY_ANALOG":
        attribute_id = str(optional["id"])
        candidate.pop(attribute_id)
        expected["optional_issue_attributes"] = [attribute_id]
        return evidence_confirmed, expected
    if outcome == "PARTIAL":
        attribute_id = str(required_noncritical["id"])
        candidate.pop(attribute_id)
        expected["required_issue_attributes"] = [attribute_id]
        return evidence_confirmed, expected
    if outcome == "UNCERTAIN":
        if ordinal % 2 == 0:
            attribute_id = str(critical["id"])
            candidate.pop(attribute_id)
            expected["critical_missing_attributes"] = [attribute_id]
        else:
            evidence_confirmed = False
        return evidence_confirmed, expected
    if outcome == "NO_MATCH":
        attribute_id = str(critical["id"])
        candidate[attribute_id] = _mismatch_candidate(critical, requested[attribute_id])
        expected["critical_mismatch_attributes"] = [attribute_id]
        return evidence_confirmed, expected
    raise ValueError(f"unsupported outcome: {outcome}")


def _surface_text(
    *,
    profile: dict[str, Any],
    manufacturer_id: str,
    source_record_id: str,
    source_format: str,
    noise_mode: str,
    candidate: dict[str, Any],
) -> str:
    alias = str(profile["aliases"][0])
    attributes = "; ".join(
        f"{key}={json.dumps(value, ensure_ascii=False, sort_keys=True)}"
        for key, value in sorted(candidate.items())
    )
    if source_format == "plain_text":
        text = f"{manufacturer_id}. {alias}. {attributes}. Источник {source_record_id}."
    elif source_format == "technical_spec_table":
        rows = "\n".join(
            f"| {key} | {json.dumps(value, ensure_ascii=False, sort_keys=True)} |"
            for key, value in sorted(candidate.items())
        )
        text = (
            f"Техническое задание: {alias}\n"
            f"Изготовитель: {manufacturer_id}\n"
            "| Параметр | Значение |\n|---|---|\n"
            f"{rows}\nСтрока: {source_record_id}"
        )
    elif source_format == "catalog_card":
        text = (
            f"КАТАЛОЖНАЯ КАРТОЧКА / {manufacturer_id} / {alias.upper()} / "
            f"{attributes} / SKU {source_record_id}"
        )
    elif source_format == "ocr_scan":
        text = f"СКАН {manufacturer_id} {alias} {attributes} {source_record_id}"
    else:
        raise ValueError(f"unsupported source format: {source_format}")

    if noise_mode == "clean":
        return text
    if noise_mode == "abbreviation":
        return text.replace("Техническое задание", "ТЗ").replace("Изготовитель", "Изг.")
    if noise_mode == "punctuation_loss":
        return " ".join(text.replace(";", " ").replace(".", " ").replace("|", " ").split())
    if noise_mode == "ocr_confusion":
        replacements = {"O": "0", "I": "1", "l": "1", "В": "B", "А": "A"}
        return "".join(replacements.get(char, char) for char in text)
    raise ValueError(f"unsupported noise mode: {noise_mode}")


def generate_profile_pack(
    profile: dict[str, Any],
    seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    seed = seed or load_seed()
    source_formats = list(seed["source_formats"])
    noise_modes = list(seed["noise_modes"])
    items: list[dict[str, Any]] = []
    global_ordinal = 0
    for split, outcome_counts in seed["split_outcome_counts"].items():
        manufacturers = list(seed["manufacturers_by_split"][split])
        for outcome, count in outcome_counts.items():
            for outcome_ordinal in range(int(count)):
                requested, candidate = _structured_input(profile)
                evidence_confirmed, expected_issues = _apply_outcome(
                    profile,
                    str(outcome),
                    outcome_ordinal,
                    requested,
                    candidate,
                )
                manufacturer_id = str(manufacturers[global_ordinal % len(manufacturers)])
                source_format = str(source_formats[outcome_ordinal % len(source_formats)])
                noise_mode = str(noise_modes[outcome_ordinal % len(noise_modes)])
                source_record_id = (
                    f"SRC-{profile['id']}-{split}-{str(outcome).lower()}-{outcome_ordinal + 1:03d}"
                )
                item_id = (
                    f"TP-{profile['id']}-{split}-{str(outcome).lower()}-"
                    f"{outcome_ordinal + 1:03d}"
                )
                slices = ["all"]
                if source_format == "ocr_scan":
                    slices.append("ocr")
                if split == "test":
                    slices.append("unseen_manufacturer")
                item: dict[str, Any] = {
                    "item_id": item_id,
                    "profile_id": profile["id"],
                    "truth_category_id": profile["target_category_id"],
                    "split": split,
                    "manufacturer_id": manufacturer_id,
                    "manufacturer_seen_in_train": split != "test",
                    "source_record_id": source_record_id,
                    "source_format": source_format,
                    "noise_mode": noise_mode,
                    "slice_labels": slices,
                    "surface_text": _surface_text(
                        profile=profile,
                        manufacturer_id=manufacturer_id,
                        source_record_id=source_record_id,
                        source_format=source_format,
                        noise_mode=noise_mode,
                        candidate=candidate,
                    ),
                    "input": {
                        "requested": requested,
                        "candidate": candidate,
                        "evidence_confirmed": evidence_confirmed,
                    },
                    "truth": {
                        "expected_outcome": outcome,
                        "positive": outcome != "NO_MATCH",
                        "hard_negative": outcome == "NO_MATCH",
                        **expected_issues,
                    },
                    "trace": {
                        "benchmark_id": "ARV-067H-ELECTRICAL-TRUTH-PACK-BENCHMARK",
                        "benchmark_version": "1.0.0",
                        "ontology_registry_id": "ARV-067D-ELECTRICAL-DETAILED-PROFILES-WAVE1",
                        "ontology_version": "1.0.0",
                        "profile_fragment_id": profile.get("domain"),
                        "generator_version": seed["generator_version"],
                        "synthetic_contract_item": True,
                        "independent_acceptance_status": "pending",
                    },
                }
                item["item_hash"] = _sha256_text(_canonical_json(item))
                items.append(item)
                global_ordinal += 1
    item_hashes = sorted(str(item["item_hash"]) for item in items)
    pack_root_hash = _sha256_text("\n".join(item_hashes))
    return {
        "pack_id": f"ARV-067H-{profile['id'].upper()}-PACK",
        "version": "1.0.0",
        "profile_id": profile["id"],
        "target_category_id": profile["target_category_id"],
        "status": "candidate_pending_independent_acceptance",
        "item_count": len(items),
        "positive_count": sum(bool(item["truth"]["positive"]) for item in items),
        "hard_negative_count": sum(bool(item["truth"]["hard_negative"]) for item in items),
        "pack_root_hash": pack_root_hash,
        "items": items,
    }


def generate_all_packs() -> list[dict[str, Any]]:
    seed = load_seed()
    profiles = load_profiles(seed)
    missing = sorted(set(seed["profiles"]) - set(profiles))
    if missing:
        raise ValueError(f"unknown seed profiles: {missing}")
    return [generate_profile_pack(profiles[profile_id], seed) for profile_id in seed["profiles"]]


def iter_items(packs: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for pack in packs:
        yield from pack["items"]


def build_index(packs: list[dict[str, Any]]) -> dict[str, Any]:
    items = list(iter_items(packs))
    return {
        "index_id": "ARV-067H-TRUTH-PACK-INDEX",
        "version": "1.0.0",
        "status": "candidate_pending_independent_acceptance",
        "profile_count": len(packs),
        "item_count": len(items),
        "positive_count": sum(bool(item["truth"]["positive"]) for item in items),
        "hard_negative_count": sum(bool(item["truth"]["hard_negative"]) for item in items),
        "pack_roots": [
            {
                "profile_id": pack["profile_id"],
                "pack_root_hash": pack["pack_root_hash"],
                "item_count": pack["item_count"],
            }
            for pack in packs
        ],
        "production_accuracy_claims_allowed": False,
        "independent_acceptance_complete": False,
    }


def materialize(output_dir: Path) -> dict[str, Any]:
    packs = generate_all_packs()
    output_dir.mkdir(parents=True, exist_ok=True)
    for pack in packs:
        path = output_dir / f"{pack['profile_id']}.jsonl"
        lines = [_canonical_json(item) for item in pack["items"]]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    index = build_index(packs)
    (output_dir / "truth_pack_index.v1.yaml").write_text(
        yaml.safe_dump(index, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return index


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    packs = generate_all_packs()
    index = build_index(packs)
    if args.output_dir:
        index = materialize(args.output_dir)
    print(yaml.safe_dump(index, allow_unicode=True, sort_keys=False).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
