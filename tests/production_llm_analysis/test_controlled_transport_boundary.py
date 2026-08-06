from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.modules.procurement_analysis.frozen_types import AnalyzedDocument
from src.modules.procurement_analysis.r10_1_producer import R10_1AnalysisRejectedError
from src.modules.production_llm_analysis.controlled_evidence import (
    ApprovedControlledProviderPolicy,
    run_controlled_provider_evidence,
)
from src.modules.production_llm_analysis.evidence import text_sha256
from src.modules.production_llm_analysis.openai_compatible import (
    OpenAICompatibleProductionLLMProvider,
    OpenAICompatibleTransportConfig,
)
from src.modules.production_llm_analysis.schemas import (
    BudgetLimits,
    BudgetPolicy,
    EvidenceReference,
    ProviderAnalysisResponse,
    ProviderClaim,
    ProviderPricing,
)
from src.modules.production_llm_analysis.transport_boundary import (
    BOUNDARY_SCHEMA_VERSION,
    authorization_consumed,
    boundary_root,
    load_authorization_state,
    transport_started,
    write_controlled_failure_descriptor,
    write_transport_start_marker,
)
from src.shared.llm.transport import HTTPRequest, HTTPResponse, ProviderTimeoutError

CUSTOMER_ID = "customer-1"
PROJECT_ID = "project-1"
CASE_ID = "case-1"
RUN_ID = "run-1"
REGISTRY_NUMBER = "0123456789012345678"
DOCUMENT_ID = "1" * 64
DOCUMENT_TEXT = (
    "Cable AVVG-P is required. Quantity is 10 meters. "
    "Delivery term is 20 days. Payment term is 30 calendar days after acceptance."
)
SECRET_KEY = "private-api-key-9f2c"


def _policy() -> ApprovedControlledProviderPolicy:
    return ApprovedControlledProviderPolicy(
        policy_version="gate5-boundary-test-v1",
        provider="openai_compatible",
        model="approved-model-v1",
        budget=BudgetPolicy(
            limits=BudgetLimits(
                max_input_tokens=100_000,
                max_output_tokens=4_096,
                timeout_ms=5_000,
                max_retries=0,
                max_total_latency_ms=10_000,
                max_estimated_cost=10.0,
                chars_per_token_estimate=4,
            ),
            pricing=ProviderPricing(
                input_cost_per_1k_tokens=0.01,
                output_cost_per_1k_tokens=0.02,
                currency="USD",
                pricing_table_version="gate5-boundary-pricing-v1",
            ),
        ),
    )


class PersistentTokenCounter:
    persistent = True
    identity = "persistent-boundary-test-tokenizer"

    def __call__(self, text: str) -> int:
        del text
        return 64


def _documents() -> list[AnalyzedDocument]:
    return [
        AnalyzedDocument(
            display_name="specification.txt",
            extension=".txt",
            role="technical_spec",
            text=DOCUMENT_TEXT,
            extracted_text_available=True,
            warnings=[],
            source="persisted_procurement_intake",
            file_id=DOCUMENT_ID,
        )
    ]


def _metadata() -> dict:
    return {
        "customer_id": CUSTOMER_ID,
        "project_id": PROJECT_ID,
        "run_id": RUN_ID,
        "procurement_id": REGISTRY_NUMBER,
        "tender_title": f"Закупка {REGISTRY_NUMBER}",
        "tender_category": "Закупка",
        "customer_name": CUSTOMER_ID,
        "status": "analyzing",
        "warnings": [],
        "limitations": [],
        "files": [],
        "procurement": {
            "registry_number": REGISTRY_NUMBER,
            "case_id": CASE_ID,
        },
    }


class ScriptedTimeoutHTTPClient:
    def __init__(self) -> None:
        self.sends = 0

    def send(self, request: HTTPRequest) -> HTTPResponse:
        del request
        self.sends += 1
        raise ProviderTimeoutError("secret upstream timeout body")


