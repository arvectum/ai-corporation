from scripts.r10_1.probe_llama_schema_contract import (
    _ALLOWED_FIELD_PATH,
    _build_probe_request,
    _sanitized_invalid_response_code,
    _single_attempt_budget,
)
from src.modules.production_llm_analysis.controlled_evidence import (
    ApprovedControlledProviderPolicy,
)
from src.shared.llm.transport import InvalidProviderResponseError

from .conftest import make_policy


def _policy(*, retries=3):
    return ApprovedControlledProviderPolicy(
        policy_version="probe-test-v1",
        provider="openai_compatible",
        model="arvectum-gemma4-12b-q4km",
        budget=make_policy(max_output_tokens=4096, max_retries=retries),
    )


def test_probe_budget_forces_one_http_attempt_without_other_budget_changes():
    policy = _policy(retries=3)
    budget = _single_attempt_budget(policy)

    assert budget.limits.max_retries == 0
    assert budget.limits.max_output_tokens == 4096
    assert budget.limits.max_input_tokens == policy.budget.limits.max_input_tokens
    assert budget.pricing == policy.budget.pricing


def test_probe_request_is_synthetic_compact_and_batch_bound():
    request = _build_probe_request(_policy())

    assert request.provider == "openai_compatible"
    assert request.model == "arvectum-gemma4-12b-q4km"
    assert request.provider_wire_contract_version == "compact-safe-v1"
    assert request.map_mode is True
    assert request.max_claims == 1
    assert request.allowed_field_paths == [_ALLOWED_FIELD_PATH]
    assert request.budget_policy.limits.max_retries == 0
    assert request.evidence_packet.customer_id == "synthetic-customer"
    assert request.evidence_packet.registry_number == "synthetic-registry"
    assert len(request.evidence_packet.fragments) == 1
    assert request.evidence_packet.fragments[0].locator == {
        "document_order": 0,
        "chunk_index": 0,
    }


def test_probe_invalid_response_diagnostic_is_allowlisted():
    assert (
        _sanitized_invalid_response_code(
            InvalidProviderResponseError("provider_wire_quote_not_found")
        )
        == "provider_wire_quote_not_found"
    )
    assert (
        _sanitized_invalid_response_code(
            InvalidProviderResponseError("private/raw/provider/body")
        )
        == "provider_response_invalid"
    )
