from __future__ import annotations

import json
from pathlib import Path

from scripts.arv001.complete_corpus_contract import (
    DEFAULT_CORPUS_SHA256,
    DEFAULT_POLICY_SHA256,
    DEFAULT_REGISTRY_NUMBER,
)


def test_acceptance_baseline_descriptor_matches_executable_contract() -> None:
    path = Path(__file__).resolve().parents[2] / "config/arv001/acceptance_baseline.json"
    descriptor = json.loads(path.read_text(encoding="utf-8"))

    assert descriptor["schema_version"] == "1.0"
    assert descriptor["baseline_id"] == "arv001-v2-6557c0fa0dcc"
    assert descriptor["registry_number"] == DEFAULT_REGISTRY_NUMBER
    assert descriptor["corpus"]["sha256"] == DEFAULT_CORPUS_SHA256
    assert descriptor["policy"]["sha256"] == DEFAULT_POLICY_SHA256
    assert descriptor["corpus"]["physical_file_count"] == 10
    assert descriptor["corpus"]["logical_document_count"] == 6
    assert descriptor["source"]["fresh_acquisitions"] >= 2
    assert descriptor["source"]["source_identity_reproduced"] is True
    assert descriptor["document_set"]["status"] == "complete"
    assert descriptor["document_set"]["analysis_allowed"] is True
    assert set(descriptor["document_set"]["required_logical_groups"]) == {
        "notice",
        "technical_specification",
        "price_justification",
        "application_requirements",
        "contract_draft",
        "contract_performance_security",
    }
    assert descriptor["corpus"]["hash_profile"]["fields"] == [
        "original_name",
        "sha256",
        "size_bytes",
    ]
    assert descriptor["corpus"]["hash_profile"]["serialization"] == "canonical_compact_newline"
    assert descriptor["provenance"]["fresh_reacquisition_matches_historical_corpus_identity"] is True
    assert descriptor["provenance"]["corpus_identity_changed"] is False
