from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "schemas" / "categories" / "electrical"
VALIDATOR_PATH = DIRECTORY / "validate_category_tree.py"
ROUTER_PATH = DIRECTORY / "category_router.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_tree() -> tuple[dict, list[dict]]:
    manifest = yaml.safe_load(
        (DIRECTORY / "category_tree.v1.yaml").read_text(encoding="utf-8")
    )
    nodes = [
        node
        for relative_path in manifest["node_files"]
        for node in yaml.safe_load(
            (DIRECTORY / relative_path).read_text(encoding="utf-8")
        )["nodes"]
    ]
    return manifest, nodes


def test_arv067b_category_tree_validates() -> None:
    validator = _load_module(VALIDATOR_PATH, "arv067b_validate_category_tree")
    assert validator.main() == 0


def test_arv067b_tree_has_stable_explicit_paths_and_full_coverage() -> None:
    manifest, nodes = _load_tree()
    by_id = {node["category_id"]: node for node in nodes}

    assert len(nodes) == 166
    assert sum(node["node_kind"] == "family" for node in nodes) == 28
    assert sum(node["node_kind"] == "subcategory" for node in nodes) == 135
    assert manifest["root_category_id"] == "electrical"

    for category_id, node in by_id.items():
        assert node["path"][-1] == category_id
        assert len(node["path"]) == node["level"] + 1
        if node["parent_id"] is None:
            assert node["path"] == [category_id]
        else:
            assert node["parent_id"] in by_id
            assert node["path"] == [*by_id[node["parent_id"]]["path"], category_id]


def test_arv067b_inheritance_maps_all_verified_profiles() -> None:
    validator = _load_module(VALIDATOR_PATH, "arv067b_validate_inheritance")
    manifest, nodes = _load_tree()
    by_id = validator.validate_graph(manifest, nodes)
    registered, aliases = validator.load_attribute_contract()
    effective = validator.validate_inheritance(nodes, by_id, registered)

    ontology = yaml.safe_load(
        (DIRECTORY / "electrical.v1.yaml").read_text(encoding="utf-8")
    )
    profile_attributes = {
        category["id"]: {attribute["id"] for attribute in category["attributes"]}
        for category in ontology["categories"]
    }
    mapped = {}
    for node in nodes:
        for raw_profile in node["detailed_profile_refs"]:
            profile = aliases.get(raw_profile, raw_profile)
            mapped[profile] = node["category_id"]
            assert profile_attributes[profile] <= effective[node["category_id"]]

    assert set(mapped) == set(profile_attributes)
    contactor = "electrical.primary.other_primary.contactor"
    assert "rated_current_a" not in effective[contactor]
    assert "rated_operational_current_a" in effective[contactor]


def test_arv067b_router_prefers_specific_leaf_and_surfaces_ambiguity() -> None:
    router = _load_module(ROUTER_PATH, "arv067b_category_router")
    _, nodes = _load_tree()

    exact = router.route_category("Система мониторинга переходных режимов", nodes)
    assert exact["status"] == "EXACT"
    assert (
        exact["category_id"]
        == "electrical.secondary.relay_protection_automation.transient_monitoring_system"
    )
    assert exact["requires_review"] is False

    ambiguous = router.route_category("предохранитель разъединитель", nodes)
    assert ambiguous == {
        "status": "UNCERTAIN",
        "category_id": None,
        "matched_alias": None,
        "candidates": [
            "electrical.primary.disconnectors_earthing_switches",
            "electrical.primary.surge_protection_fuses",
        ],
        "requires_review": True,
    }


def test_arv067b_production_runtime_is_not_wired() -> None:
    forbidden = {
        "category_tree.v1.yaml",
        "ARV-067B-ELECTRICAL-CATEGORY-TREE",
    }
    hits: list[str] = []
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                hits.append(f"{path.relative_to(ROOT)}:{token}")
    assert hits == []
