import io
import json

import pytest

from src.modules.production_llm_analysis.batching import (
    BatchCoverageError,
    BatchPolicy,
    ControlledTokenizerNotPersistent,
    LlamaServerTokenCounter,
    OversizedEvidenceChunk,
    TokenizerEndpointUnsafe,
    build_evidence_batch_plan,
)
from src.modules.production_llm_analysis.schemas import (
    BudgetLimits,
    BudgetPolicy,
    EvidenceFragmentInput,
    ProviderPricing,
)


def _fragments():
    return [
        EvidenceFragmentInput(
            document_id="doc-1",
            document_name="a.txt",
            chunk_id=f"chunk-{i}",
            locator={"chunk_index": i},
            text="x" * size,
        )
        for i, size in enumerate((4, 5, 3, 7))
    ]


def test_plan_is_exactly_covered_and_stable():
    policy = BatchPolicy(
        context_window=20, evidence_budget=12, output_reserve=4, safety_margin=2
    )
    first = build_evidence_batch_plan(
        _fragments(), tokenizer=lambda text: len(text), policy=policy
    )
    second = build_evidence_batch_plan(
        _fragments(), tokenizer=lambda text: len(text), policy=policy
    )

    assert first.plan_hash == second.plan_hash
    assert [item.chunk_id for batch in first.batches for item in batch.fragments] == [
        "chunk-0",
        "chunk-1",
        "chunk-2",
        "chunk-3",
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
    policy = BatchPolicy(
        context_window=10, evidence_budget=4, output_reserve=4, safety_margin=2
    )
    try:
        build_evidence_batch_plan(
            _fragments(), tokenizer=lambda text: len(text), policy=policy
        )
    except OversizedEvidenceChunk as exc:
        assert exc.code == "evidence_batch_oversized_chunk"
    else:
        raise AssertionError("oversized chunk was split or accepted")


def test_approved_profiles_pin_runtime_overhead_and_deadline():
    assert (
        BatchPolicy.approved_32k(tokenizer_identity="pinned").chat_template_overhead
        == 32
    )
    assert (
        BatchPolicy.approved_64k(tokenizer_identity="pinned").chat_template_overhead
        == 32
    )
    assert (
        BatchPolicy.approved_32k(tokenizer_identity="pinned").execution_deadline_ms
        >= 7_200_000
    )


def test_command_token_counter_caches_exact_inputs():
    from src.modules.production_llm_analysis.batching import CommandTokenCounter

    counter = CommandTokenCounter(
        "python -c 'print(\"Total number of tokens: 7\")'", identity="test"
    )
    assert counter("same") == 7
    assert counter("same") == 7
    assert counter.invocations == 1
    assert counter.cache_hits == 1


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def geturl(self):
        return "http://127.0.0.1:8081/tokenize"


def test_in_process_llama_counter_caches_without_subprocess(monkeypatch):
    calls = 0

    def opener(request, *, timeout):
        nonlocal calls
        calls += 1
        return _Response(json.dumps({"tokens": [1, 2, 3]}).encode())

    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    counter = LlamaServerTokenCounter(
        "http://127.0.0.1:8081/tokenize",
        identity="pinned-server-tokenize-v2",
        opener=opener,
    )
    assert counter("evidence") == counter("evidence") == 3
    assert calls == 1
    assert counter.invocations == 1
    assert counter.cache_hits == 1
    assert counter.subprocess_count == 0


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:8081/tokenize",
        "http://example.invalid/tokenize",
        "http://user:secret@127.0.0.1:8081/tokenize",
        "http://127.0.0.1:8081/tokenize?x=1",
    ],
)
def test_in_process_counter_rejects_unsafe_endpoint(endpoint):
    with pytest.raises(TokenizerEndpointUnsafe):
        LlamaServerTokenCounter(endpoint, identity="pinned")


def test_controlled_planning_rejects_subprocess_counter():
    from src.modules.production_llm_analysis.batching import CommandTokenCounter

    with pytest.raises(ControlledTokenizerNotPersistent):
        build_evidence_batch_plan(
            _fragments(),
            tokenizer=CommandTokenCounter("python -c 'print(1)'", identity="test"),
            policy=BatchPolicy.approved_32k(tokenizer_identity="test"),
            controlled=True,
        )


class _SyntheticExactCounter:
    identity = "synthetic-server-tokenize-v2"
    persistent = True

    def __init__(self):
        self.invocations = 0
        self.cache_hits = 0

    def __call__(self, text: str) -> int:
        self.invocations += 1
        return max(1, len(text.encode()) // 2)


def _provider_budget(output_tokens: int) -> BudgetPolicy:
    return BudgetPolicy(
        limits=BudgetLimits(
            max_input_tokens=1_000_000,
            max_output_tokens=output_tokens,
            timeout_ms=900_000,
            max_retries=0,
            max_total_latency_ms=1_200_000,
            max_estimated_cost=1.0,
        ),
        pricing=ProviderPricing(
            input_cost_per_1k_tokens=0,
            output_cost_per_1k_tokens=0,
            currency="USD",
            pricing_table_version="synthetic-v1",
        ),
    )


def test_calibrated_planner_is_bounded_for_underestimated_1266_fragment_corpus():
    fragments = [
        EvidenceFragmentInput(
            document_id=f"doc-{index // 300}",
            document_name="synthetic.txt",
            chunk_id=f"chunk-{index}",
            locator={
                "document_order": index // 300,
                "chunk_index": index,
                "token_estimate": 104,
            },
            text="x" * 500,
        )
        for index in range(1266)
    ]

    def plan(profile: str):
        counter = _SyntheticExactCounter()
        policy = (
            BatchPolicy.approved_32k(tokenizer_identity=counter.identity)
            if profile == "32k"
            else BatchPolicy.approved_64k(tokenizer_identity=counter.identity)
        )

        def measure(candidate):
            return counter("\n".join(item.text for item in candidate)) + 800, "0" * 64

        result = build_evidence_batch_plan(
            fragments,
            tokenizer=counter,
            policy=policy,
            request_measure=measure,
            budget_policy=_provider_budget(policy.output_reserve),
            controlled=True,
        )
        return result, counter

    plan_32, counter_32 = plan("32k")
    plan_64, counter_64 = plan("64k")
    assert len(plan_32.fragment_ids) == len(set(plan_32.fragment_ids)) == 1266
    assert len(plan_64.fragment_ids) == len(set(plan_64.fragment_ids)) == 1266
    assert counter_32.invocations <= 65
    assert counter_64.invocations <= 35
    assert max(batch.adjustment_rounds for batch in plan_32.batches) <= 3
    assert max(batch.adjustment_rounds for batch in plan_64.batches) <= 3
    assert plan_32.plan_hash != plan_64.plan_hash