def _run(output_root: Path, provider_factory, *, controlled: bool = True):
    return run_controlled_provider_evidence(
        output_root=output_root,
        customer_id=CUSTOMER_ID,
        project_id=PROJECT_ID,
        procurement_case_id=CASE_ID,
        registry_number=REGISTRY_NUMBER,
        run_id=RUN_ID,
        metadata=_metadata(),
        documents=_documents(),
        provider_factory=provider_factory,
        policy=_policy(),
        token_counter=PersistentTokenCounter(),
        controlled=controlled,
    )


# ---------------------------------------------------------------------------
# durable stack unit tests
# ---------------------------------------------------------------------------


def test_fresh_boundary_reports_no_authorization_consumed(tmp_path: Path) -> None:
    root = boundary_root(tmp_path / "never-created")
    assert authorization_consumed(root) is False
    assert transport_started(root) is False
    state = load_authorization_state(root)
    assert state["authorization_consumed"] is False
    assert state["transport_started"] is False


def test_transport_start_marker_is_durable_and_monotone(tmp_path: Path) -> None:
    root = boundary_root(tmp_path / "out")
    write_transport_start_marker(
        root,
        execution_ordinal=1,
        batch_ordinal=1,
        attempt_ordinal=0,
        request_identity_hash="a" * 64,
    )
    write_transport_start_marker(
        root,
        execution_ordinal=2,
        batch_ordinal=2,
        attempt_ordinal=1,
        request_identity_hash="b" * 64,
    )
    marker = json.loads(
        (root / "transport-started.marker.json").read_text(encoding="utf-8")
    )
    assert marker["schema_version"] == BOUNDARY_SCHEMA_VERSION
    assert marker["transport_started"] is True
    assert marker["request_identity_hash"] in {"a" * 64, "b" * 64}
    assert authorization_consumed(root) is True


def test_failure_descriptor_minimal_fields(tmp_path: Path) -> None:
    root = boundary_root(tmp_path / "out")
    path = write_controlled_failure_descriptor(
        root,
        sanitized_failure_code="evidence_batch_execution_timeout",
    )
    descriptor = json.loads(path.read_text(encoding="utf-8"))
    assert descriptor["status"] == "controlled_provider_failure"
    assert descriptor["transport_started"] is False
    assert descriptor["authorization_consumed"] is False
    assert descriptor["sanitized_failure_code"] == "evidence_batch_execution_timeout"
    assert descriptor["raw_response_stored"] is False
    assert descriptor["raw_provider_body_recorded"] is False
    assert descriptor["raw_tender_text_recorded"] is False
    assert descriptor["credential_value_recorded"] is False
    assert descriptor["local_paths_recorded"] is False


# ---------------------------------------------------------------------------
# 1. pre-transport failure -> no marker, authorization not consumed
# ---------------------------------------------------------------------------


def test_failure_before_transport_has_no_marker_and_authorization_not_consumed(
    tmp_path: Path,
) -> None:
    root = boundary_root(tmp_path / "controlled")

    def factory():
        raise R10_1AnalysisRejectedError("insufficient_evidence")

    with pytest.raises(R10_1AnalysisRejectedError, match="insufficient_evidence"):
        _run(tmp_path / "controlled", factory)

    assert (root / "transport-started.marker.json").exists() is False
    assert authorization_consumed(root) is False
    descriptor = json.loads(
        (root / "controlled-failure.descriptor.json").read_text(encoding="utf-8")
    )
    assert descriptor["transport_started"] is False
    assert descriptor["authorization_consumed"] is False


# ---------------------------------------------------------------------------
# 2. HTTP client timeout -> marker saved, authorization consumed, descriptor
# ---------------------------------------------------------------------------


def test_timeout_after_transport_persists_marker_and_consumes_authorization(
    tmp_path: Path,
) -> None:
    client = ScriptedTimeoutHTTPClient()
    output_root = tmp_path / "controlled"

    def factory():
        return OpenAICompatibleProductionLLMProvider(
            OpenAICompatibleTransportConfig(
                base_url="http://127.0.0.1:9000/v1", api_key=SECRET_KEY
            ),
            http_client=client,
        )

    with pytest.raises(R10_1AnalysisRejectedError):
        _run(output_root, factory)

    root = boundary_root(output_root)
    assert client.sends >= 1
    assert authorization_consumed(root) is True
    assert (root / "transport-started.marker.json").exists()
    descriptor = json.loads(
        (root / "controlled-failure.descriptor.json").read_text(encoding="utf-8")
    )
    assert descriptor["transport_started"] is True
    assert descriptor["authorization_consumed"] is True
    assert descriptor["sanitized_failure_code"] == "evidence_batch_execution_timeout"
    assert descriptor["raw_response_stored"] is False


