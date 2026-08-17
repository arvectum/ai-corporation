from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.modules.procurement_analysis.r10_1_producer import (
    produce_r10_1_canonical_analysis,
)
from src.modules.production_llm_analysis.budgets import BudgetEvaluation, BudgetStatus
from src.modules.production_llm_analysis.schemas import (
    AnalysisStatus,
    BudgetPolicy,
    EvidencePacket,
    ProductionLLMAnalysisRequest,
    ProductionLLMAnalysisResult,
)
from src.modules.production_llm_analysis.service import run_production_llm_analysis


def get_valid_request() -> ProductionLLMAnalysisRequest:
    packet = EvidencePacket.model_validate(
        {
            "customer_id": "c",
            "project_id": "p",
            "procurement_case_id": "case",
            "run_id": "run",
            "registry_number": "r",
            "packet_hash": "0" * 64,
            "fragments": [
                {
                    "fragment_id": "0" * 64,
                    "document_id": "d",
                    "document_name": "n",
                    "chunk_id": "c",
                    "locator": {"document_order": 1, "chunk_index": 0},
                    "text": "t",
                    "text_sha256": "0" * 64,
                }
            ],
            "data_handling": {},
        }
    )
    return ProductionLLMAnalysisRequest(
        request_id="0" * 64,
        run_id="0" * 64,
        customer_id="c",
        project_id="p",
        procurement_case_id="case",
        registry_number="r",
        provider="prov",
        provider_wire_contract_version="compact-safe-v1",
        model="m",
        prompt_id="pi",
        prompt_version="pv",
        output_schema_id="osi",
        output_schema_version="osv",
        grounding_policy_version="gpv",
        evidence_packet=packet,
        budget_policy=BudgetPolicy(
            limits={
                "max_input_tokens": 100000,
                "max_output_tokens": 100000,
                "timeout_ms": 60000,
                "max_total_latency_ms": 120000,
                "max_estimated_cost": 100.0,
                "max_retries": 0,
                "chars_per_token_estimate": 4,
            },
            pricing={
                "input_cost_per_1k_tokens": 0.0,
                "output_cost_per_1k_tokens": 0.0,
                "pricing_table_version": "v1",
            },
        ),
        batch_plan_version="v1",
        batch_plan_hash="0" * 64,
        batch_hash="0" * 64,
        batch_ordinal=1,
        batch_count=1,
        corpus_evidence_hash="0" * 64,
        map_mode=True,
        max_claims=3,
    )


def test_pre_transport_safe_failure() -> None:
    request = get_valid_request()
    provider = MagicMock()
    provider.generate.side_effect = ValueError("final_body_live_schema_mismatch")

    result = run_production_llm_analysis(request, provider)

    assert result.status == AnalysisStatus.PROVIDER_UNAVAILABLE
    assert result.sanitized_error_code == "final_body_live_schema_mismatch"


def test_pre_transport_unsafe_failure() -> None:
    request = get_valid_request()
    provider = MagicMock()
    provider.generate.side_effect = ValueError("private /Users/master/secret")

    result = run_production_llm_analysis(request, provider)

    assert result.status == AnalysisStatus.PROVIDER_UNAVAILABLE
    assert result.sanitized_error_code == "provider_call_failed"


def test_pre_transport_marker_absence(tmp_path: Path) -> None:
    from src.modules.production_llm_analysis.openai_compatible import (
        OpenAICompatibleProductionLLMProvider,
        OpenAICompatibleTransportConfig,
    )
    from src.modules.production_llm_analysis.transport_boundary import (
        authorization_consumed,
    )

    config = OpenAICompatibleTransportConfig(
        base_url="http://127.0.0.1", api_key="test"
    )
    boundary = tmp_path / "boundary"
    provider = OpenAICompatibleProductionLLMProvider(
        config, transport_boundary=boundary
    )

    with patch.object(
        OpenAICompatibleProductionLLMProvider,
        "_build_request_body",
        side_effect=ValueError("final_body_live_schema_mismatch"),
    ):
        result = run_production_llm_analysis(get_valid_request(), provider)

    assert result.status == AnalysisStatus.PROVIDER_UNAVAILABLE
    assert result.sanitized_error_code == "final_body_live_schema_mismatch"
    assert not authorization_consumed(boundary)


