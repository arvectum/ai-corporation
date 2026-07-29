"""Fail-closed deterministic batching for the R10.1 map phase."""

from __future__ import annotations

import os
import hashlib
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from src.modules.production_llm_analysis.evidence import (
    build_evidence_packet,
    canonical_json_bytes,
    canonical_sha256,
)
from src.modules.production_llm_analysis.schemas import (
    BudgetPolicy,
    EvidenceFragment,
    EvidenceFragmentInput,
)


class ExactTokenCounter(Protocol):
    identity: str

    def __call__(self, text: str) -> int: ...


class BatchPlanningError(ValueError):
    code = "evidence_batch_policy_invalid"


class ExactTokenizerUnavailable(BatchPlanningError):
    code = "evidence_batch_exact_tokenizer_unavailable"


class BatchPolicyInvalid(BatchPlanningError):
    code = "evidence_batch_policy_invalid"


class OutputBudgetMismatch(BatchPlanningError):
    code = "evidence_batch_output_budget_mismatch"


class ContextBudgetExceeded(BatchPlanningError):
    code = "evidence_batch_context_budget_exceeded"


class OversizedEvidenceChunk(BatchPlanningError):
    code = "evidence_batch_oversized_chunk"


class BatchCoverageError(BatchPlanningError):
    code = "evidence_batch_coverage_incomplete"


class DuplicateAssignment(BatchCoverageError):
    code = "evidence_batch_duplicate_assignment"


class BatchExecutionTimeout(BatchPlanningError):
    code = "evidence_batch_execution_timeout"


@dataclass(frozen=True)
class BatchPolicy:
    """One of the two approved context profiles; arbitrary defaults are forbidden."""

    profile: str = "32k"
    plan_version: str = "arv003-map-plan-v1"
    context_window: int = 32768
    evidence_budget: int = 24488
    output_reserve: int = 4096
    safety_margin: int = 3277
    measured_overhead: int = 0
    max_claims: int = 3
    parallelism: int = 1
    tokenizer_identity: str = ""
    chat_template_overhead: int = 32
    execution_deadline_ms: int = 7_200_000
    max_provider_calls: int = 32
    max_total_input_tokens: int = 1_000_000
    max_total_output_tokens: int = 262_144
    max_total_retries: int = 32
    max_total_cost: float = 1.0
    _approved: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def approved_32k(cls, *, tokenizer_identity: str, measured_overhead: int = 0) -> "BatchPolicy":
        return cls(
            profile="32k", context_window=32768, evidence_budget=24488,
            output_reserve=4096, safety_margin=3277, max_claims=3,
            tokenizer_identity=tokenizer_identity, measured_overhead=measured_overhead,
            chat_template_overhead=32, execution_deadline_ms=7_200_000, _approved=True,
        )

    @classmethod
    def approved_64k(cls, *, tokenizer_identity: str, measured_overhead: int = 0) -> "BatchPolicy":
        return cls(
            profile="64k", context_window=65536, evidence_budget=49883,
            output_reserve=8192, safety_margin=6554, max_claims=7,
            tokenizer_identity=tokenizer_identity, measured_overhead=measured_overhead,
            chat_template_overhead=32, execution_deadline_ms=7_200_000, _approved=True,
        )

    @property
    def max_evidence_tokens(self) -> int:
        return self.evidence_budget

    def validate(self, budget_policy: BudgetPolicy | None = None, *, controlled: bool = False) -> None:
        expected = {"32k": (32768, 24488, 4096, 3277, 3), "64k": (65536, 49883, 8192, 6554, 7)}
        if self.profile not in expected or (controlled and not self._approved):
            raise BatchPolicyInvalid("unknown or unapproved context profile")
        if not controlled and not self._approved and self.context_window != 32768:
            if self.context_window <= 0 or self.evidence_budget <= 0 or self.output_reserve <= 0 or self.safety_margin <= 0:
                raise BatchPolicyInvalid("offline policy contains invalid limits")
            if self.evidence_budget + self.output_reserve + self.safety_margin + self.measured_overhead > self.context_window:
                raise ContextBudgetExceeded("batch context budget exceeded")
            return
        context, evidence, output, safety, claims = expected[self.profile]
        if (self.context_window, self.evidence_budget, self.output_reserve, self.safety_margin, self.max_claims) != (context, evidence, output, safety, claims):
            raise BatchPolicyInvalid("context profile values are not approved")
        if self.evidence_budget <= 0 or self.output_reserve <= 0 or self.safety_margin <= 0 or self.parallelism != 1:
            raise BatchPolicyInvalid("batch policy contains a non-positive or parallel setting")
        if self.evidence_budget + self.output_reserve + self.safety_margin + self.measured_overhead > self.context_window:
            raise ContextBudgetExceeded("batch context budget exceeded")
        if self.chat_template_overhead <= 0:
            raise BatchPolicyInvalid("chat template overhead must be positive")
        if min(self.execution_deadline_ms, self.max_provider_calls, self.max_total_input_tokens,
               self.max_total_output_tokens, self.max_total_retries) <= 0 or self.max_total_cost < 0:
            raise BatchPolicyInvalid("execution budget contains invalid limits")
        if controlled and not self.tokenizer_identity:
            raise ExactTokenizerUnavailable("tokenizer identity is required")
        if budget_policy is not None and (controlled or self._approved) and budget_policy.limits.max_output_tokens != self.output_reserve:
            raise OutputBudgetMismatch("batch output reserve does not match provider output budget")


