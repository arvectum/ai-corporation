from pathlib import Path

from src.modules.production_llm_analysis.contracts import R10_1_CONTROLLED_MAP_CONTRACT


def test_verifier_uses_shared_compact_contract():
    source = Path("scripts/r10_1/verify_batch_audit_plans.py").read_text()
    assert "R10_1_CONTROLLED_MAP_CONTRACT.prompt_version" in source
    assert "r10.1-batched-v1" not in source
    assert R10_1_CONTROLLED_MAP_CONTRACT.prompt_version == "r10.1-batched-compact-v2"