def test_actual_send_marker_presence(tmp_path: Path) -> None:
    from src.modules.production_llm_analysis.openai_compatible import (
        OpenAICompatibleProductionLLMProvider,
        OpenAICompatibleTransportConfig,
    )
    from src.modules.production_llm_analysis.transport_boundary import (
        authorization_consumed,
    )
    from src.shared.llm.transport import ProviderTransientError

    config = OpenAICompatibleTransportConfig(
        base_url="http://127.0.0.1", api_key="test"
    )
    boundary = tmp_path / "boundary"
    http_client = MagicMock()
    http_client.send.side_effect = ProviderTransientError("provider_transient_failure")
    provider = OpenAICompatibleProductionLLMProvider(
        config, http_client=http_client, transport_boundary=boundary
    )

    # This test isolates transport-marker semantics from compact-schema live
    # verification. Other tests in the full suite may enable that verification
    # globally, so use the supported full-v1 non-map path to guarantee that the
    # mocked HTTP send is actually reached.
    request = get_valid_request().model_copy(
        update={"provider_wire_contract_version": "full-v1", "map_mode": False}
    )
    result = run_production_llm_analysis(request, provider)

    assert result.status == AnalysisStatus.PROVIDER_UNAVAILABLE
    assert result.sanitized_error_code == "provider_transient_failure"
    assert authorization_consumed(boundary)
    http_client.send.assert_called_once()


def _provider_failure_result(code: str) -> ProductionLLMAnalysisResult:
    return ProductionLLMAnalysisResult(
        status=AnalysisStatus.PROVIDER_UNAVAILABLE,
        sanitized_error_code=code,
        request_id="0" * 64,
        provider="p",
        model="m",
        provider_wire_contract_version="compact-safe-v1",
        prompt_id="p",
        prompt_version="v",
        output_schema_id="s",
        output_schema_version="v",
        grounding_policy_version="v",
        evidence_packet_hash="0" * 64,
        accepted_claims=[],
        rejected_claims=[],
        limitations=[],
        budget=BudgetEvaluation(
            status=BudgetStatus.WITHIN_BUDGET,
            pricing_table_version="v1",
        ),
        validated_result_hash="0" * 64,
    )


def _assert_controlled_producer_error(code: str, expected: str) -> None:
    mock_result = _provider_failure_result(code)

    with (
        patch("src.modules.procurement_analysis.r10_1_producer._owned_identity"),
        patch(
            "src.modules.procurement_analysis.r10_1_producer._evidence_packet_from_documents"
        ) as mock_epfd,
        patch(
            "src.modules.procurement_analysis.r10_1_producer.build_r10_1_batch_plan"
        ) as mock_plan,
        patch(
            "src.modules.procurement_analysis.r10_1_producer.run_production_llm_analysis",
            return_value=mock_result,
        ),
        patch("src.modules.procurement_analysis.r10_1_producer.persist_canonical_outputs"),
        patch(
            "src.modules.procurement_analysis.r10_1_producer.verify_persisted_canonical_outputs"
        ),
        patch(
            "src.modules.procurement_analysis.r10_1_producer.measure_openai_request_tokens"
        ) as mock_measure,
        patch(
            "src.modules.procurement_analysis.r10_1_producer.build_production_llm_request"
        ) as mock_bpr,
    ):
        mock_epfd.return_value.packet_hash = "0" * 64
        batch_mock = MagicMock()
        batch_mock.fragments = []
        batch_mock.batch_hash = "h"
        batch_mock.batch_ordinal = 1
        mock_plan.return_value.batches = [batch_mock]
        mock_plan.return_value.plan_hash = "ph"
        mock_plan.return_value.corpus_evidence_hash = "ceh"
        mock_plan.return_value.policy.execution_deadline_ms = 1000

        mock_measure.return_value.request_body_hash = "0" * 64
        mock_measure.return_value.full_request_tokens = 100
        mock_measure.return_value.serialized_evidence_tokens = 50
        mock_bpr.return_value = get_valid_request()

        with patch(
            "src.modules.production_llm_analysis.openai_compatible."
            "OpenAICompatibleProductionLLMProvider._build_request_body",
            return_value={"mock": "body"},
        ):
            with pytest.raises(Exception) as excinfo:
                produce_r10_1_canonical_analysis(
                    customer_id="c",
                    project_id="p",
                    procurement_case_id="case",
                    registry_number="r",
                    run_id="run",
                    output_dir=Path("/tmp"),
                    metadata={},
                    documents=[MagicMock(role="notice", text="t")],
                    provider=MagicMock(),
                    budget_policy=MagicMock(),
                    provider_name="p",
                    model="m",
                    controlled=True,
                )

    assert str(excinfo.value) == expected


def test_controlled_producer_preserves_safe_code() -> None:
    _assert_controlled_producer_error(
        "final_body_live_schema_mismatch", "final_body_live_schema_mismatch"
    )


def test_controlled_producer_collapses_unsafe_code() -> None:
    _assert_controlled_producer_error(
        "something_not_in_allow_list", "evidence_batch_provider_failed"
    )
