from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "schemas" / "categories" / "electrical"


def _load_module(path: Path, name: str):
    sys.path.insert(0, str(DIRECTORY))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def _load_dataset():
    manifest = yaml.safe_load((DIRECTORY / "provenance_registry.v1.yaml").read_text(encoding="utf-8"))
    sources = yaml.safe_load((DIRECTORY / manifest["source_file"]).read_text(encoding="utf-8"))["sources"]
    claims = [
        row
        for path in manifest["claim_files"]
        for row in yaml.safe_load((DIRECTORY / path).read_text(encoding="utf-8"))["claims"]
    ]
    events = yaml.safe_load((DIRECTORY / manifest["review_event_file"]).read_text(encoding="utf-8"))["events"]
    conflicts = yaml.safe_load((DIRECTORY / manifest["conflict_file"]).read_text(encoding="utf-8"))["conflicts"]
    report = yaml.safe_load((DIRECTORY / manifest["audit_report_file"]).read_text(encoding="utf-8"))
    return manifest, sources, claims, events, conflicts, report


def test_arv067g_validator_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(DIRECTORY / "validate_provenance.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ARV-067G provenance: OK" in result.stdout


def test_arv067g_covers_all_required_claim_types() -> None:
    manifest, sources, claims, events, conflicts, report = _load_dataset()
    assert len(sources) == 6
    assert len(claims) == 24
    assert len(events) == 24
    assert conflicts == []
    assert {row["claim_type"] for row in claims} == set(manifest["claim_types"])
    assert report["counts"]["claims"] == 24


def test_arv067g_review_history_is_hash_chained_and_current() -> None:
    contract = _load_module(DIRECTORY / "provenance_contract.py", "arv067g_contract")
    _, _, claims, events, _, _ = _load_dataset()
    claim_by_id = {row["claim_id"]: row for row in claims}
    for event in events:
        assert event["event_hash"] == contract.canonical_hash(contract.event_payload(event))
        assert event["sequence"] == 1
        assert event["previous_event_hash"] is None
        claim = claim_by_id[event["claim_id"]]
        assert claim["review"]["current_event_id"] == event["event_id"]
        assert claim["review"]["current_event_hash"] == event["event_hash"]


def test_arv067g_low_confidence_and_unverified_claims_remain_blocked() -> None:
    manifest, _, claims, _, _, report = _load_dataset()
    threshold = manifest["confidence_policy"]["low_below"]
    low = [row for row in claims if row["confidence"] < threshold]
    assert {row["claim_id"] for row in low} == set(report["low_confidence_claim_ids"])
    assert all(row["review_required"] is True for row in low)
    assert all(row["production_ready"] is False for row in claims)
    assert set(report["unverified_claim_ids"]) == {row["claim_id"] for row in claims}


def test_arv067g_report_is_deterministic() -> None:
    generator = _load_module(DIRECTORY / "generate_provenance_report.py", "arv067g_report")
    manifest, sources, claims, events, conflicts, committed = _load_dataset()
    generated = generator.build_report(manifest, sources, claims, events, conflicts)
    assert generated == committed
    assert generated["automatic_activation_allowed"] is False
    assert generated["source_recheck_required_ids"] == ["SRC-ROSSETTI-PRIMARY-PDF@2026-06-10"]


def test_arv067g_source_revisions_are_content_addressed() -> None:
    validator = _load_module(DIRECTORY / "validate_provenance.py", "arv067g_validator")
    _, sources, claims, _, _, _ = _load_dataset()
    source_by_revision = {row["revision_id"]: row for row in sources}
    for source in sources:
        if source["source_kind"] == "repository_asset":
            assert validator.git_blob_sha1(ROOT / source["location"]) == source["content_hash"]
    for claim in claims:
        for link in claim["source_links"]:
            assert link["source_content_hash"] == source_by_revision[link["source_revision_id"]]["content_hash"]


def test_arv067g_production_runtime_is_not_wired() -> None:
    forbidden = {
        "ARV-067G-ELECTRICAL-PROVENANCE",
        "provenance_registry.v1.yaml",
        "validate_provenance",
        "generate_provenance_report",
    }
    hits = []
    src = ROOT / "src"
    if src.exists():
        for path in src.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    hits.append(f"{path.relative_to(ROOT)}:{token}")
    assert hits == []
