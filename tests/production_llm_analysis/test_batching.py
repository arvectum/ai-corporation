import io
import json
from dataclasses import replace

import pytest

from src.modules.production_llm_analysis.batching import (
    BatchCoverageError,
    BatchPolicy,
    ControlledTokenizerNotPersistent,
    ExactRequestMeasurement,
    LlamaServerTokenCounter,
    OversizedEvidenceChunk,
    TokenizerEndpointUnsafe,
    TokenizerPolicyStructurallyInvalid,
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
    assert all(
        batch.evidence_tokens <= policy.max_evidence_tokens for batch in first.batches
    )


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
    assert counter.logical_calls == 2


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
                "token_estimate": 110,
            },
            text="x" * 1160,
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
            payload = "\n".join(item.text for item in candidate)
            evidence = counter(payload)
            return ExactRequestMeasurement(
                full_request_tokens=counter(payload) + 800,
                request_body_hash="0" * 64,
                serialized_evidence_tokens=evidence,
                serialized_evidence_hash="1" * 64,
                fixed_envelope_tokens=800,
                chat_template_overhead=0,
            )

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
    assert len(plan_32.batches) <= 32
    assert len(plan_64.batches) <= 18
    assert counter_32.invocations <= 80
    assert counter_64.invocations <= 48
    assert max(batch.adjustment_rounds for batch in plan_32.batches) <= 3
    assert max(batch.adjustment_rounds for batch in plan_64.batches) <= 3
    assert plan_32.plan_hash != plan_64.plan_hash


@pytest.mark.parametrize(("profile", "unsafe_budget"), (("32k", 65), ("64k", 35)))
def test_structurally_insufficient_http_budget_is_rejected(profile, unsafe_budget):
    policy = (
        BatchPolicy.approved_32k(tokenizer_identity="pinned")
        if profile == "32k"
        else BatchPolicy.approved_64k(tokenizer_identity="pinned")
    )
    unsafe = replace(
        policy,
        max_http_tokenizer_requests=unsafe_budget,
        max_tokenizer_invocations=unsafe_budget,
    )
    with pytest.raises(TokenizerPolicyStructurallyInvalid):
        unsafe.validate(_provider_budget(unsafe.output_reserve), controlled=True)


def test_calibration_candidate_reuses_planner_cache():
    counter = _SyntheticExactCounter()
    policy = BatchPolicy(
        context_window=64,
        evidence_budget=40,
        output_reserve=12,
        safety_margin=12,
        max_candidate_evaluations=3,
    )
    fragments = [
        EvidenceFragmentInput(
            document_id="doc",
            document_name="synthetic.txt",
            chunk_id=f"chunk-{index}",
            locator={"chunk_index": index, "token_estimate": 10},
            text="x" * 10,
        )
        for index in range(4)
    ]

    result = build_evidence_batch_plan(
        fragments,
        tokenizer=counter,
        policy=policy,
        request_measure=lambda candidate: ExactRequestMeasurement(
            full_request_tokens=counter("\n".join(item.text for item in candidate))
            + 12,
            request_body_hash="0" * 64,
            serialized_evidence_tokens=counter(
                "\n".join(item.text for item in candidate)
            ),
            serialized_evidence_hash="1" * 64,
            fixed_envelope_tokens=12,
            chat_template_overhead=0,
        ),
    )

    assert result.planning_diagnostics["planner_cache_hits"] >= 0
    assert counter.invocations <= policy.max_http_tokenizer_requests


def _slot_balanced_fragments(count: int) -> list[EvidenceFragmentInput]:
    return [
        EvidenceFragmentInput(
            document_id="synthetic",
            document_name="synthetic.txt",
            chunk_id=f"slot-{index}",
            locator={"chunk_index": index, "token_estimate": 10},
            text="x" * 10,
        )
        for index in range(count)
    ]


def _slot_balanced_policy(max_batches: int) -> BatchPolicy:
    request_budget = 2 + max_batches * 2
    return BatchPolicy(
        context_window=100,
        evidence_budget=40,
        output_reserve=20,
        safety_margin=10,
        max_batches=max_batches,
        max_http_tokenizer_requests=request_budget,
        max_tokenizer_invocations=request_budget,
        correction_request_reserve=0,
        packing_target_utilization=0.98,
    )


def _slot_balanced_measure(counter, candidate):
    rough = sum(int(item.locator["token_estimate"]) for item in candidate)
    counter("request")
    counter("evidence")
    return ExactRequestMeasurement(
        full_request_tokens=rough + 5,
        request_body_hash="0" * 64,
        serialized_evidence_tokens=rough,
        serialized_evidence_hash="1" * 64,
        fixed_envelope_tokens=5,
        chat_template_overhead=0,
    )


