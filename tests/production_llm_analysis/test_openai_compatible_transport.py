from __future__ import annotations

import json
import socket
from collections.abc import Iterable
from typing import Any

import pytest

from src.modules.production_llm_analysis.evidence import text_sha256
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
    OpenAICompatibleTransportConfig,
)
from src.modules.production_llm_analysis.schemas import AnalysisStatus, BudgetStatus
from src.modules.production_llm_analysis.service import (
    build_production_llm_request,
    run_production_llm_analysis,
)
from src.shared.llm.transport import (
    HTTPRequest,
    HTTPResponse,
    ProviderPermanentError,
    ProviderTimeoutError,
    UrllibHTTPClient,
)
from tests.production_llm_analysis.conftest import make_policy, make_reference, make_request


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds / 1000


class ScriptedHTTPClient:
    def __init__(self, actions: Iterable[Any], *, clock: FakeClock | None = None) -> None:
        self.actions = list(actions)
        self.clock = clock
        self.requests: list[HTTPRequest] = []

    def send(self, request: HTTPRequest) -> HTTPResponse:
        self.requests.append(request)
        if not self.actions:
            raise AssertionError("No scripted HTTP action remains")
        action = self.actions.pop(0)
        advance_ms = 0
        if isinstance(action, tuple):
            action, advance_ms = action
        if self.clock is not None and advance_ms:
            self.clock.advance(advance_ms)
        if isinstance(action, BaseException):
            raise action
        return action


def _provider(client: ScriptedHTTPClient, *, clock: FakeClock | None = None, api_key: str = "test-secret"):
    return OpenAICompatibleProductionLLMProvider(
        OpenAICompatibleTransportConfig(
            base_url="https://provider.invalid/v1",
            api_key=api_key,
        ),
        http_client=client,
        clock=clock or FakeClock(),
    )


def _claim(request, *, claim_id: str = "claim-1") -> dict[str, Any]:
    reference = make_reference(
        request.evidence_packet,
        quote="Delivery term is 20 days.",
    )
    return {
        "claim_id": claim_id,
        "field_path": "requirements.delivery_days",
        "value": 20,
        "provider_confidence": 0.91,
        "evidence_references": [reference.model_dump(mode="json")],
    }


def _success_response(
    request,
    *,
    claims: list[dict[str, Any]] | None = None,
    include_usage: bool = True,
    provider_request_id: str | None = "provider-request-1",
    headers: dict[str, str] | None = None,
) -> HTTPResponse:
    envelope: dict[str, Any] = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"claims": claims if claims is not None else [_claim(request)]},
                        ensure_ascii=False,
                    )
                }
            }
        ]
    }
    if provider_request_id is not None:
        envelope["id"] = provider_request_id
    if include_usage:
        envelope["usage"] = {"prompt_tokens": 120, "completion_tokens": 40}
    return HTTPResponse(
        status_code=200,
        body=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
        headers=headers or {},
    )


def test_valid_mocked_response_is_grounded_and_canonical_eligible(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("network used"))
    request = make_request()
    client = ScriptedHTTPClient([_success_response(request)])

    result = run_production_llm_analysis(request, _provider(client))

    assert result.status == AnalysisStatus.SUCCESS
    assert result.canonical_input_eligible is True
    assert result.provider_request_id == "provider-request-1"
    assert result.accepted_claims[0].validated_confidence == 0.95
    assert result.accepted_claims[0].provider_confidence == 0.91
    assert result.raw_response_sha256 is not None
    assert result.budget.actual_input_tokens == 120
    assert result.budget.actual_output_tokens == 40
    assert len(client.requests) == 1


def test_request_is_json_only_allowlisted_and_never_contains_api_key():
    request = make_request()
    secret = "super-secret-api-key"
    client = ScriptedHTTPClient([_success_response(request)])

    _provider(client, api_key=secret).generate(request)

    sent = client.requests[0]
    body_text = sent.body.decode("utf-8")
    body = json.loads(body_text)
    user_payload = json.loads(body["messages"][1]["content"])
    assert body["temperature"] == 0
    assert body["max_tokens"] == request.budget_policy.limits.max_output_tokens
    assert body["response_format"] == {"type": "json_object"}
    assert secret not in body_text
    assert request.customer_id not in body_text
    assert request.project_id not in body_text
    assert request.run_id not in body_text
    assert user_payload["procurement_case_id"] == request.procurement_case_id
    assert user_payload["evidence_packet_hash"] == request.evidence_packet.packet_hash
    assert "claims" in user_payload["output_contract"]["properties"]


