from pathlib import Path


def test_full_pre_provider_uses_canonical_r10_1_batch_planner() -> None:
    source = Path("scripts/arv001/full_pre_provider.py").read_text(encoding="utf-8")
    assert "build_r10_1_batch_plan" in source
    assert "build_evidence_batch_plan(" not in source[source.index("def _reconstruct_actual_batch_requests"):source.index("def _arguments")]


def test_full_pre_provider_does_not_hardcode_projection_acceptance() -> None:
    source = Path("scripts/arv001/full_pre_provider.py").read_text(encoding="utf-8")
    assert '"canonical_evidence_projection_match": True' not in source
    assert '"target_run_binding_verified": True' not in source