# ---------------------------------------------------------------------------
# 4. partial stage removed but marker + descriptor preserved
# ---------------------------------------------------------------------------


def test_timeout_removes_partial_stage_but_preserves_boundary_and_descriptor(
    tmp_path: Path,
) -> None:
    client = ScriptedTimeoutHTTPClient()
    output_root = tmp_path / "controlled"

    def factory():
        return OpenAICompatibleProductionLLMProvider(
            OpenAICompatibleTransportConfig(
                base_url="http://127.0.0.1:9000/v1", api_key=SECRET_KEY
            ),
            http_client=client,
        )

    with pytest.raises(R10_1AnalysisRejectedError):
        _run(output_root, factory)

    root = boundary_root(output_root)
    assert (root / "transport-started.marker.json").exists()
    assert (root / "controlled-failure.descriptor.json").exists()
    assert not output_root.exists()
    assert not list(tmp_path.glob(".controlled.partial.*"))


# ---------------------------------------------------------------------------
# 5. privacy: marker/descriptor never contain URL, credential, paths, tender
# ---------------------------------------------------------------------------


def test_url_and_credential_never_inside_marker_or_descriptor(tmp_path: Path) -> None:
    root = boundary_root(tmp_path / "privacy")
    write_transport_start_marker(
        root,
        execution_ordinal=1,
        batch_ordinal=1,
        attempt_ordinal=0,
        request_identity_hash="f" * 64,
    )
    write_controlled_failure_descriptor(
        root, sanitized_failure_code="evidence_batch_execution_timeout"
    )
    for name in ("transport-started.marker.json", "controlled-failure.descriptor.json"):
        text = (root / name).read_text(encoding="utf-8")
        assert "http://" not in text
        assert SECRET_KEY not in text
        assert "/Users/" not in text
        assert "/private/" not in text
        assert DOCUMENT_TEXT not in text


def test_boundary_module_imports(tmp_path: Path) -> None:
    assert BOUNDARY_SCHEMA_VERSION.startswith("arv001.transport-boundary.")
    out = tmp_path / "x"
    assert boundary_root(out) == tmp_path / ".x.transport-boundary"


# ---------------------------------------------------------------------------
# 6. successful controlled run -> no failure descriptor, boundary untouched
# ---------------------------------------------------------------------------


class StableSuccessProvider:
    def __init__(self, *, ordinal: int) -> None:
        self.ordinal = ordinal

    def generate(self, request):
        document = request.evidence_packet.fragments[0]
        return ProviderAnalysisResponse(
            provider_request_id=f"ok-{self.ordinal}",
            claims=[
                ProviderClaim(
                    claim_id="technical-requirement-1",
                    field_path="requirements.technical_requirements",
                    value="Cable AVVG-P is required.",
                    provider_confidence=0.95,
                    evidence_references=[
                        EvidenceReference(
                            procurement_case_id=request.procurement_case_id,
                            registry_number=request.registry_number,
                            fragment_id=document.fragment_id,
                            document_id=document.document_id,
                            document_name=document.document_name,
                            chunk_id=document.chunk_id,
                            locator=document.locator,
                            quote="Cable AVVG-P is required.",
                            quote_sha256=text_sha256("Cable AVVG-P is required."),
                        )
                    ],
                )
            ],
            input_tokens=99,
            output_tokens=29,
            total_latency_ms=4,
            retry_count=0,
        )


def test_successful_controlled_run_preserves_boundary_and_writes_no_descriptor(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "controlled"
    state = {"created": 0}

    def factory():
        state["created"] += 1
        return StableSuccessProvider(ordinal=state["created"])

    bundle = _run(output_root, factory, controlled=False)

    assert bundle.manifest_path.exists()
    root = boundary_root(output_root)
    assert authorization_consumed(root) is False
    assert (root / "transport-started.marker.json").exists() is False
    assert (root / "controlled-failure.descriptor.json").exists() is False