def test_raw_response_body_is_not_persisted_in_contract_result():
    request = make_request()
    response = _success_response(request)
    marker = response.body.decode("utf-8")
    client = ScriptedHTTPClient([response])

    result = run_production_llm_analysis(request, _provider(client))
    serialized = result.model_dump_json()

    assert marker not in serialized
    assert result.raw_response_sha256 == text_sha256(response.body.decode("utf-8"))


def test_malformed_envelope_json_returns_invalid_response():
    request = make_request()
    client = ScriptedHTTPClient([HTTPResponse(status_code=200, body=b"{not-json")])

    result = run_production_llm_analysis(request, _provider(client))

    assert result.status == AnalysisStatus.INVALID_RESPONSE
    assert result.sanitized_error_code == "provider_response_invalid"
    assert result.canonical_input_eligible is False


def test_malformed_content_schema_returns_invalid_response():
    request = make_request()
    envelope = {
        "id": "provider-request-1",
        "choices": [{"message": {"content": json.dumps({"answer": "unsupported"})}}],
    }
    client = ScriptedHTTPClient(
        [HTTPResponse(status_code=200, body=json.dumps(envelope).encode("utf-8"))]
    )

    result = run_production_llm_analysis(request, _provider(client))

    assert result.status == AnalysisStatus.INVALID_RESPONSE
    assert result.accepted_claims == []


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
def test_permanent_http_error_is_not_retried(status_code):
    request = make_request()
    client = ScriptedHTTPClient(
        [
            HTTPResponse(
                status_code=status_code,
                body=b'{"error":"sensitive provider text"}',
            ),
            _success_response(request),
        ]
    )

    result = run_production_llm_analysis(request, _provider(client, api_key="do-not-leak"))

    assert result.status == AnalysisStatus.PROVIDER_UNAVAILABLE
    assert result.sanitized_error_code == "provider_request_rejected"
    assert len(client.requests) == 1
    assert "do-not-leak" not in result.model_dump_json()
    assert "sensitive provider text" not in result.model_dump_json()


def test_429_is_retried_then_success_captures_retry_metadata():
    request = make_request()
    client = ScriptedHTTPClient(
        [
            HTTPResponse(status_code=429, body=b'{"error":"rate_limited"}'),
            _success_response(request),
        ]
    )

    response = _provider(client).generate(request)

    assert response.retry_count == 1
    assert len(response.attempt_latencies_ms) == 2
    assert len(client.requests) == 2


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_transient_http_error_exhaustion_fails_closed(status_code):
    request = make_request()
    client = ScriptedHTTPClient(
        [
            HTTPResponse(status_code=status_code, body=b"failure-1"),
            HTTPResponse(status_code=status_code, body=b"failure-2"),
        ]
    )

    result = run_production_llm_analysis(request, _provider(client))

    assert result.status == AnalysisStatus.PROVIDER_UNAVAILABLE
    assert result.sanitized_error_code == "provider_transient_failure"
    assert result.canonical_input_eligible is False
    assert len(client.requests) == 2


def test_timeout_is_bounded_and_returns_timeout_without_stub():
    request = make_request()
    client = ScriptedHTTPClient(
        [ProviderTimeoutError("secret timeout detail"), ProviderTimeoutError("another detail")]
    )

    result = run_production_llm_analysis(request, _provider(client))

    assert result.status == AnalysisStatus.TIMEOUT
    assert result.sanitized_error_code == "provider_timeout"
    assert result.accepted_claims == []
    assert "secret timeout detail" not in result.model_dump_json()
    assert len(client.requests) == 2


def test_total_latency_budget_stops_retry_before_second_call():
    clock = FakeClock()
    request = make_request(policy=make_policy(max_total_latency_ms=50, timeout_ms=50, max_retries=2))
    client = ScriptedHTTPClient(
        [(HTTPResponse(status_code=429, body=b"rate-limited"), 51), _success_response(request)],
        clock=clock,
    )

    result = run_production_llm_analysis(request, _provider(client, clock=clock))

    assert result.status == AnalysisStatus.BUDGET_EXCEEDED
    assert result.sanitized_error_code == "provider_runtime_budget_exceeded"
    assert result.budget.status == BudgetStatus.EXCEEDED
    assert len(client.requests) == 1


