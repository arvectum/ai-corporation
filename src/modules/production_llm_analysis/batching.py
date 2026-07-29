"""Fail-closed deterministic batching for the R10.1 map phase."""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

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


class TokenizerInvocationLimitExceeded(BatchPlanningError):
    code = "evidence_batch_tokenizer_invocation_limit_exceeded"


class PlanningConvergenceFailed(BatchPlanningError):
    code = "evidence_batch_planning_convergence_failed"


class PlanningTimeout(BatchPlanningError):
    code = "evidence_batch_planning_timeout"


class ControlledTokenizerNotPersistent(BatchPlanningError):
    code = "evidence_batch_controlled_tokenizer_not_persistent"


class TokenizerEndpointUnsafe(BatchPlanningError):
    code = "evidence_batch_tokenizer_endpoint_unsafe"


class TokenizerResponseInvalid(BatchPlanningError):
    code = "evidence_batch_tokenizer_response_invalid"


class TokenizerTimeout(BatchPlanningError):
    code = "evidence_batch_tokenizer_timeout"


@dataclass(frozen=True)
class BatchPolicy:
    """One of the two approved context profiles; arbitrary defaults are forbidden."""

    profile: str = "32k"
    plan_version: str = "arv003-map-plan-v2"
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
    planning_algorithm_version: str = "calibrated-bounded-v1"
    calibration_policy_version: str = "rolling-conservative-v1"
    max_tokenizer_invocations: int = 65
    max_adjustments_per_batch: int = 3
    max_planning_duration_ms: int = 120_000
    _approved: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def approved_32k(
        cls, *, tokenizer_identity: str, measured_overhead: int = 0
    ) -> BatchPolicy:
        return cls(
            profile="32k",
            context_window=32768,
            evidence_budget=24488,
            output_reserve=4096,
            safety_margin=3277,
            max_claims=3,
            tokenizer_identity=tokenizer_identity,
            measured_overhead=measured_overhead,
            chat_template_overhead=32,
            execution_deadline_ms=7_200_000,
            max_tokenizer_invocations=65,
            max_adjustments_per_batch=3,
            max_planning_duration_ms=120_000,
            _approved=True,
        )

    @classmethod
    def approved_64k(
        cls, *, tokenizer_identity: str, measured_overhead: int = 0
    ) -> BatchPolicy:
        return cls(
            profile="64k",
            context_window=65536,
            evidence_budget=49883,
            output_reserve=8192,
            safety_margin=6554,
            max_claims=7,
            tokenizer_identity=tokenizer_identity,
            measured_overhead=measured_overhead,
            chat_template_overhead=32,
            execution_deadline_ms=7_200_000,
            max_tokenizer_invocations=35,
            max_adjustments_per_batch=3,
            max_planning_duration_ms=120_000,
            _approved=True,
        )

    @property
    def max_evidence_tokens(self) -> int:
        return self.evidence_budget

    def validate(
        self, budget_policy: BudgetPolicy | None = None, *, controlled: bool = False
    ) -> None:
        expected = {
            "32k": (32768, 24488, 4096, 3277, 3),
            "64k": (65536, 49883, 8192, 6554, 7),
        }
        if self.profile not in expected or (controlled and not self._approved):
            raise BatchPolicyInvalid("unknown or unapproved context profile")
        if not controlled and not self._approved and self.context_window != 32768:
            if (
                self.context_window <= 0
                or self.evidence_budget <= 0
                or self.output_reserve <= 0
                or self.safety_margin <= 0
            ):
                raise BatchPolicyInvalid("offline policy contains invalid limits")
            if (
                self.evidence_budget
                + self.output_reserve
                + self.safety_margin
                + self.measured_overhead
                > self.context_window
            ):
                raise ContextBudgetExceeded("batch context budget exceeded")
            return
        context, evidence, output, safety, claims = expected[self.profile]
        if (
            self.context_window,
            self.evidence_budget,
            self.output_reserve,
            self.safety_margin,
            self.max_claims,
        ) != (context, evidence, output, safety, claims):
            raise BatchPolicyInvalid("context profile values are not approved")
        if (
            self.evidence_budget <= 0
            or self.output_reserve <= 0
            or self.safety_margin <= 0
            or self.parallelism != 1
        ):
            raise BatchPolicyInvalid(
                "batch policy contains a non-positive or parallel setting"
            )
        if (
            self.evidence_budget
            + self.output_reserve
            + self.safety_margin
            + self.measured_overhead
            > self.context_window
        ):
            raise ContextBudgetExceeded("batch context budget exceeded")
        if self.chat_template_overhead <= 0:
            raise BatchPolicyInvalid("chat template overhead must be positive")
        if (
            min(
                self.execution_deadline_ms,
                self.max_provider_calls,
                self.max_total_input_tokens,
                self.max_total_output_tokens,
                self.max_total_retries,
                self.max_tokenizer_invocations,
                self.max_adjustments_per_batch,
                self.max_planning_duration_ms,
            )
            <= 0
            or self.max_total_cost < 0
        ):
            raise BatchPolicyInvalid("execution budget contains invalid limits")
        if controlled and not self.tokenizer_identity:
            raise ExactTokenizerUnavailable("tokenizer identity is required")
        if (
            budget_policy is not None
            and (controlled or self._approved)
            and budget_policy.limits.max_output_tokens != self.output_reserve
        ):
            raise OutputBudgetMismatch(
                "batch output reserve does not match provider output budget"
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
    provisional_request_body_hash: str = ""
    rough_evidence_tokens: int = 0
    exact_evidence_tokens: int = 0
    exact_projected_request_tokens: int = 0
    adjustment_rounds: int = 0
    calibration_ratio: float = 1.0
    final_request_body_hash: str = ""

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
        return tuple(
            fragment_id(item) for batch in self.batches for item in batch.fragments
        )

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
        customer_id="batch-planner",
        project_id="batch-planner",
        procurement_case_id="batch-planner",
        run_id="batch-planner",
        registry_number="batch-planner",
        fragments=[item],
    )
    return packet.fragments[0].fragment_id


