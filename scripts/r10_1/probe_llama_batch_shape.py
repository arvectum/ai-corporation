#!/usr/bin/env python3
"""One-call long-context compatibility probe for the local llama.cpp Gate 5 path.

The probe is database-free and customer-data-free. It exercises the exact
schema-constrained adapter with a deterministic synthetic batch shaped like a
real 32K map request, while forcing template-level thinking off.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.modules.production_llm_analysis.contracts import R10_1_CONTROLLED_MAP_CONTRACT
from src.modules.production_llm_analysis.controlled_evidence import (
    load_approved_provider_policy,
)
from src.modules.production_llm_analysis.evidence import build_evidence_packet
from src.modules.production_llm_analysis.llama_reasoning_control import (
    install_llama_non_reasoning_mode,
)
from src.modules.production_llm_analysis.llama_schema_constraint import (
    install_llama_schema_constraint,
)
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
    OpenAICompatibleTransportConfig,
)
from src.modules.production_llm_analysis.schemas import EvidenceFragmentInput
from src.modules.production_llm_analysis.service import build_production_llm_request
from src.shared.config.settings import get_settings
from src.shared.llm.transport import (
    InvalidProviderResponseError,
    ProviderBudgetExceededError,
    ProviderPermanentError,
    ProviderTimeoutError,
    ProviderTransientError,
)

_FRAGMENT_COUNT = 96
_ALLOWED_FIELD_PATHS = [
    "requirements.document_requirements",
    "requirements.evaluation_criteria",
    "requirements.qualification_requirements",
    "requirements.technical_requirements",
]
_SAFE_INVALID_RESPONSE_CODES = frozenset(
    {
        "provider_claims_not_list",
        "provider_content_schema_mismatch",
        "provider_input_tokens_invalid",
        "provider_message_content_invalid",
        "provider_message_content_missing",
        "provider_output_tokens_invalid",
        "provider_request_id_invalid",
        "provider_response_invalid",
        "provider_response_invalid_envelope",
        "provider_response_invalid_json",
        "provider_usage_invalid",
        "provider_wire_claim_id_sentinel_invalid",
        "provider_wire_claim_schema_invalid",
        "provider_wire_duplicate_reference",
        "provider_wire_field_path_not_extractive",
        "provider_wire_fragment_not_found",
        "provider_wire_quote_empty",
        "provider_wire_quote_not_found",
        "provider_wire_quote_sentinel_invalid",
        "provider_wire_reference_count_invalid",
        "provider_wire_reference_schema_invalid",
        "provider_wire_value_sentinel_invalid",
    }
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--approved-policy", required=True, type=Path)
    return parser.parse_args()


def _single_attempt_budget(policy):
    limits = policy.budget.limits.model_copy(update={"max_retries": 0})
    return policy.budget.model_copy(update={"limits": limits})


def _provider_boundary(policy):
    settings = get_settings()
    configured_provider = (settings.llm_provider or "").strip().lower()
    if configured_provider != policy.provider:
        raise RuntimeError("configured_provider_not_approved")
    if settings.llm_model != policy.model:
        raise RuntimeError("configured_model_not_approved")
    if policy.provider not in {"openai", "openai_compatible"}:
        raise RuntimeError("provider_not_supported_by_probe")
    if not settings.openai_api_key:
        raise RuntimeError("provider_credential_missing")
    return settings.openai_base_url, settings.openai_api_key


def _synthetic_text(index: int) -> str:
    prefix = f"Synthetic technical requirement block {index:03d}. "
    sentence = (
        "The supplied cable shall use copper conductors, rated insulation, "
        "factory quality control, traceable marking, and documented acceptance. "
    )
    return prefix + (sentence * 9) + f"Unique marker ARV-BATCH-{index:03d}."


def _build_batch_shaped_request(policy):
    fragments = [
        EvidenceFragmentInput(
            document_id=f"synthetic-document-{index // 24:02d}",
            document_name=f"synthetic-{index // 24:02d}.txt",
            chunk_id=f"synthetic-chunk-{index:03d}",
            locator={"document_order": index // 24, "chunk_index": index},
            text=_synthetic_text(index),
        )
        for index in range(_FRAGMENT_COUNT)
    ]
    packet = build_evidence_packet(
        customer_id="synthetic-customer",
        project_id="synthetic-project",
        procurement_case_id="synthetic-case",
        run_id="synthetic-run",
        registry_number="synthetic-registry",
        fragments=fragments,
    )
    contract = R10_1_CONTROLLED_MAP_CONTRACT
    return build_production_llm_request(
        evidence_packet=packet,
        provider=policy.provider,
        provider_wire_contract_version=contract.provider_wire_contract_version,
        model=policy.model,
        prompt_id=contract.prompt_id,
        prompt_version=contract.prompt_version,
        output_schema_id=contract.output_schema_id,
        output_schema_version=contract.output_schema_version,
        grounding_policy_version=contract.grounding_policy_version,
        budget_policy=_single_attempt_budget(policy),
        batch_plan_version=contract.plan_version,
        batch_plan_hash="1" * 64,
        batch_hash="2" * 64,
        batch_ordinal=0,
        batch_count=17,
        corpus_evidence_hash="3" * 64,
        map_mode=True,
        max_claims=3,
        allowed_field_paths=_ALLOWED_FIELD_PATHS,
        context_profile="32k",
        tokenizer_identity="synthetic-batch-probe-no-tokenizer-call",
        evidence_budget=24488,
        chat_template_overhead=32,
        execution_deadline_ms=600000,
    )


def _sanitized_invalid_response_code(exc: InvalidProviderResponseError) -> str:
    candidate = str(exc).strip().lower()
    if candidate in _SAFE_INVALID_RESPONSE_CODES:
        return candidate
    return "provider_response_invalid"


def _validate_response(request, response) -> tuple[int, int]:
    if not response.claims:
        raise RuntimeError("batch_probe_empty_claims")
    if len(response.claims) > 3:
        raise RuntimeError("batch_probe_claim_count_invalid")

    fragments = {
        fragment.fragment_id: fragment for fragment in request.evidence_packet.fragments
    }
    reference_count = 0
    for claim in response.claims:
        if claim.field_path not in _ALLOWED_FIELD_PATHS:
            raise RuntimeError("batch_probe_field_path_invalid")
        if len(claim.evidence_references) != 1:
            raise RuntimeError("batch_probe_reference_count_invalid")
        reference = claim.evidence_references[0]
        fragment = fragments.get(reference.fragment_id)
        if fragment is None:
            raise RuntimeError("batch_probe_fragment_invalid")
        if claim.value != fragment.text or reference.quote != fragment.text:
            raise RuntimeError("batch_probe_server_grounding_invalid")
        reference_count += 1
    return len(response.claims), reference_count


def main() -> int:
    args = _arguments()
    try:
        policy = load_approved_provider_policy(args.approved_policy)
        base_url, api_key = _provider_boundary(policy)
        install_llama_schema_constraint()
        install_llama_non_reasoning_mode()
        request = _build_batch_shaped_request(policy)
        provider = OpenAICompatibleProductionLLMProvider(
            OpenAICompatibleTransportConfig(base_url=base_url, api_key=api_key)
        )
        request_body = provider._build_request_body(request)
        if request_body.get("chat_template_kwargs", {}).get("enable_thinking") is not False:
            raise RuntimeError("batch_probe_thinking_not_disabled")

        response = provider.generate(request)
        claim_count, reference_count = _validate_response(request, response)
        print(
            json.dumps(
                {
                    "status": "batch_shaped_compact_contract_passed",
                    "provider_call_count": 1,
                    "claim_count": claim_count,
                    "reference_count": reference_count,
                    "retry_count": response.retry_count,
                    "server_owned_grounding": True,
                    "reasoning_enabled": False,
                    "synthetic_fragment_count": _FRAGMENT_COUNT,
                    "model": policy.model,
                },
                sort_keys=True,
            )
        )
        return 0
    except InvalidProviderResponseError as exc:
        print(
            json.dumps(
                {
                    "status": "invalid_provider_response",
                    "code": _sanitized_invalid_response_code(exc),
                    "provider_call_count": 1,
                    "reasoning_enabled": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except ProviderPermanentError:
        print("provider_request_rejected", file=sys.stderr)
        return 3
    except ProviderTimeoutError:
        print("provider_timeout", file=sys.stderr)
        return 3
    except ProviderTransientError:
        print("provider_transient_failure", file=sys.stderr)
        return 3
    except ProviderBudgetExceededError:
        print("provider_budget_exceeded", file=sys.stderr)
        return 3
    except Exception as exc:
        code = str(exc).strip().lower()
        if not code.replace("_", "").isalnum() or len(code) > 120:
            code = "batch_probe_failed"
        print(code or "batch_probe_failed", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
