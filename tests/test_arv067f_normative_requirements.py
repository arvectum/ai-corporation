from __future__ import annotations

import importlib.util
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


def _load_requirements() -> tuple[dict, list[dict]]:
    manifest = yaml.safe_load((DIRECTORY / "normative_requirements.v1.yaml").read_text(encoding="utf-8"))
    rows = [row for path in manifest["requirement_files"] for row in yaml.safe_load((DIRECTORY / path).read_text(encoding="utf-8"))["requirements"]]
    return manifest, rows


def test_arv067f_validator_passes() -> None:
    validator = _load_module(DIRECTORY / "validate_normative_requirements.py", "arv067f_validator")
    assert validator.main() == 0


def test_arv067f_has_exact_source_locators_and_review_gates() -> None:
    manifest, rows = _load_requirements()
    assert len(rows) == 22
    assert all(row["page"] >= 1 and len(row["page_text_sha256"]) == 64 for row in rows)
    assert all(row["clause_ref"] and row["source_excerpt"] for row in rows)
    assert all(row["human_review_required"] is True for row in rows)
    assert all(row["automatic_compliance_decision"] is False for row in rows)
    assert manifest["runtime_import"] is False


def test_arv067f_supports_all_required_constraint_and_applicability_types() -> None:
    manifest, rows = _load_requirements()
    assert set(manifest["constraint_types"]) == {"allowed_values","minimum","maximum","range","required_evidence","marking","documentation"}
    assert set(manifest["applicability_priorities"]) == {"mandatory","contractual","operator","recommended"}
    actual = {row["constraint"]["type"] for row in rows}
    assert {"allowed_values","minimum","maximum","required_evidence","marking","documentation"} <= actual


def test_arv067f_evaluator_is_fail_closed_and_never_decides_compliance() -> None:
    evaluator = _load_module(DIRECTORY / "normative_requirement_evaluator.py", "arv067f_evaluator")
    _, rows = _load_requirements(); by_id = {row["id"]: row for row in rows}
    requirement = by_id["NRF-PRI-ROW2-UHL-VOLTAGE"]
    context = {"operator_id":"rossetti","registry_row":2,"model_family":"ВГТ-УЭТМ-330","climate_variant":"УХЛ*"}
    result = evaluator.evaluate_requirement(requirement, {"rated_voltage_kv":330}, context)
    assert result["status"] == "SATISFIED"
    assert result["requires_review"] is True
    assert result["automatic_compliance_decision"] is False
    result = evaluator.evaluate_requirement(requirement, {"rated_voltage_kv":220}, context)
    assert result["status"] == "VIOLATED"
    result = evaluator.evaluate_requirement(requirement, {"rated_voltage_kv":330}, {"operator_id":"other"})
    assert result["status"] == "NOT_APPLICABLE"
    result = evaluator.evaluate_requirement(requirement, {}, context)
    assert result["status"] == "UNCERTAIN"


def test_arv067f_ip_and_version_minimum_comparators() -> None:
    evaluator = _load_module(DIRECTORY / "normative_requirement_evaluator.py", "arv067f_evaluator_min")
    _, rows = _load_requirements(); by_id = {row["id"]: row for row in rows}
    ip = by_id["NRF-SEC-ROW7-IP54"]
    ctx = {"operator_id":"rossetti","registry_row":7,"installation_type":"cabinet"}
    assert evaluator.evaluate_requirement(ip, {"enclosure_ip":"IP55"}, ctx)["status"] == "SATISFIED"
    assert evaluator.evaluate_requirement(ip, {"enclosure_ip":"IP44"}, ctx)["status"] == "VIOLATED"
    version = by_id["NRF-SEC-ROW6-SOFTWARE"]
    vctx = {"operator_id":"rossetti","registry_row":6}
    assert evaluator.evaluate_requirement(version, {"software_version":"v4.10"}, vctx)["status"] == "SATISFIED"
    assert evaluator.evaluate_requirement(version, {"software_version":"v3.99"}, vctx)["status"] == "VIOLATED"


def test_arv067f_runtime_is_not_wired() -> None:
    forbidden = {"normative_requirements.v1.yaml","ARV-067F-ELECTRICAL-NORMATIVE-REQUIREMENTS","normative_requirement_evaluator"}
    hits = []
    src = ROOT / "src"
    if src.exists():
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text: hits.append(f"{path.relative_to(ROOT)}:{token}")
    assert hits == []