def canonical_fragment_order(
    item: EvidenceFragmentInput | EvidenceFragment,
) -> tuple[int, int, str]:
    document_order = (
        item.locator.get("document_order", 2**31 - 1) if item.locator else 2**31 - 1
    )
    try:
        document_order = int(document_order)
    except (TypeError, ValueError):
        document_order = 2**31 - 1
    return document_order, _numeric_chunk_index(item), fragment_id(item)


def _batch_hash(
    batch_number: int,
    fragments: list[EvidenceFragmentInput],
    *,
    exact_evidence_tokens: int,
    exact_projected_request_tokens: int,
    adjustment_rounds: int,
    policy: BatchPolicy,
) -> str:
    return canonical_sha256(
        {
            "plan_version": policy.plan_version,
            "profile": policy.profile,
            "batch_ordinal": batch_number,
            "fragment_ids": [fragment_id(item) for item in fragments],
            "batch_content_hash": canonical_sha256(
                [item.model_dump(mode="json") for item in fragments]
            ),
            "exact_evidence_tokens": exact_evidence_tokens,
            "exact_projected_request_tokens": exact_projected_request_tokens,
            "adjustment_rounds": adjustment_rounds,
            "planning_algorithm_version": policy.planning_algorithm_version,
            "calibration_policy_version": policy.calibration_policy_version,
            "output_reserve": policy.output_reserve,
            "safety_margin": policy.safety_margin,
            "chat_template_overhead": policy.chat_template_overhead,
            "max_claims": policy.max_claims,
            "tokenizer_identity": policy.tokenizer_identity,
        }
    )


_DEFAULT_BATCH_POLICY = BatchPolicy()


