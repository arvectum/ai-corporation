from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "schemas" / "categories" / "electrical"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_arv067h_validator_passes() -> None:
    validator = _load_module(
        DIRECTORY / "validate_truth_packs.py",
        "arv067h_validator",
    )
    assert validator.main() == 0


def test_arv067h_generation_is_deterministic_and_meets_volume_contract() -> None:
    generator = _load_module(
        DIRECTORY / "truth_pack_generator.py",
        "arv067h_generator",
    )
    first = generator.generate_all_packs()
    second = generator.generate_all_packs()
    assert len(first) == 15
    assert [pack["pack_root_hash"] for pack in first] == [
        pack["pack_root_hash"] for pack in second
    ]
    assert len({pack["pack_root_hash"] for pack in first}) == 15
    for pack in first:
        assert pack["item_count"] == 160
        assert pack["positive_count"] == 100
        assert pack["hard_negative_count"] == 60
        assert len(pack["pack_root_hash"]) == 64
        assert Counter(item["split"] for item in pack["items"]) == {
            "train": 80,
            "dev": 40,
            "test": 40,
        }
        assert Counter(item["truth"]["expected_outcome"] for item in pack["items"]) == {
            "EXACT": 35,
            "LIKELY_ANALOG": 25,
            "PARTIAL": 20,
            "UNCERTAIN": 20,
            "NO_MATCH": 60,
        }


def test_arv067h_exact_leakage_and_unseen_manufacturer_isolation() -> None:
    generator = _load_module(
        DIRECTORY / "truth_pack_generator.py",
        "arv067h_generator_leakage",
    )
    items = list(generator.iter_items(generator.generate_all_packs()))
    assert len(items) == 2400
    assert len({item["item_id"] for item in items}) == 2400
    assert len({item["item_hash"] for item in items}) == 2400
    assert len({item["source_record_id"] for item in items}) == 2400
    assert len({item["surface_text"] for item in items}) == 2400
    train_dev = {
        item["manufacturer_id"]
        for item in items
        if item["split"] in {"train", "dev"}
    }
    test = {item["manufacturer_id"] for item in items if item["split"] == "test"}
    assert train_dev.isdisjoint(test)
    assert sum("ocr" in item["slice_labels"] for item in items) >= 500
    assert sum("unseen_manufacturer" in item["slice_labels"] for item in items) == 600


def test_arv067h_runner_passes_contract_metrics_but_blocks_release() -> None:
    runner = _load_module(
        DIRECTORY / "truth_pack_runner.py",
        "arv067h_runner",
    )
    report = runner.run_benchmark()
    assert report["status"] == "RELEASE_BLOCKED"
    assert report["accuracy_claim_status"] == "synthetic_contract_replay_only"
    assert report["gates"]["metrics_passed"] is True
    assert report["gates"]["leakage_passed"] is True
    assert report["gates"]["pack_roots_valid"] is True
    assert report["gates"]["independent_acceptance_passed"] is False
    assert report["gates"]["release_gate_passed"] is False
    assert report["metrics"]["category_precision"] == 1.0
    assert report["metrics"]["category_recall"] == 0.8
    assert report["metrics"]["false_exact_rate_on_hard_negatives"] == 0.0
    assert report["metrics"]["false_analog_rate_on_hard_negatives"] == 0.0
    assert report["metrics"]["critical_mismatch_recall"] == 1.0
    assert report["production_accuracy_claims_allowed"] is False


def test_arv067h_materialization_is_reproducible(tmp_path: Path) -> None:
    generator = _load_module(
        DIRECTORY / "truth_pack_generator.py",
        "arv067h_generator_materialize",
    )
    index = generator.materialize(tmp_path)
    assert index["profile_count"] == 15
    assert index["item_count"] == 2400
    assert index["positive_count"] == 1500
    assert index["hard_negative_count"] == 900
    assert index["independent_acceptance_complete"] is False
    jsonl_files = sorted(tmp_path.glob("*.jsonl"))
    assert len(jsonl_files) == 15
    assert all(len(path.read_text(encoding="utf-8").splitlines()) == 160 for path in jsonl_files)
    stored_index = yaml.safe_load(
        (tmp_path / "truth_pack_index.v1.yaml").read_text(encoding="utf-8")
    )
    assert stored_index == index


def test_arv067h_does_not_invent_independent_acceptance() -> None:
    acceptance = yaml.safe_load(
        (DIRECTORY / "truth_pack_acceptance.v1.yaml").read_text(encoding="utf-8")
    )
    assert acceptance["summary"]["accepted_profiles"] == 0
    assert acceptance["summary"]["pending_profiles"] == 15
    assert acceptance["summary"]["independent_acceptance_complete"] is False
    assert all(row["acceptance_status"] == "pending" for row in acceptance["profiles"])
    assert all(row["primary_annotator_id"] is None for row in acceptance["profiles"])
    assert all(row["acceptance_annotator_id"] is None for row in acceptance["profiles"])


def test_arv067h_runtime_is_not_wired() -> None:
    forbidden = {
        "truth_pack_manifest.v1.yaml",
        "ARV-067H-ELECTRICAL-TRUTH-PACK-BENCHMARK",
        "truth_pack_runner",
    }
    hits: list[str] = []
    source_root = ROOT / "src"
    if source_root.exists():
        for path in source_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    hits.append(f"{path.relative_to(ROOT)}:{token}")
    assert hits == []
