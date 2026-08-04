from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts.r10_1 import run_controlled_provider_evidence as runner


class _Session:
    def __init__(self, values: list[object]) -> None:
        self.values = list(values)

    def scalar(self, statement):
        del statement
        return self.values.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class _Tokenizer:
    persistent = True
    identity = "persistent-test-tokenizer"


def _run() -> SimpleNamespace:
    return SimpleNamespace(
        id="run-1",
        status="completed",
        registry_number="registry-1",
        customer_id="customer-1",
        project_id="project-1",
        procurement_case_id="case-1",
        metadata_json=json.dumps({"arv001_tender_id": "tender-1"}),
    )


def _case() -> SimpleNamespace:
    return SimpleNamespace(
        id="case-1",
        customer_id="customer-1",
        project_id="project-1",
        procurement_number="registry-1",
    )


def _policy() -> SimpleNamespace:
    return SimpleNamespace(
        provider="openai_compatible",
        model="approved-model-v1",
        budget=object(),
    )


def _tender() -> SimpleNamespace:
    return SimpleNamespace(
        id="tender-1",
        title="Tender",
        law_type=None,
        customer_name=None,
        customer_inn=None,
        customer_kpp=None,
        publication_date=None,
        application_deadline=None,
        nmck_amount=None,
    )


def test_preparation_builds_deterministic_boundary_without_provider_constructor(
    tmp_path: Path, monkeypatch
) -> None:
    run = _run()
    case = _case()
    inputs = SimpleNamespace(documents=[], warnings=[], limitations=[])
    packet = SimpleNamespace(packet_hash="a" * 64)
    plan = SimpleNamespace(plan_hash="b" * 64)
    constructor_calls = 0

    monkeypatch.setattr(
        runner,
        "resolve_customer_run_inputs_for_analysis_run",
        lambda session, exact_run: inputs,
    )
    monkeypatch.setattr(
        runner,
        "build_r10_1_evidence_packet",
        lambda **kwargs: packet,
    )
    monkeypatch.setattr(
        runner,
        "build_r10_1_batch_plan",
        lambda **kwargs: plan,
    )

    def forbidden_constructor(*args, **kwargs):
        nonlocal constructor_calls
        constructor_calls += 1
        raise AssertionError("provider constructor crossed preflight boundary")

    monkeypatch.setattr(
        runner,
        "OpenAICompatibleProductionLLMProvider",
        forbidden_constructor,
    )

    output_root = tmp_path / "controlled-output"
    prepared = runner.prepare_controlled_provider_evidence(
        session=_Session([_tender()]),
        run=run,
        case=case,
        policy=_policy(),
        base_url="http://127.0.0.1:9000/v1",
        api_key="private-test-key",
        output_root=output_root,
        token_counter=_Tokenizer(),
    )

    assert constructor_calls == 0
    assert prepared.packet_hash == "a" * 64
    assert prepared.batch_plan_hash == "b" * 64
    assert len(prepared.transport_identity_hash) == 64
    assert not output_root.exists()


def test_main_preflight_stops_before_factory_transport_and_bundle_writes(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    run = _run()
    case = _case()
    policy = _policy()
    output_root = tmp_path / "controlled-output"
    counts = {
        "provider_constructor": 0,
        "transport": 0,
        "bundle_write": 0,
    }

    monkeypatch.setattr(
        runner,
        "_arguments",
        lambda: SimpleNamespace(
            preflight_only=True,
            run_id=run.id,
            expected_registry_number=run.registry_number,
            approved_policy=tmp_path / "approved-policy.json",
            output_root=output_root,
        ),
    )
    monkeypatch.setattr(runner, "load_approved_provider_policy", lambda path: policy)
    monkeypatch.setattr(
        runner,
        "get_settings",
        lambda: SimpleNamespace(llm_model=policy.model),
    )
    monkeypatch.setattr(
        runner,
        "_provider_secret_boundary",
        lambda provider: ("http://127.0.0.1:9000/v1", "private-test-key"),
    )
    monkeypatch.setattr(runner, "SessionLocal", lambda: _Session([run, case]))
    monkeypatch.setattr(runner, "tokenizer_from_environment", _Tokenizer)
    monkeypatch.setattr(
        runner,
        "prepare_controlled_provider_evidence",
        lambda **kwargs: runner.PreparedControlledEvidence(
            run=run,
            inputs=SimpleNamespace(documents=[]),
            metadata={},
            packet_hash="a" * 64,
            batch_plan_hash="b" * 64,
            transport_identity_hash="c" * 64,
            output_root=output_root,
            token_counter=_Tokenizer(),
        ),
    )

    def forbidden_constructor(*args, **kwargs):
        counts["provider_constructor"] += 1
        raise AssertionError("provider constructor crossed preflight boundary")

    def forbidden_bundle_write(*args, **kwargs):
        counts["bundle_write"] += 1
        raise AssertionError("bundle write crossed preflight boundary")

    monkeypatch.setattr(
        runner,
        "OpenAICompatibleProductionLLMProvider",
        forbidden_constructor,
    )
    monkeypatch.setattr(
        runner,
        "run_controlled_provider_evidence",
        forbidden_bundle_write,
    )
    monkeypatch.setattr(
        runner,
        "OpenAICompatibleTransportConfig",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    assert runner.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload == {
        "batch_plan_hash": "b" * 64,
        "controlled_preflight_invocations": 1,
        "controlled_provider_invocations": 0,
        "evidence_packet_hash": "a" * 64,
        "provider_generation_calls": 0,
        "ready_for_transport": True,
        "status": "controlled_preflight_complete",
    }
    assert counts == {
        "provider_constructor": 0,
        "transport": 0,
        "bundle_write": 0,
    }
    assert not output_root.exists()


def test_existing_output_root_fails_before_provider_construction(
    tmp_path: Path, monkeypatch
) -> None:
    output_root = tmp_path / "controlled-output"
    output_root.mkdir()
    constructor_calls = 0

    monkeypatch.setattr(
        runner,
        "resolve_customer_run_inputs_for_analysis_run",
        lambda session, run: SimpleNamespace(documents=[], warnings=[], limitations=[]),
    )

    def forbidden_constructor(*args, **kwargs):
        nonlocal constructor_calls
        constructor_calls += 1
        raise AssertionError("provider constructor crossed preflight boundary")

    monkeypatch.setattr(
        runner,
        "OpenAICompatibleProductionLLMProvider",
        forbidden_constructor,
    )

    try:
        runner.prepare_controlled_provider_evidence(
            session=_Session([_tender()]),
            run=_run(),
            case=_case(),
            policy=_policy(),
            base_url="http://127.0.0.1:9000/v1",
            api_key="private-test-key",
            output_root=output_root,
            token_counter=_Tokenizer(),
        )
    except runner.ControlledRunnerConfigurationError as exc:
        assert str(exc) == "output_root_already_exists"
    else:
        raise AssertionError("existing output root must fail closed")

    assert constructor_calls == 0