@dataclass(frozen=True)
class EvidenceBatch:
    batch_ordinal: int
    fragments: tuple[EvidenceFragmentInput, ...]
    evidence_tokens: int
    projected_request_tokens: int
    output_reserve: int
    safety_margin: int
    batch_hash: str
    provisional_request_body_hash: str = ""

    @property
    def request_hash(self) -> str:
        """Backward-compatible name for the provisional planning measurement."""
        return self.provisional_request_body_hash


@dataclass(frozen=True)
class EvidenceBatchPlan:
    plan_version: str
    policy: BatchPolicy
    corpus_evidence_hash: str
    batches: tuple[EvidenceBatch, ...]
    plan_hash: str
    tokenizer_identity: str

    @property
    def fragment_ids(self) -> tuple[str, ...]:
        return tuple(fragment_id(item) for batch in self.batches for item in batch.fragments)

    @property
    def ordered_batch_hashes(self) -> tuple[str, ...]:
        return tuple(batch.batch_hash for batch in self.batches)


def _numeric_chunk_index(item: EvidenceFragmentInput | EvidenceFragment) -> int:
    value = item.locator.get("chunk_index") if item.locator else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return 2**63 - 1


def fragment_id(item: EvidenceFragmentInput | EvidenceFragment) -> str:
    if isinstance(item, EvidenceFragment):
        return item.fragment_id
    packet = build_evidence_packet(
        customer_id="batch-planner", project_id="batch-planner",
        procurement_case_id="batch-planner", run_id="batch-planner",
        registry_number="batch-planner", fragments=[item],
    )
    return packet.fragments[0].fragment_id


def canonical_fragment_order(item: EvidenceFragmentInput | EvidenceFragment) -> tuple[int, int, str]:
    document_order = item.locator.get("document_order", 2**31 - 1) if item.locator else 2**31 - 1
    try:
        document_order = int(document_order)
    except (TypeError, ValueError):
        document_order = 2**31 - 1
    return document_order, _numeric_chunk_index(item), fragment_id(item)


def _batch_hash(batch_number: int, fragments: list[EvidenceFragmentInput], tokens: int, policy: BatchPolicy) -> str:
    return canonical_sha256({
        "plan_version": policy.plan_version, "profile": policy.profile,
        "batch_ordinal": batch_number, "fragment_ids": [fragment_id(item) for item in fragments],
        "batch_content_hash": canonical_sha256([item.model_dump(mode="json") for item in fragments]),
        "evidence_tokens": tokens, "output_reserve": policy.output_reserve,
        "safety_margin": policy.safety_margin, "chat_template_overhead": policy.chat_template_overhead,
        "max_claims": policy.max_claims, "tokenizer_identity": policy.tokenizer_identity,
    })


