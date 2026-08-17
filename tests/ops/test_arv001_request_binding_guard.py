from pathlib import Path


def _source() -> str:
    return Path("scripts/arv001/full_pre_provider.py").read_text(encoding="utf-8")


def test_full_pre_provider_uses_canonical_r10_1_batch_planner() -> None:
    source = _source()
    assert "build_r10_1_batch_plan" in source
    assert "build_evidence_batch_plan(" not in source[
        source.index("def _reconstruct_actual_batch_requests") : source.index(
            "def _arguments"
        )
    ]


def test_full_pre_provider_does_not_hardcode_projection_acceptance() -> None:
    source = _source()
    assert '"canonical_evidence_projection_match": True' not in source
    assert '"target_run_binding_verified": True' not in source


def test_raw_mode_reconstructs_requests_from_prepared_state() -> None:
    source = _source()
    raw_branch_start = source.index(
        'command = [sys.executable, "-m", "scripts.arv001.run_complete_corpus_acceptance_split_roots"'
    )
    raw_branch_end = source.index(
        'for phase in ("corpus_contract", "database", "application_persistence"',
        raw_branch_start,
    )
    raw_branch = source[raw_branch_start:raw_branch_end]

    assert '"request.json"' not in raw_branch
    assert "_reconstruct_actual_batch_requests(" in raw_branch
    assert "reconstruction.requests" in raw_branch
    assert "reconstruction.plan" in raw_branch
