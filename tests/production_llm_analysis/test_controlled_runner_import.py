from __future__ import annotations

from scripts.r10_1 import run_controlled_provider_evidence as runner
from src.modules.customer_pilot.input_resolver import resolve_customer_run_inputs


def test_controlled_runner_uses_existing_trusted_input_resolver() -> None:
    assert runner.resolve_customer_run_inputs is resolve_customer_run_inputs
