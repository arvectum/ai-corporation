from src.modules.production_llm_analysis.batching import (
    BatchCoverageError,
    BatchPolicy,
    OversizedEvidenceChunk,
    build_evidence_batch_plan,
)
from src.modules.production_llm_analysis.schemas import EvidenceFragmentInput


def _fragments():
    return [
        EvidenceFragmentInput(
            document_id="doc-1", document_name="a.txt", chunk_id=f"chunk-{i}",
            locator={"chunk_index": i}, text="x" * size,
        )
        for i, size in enumerate((4, 5, 3, 7))
    ]


def test_plan_is_exactly_covered_and_stable():
    policy = BatchPolicy(context_window=20, evidence_budget=12, output_reserve=4, safety_margin=2)
    first = build_evidence_batch_plan(_fragments(), tokenizer=lambda text: len(text), policy=policy)
    second = build_evidence_batch_plan(_fragments(), tokenizer=lambda text: len(text), policy=policy)

    assert first.plan_hash == second.plan_hash
    assert [item.chunk_id for batch in first.batches for item in batch.fragments] == [
        "chunk-0", "chunk-1", "chunk-2", "chunk-3"
    ]
    assert len(first.fragment_ids) == len(set(first.fragment_ids)) == 4
    assert [batch.evidence_tokens for batch in first.batches] == [10, 11]


def test_duplicate_fragment_is_rejected():
    fragments = _fragments()
    fragments.append(fragments[0])
    try:
        build_evidence_batch_plan(fragments, tokenizer=lambda text: len(text))
    except BatchCoverageError as exc:
        assert exc.code == "evidence_batch_duplicate_assignment"
    else:
        raise AssertionError("duplicate fragment was accepted")


def test_oversized_chunk_is_never_split():
    policy = BatchPolicy(context_window=10, evidence_budget=4, output_reserve=4, safety_margin=2)
    try:
        build_evidence_batch_plan(_fragments(), tokenizer=lambda text: len(text), policy=policy)
    except OversizedEvidenceChunk as exc:
        assert exc.code == "evidence_batch_oversized_chunk"
    else:
        raise AssertionError("oversized chunk was split or accepted")


def test_approved_profiles_pin_runtime_overhead_and_deadline():
    assert BatchPolicy.approved_32k(tokenizer_identity="pinned").chat_template_overhead == 32
    assert BatchPolicy.approved_64k(tokenizer_identity="pinned").chat_template_overhead == 32
    assert BatchPolicy.approved_32k(tokenizer_identity="pinned").execution_deadline_ms >= 7_200_000


def test_command_token_counter_caches_exact_inputs():
    from src.modules.production_llm_analysis.batching import CommandTokenCounter

    counter = CommandTokenCounter("python -c 'print(\"Total number of tokens: 7\")'", identity="test")
    assert counter("same") == 7
    assert counter("same") == 7
    assert counter.invocations == 1
    assert counter.cache_hits == 1
