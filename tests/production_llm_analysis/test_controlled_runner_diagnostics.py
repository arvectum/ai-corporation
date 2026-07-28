from __future__ import annotations

from scripts.r10_1.run_controlled_provider_evidence import (
    _controlled_failure_message,
    _sanitized_producer_failure,
)
from src.modules.procurement_analysis.r10_1_producer import (
    R10_1AnalysisRejectedError,
    R10_1CanonicalProductionError,
)


def test_repository_owned_failure_code_is_preserved() -> None:
    error = R10_1AnalysisRejectedError("provider_response_invalid")

    assert _sanitized_producer_failure(error) == "provider_response_invalid"
    assert (
        _controlled_failure_message(error)
        == "controlled_provider_evidence_rejected:provider_response_invalid"
    )


def test_timeout_failure_code_is_preserved() -> None:
    error = R10_1AnalysisRejectedError("provider_timeout")

    assert (
        _controlled_failure_message(error)
        == "controlled_provider_evidence_rejected:provider_timeout"
    )


def test_untrusted_exception_text_is_not_exposed() -> None:
    error = R10_1CanonicalProductionError(
        "sensitive path /Users/operator and Authorization: Bearer secret"
    )

    message = _controlled_failure_message(error)

    assert message == "controlled_provider_evidence_rejected:canonical_production_failed"
    assert "/Users/" not in message
    assert "secret" not in message
