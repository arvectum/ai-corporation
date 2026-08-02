from __future__ import annotations

import pytest

from scripts.arv001 import run_complete_corpus_acceptance as runner


def _execution() -> dict:
    return {
        "status": "success",
        "canonical_input_eligible": True,
        "accepted_claim_count": 1,
        "rejected_claim_count": 0,
        "accepted_claims": [
            {"claim_id": "claim-1", "support_status": "supported"}
        ],
        "rejected_claims": [],
        "retry_count": 0,
        "batch_count": 7,
        "provider_call_count": 7,
        "raw_response_stored": False,
    }


def _manifest() -> dict:
    return {
        "repeat_count": 2,
        "repeat_identity_verified": True,
        "executions": [_execution(), _execution()],
        "safety": {
            "credential_value_recorded": False,
            "raw_tender_text_recorded": False,
            "raw_provider_body_recorded": False,
            "raw_response_stored": False,
            "evidence_quotes_recorded": False,
            "local_paths_recorded": False,
        },
    }


def test_controlled_manifest_metrics_require_two_grounded_identical_executions():
    metrics = runner._controlled_manifest_metrics(_manifest())

    assert metrics["execution_count"] == 2
    assert metrics["batch_count_per_execution"] == 7
    assert metrics["accepted_claim_count_per_execution"] == 1
    assert metrics["rejected_claim_count_per_execution"] == 0
    assert metrics["unsupported_claim_count_per_execution"] == 0
    assert metrics["retry_count_per_execution"] == 0
    assert metrics["raw_provider_response_stored"] is False
    assert metrics["provider_reasoning_stored"] is False


def test_controlled_manifest_metrics_fail_closed_on_unsupported_claim():
    manifest = _manifest()
    manifest["executions"][0]["accepted_claims"][0]["support_status"] = "unsupported"

    with pytest.raises(
        runner.AcceptanceBlocked, match="controlled_execution_contract_failed"
    ):
        runner._controlled_manifest_metrics(manifest)


def test_controlled_manifest_metrics_fail_closed_on_provider_call_mismatch():
    manifest = _manifest()
    manifest["executions"][1]["provider_call_count"] = 6

    with pytest.raises(
        runner.AcceptanceBlocked, match="controlled_execution_contract_failed"
    ):
        runner._controlled_manifest_metrics(manifest)
