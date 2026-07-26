"""CI gate for the isolated ARV-067 electrical ontology data asset."""

from __future__ import annotations

import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_PATH = REPO_ROOT / "schemas" / "categories" / "electrical" / "validate.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("arv067_electrical_validator", VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_arv067_electrical_ontology_contract() -> None:
    result = _load_validator().run_validation()
    assert result == {
        "ontology_id": "ARV-067-ELECTRICAL",
        "version": "1.0.0",
        "categories": 4,
        "synonym_cases": 8,
        "match_cases": 13,
        "labels": ["EXACT", "LIKELY_ANALOG", "NO_MATCH", "PARTIAL", "UNCERTAIN"],
        "runtime_import": False,
    }


def test_arv067_is_not_wired_into_production_runtime() -> None:
    ontology_path = REPO_ROOT / "schemas" / "categories" / "electrical" / "electrical.v1.yaml"
    source_files = list((REPO_ROOT / "src").rglob("*.py"))
    prohibited_references = {
        "ARV-067-ELECTRICAL",
        "electrical.v1.yaml",
        "fixtures/ontology/electrical",
        str(ontology_path.relative_to(REPO_ROOT)),
    }
    references = []
    for path in source_files:
        content = path.read_text(encoding="utf-8")
        if any(reference in content for reference in prohibited_references):
            references.append(path.relative_to(REPO_ROOT))
    assert references == []
