#!/usr/bin/env python3
"""Offline deterministic router for ARV-067B category-tree fixtures."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
DEFAULT_TREE = HERE / "category_tree.v1.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name}: root must be an object")
    return value


def load_tree(path: Path = DEFAULT_TREE) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = _load_yaml(path)
    nodes: list[dict[str, Any]] = []
    for relative_path in manifest["node_files"]:
        fragment = _load_yaml(path.parent / relative_path)
        nodes.extend(fragment["nodes"])
    return manifest, nodes


def normalize(text: str) -> str:
    value = text.lower().replace("ё", "е")
    value = re.sub(r"[^0-9a-zа-я]+", " ", value, flags=re.IGNORECASE)
    return " ".join(value.split())


def _contains_boundary(query: str, alias: str) -> bool:
    return f" {alias} " in f" {query} "


def route_category(query: str, nodes: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_query = normalize(query)
    matches: dict[str, tuple[tuple[int, int, int, int], str, dict[str, Any]]] = {}

    for node in nodes:
        routing = node["routing"]
        if routing["block_auto_route"]:
            continue

        best: tuple[tuple[int, int, int, int], str, dict[str, Any]] | None = None
        for raw_alias in routing["aliases"]:
            alias = normalize(raw_alias)
            if len(alias) < 2 or not _contains_boundary(normalized_query, alias):
                continue
            score = (
                1 if normalized_query == alias else 0,
                len(alias.split()),
                int(node["level"]),
                int(routing["priority"]),
            )
            candidate = (score, alias, node)
            if best is None or candidate[0] > best[0]:
                best = candidate

        if best is not None:
            matches[node["category_id"]] = best

    if not matches:
        return {
            "status": "NO_MATCH",
            "category_id": None,
            "matched_alias": None,
            "candidates": [],
            "requires_review": True,
        }

    best_score = max(value[0] for value in matches.values())
    top = [value for value in matches.values() if value[0] == best_score]

    if len(top) > 1:
        return {
            "status": "UNCERTAIN",
            "category_id": None,
            "matched_alias": None,
            "candidates": sorted(value[2]["category_id"] for value in top),
            "requires_review": True,
        }

    _, matched_alias, node = top[0]
    is_leaf = node["node_kind"] == "subcategory"
    return {
        "status": "EXACT" if is_leaf else "FAMILY_MATCH",
        "category_id": node["category_id"],
        "matched_alias": matched_alias,
        "candidates": [node["category_id"]],
        "requires_review": bool(node["routing"]["review_if_only_match"]),
    }