def build_evidence_batch_plan(
    fragments: Iterable[EvidenceFragmentInput],
    *,
    tokenizer: ExactTokenCounter | Callable[[str], int] | None,
    policy: BatchPolicy = BatchPolicy(),
    request_measure: Callable[[list[EvidenceFragmentInput]], tuple[int, str]] | None = None,
    request_token_overhead: int | None = None,
    budget_policy: BudgetPolicy | None = None,
    controlled: bool = False,
) -> EvidenceBatchPlan:
    """Pack whole chunks using exact tokens of the complete request candidate."""
    policy.validate(budget_policy, controlled=controlled)
    if tokenizer is None:
        raise ExactTokenizerUnavailable("An approved exact tokenizer is required")
    if request_measure is None and request_token_overhead is not None and request_token_overhead <= 0:
        raise BatchPolicyInvalid("request token overhead must be measured, not defaulted to zero")
    items = sorted(list(fragments), key=canonical_fragment_order)
    if not items:
        raise BatchCoverageError("No evidence fragments supplied")
    identities = [fragment_id(item) for item in items]
    if len(identities) != len(set(identities)):
        raise DuplicateAssignment("Duplicate evidence fragment identity")
    corpus_hash = canonical_sha256({"fragment_ids": identities})
    batches: list[EvidenceBatch] = []
    cursor = 0
    rough_limit = policy.max_evidence_tokens
    while cursor < len(items):
        selected: list[EvidenceFragmentInput] = []
        rough_tokens = 0
        while cursor + len(selected) < len(items):
            item = items[cursor + len(selected)]
            item_tokens = int(item.locator.get("token_estimate", 0) or 0) if item.locator else 0
            if item_tokens <= 0:
                item_tokens = max(1, len(item.text.encode("utf-8")) // 3)
            if selected and rough_tokens + item_tokens > rough_limit:
                break
            if not selected and item_tokens > rough_limit:
                raise OversizedEvidenceChunk("One source chunk exceeds the safe context budget")
            selected.append(item)
            rough_tokens += item_tokens
        ordinal = len(batches) + 1
        if request_measure is not None:
            projected, request_hash = request_measure(selected)
            evidence_tokens = int(tokenizer("\n".join(item.text for item in selected)))
            while (evidence_tokens > policy.max_evidence_tokens or projected + policy.output_reserve + policy.safety_margin > policy.context_window) and len(selected) > 1:
                selected.pop()
                projected, request_hash = request_measure(selected)
                evidence_tokens = int(tokenizer("\n".join(item.text for item in selected)))
            if evidence_tokens > policy.max_evidence_tokens or projected + policy.output_reserve + policy.safety_margin > policy.context_window:
                raise OversizedEvidenceChunk("One source chunk exceeds the safe context budget")
        else:
            request_hash = ""
            evidence_tokens = int(tokenizer("\n".join(item.text for item in selected)))
            projected = evidence_tokens + int(request_token_overhead or 0)
            while evidence_tokens > policy.max_evidence_tokens and len(selected) > 1:
                selected.pop()
                evidence_tokens = int(tokenizer("\n".join(item.text for item in selected)))
                projected = evidence_tokens + int(request_token_overhead or 0)
            if evidence_tokens > policy.max_evidence_tokens:
                raise OversizedEvidenceChunk("One source chunk exceeds the safe context budget")
        batches.append(EvidenceBatch(
            ordinal, tuple(selected), evidence_tokens, projected,
            policy.output_reserve, policy.safety_margin,
            _batch_hash(ordinal, selected, evidence_tokens, policy), request_hash,
        ))
        cursor += len(selected)
    assigned = [fragment_id(item) for batch in batches for item in batch.fragments]
    if assigned != identities:
        raise BatchCoverageError("Batch coverage is not exactly one-to-one")
    tokenizer_identity = policy.tokenizer_identity or str(getattr(tokenizer, "identity", "offline-estimated"))
    unsigned = {
        "plan_version": policy.plan_version, "profile": policy.profile,
        "tokenizer_identity": tokenizer_identity, "context_window": policy.context_window,
        "evidence_budget": policy.evidence_budget, "output_reserve": policy.output_reserve,
        "safety_margin": policy.safety_margin, "chat_template_overhead": policy.chat_template_overhead,
        "max_claims": policy.max_claims, "execution_deadline_ms": policy.execution_deadline_ms,
        "max_provider_calls": policy.max_provider_calls,
        "max_total_input_tokens": policy.max_total_input_tokens,
        "max_total_output_tokens": policy.max_total_output_tokens,
        "max_total_retries": policy.max_total_retries,
        "max_total_cost": policy.max_total_cost,
        "measured_overhead": policy.measured_overhead, "corpus_evidence_hash": corpus_hash,
        "batches": [batch.__dict__ | {"fragments": [fragment_id(item) for item in batch.fragments]}
                    for batch in batches],
    }
    return EvidenceBatchPlan(policy.plan_version, policy, corpus_hash, tuple(batches), canonical_sha256(unsigned), tokenizer_identity)


class CommandTokenCounter:
    """Bounded stdin tokenizer adapter; never places evidence in argv or errors."""

    def __init__(self, command: str, *, identity: str, timeout_seconds: int = 30):
        self.command = tuple(shlex.split(command))
        self.identity = identity
        self.timeout_seconds = timeout_seconds
        self.invocations = 0
        self.cache_hits = 0
        self._cache: dict[str, int] = {}
        if not self.command or not identity or timeout_seconds <= 0:
            raise ExactTokenizerUnavailable("tokenizer configuration is incomplete")

    def __call__(self, text: str) -> int:
        cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if cache_key in self._cache:
            self.cache_hits += 1
            return self._cache[cache_key]
        self.invocations += 1
        try:
            completed = subprocess.run(
                self.command, input=text.encode("utf-8"), check=True,
                capture_output=True, timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise BatchExecutionTimeout("exact tokenizer timed out") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExactTokenizerUnavailable("exact tokenizer command failed") from exc
        output = completed.stdout.decode("utf-8", "replace")
        for line in reversed(output.splitlines()):
            if line.startswith("Total number of tokens:"):
                try:
                    value = int(line.rsplit(":", 1)[1].strip())
                    self._cache[cache_key] = value
                    return value
                except ValueError:
                    break
        raise ExactTokenizerUnavailable("exact tokenizer returned invalid output")


def tokenizer_from_environment() -> CommandTokenCounter:
    command = os.environ.get("ARV003_EXACT_TOKENIZER_COMMAND", "")
    identity = os.environ.get("ARV003_TOKENIZER_IDENTITY", "")
    return CommandTokenCounter(command, identity=identity)