def build_evidence_batch_plan(
    fragments: Iterable[EvidenceFragmentInput],
    *,
    tokenizer: ExactTokenCounter | Callable[[str], int] | None,
    policy: BatchPolicy = _DEFAULT_BATCH_POLICY,
    request_measure: Callable[[list[EvidenceFragmentInput]], tuple[int, str]]
    | None = None,
    request_token_overhead: int | None = None,
    budget_policy: BudgetPolicy | None = None,
    controlled: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> EvidenceBatchPlan:
    """Pack whole chunks using exact tokens of the complete request candidate."""
    policy.validate(budget_policy, controlled=controlled)
    if tokenizer is None:
        raise ExactTokenizerUnavailable("An approved exact tokenizer is required")
    if controlled and not bool(getattr(tokenizer, "persistent", False)):
        raise ControlledTokenizerNotPersistent(
            "controlled planning requires a persistent tokenizer"
        )
    if (
        request_measure is None
        and request_token_overhead is not None
        and request_token_overhead <= 0
    ):
        raise BatchPolicyInvalid(
            "request token overhead must be measured, not defaulted to zero"
        )
    items = sorted(fragments, key=canonical_fragment_order)
    if not items:
        raise BatchCoverageError("No evidence fragments supplied")
    identities = [fragment_id(item) for item in items]
    if len(identities) != len(set(identities)):
        raise DuplicateAssignment("Duplicate evidence fragment identity")
    corpus_hash = canonical_sha256({"fragment_ids": identities})
    batches: list[EvidenceBatch] = []
    cursor = 0
    started = clock()
    exact_calls = 0
    counter_start = int(getattr(tokenizer, "invocations", 0))
    conservative_ratio = 1.05

    def ensure_deadline() -> None:
        if (clock() - started) * 1000 > policy.max_planning_duration_ms:
            raise PlanningTimeout("bounded planning deadline exceeded")

    def measured_invocations() -> int:
        return max(
            exact_calls,
            int(getattr(tokenizer, "invocations", counter_start)) - counter_start,
        )

    def ensure_invocation_capacity() -> None:
        if measured_invocations() >= policy.max_tokenizer_invocations:
            raise TokenizerInvocationLimitExceeded(
                "bounded tokenizer invocation limit exceeded"
            )

    def exact(text: str) -> int:
        nonlocal exact_calls
        ensure_deadline()
        ensure_invocation_capacity()
        exact_calls += 1
        return int(tokenizer(text))

    def rough_tokens(item: EvidenceFragmentInput) -> int:
        estimate = (
            int(item.locator.get("token_estimate", 0) or 0) if item.locator else 0
        )
        return estimate if estimate > 0 else max(1, len(item.text.encode("utf-8")) // 3)

    while cursor < len(items):
        ensure_deadline()
        calibrated_limit = max(1, int(policy.max_evidence_tokens / conservative_ratio))
        selected: list[EvidenceFragmentInput] = []
        rough_evidence_tokens = 0
        while cursor + len(selected) < len(items):
            item = items[cursor + len(selected)]
            item_tokens = rough_tokens(item)
            if selected and rough_evidence_tokens + item_tokens > calibrated_limit:
                break
            if not selected and item_tokens > calibrated_limit:
                # The exact check below decides whether this source chunk is genuinely oversized.
                selected.append(item)
                rough_evidence_tokens += item_tokens
                break
            selected.append(item)
            rough_evidence_tokens += item_tokens
        ordinal = len(batches) + 1
        adjustment_rounds = 0
        if request_measure is not None:
            while True:
                ensure_deadline()
                ensure_invocation_capacity()
                projected, request_hash = request_measure(selected)
                if measured_invocations() > policy.max_tokenizer_invocations:
                    raise TokenizerInvocationLimitExceeded(
                        "bounded tokenizer invocation limit exceeded"
                    )
                evidence_tokens = exact("\n".join(item.text for item in selected))
                fits = (
                    evidence_tokens <= policy.max_evidence_tokens
                    and projected + policy.output_reserve + policy.safety_margin
                    <= policy.context_window
                )
                if fits:
                    break
                if len(selected) == 1:
                    raise OversizedEvidenceChunk(
                        "One source chunk exceeds the safe context budget"
                    )
                if adjustment_rounds >= policy.max_adjustments_per_batch:
                    raise PlanningConvergenceFailed(
                        "bounded batch adjustment did not converge"
                    )
                available = min(
                    policy.max_evidence_tokens,
                    policy.context_window
                    - policy.output_reserve
                    - policy.safety_margin,
                )
                observed = max(evidence_tokens, projected)
                target_count = max(
                    1, int(len(selected) * available / max(1, observed) * 0.95)
                )
                required_drop = max(1, int(len(selected) * 0.05))
                target_count = min(target_count, len(selected) - required_drop)
                selected = selected[:target_count]
                rough_evidence_tokens = sum(rough_tokens(item) for item in selected)
                adjustment_rounds += 1
        else:
            request_hash = ""
            while True:
                evidence_tokens = exact("\n".join(item.text for item in selected))
                projected = evidence_tokens + int(request_token_overhead or 0)
                if evidence_tokens <= policy.max_evidence_tokens:
                    break
                if len(selected) == 1:
                    raise OversizedEvidenceChunk(
                        "One source chunk exceeds the safe context budget"
                    )
                if adjustment_rounds >= policy.max_adjustments_per_batch:
                    raise PlanningConvergenceFailed(
                        "bounded batch adjustment did not converge"
                    )
                target_count = max(
                    1,
                    int(
                        len(selected)
                        * policy.max_evidence_tokens
                        / evidence_tokens
                        * 0.95
                    ),
                )
                target_count = min(
                    target_count, len(selected) - max(1, int(len(selected) * 0.05))
                )
                selected = selected[:target_count]
                rough_evidence_tokens = sum(rough_tokens(item) for item in selected)
                adjustment_rounds += 1
        if request_measure is not None:
            conservative_ratio = max(
                conservative_ratio,
                evidence_tokens / max(1, rough_evidence_tokens) * 1.05,
            )
        batches.append(
            EvidenceBatch(
                ordinal,
                tuple(selected),
                evidence_tokens,
                projected,
                policy.output_reserve,
                policy.safety_margin,
                _batch_hash(
                    ordinal,
                    selected,
                    exact_evidence_tokens=evidence_tokens,
                    exact_projected_request_tokens=projected,
                    adjustment_rounds=adjustment_rounds,
                    policy=policy,
                ),
                request_hash,
                rough_evidence_tokens=rough_evidence_tokens,
                exact_evidence_tokens=evidence_tokens,
                exact_projected_request_tokens=projected,
                adjustment_rounds=adjustment_rounds,
                calibration_ratio=conservative_ratio,
            )
        )
        cursor += len(selected)
    assigned = [fragment_id(item) for batch in batches for item in batch.fragments]
    if assigned != identities:
        raise BatchCoverageError("Batch coverage is not exactly one-to-one")
    tokenizer_identity = policy.tokenizer_identity or str(
        getattr(tokenizer, "identity", "offline-estimated")
    )
    unsigned = {
        "plan_version": policy.plan_version,
        "profile": policy.profile,
        "tokenizer_identity": tokenizer_identity,
        "context_window": policy.context_window,
        "evidence_budget": policy.evidence_budget,
        "output_reserve": policy.output_reserve,
        "safety_margin": policy.safety_margin,
        "chat_template_overhead": policy.chat_template_overhead,
        "max_claims": policy.max_claims,
        "execution_deadline_ms": policy.execution_deadline_ms,
        "max_provider_calls": policy.max_provider_calls,
        "max_total_input_tokens": policy.max_total_input_tokens,
        "max_total_output_tokens": policy.max_total_output_tokens,
        "max_total_retries": policy.max_total_retries,
        "max_total_cost": policy.max_total_cost,
        "planning_algorithm_version": policy.planning_algorithm_version,
        "calibration_policy_version": policy.calibration_policy_version,
        "max_tokenizer_invocations": policy.max_tokenizer_invocations,
        "max_adjustments_per_batch": policy.max_adjustments_per_batch,
        "max_planning_duration_ms": policy.max_planning_duration_ms,
        "measured_overhead": policy.measured_overhead,
        "corpus_evidence_hash": corpus_hash,
        "batches": [
            batch.__dict__
            | {"fragments": [fragment_id(item) for item in batch.fragments]}
            for batch in batches
        ],
    }
    return EvidenceBatchPlan(
        policy.plan_version,
        policy,
        corpus_hash,
        tuple(batches),
        canonical_sha256(unsigned),
        tokenizer_identity,
    )


class CommandTokenCounter:
    """Bounded stdin tokenizer adapter; never places evidence in argv or errors."""

    def __init__(self, command: str, *, identity: str, timeout_seconds: int = 30):
        self.command = tuple(shlex.split(command))
        self.identity = identity
        self.timeout_seconds = timeout_seconds
        self.persistent = False
        self.subprocess_count = 0
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
        self.subprocess_count += 1
        try:
            completed = subprocess.run(
                self.command,
                input=text.encode("utf-8"),
                check=True,
                capture_output=True,
                timeout=self.timeout_seconds,
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


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise TokenizerEndpointUnsafe("tokenizer redirect is unsafe")


class LlamaServerTokenCounter:
    """Persistent in-process exact counter backed only by loopback /tokenize."""

    def __init__(
        self,
        endpoint: str,
        *,
        identity: str,
        timeout_seconds: float = 30.0,
        opener: Callable[..., Any] | None = None,
    ):
        parsed = urlsplit(endpoint.strip())
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.rstrip("/") != "/tokenize"
            or parsed.port is None
        ):
            raise TokenizerEndpointUnsafe("tokenizer endpoint is not safe loopback")
        if not identity or timeout_seconds <= 0:
            raise ExactTokenizerUnavailable("tokenizer configuration is incomplete")
        self.endpoint = endpoint.strip()
        self.identity = identity
        self.timeout_seconds = timeout_seconds
        self.persistent = True
        self.subprocess_count = 0
        self.tokenizer_mode = "server-tokenize-v2"
        self.invocations = 0
        self.cache_hits = 0
        self.request_duration_ms_total = 0
        self.request_duration_ms_max = 0
        self.bytes_submitted_total = 0
        self._cache: dict[str, int] = {}
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirect()).open

    def __call__(self, text: str) -> int:
        data = text.encode("utf-8")
        cache_key = hashlib.sha256(data).hexdigest()
        if cache_key in self._cache:
            self.cache_hits += 1
            return self._cache[cache_key]
        body = canonical_json_bytes(
            {
                "content": text,
                "add_special": False,
                "parse_special": True,
                "with_pieces": False,
            }
        )
        request = Request(
            self.endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                if (
                    getattr(response, "geturl", lambda: self.endpoint)()
                    != self.endpoint
                ):
                    raise TokenizerEndpointUnsafe("tokenizer redirect is unsafe")
                response_body = response.read()
        except TimeoutError as exc:
            raise TokenizerTimeout("exact tokenizer timed out") from exc
        except TokenizerEndpointUnsafe:
            raise
        except (HTTPError, URLError, OSError) as exc:
            raise ExactTokenizerUnavailable("exact tokenizer request failed") from exc
        elapsed = int((time.monotonic() - started) * 1000)
        self.request_duration_ms_total += elapsed
        self.request_duration_ms_max = max(self.request_duration_ms_max, elapsed)
        self.bytes_submitted_total += len(data)
        self.invocations += 1
        try:
            import json

            decoded = json.loads(response_body)
            tokens = decoded.get("tokens") if isinstance(decoded, dict) else None
        except (TypeError, ValueError):
            tokens = None
        if not isinstance(tokens, list) or any(
            not isinstance(token, int) for token in tokens
        ):
            raise TokenizerResponseInvalid("exact tokenizer response invalid")
        value = len(tokens)
        self._cache[cache_key] = value
        return value


def tokenizer_from_environment() -> ExactTokenCounter:
    identity = os.environ.get("ARV003_TOKENIZER_IDENTITY", "")
    endpoint = os.environ.get("ARV003_LLAMA_TOKENIZER_URL", "")
    if endpoint:
        return LlamaServerTokenCounter(endpoint, identity=identity)
    command = os.environ.get("ARV003_EXACT_TOKENIZER_COMMAND", "")
    return CommandTokenCounter(command, identity=identity)