def test_retry_cost_budget_stops_second_attempt():
    provisional = make_request()
    provisional_client = ScriptedHTTPClient([_success_response(provisional)])
    provider = _provider(provisional_client)
    body_size = len(json.dumps(provider._build_request_body(provisional), ensure_ascii=False, sort_keys=True).encode("utf-8"))
    estimated_cost = provider._estimate_attempt_cost(provisional, body_size)
    policy = make_policy(max_estimated_cost=estimated_cost * 1.25, max_retries=2)
    request = build_production_llm_request(
        evidence_packet=provisional.evidence_packet,
        provider=provisional.provider,
        model=provisional.model,
        prompt_id=provisional.prompt_id,
        prompt_version=provisional.prompt_version,
        output_schema_id=provisional.output_schema_id,
        output_schema_version=provisional.output_schema_version,
        grounding_policy_version=provisional.grounding_policy_version,
        budget_policy=policy,
    )
    client = ScriptedHTTPClient(
        [HTTPResponse(status_code=429, body=b"rate-limited"), _success_response(request)]
    )

    result = run_production_llm_analysis(request, _provider(client))

    assert result.status == AnalysisStatus.BUDGET_EXCEEDED
    assert result.sanitized_error_code == "provider_runtime_budget_exceeded"
    assert len(client.requests) == 1


def test_missing_usage_is_estimated_not_zero():
    request = make_request()
    client = ScriptedHTTPClient([_success_response(request, include_usage=False)])

    result = run_production_llm_analysis(request, _provider(client))

    assert result.status == AnalysisStatus.SUCCESS
    assert result.budget.actual_input_tokens is None
    assert result.budget.actual_output_tokens is None
    assert result.budget.actual_or_reconciled_cost is not None
    assert result.budget.actual_or_reconciled_cost > 0
    assert "provider_usage_missing_estimate_used" in result.budget.reasons


def test_request_id_falls_back_to_case_insensitive_header():
    request = make_request()
    client = ScriptedHTTPClient(
        [
            _success_response(
                request,
                provider_request_id=None,
                headers={"X-Request-ID": "header-request-7"},
            )
        ]
    )

    response = _provider(client).generate(request)

    assert response.provider_request_id == "header-request-7"


def test_invalid_claim_shape_never_reaches_grounding():
    request = make_request()
    invalid_claim = _claim(request)
    invalid_claim["provider_confidence"] = 7
    client = ScriptedHTTPClient([_success_response(request, claims=[invalid_claim])])

    result = run_production_llm_analysis(request, _provider(client))

    assert result.status == AnalysisStatus.INVALID_RESPONSE
    assert result.accepted_claims == []


def test_urllib_boundary_sanitizes_timeout_and_connection_errors(monkeypatch):
    request = HTTPRequest(
        url="https://provider.invalid/v1/chat/completions",
        body=b"{}",
        headers={"Authorization": "Bearer private-key"},
        timeout_ms=10,
    )

    monkeypatch.setattr(
        "src.shared.llm.transport.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("private-key")),
    )
    with pytest.raises(ProviderTimeoutError, match="provider_timeout") as timeout_error:
        UrllibHTTPClient().send(request)
    assert "private-key" not in str(timeout_error.value)

    monkeypatch.setattr(
        "src.shared.llm.transport.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("private-key")),
    )
    with pytest.raises(ConnectionError, match="provider_connection_failed") as connection_error:
        UrllibHTTPClient().send(request)
    assert "private-key" not in str(connection_error.value)


def test_config_repr_does_not_expose_api_key():
    config = OpenAICompatibleTransportConfig(
        base_url="https://provider.invalid/v1",
        api_key="never-print-me",
    )

    assert "never-print-me" not in repr(config)


def test_provider_permanent_error_message_is_sanitized():
    request = make_request()
    client = ScriptedHTTPClient([HTTPResponse(status_code=401, body=b"secret body")])

    with pytest.raises(ProviderPermanentError, match="provider_request_rejected") as error:
        _provider(client, api_key="secret key").generate(request)

    assert "secret" not in str(error.value)
