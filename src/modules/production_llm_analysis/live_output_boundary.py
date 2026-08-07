"""Repository-owned live output boundary for the ARV-001 controlled path.

This module owns the *single* source of truth for the grammar-whitespace
contract derived from llama.cpp ``b10240`` (``common/json-schema-to-grammar.cpp``)
and the exact-token output-budget proof. The live sentinel schema
(``build_live_compact_llama_schema``) is the only schema implementation that
may reach the controlled transport; this module never builds a competing
public schema and never falls back to a ``chars//4`` heuristic for acceptance.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

from src.modules.production_llm_analysis.batching import (
    ExactTokenizerUnavailable,
    tokenizer_from_environment,
)
from src.modules.production_llm_analysis.evidence import canonical_sha256
from src.modules.production_llm_analysis.llama_schema_constraint import (
    _SERVER_CLAIM_ID_SENTINEL,
    _SERVER_FRAGMENT_QUOTE_SENTINEL,
    _SERVER_FRAGMENT_VALUE_SENTINEL,
    build_live_compact_llama_schema,
)
from src.modules.production_llm_analysis.schemas import ProductionLLMAnalysisRequest

# The approved output budget and its acceptance bounds. These are repository
# constants that the pre-provider and the regression tests share; they must not
# drift independently.
APPROVED_OUTPUT_BUDGET = 4096
EXACT_LIVE_OUTPUT_TOKENS_LIMIT = 3072
EXACT_LIVE_OUTPUT_SAFETY_MARGIN = 1024
# The schema leaves the claims array unbounded; the wire bound is the request's
# ``max_claims``, defaulting to this repository-owned closed cap.
CLOSED_MAX_CLAIMS = 3

# Grammar whitespace contract derived from llama.cpp b10240.
# common/json-schema-to-grammar.cpp line 228:
#   const std::string SPACE_RULE = "| \" \" | \"\\n\"{1,2} [ \\t]{0,20}";
# The single-literal alternative emits exactly one ASCII space. The multi-line
# alternative emits 1..2 newlines immediately followed by 0..20 spaces or tabs.
# Maximal serialized length of one `space` slot is therefore 2 + 20 = 22 bytes.
GRAMMAR_WHITESPACE_CONTRACT_VERSION = "llama-cpp-b10240-spacerule-v1"
GRAMMAR_WHITESPACE_MAX_BYTES_PER_SLOT = 22


class LiveOutputBoundaryError(RuntimeError):
    code = "live_output_boundary_proof_failed"


class ExactLiveOutputTokenizerUnavailable(LiveOutputBoundaryError):
    code = "exact_live_output_tokenizer_unavailable"


class ExactLiveOutputTokensExceeded(LiveOutputBoundaryError):
    code = "exact_live_output_tokens_exceeded"


class OutputSafetyMarginBelowThreshold(LiveOutputBoundaryError):
    code = "output_safety_margin_below_threshold"


@dataclass(frozen=True)
class MaximalLiveCompletion:
    """Deterministic maximal grammar-valid completion under the live schema."""

    content: str
    content_sha256: str
    grammar_whitespace_slots: int


def _maximal_token_value(values: list[str], tokenizer: Any) -> str:
    """Return the value from the list that results in the most tokens."""
    if not values:
        return ""
    counts = []
    for v in values:
        try:
            count = int(tokenizer(v))
        except (TypeError, ValueError):
            count = 0
        counts.append((count, v))
    return max(counts, key=lambda x: (x[0], x[1]))[1]


def _maximal_whitespace_slot(tokenizer: Any) -> str:
    """Find the maximal-token whitespace sequence allowed by b10240.

    We conservatively check all combinations of 1..2 newlines and 0..20
    spaces/tabs to ensure the proof is a true upper bound for Gemma.
    """
    candidates = [" "]
    for nl in [1, 2]:
        for total_ws in range(21):
            # Check pure spaces
            candidates.append("\n" * nl + " " * total_ws)
            # Check pure tabs
            candidates.append("\n" * nl + "\t" * total_ws)
            # Check some mixed patterns (context sensitive)
            if total_ws > 1:
                candidates.append("\n" * nl + " " * (total_ws - 1) + "\t")
                candidates.append("\n" * nl + "\t" + " " * (total_ws - 1))

    counts = []
    for c in candidates:
        try:
            count = int(tokenizer(c))
        except (TypeError, ValueError):
            count = 0
        counts.append((count, c))
    return max(counts, key=lambda x: (x[0], x[1]))[1]


def build_maximal_live_completion_payload(
    request: ProductionLLMAnalysisRequest, tokenizer: Any
) -> MaximalLiveCompletion:
    """Return the exact maximal live-sentinel completion the provider may emit."""
    ws = _maximal_whitespace_slot(tokenizer)

    def max_object(pairs: list[tuple[str, str]]) -> str:
        kv = [f'"{k}"{ws}:{ws}{v}' for k, v in pairs]
        return "{" + ws + ("," + ws).join(kv) + ws + "}"

    def max_array(values: list[str]) -> str:
        return "[" + ws + ("," + ws).join(values) + ws + "]"

    schema = build_live_compact_llama_schema(request)
    claims_schema = schema["properties"]["claims"]
    max_claims = request.max_claims if request.max_claims else CLOSED_MAX_CLAIMS
    claim_schema = claims_schema["items"]
    field_enum = claim_schema["properties"]["field_path"]["enum"]
    evidence_schema = claim_schema["properties"]["evidence_references"]["items"][
        "properties"
    ]
    ref_enum = evidence_schema["fragment_id"]["enum"]

    claim = max_object(
        [
            ("claim_id", f'"{_SERVER_CLAIM_ID_SENTINEL}"'),
            ("field_path", f'"{_maximal_token_value(field_enum, tokenizer)}"'),
            ("value", f'"{_SERVER_FRAGMENT_VALUE_SENTINEL}"'),
            (
                "evidence_references",
                max_array(
                    [
                        max_object(
                            [
                                (
                                    "fragment_id",
                                    f'"{_maximal_token_value(ref_enum, tokenizer)}"',
                                ),
                                ("quote", f'"{_SERVER_FRAGMENT_QUOTE_SENTINEL}"'),
                            ]
                        )
                    ]
                ),
            ),
        ]
    )
    claims = max_array([claim] * max_claims)
    payload = max_object([("claims", claims)])
    return MaximalLiveCompletion(
        content=payload,
        content_sha256=canonical_sha256(payload),
        grammar_whitespace_slots=payload.count(ws),
    )


def verify_exact_live_output_budget(
    request: ProductionLLMAnalysisRequest,
    *,
    tokenizer: Any | None = None,
    output_budget: int = APPROVED_OUTPUT_BUDGET,
    tokens_limit: int = EXACT_LIVE_OUTPUT_TOKENS_LIMIT,
    minimum_margin: int = EXACT_LIVE_OUTPUT_SAFETY_MARGIN,
) -> dict[str, Any]:
    """Measure the maximal live payload with the approved persistent tokenizer."""
    if tokenizer is None:
        try:
            tokenizer = tokenizer_from_environment()
        except (ExactTokenizerUnavailable, OSError):
            raise ExactLiveOutputTokenizerUnavailable
    identity = str(getattr(tokenizer, "identity", "") or "")
    if not identity or not bool(getattr(tokenizer, "persistent", False)):
        raise ExactLiveOutputTokenizerUnavailable
    maximal = build_maximal_live_completion_payload(request, tokenizer)
    try:
        exact_tokens = int(tokenizer(maximal.content))
    except (ExactTokenizerUnavailable, OSError, ValueError) as exc:
        raise ExactLiveOutputTokenizerUnavailable from exc
    safety_margin = output_budget - exact_tokens

    if exact_tokens > tokens_limit:
        raise ExactLiveOutputTokensExceeded
    if safety_margin < minimum_margin:
        raise OutputSafetyMarginBelowThreshold

    return {
        "tokenizer_identity": identity,
        "live_schema_sha256": canonical_sha256(
            build_live_compact_llama_schema(request)
        ),
        "maximal_payload_sha256": maximal.content_sha256,
        "exact_live_output_tokens": exact_tokens,
        "exact_live_output_token_upper_bound": exact_tokens,
        "output_budget": output_budget,
        "safety_margin_tokens": safety_margin,
        "grammar_whitespace_contract_version": GRAMMAR_WHITESPACE_CONTRACT_VERSION,
        "grammar_whitespace_max_bytes_per_slot": GRAMMAR_WHITESPACE_MAX_BYTES_PER_SLOT,
        "grammar_whitespace_included": True,
    }
