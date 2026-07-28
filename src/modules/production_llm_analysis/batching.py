"""Deterministic, storage-neutral batching for R10.1 evidence analysis.

The planner never reads a database and never splits a source chunk.  Callers
must provide the persisted chunks as :class:`EvidenceFragmentInput` values and
an exact tokenizer for the approved provider/model.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Iterable, Protocol

from src.modules.production_llm_analysis.evidence import canonical_sha256, build_evidence_packet
from src.modules.production_llm_analysis.schemas import EvidenceFragment, EvidenceFragmentInput


class ExactTokenCounter(Protocol):
    def __call__(self, text: str) -> int: ...


class BatchPlanningError(ValueError):
    code = "batch_planning_failed"


class ExactTokenizerUnavailable(BatchPlanningError):
    code = "exact_tokenizer_unavailable"


class OversizedEvidenceChunk(BatchPlanningError):
    code = "oversized_evidence_chunk"


class BatchCoverageError(BatchPlanningError):
    code = "batch_coverage_invalid"


@dataclass(frozen=True)
class BatchPolicy:
    plan_version: str = "arv003-map-plan-v1"
    context_window: int = 32768
    evidence_budget: int = 27648
    output_reserve: int = 4096
    safety_margin: int = 1024
    tokenizer_identity: str = "exact-tokenizer-required"

    @property
    def max_evidence_tokens(self) -> int:
        return min(
            self.evidence_budget,
            self.context_window - self.output_reserve - self.safety_margin,
        )


@dataclass(frozen=True)
class EvidenceBatch:
    batch_ordinal: int
    fragments: tuple[EvidenceFragmentInput, ...]
    evidence_tokens: int
    projected_request_tokens: int
    output_reserve: int
    safety_margin: int
    batch_hash: str


@dataclass(frozen=True)
class EvidenceBatchPlan:
    plan_version: str
    policy: BatchPolicy
    corpus_evidence_hash: str
    batches: tuple[EvidenceBatch, ...]
    plan_hash: str

    @property
    def fragment_ids(self) -> tuple[str, ...]:
        return tuple(fragment_id(item) for batch in self.batches for item in batch.fragments)


def fragment_id(item: EvidenceFragmentInput | EvidenceFragment) -> str:
    if isinstance(item, EvidenceFragment):
        return item.fragment_id
    # Evidence packet construction is the single source of fragment identity.
    packet = build_evidence_packet(
        customer_id="batch-planner", project_id="batch-planner",
        procurement_case_id="batch-planner", run_id="batch-planner",
        registry_number="batch-planner", fragments=[item],
    )
    return packet.fragments[0].fragment_id


def _batch_hash(batch_number: int, fragments: list[EvidenceFragmentInput], tokens: int, policy: BatchPolicy) -> str:
    return canonical_sha256({
        "plan_version": policy.plan_version,
        "batch_ordinal": batch_number,
        "fragment_ids": [fragment_id(item) for item in fragments],
        "evidence_tokens": tokens,
        "output_reserve": policy.output_reserve,
        "safety_margin": policy.safety_margin,
    })


def build_evidence_batch_plan(
    fragments: Iterable[EvidenceFragmentInput],
    *,
    tokenizer: ExactTokenCounter | None,
    policy: BatchPolicy = BatchPolicy(),
    request_token_overhead: int = 0,
) -> EvidenceBatchPlan:
    """Pack source chunks in stable input order, with no chunk splitting."""
    if tokenizer is None:
        raise ExactTokenizerUnavailable("An approved exact tokenizer is required")
    items = list(fragments)
    if not items:
        raise BatchCoverageError("No evidence fragments supplied")
    identities = [fragment_id(item) for item in items]
    if len(identities) != len(set(identities)):
        raise BatchCoverageError("Duplicate evidence fragment identity")
    corpus_hash = canonical_sha256({"fragment_ids": identities})
    batches: list[EvidenceBatch] = []
    current: list[EvidenceFragmentInput] = []
    current_tokens = 0
    for item in items:
        tokens = int(tokenizer(item.text))
        if tokens <= 0:
            raise BatchPlanningError("Exact tokenizer returned no tokens")
        if tokens + request_token_overhead > policy.max_evidence_tokens:
            raise OversizedEvidenceChunk("One source chunk exceeds the safe context budget")
        if current and current_tokens + tokens + request_token_overhead > policy.max_evidence_tokens:
            ordinal = len(batches) + 1
            batches.append(EvidenceBatch(
                ordinal, tuple(current), current_tokens,
                current_tokens + request_token_overhead, policy.output_reserve,
                policy.safety_margin, _batch_hash(ordinal, current, current_tokens, policy),
            ))
            current, current_tokens = [], 0
        current.append(item)
        current_tokens += tokens
    if current:
        ordinal = len(batches) + 1
        batches.append(EvidenceBatch(
            ordinal, tuple(current), current_tokens,
            current_tokens + request_token_overhead, policy.output_reserve,
            policy.safety_margin, _batch_hash(ordinal, current, current_tokens, policy),
        ))
    assigned = [fragment_id(item) for batch in batches for item in batch.fragments]
    if assigned != identities or len(assigned) != len(set(assigned)):
        raise BatchCoverageError("Batch coverage is not exactly one-to-one")
    unsigned = {
        "plan_version": policy.plan_version,
        "tokenizer_identity": policy.tokenizer_identity,
        "context_window": policy.context_window,
        "evidence_budget": policy.evidence_budget,
        "output_reserve": policy.output_reserve,
        "safety_margin": policy.safety_margin,
        "corpus_evidence_hash": corpus_hash,
        "batches": [batch.__dict__ | {"fragments": [fragment_id(item) for item in batch.fragments]}
                    for batch in batches],
    }
    return EvidenceBatchPlan(policy.plan_version, policy, corpus_hash, tuple(batches), canonical_sha256(unsigned))


class CommandTokenCounter:
    """Exact tokenizer adapter for a local command such as llama-tokenize."""

    def __init__(self, command: str):
        self.command = tuple(shlex.split(command))
        if not self.command:
            raise ExactTokenizerUnavailable("Tokenizer command is empty")

    def __call__(self, text: str) -> int:
        try:
            completed = subprocess.run(
                [*self.command, text], check=True, capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExactTokenizerUnavailable("Exact tokenizer command failed") from exc
        try:
            return int(completed.stdout.strip())
        except ValueError as exc:
            raise ExactTokenizerUnavailable("Exact tokenizer returned invalid output") from exc
