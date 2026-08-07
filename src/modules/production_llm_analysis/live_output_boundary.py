"""Repository-owned live output boundary for the ARV-001 controlled path.

This module owns the *single* source of truth for the grammar-whitespace
contract derived from llama.cpp ``b10240`` (``common/json-schema-to-grammar.cpp``)
and the exact-token output-budget proof. The live sentinel schema
(``build_live_compact_llama_schema``) is the only schema implementation that
may reach the controlled transport; this module never builds a competing
public schema and never falls back to a ``chars//4`` heuristic for acceptance.
"""

from __future__ import annotations

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
# Canonic maximal whitespace for one grammar `space` slot.
GRAMMAR_WHITESPACE_SLOT = "\n\n" + " " * 20


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


def _longest_value(values: list[str]) -> str:
    """Return the lexicographically-last of the equally-longest enum literals."""
    longest = max(len(v) for v in values) if values else 0
    longest_values = sorted(v for v in values if len(v) == longest)
    return longest_values[-1] if longest_values else ""


def _max_object_value(pairs: list[tuple[str, str]]) -> str:
    """Serialize an object with maximal grammar whitespace in every slot.

    Matches b10240 ``_build_object_rule``: ``"{" space (kv ("," space kv)* )?
    space "}"`` where each ``kv`` is ``"key" space ":" space value``.
    """
    ws = GRAMMAR_WHITESPACE_SLOT
    kv = [f'"{k}"{ws}:{ws}{v}' for k, v in pairs]
    return "{" + ws + ("," + ws).join(kv) + ws + "}"


def _max_array_value(values: list[str]) -> str:
    """Serialize an array with maximal grammar whitespace in every slot.

    Matches the b10240 ``array`` builtin: ``"[" space (value ("," space value)*)?
    space "]"``.
    """
    ws = GRAMMAR_WHITESPACE_SLOT
    return "[" + ws + ("," + ws).join(values) + ws + "]"


def _count_whitespace_slots(text: str) -> int:
    return text.count(GRAMMAR_WHITESPACE_SLOT)


def build_maximal_live_completion_payload(
    request: ProductionLLMAnalysisRequest,
) -> MaximalLiveCompletion:
    """Return the exact maximal live-sentinel completion the provider may emit.

    The payload is built from the single live schema
    (``build_live_compact_llama_schema``) so the bound cannot diverge from the
    schema enforced on the wire. It uses:
      * claims = maxItems (every claim present, one evidence reference);
      * the longest ``field_path`` enum literal and ``fragment_id`` enum literal;
      * the server-owned ``const`` sentinels for claim_id/value/quote;
      * ``provider_confidence = 1.0`` (longest schema-valid number in [0, 1]).
    Every grammar ``space`` slot is replaced by the b10240 maximal whitespace
    (2 newlines + 20 spaces). Because the JSON-schema grammar only permits
    whitespace at named ``space`` slots (never inside a ``string`` production),
    this serialization is an exact upper-bound representation.
    """
    schema = build_live_compact_llama_schema(request)
    claims_schema = schema["properties"]["claims"]
    max_claims = request.max_claims if request.max_claims else CLOSED_MAX_CLAIMS
    claim_schema = claims_schema["items"]
    field_enum = claim_schema["properties"]["field_path"]["enum"]
    evidence_schema = claim_schema["properties"]["evidence_references"]["items"][
        "properties"
    ]
    ref_enum = evidence_schema["fragment_id"]["enum"]

    claim = _max_object_value(
        [
            ("claim_id", f'"{_SERVER_CLAIM_ID_SENTINEL}"'),
            ("field_path", f'"{_longest_value(field_enum)}"'),
            ("value", f'"{_SERVER_FRAGMENT_VALUE_SENTINEL}"'),
            ("provider_confidence", "1.0"),
            (
                "evidence_references",
                _max_array_value(
                    [
                        _max_object_value(
                            [
                                (
                                    "fragment_id",
                                    f'"{_longest_value(ref_enum)}"',
                                ),
                                ("quote", f'"{_SERVER_FRAGMENT_QUOTE_SENTINEL}"'),
                            ]
                        )
                    ]
                ),
            ),
        ]
    )
    claims = _max_array_value([claim] * max_claims)
    payload = _max_object_value([("claims", claims)])
    return MaximalLiveCompletion(
        content=payload,
        content_sha256=canonical_sha256(payload),
        grammar_whitespace_slots=_count_whitespace_slots(payload),
    )


def verify_exact_live_output_budget(
    request: ProductionLLMAnalysisRequest,
    *,
    tokenizer: Any | None = None,
    output_budget: int = APPROVED_OUTPUT_BUDGET,
    tokens_limit: int = EXACT_LIVE_OUTPUT_TOKENS_LIMIT,
    minimum_margin: int = EXACT_LIVE_OUTPUT_SAFETY_MARGIN,
) -> dict[str, Any]:
    """Measure the maximal live payload with the approved persistent tokenizer.

    This is the controlled/pre-provider acceptance gate. It fails closed and
    never falls back to a ``bytes//4`` or ``chars//4`` heuristic. The tokenizer
    must be the same persistent exact counter returned by
    ``tokenizer_from_environment()`` (approved ``/tokenize``). If no persistent
    exact tokenizer is available the proof raises
    ``exact_live_output_tokenizer_unavailable``.
    """
    tokenizer = tokenizer
    if tokenizer is None:
        try:
            tokenizer = tokenizer_from_environment()
        except (ExactTokenizerUnavailable, OSError):
            raise ExactLiveOutputTokenizerUnavailable
    identity = str(getattr(tokenizer, "identity", "") or "")
    if not identity or not bool(getattr(tokenizer, "persistent", False)):
        raise ExactLiveOutputTokenizerUnavailable
    maximal = build_maximal_live_completion_payload(request)
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
        "output_budget": output_budget,
        "safety_margin_tokens": safety_margin,
        "grammar_whitespace_contract_version": GRAMMAR_WHITESPACE_CONTRACT_VERSION,
        "grammar_whitespace_max_bytes_per_slot": GRAMMAR_WHITESPACE_MAX_BYTES_PER_SLOT,
        "grammar_whitespace_included": True,
    }