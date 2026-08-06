from pathlib import Path

from src.modules.production_llm_analysis.contracts import R10_1_CONTROLLED_MAP_CONTRACT


def test_verifier_uses_shared_compact_contract():
    roots = [
        Path("scripts/r10_1/verify_batch_audit_plans.py"),
        Path(__file__).resolve().parents[2] / "scripts/r10_1/verify_batch_audit_plans.py",
    ]
    source = next((path.read_text() for path in roots if path.exists()), "")
    assert "R10_1_CONTROLLED_MAP_CONTRACT.prompt_version" in source
    assert "r10.1-batched-v1" not in source
    assert R10_1_CONTROLLED_MAP_CONTRACT.prompt_version == "r10.1-batched-compact-v3"
    assert R10_1_CONTROLLED_MAP_CONTRACT.output_schema_version == "v2"
    assert R10_1_CONTROLLED_MAP_CONTRACT.provider_wire_contract_version == "compact-safe-v2"
    assert R10_1_CONTROLLED_MAP_CONTRACT.plan_version == "arv003-map-plan-v7"
