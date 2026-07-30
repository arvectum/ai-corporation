#!/usr/bin/env python3
"""One-call synthetic compatibility probe for the local llama.cpp Gate 5 path.

The probe never reads customer data or the database. It exercises the exact
schema-constrained OpenAI-compatible adapter with one synthetic evidence
fragment and prints only a sanitized outcome.
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

_SYNTHETIC_TEXT = (
    "Technical requirement: the cable shall have copper conductors and a rated "
    "voltage of 0.66 kV."
)
_ALLOWED_FIELD_PATH = "requirements.technical_requirements"
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
        "provider_wire_claim_schema_invalid",
        "provider_wire_duplicate_reference",
        "provider_wire_fragment_not_found",
        "provider_wire_quote_empty",
        "provider_wire_quote_not_found",
        "provider_wire_reference_schema_invalid",
    }
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--approved-policy", required=True, type=Path)
    return parser.parse_args()


def _sanitized_invalid_response_code(exc: InvalidProviderResponseError) -> str:
    candidate = str(exc).strip().lower()
    if candidate in _SAFE_INVALID_RESPONSE_CODES:
        return candidate
    return "provider_response_invalid"


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


def _build_probe_request(policy):
    packet = build_evidence_packet(
        customer_id="synthetic-customer",
        project_id="synthetic-project",
        procurement_case_id="synthetic-case",
        run_id="synthetic-run",
        registry_number="synthetic-registry",
        fragments=[
            EvidenceFragmentInput(
                document_id="synthetic-document",
                document_name="synthetic.txt",
                chunk_id="synthetic-chunk-1",
                locator={"document_order": 0, "chunk_index": 0},
                text=_SYNTHETIC_TEXT,
            )
        ],
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
        map_mode=True,
        max_claims=1,
        allowed_field_paths=[_ALLOWED_FIELD_PATH],
        context_profile="32k",
        tokenizer_identity="synthetic-probe-no-tokenizer-call",
        evidence_budget=24488,
        chat_template_overhead=32,
        execution_deadline_ms=120000,
    )


def main() -> int:
    args = _arguments()
    try:
        policy = load_approved_provider_policy(args.approved_policy)
        base_url, api_key = _provider_boundary(policy)
        install_llama_schema_constraint()
        request = _build_probe_request(policy)
        provider = OpenAICompatibleProductionLLMProvider(
            OpenAICompatibleTransportConfig(base_url=base_url, api_key=api_key)
        )
        response = provider.generate(request)

        if not response.claims:
            print(
                json.dumps(
                    {
                        "status": "schema_valid_empty_response",
                        "provider_call_count": 1,
                        "claim_count": 0,
                        "model": policy.model,
                    },
                    sort_keys=True,
                )
            )
            return 4

        if len(response.claims) != 1:
            raise RuntimeError("probe_claim_count_invalid")
        claim = response.claims[0]
        if claim.field_path != _ALLOWED_FIELD_PATH:
            raise RuntimeError("probe_field_path_invalid")
        if not claim.evidence_references:
            raise RuntimeError("probe_reference_missing")
        if any(reference.quote not in _SYNTHETIC_TEXT for reference in claim.evidence_references):
            raise RuntimeError("probe_quote_not_grounded")

        print(
            json.dumps(
                {
                    "status": "full_compact_contract_passed",
                    "provider_call_count": 1,
                    "claim_count": 1,
                    "reference_count": len(claim.evidence_references),
                    "model": policy.model,
                    "retry_count": response.retry_count,
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
            code = "probe_failed"
        print(code or "probe_failed", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