def test_slot_balanced_v5_prevents_v4_style_max_batches_underfill():
    fragments = _slot_balanced_fragments(12)
    policy = _slot_balanced_policy(max_batches=3)
    counter = _SyntheticExactCounter()
    plan = build_evidence_batch_plan(
        fragments,
        tokenizer=counter,
        policy=policy,
        request_measure=lambda candidate: _slot_balanced_measure(counter, candidate),
    )

    # A v4-style fixed target of 30 rough tokens would leave three fragments
    # after its three allowed batches.  V5 derives 40 from remaining slots.
    assert 3 * 3 == 9 < len(fragments)
    assert len(plan.fragment_ids) == 12
    assert len(plan.batches) == 3
    assert [len(batch.fragments) for batch in plan.batches] == [4, 4, 4]
    assert counter.invocations == 8
    assert plan.planning_diagnostics["capacity_recalculation_count"] >= 4
    assert plan.planning_diagnostics["required_average_rough_tokens"] == 40
    assert plan.planning_diagnostics["planning_payload_ratio"] == 1


def test_realistic_1266_fragment_underfill_regression_is_slot_balanced():
    fragments = [
        EvidenceFragmentInput(
            document_id=f"doc-{index // 250}",
            document_name="synthetic.txt",
            chunk_id=f"realistic-{index}",
            locator={
                "document_order": index // 250,
                "chunk_index": index,
                "token_estimate": 50,
            },
            text="x" * 50,
        )
        for index in range(1266)
    ]
    # V4's fixed 1,600 rough-token boundary accepts 32 fragments per batch,
    # reproducing a 32-slot underfill with 242 fragments left behind.
    assert 32 * (1600 // 50) == 1024
    assert len(fragments) - 1024 == 242

    counter = _SyntheticExactCounter()

    def measure(candidate):
        rough = sum(int(item.locator["token_estimate"]) for item in candidate)
        counter("request")
        counter("evidence")
        evidence = rough * 12
        return ExactRequestMeasurement(
            full_request_tokens=evidence + 1390,
            request_body_hash="0" * 64,
            serialized_evidence_tokens=evidence,
            serialized_evidence_hash="1" * 64,
            fixed_envelope_tokens=1390,
            chat_template_overhead=0,
        )

    plan = build_evidence_batch_plan(
        fragments,
        tokenizer=counter,
        policy=BatchPolicy.approved_32k(tokenizer_identity=counter.identity),
        request_measure=measure,
        budget_policy=_provider_budget(4096),
        controlled=True,
    )

    assert len(plan.fragment_ids) == len(set(plan.fragment_ids)) == 1266
    assert len(plan.batches) == 32
    assert counter.invocations <= 80
    assert plan.planning_diagnostics["planner_cache_hits"] >= 1
    assert plan.planning_diagnostics["remaining_fragment_count"] == 0
    assert plan.planning_diagnostics["grow_attempt_count"] == 0
    assert plan.planning_diagnostics["adjustment_rounds_max"] <= 3


def test_slot_balanced_v5_last_slot_measures_all_remaining_fragments():
    counter = _SyntheticExactCounter()
    plan = build_evidence_batch_plan(
        _slot_balanced_fragments(5),
        tokenizer=counter,
        policy=_slot_balanced_policy(max_batches=2),
        request_measure=lambda candidate: _slot_balanced_measure(counter, candidate),
    )

    assert len(plan.batches) == 2
    assert len(plan.batches[-1].fragments) == 2
    assert len(plan.fragment_ids) == 5
    assert counter.invocations == 6


def test_packing_target_utilization_is_validated_and_hashes_plan():
    policy = _slot_balanced_policy(max_batches=3)
    counter = _SyntheticExactCounter()
    first = build_evidence_batch_plan(
        _slot_balanced_fragments(12),
        tokenizer=counter,
        policy=policy,
        request_measure=lambda candidate: _slot_balanced_measure(counter, candidate),
    )
    changed = replace(policy, packing_target_utilization=0.9)
    second = build_evidence_batch_plan(
        _slot_balanced_fragments(12),
        tokenizer=_SyntheticExactCounter(),
        policy=changed,
        request_measure=lambda candidate: _slot_balanced_measure(
            _SyntheticExactCounter(), candidate
        ),
    )

    assert first.plan_hash != second.plan_hash
    with pytest.raises(Exception, match="packing target utilization"):
        replace(policy, packing_target_utilization=0).validate()
