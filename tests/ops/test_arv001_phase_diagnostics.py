from __future__ import annotations

import hashlib
import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.arv001 import run_complete_corpus_acceptance as runner
from scripts.arv001 import run_complete_corpus_acceptance_split_roots as adapter


def _install_pre_stage_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, execute: bool, verify: bool
) -> Namespace:
    physical = [
        {
            "original_name": f"file-{index}",
            "sha256": hashlib.sha256(str(index).encode()).hexdigest(),
            "size_bytes": index + 1,
        }
        for index in range(10)
    ]
    args = Namespace(
        expected_head="head",
        execute_provider=execute,
        verify_pre_provider_stage_boundary=verify,
        expected_corpus_sha=_bound_expected(physical),
        expected_policy_sha="policy",
        customer_name="customer",
        project_name="project",
        registry_number="registry",
        initialize_database=True,
    )
    paths = {
        "candidate_root": tmp_path,
        "intake_root": tmp_path,
        "database_path": tmp_path / "db.sqlite3",
        "data_dir": tmp_path / "data",
        "output_root": tmp_path / "output",
        "policy_path": tmp_path / "policy.json",
    }
    values = {
        "physical-files.json": physical,
        "metadata.json": {},
        "intake-summary.json": {"corpus_sha256": args.expected_corpus_sha},
        "deterministic-parse-summary.json": {},
        "logical-documents.json": [],
    }
    monkeypatch.setattr(runner, "_arguments", lambda: args)
    monkeypatch.setattr(runner, "_repository_root", lambda: tmp_path)
    monkeypatch.setattr(runner, "_git_preflight", lambda *_: {"head_sha": "head"})
    monkeypatch.setattr(runner, "_configure", lambda *_: paths)
    monkeypatch.setattr(runner, "_initialize_local_runtime", lambda *_args, **_kw: None)
    monkeypatch.setattr(runner, "load_candidate", lambda *_: (values, {"safe": True}))
    monkeypatch.setattr(runner, "validate_document_set", lambda *_: None)
    monkeypatch.setattr(runner, "static_contract_preflight", lambda: {"ok": True})
    monkeypatch.setattr(runner, "database_preflight", lambda: {"ok": True})
    monkeypatch.setattr(runner, "provider_preflight", lambda *_: {"ok": True})
    monkeypatch.setattr(
        runner,
        "_prepare_documents",
        lambda **_kw: [SimpleNamespace(chunks=(object(),)) for _ in range(10)],
    )
    import src.shared.config.settings as settings_module

    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(
            document_extract_max_chars=100,
            rag_chunk_size_chars=10,
            rag_chunk_overlap_chars=0,
        ),
    )
    return args


@pytest.mark.parametrize(
    ("phase", "replacement", "expected"),
    [
        ("application_data", "create_application_data", "application_data"),
        ("post_persistence_preflight", "post_persistence_preflight", "post_persistence_preflight"),
        ("controlled_invocation", "_run_controlled_once", "controlled_invocation"),
    ],
)
def test_unexpected_exception_reports_allowlisted_phase(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    phase: str,
    replacement: str,
    expected: str,
):
    _install_pre_stage_success(monkeypatch, tmp_path, execute=True, verify=False)
    monkeypatch.setattr(runner, "create_application_data", lambda **_kw: {"run_id": "run"})
    monkeypatch.setattr(runner, "post_persistence_preflight", lambda *_: {"ok": True})
    monkeypatch.setattr(runner, "_finalize", lambda *_args: {"metrics": {}})

    def boom(*_args, **_kwargs):
        raise RuntimeError("/private/path must not leak")

    monkeypatch.setattr(runner, replacement, boom)
    assert runner.main() == 3
    assert capsys.readouterr().err == f"arv001_unexpected_exception:{expected}:RuntimeError\n"


def test_acceptance_blocked_keeps_repository_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    _install_pre_stage_success(monkeypatch, tmp_path, execute=True, verify=False)
    monkeypatch.setattr(
        runner,
        "create_application_data",
        lambda **_kw: (_ for _ in ()).throw(runner.AcceptanceBlocked("safe_code")),
    )
    assert runner.main() == 2
    assert capsys.readouterr().err == "safe_code\n"


def test_unexpected_error_never_discloses_unsafe_class_or_text():
    unsafe = type("Unsafe-Class", (Exception,), {})

    assert (
        runner._safe_unexpected_code("not-an-allowed-phase", unsafe("/private/path"))
        == "arv001_unexpected_exception:unknown:Exception"
    )


def test_pre_provider_boundary_creates_then_cleans_static_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    _install_pre_stage_success(monkeypatch, tmp_path, execute=False, verify=True)
    called = {"application": False, "controlled": False}
    monkeypatch.setattr(
        runner,
        "_corpus_hash_profile",
        lambda: {
            "sha256": runner._arguments().expected_corpus_sha,
            "fields": ["original_name", "sha256", "size_bytes"],
            "serialization": "canonical_compact_newline",
            "ordering": "original_name_unicode_codepoint_ascending",
        },
    )
    monkeypatch.setattr(runner, "create_application_data", lambda **_kw: called.update(application=True))
    monkeypatch.setattr(runner, "_run_controlled_once", lambda *_args: called.update(controlled=True))

    assert runner.main() == 0
    output = capsys.readouterr().out
    assert "ARV-001_PRE_PROVIDER_STAGE_BOUNDARY_VERIFIED" in output
    assert "diagnostic_stage_cleaned" in output
    assert not list(tmp_path.glob(".output.partial.*"))
    assert not (tmp_path / "output").exists()
    assert called == {"application": False, "controlled": False}


def test_direct_diagnostic_requires_bound_corpus_hash_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    _install_pre_stage_success(monkeypatch, tmp_path, execute=False, verify=True)

    assert runner.main() == 2
    assert (
        capsys.readouterr().err
        == "diagnostic_bound_corpus_hash_profile_missing_or_invalid\n"
    )


def _bound_expected(physical: list[dict]) -> str:
    projected = [
        {key: item[key] for key in ("original_name", "sha256", "size_bytes")}
        for item in physical
    ]
    payload = (
        json.dumps(
            sorted(projected, key=lambda item: item["original_name"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def test_split_root_diagnostic_uses_bound_profile_and_cleans_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    candidate = tmp_path / "candidate"
    intake = tmp_path / "intake"
    candidate.mkdir()
    intake.mkdir()
    physical = [
        {
            "ordinal": index,
            "original_name": f"file-{index}",
            "sha256": str(index) * 64,
            "size_bytes": index + 1,
        }
        for index in range(10)
    ]
    expected = _bound_expected(physical)
    logical = [
        {"name": name}
        for name in (
            "Извещение",
            "Описание объекта",
            "Обоснование НМЦК",
            "Требования к составу заявки",
            "Проект контракта",
            "Обеспечения исполнения контракта",
        )
    ]
    artifacts = {
        "physical-files.json": physical,
        "logical-documents.json": logical,
        "document-set-summary.json": {
            "status": "complete", "analysis_allowed": True, "physical_file_count": 10
        },
        "deterministic-parse-summary.json": {},
        "intake-summary.json": {"corpus_sha256": expected},
    }
    for name, value in artifacts.items():
        (candidate / name).write_text(json.dumps(value, ensure_ascii=False))
    (intake / "metadata.json").write_text(json.dumps({"files": []}))
    policy = tmp_path / "policy.json"
    policy.write_text("{}")
    output = tmp_path / "output"
    seen_static_stage: list[bool] = []
    original_write = runner._write_json

    def observe_static_stage(path: Path, value: object) -> None:
        original_write(path, value)
        if path.name == "static-preflight.json":
            seen_static_stage.append(path.is_file())

    monkeypatch.setattr(runner, "_git_preflight", lambda *_: {"head_sha": "head"})
    monkeypatch.setattr(runner, "_initialize_local_runtime", lambda *_args, **_kw: None)
    monkeypatch.setattr(runner, "static_contract_preflight", lambda: {"ok": True})
    monkeypatch.setattr(runner, "database_preflight", lambda: {"ok": True})
    monkeypatch.setattr(runner, "provider_preflight", lambda *_: {"ok": True})
    monkeypatch.setattr(
        runner,
        "_prepare_documents",
        lambda **_kw: [SimpleNamespace(chunks=(object(),)) for _ in range(10)],
    )
    monkeypatch.setattr(runner, "_write_json", observe_static_stage)
    monkeypatch.setattr(
        runner,
        "create_application_data",
        lambda **_kw: pytest.fail("application data must not be created"),
    )
    monkeypatch.setattr(
        runner,
        "_run_controlled_once",
        lambda *_args: pytest.fail("controlled runner must not be started"),
    )
    import src.shared.config.settings as settings_module

    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(
            document_extract_max_chars=100,
            rag_chunk_size_chars=10,
            rag_chunk_overlap_chars=0,
        ),
    )

    assert runner._corpus_hash(physical) != expected
    assert (
        adapter.main(
            [
                "adapter",
                "--candidate-root", str(candidate),
                "--intake-root", str(intake),
                "--database-path", str(tmp_path / "diagnostic.sqlite3"),
                "--initialize-database",
                "--data-dir", str(tmp_path / "data"),
                "--approved-policy", str(policy),
                "--output-root", str(output),
                "--expected-head", "head",
                "--expected-corpus-sha", expected,
                "--expected-policy-sha", "policy",
                "--verify-pre-provider-stage-boundary",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    profile = payload["corpus_hash_profile"]
    assert payload["marker"] == "ARV-001_PRE_PROVIDER_STAGE_BOUNDARY_VERIFIED"
    assert profile["sha256"] == expected
    assert profile["fields"] == ["original_name", "sha256", "size_bytes"]
    assert profile["serialization"] == "canonical_compact_newline"
    assert seen_static_stage == [True]
    assert not list(tmp_path.glob(".output.partial.*"))
    assert not output.exists()


def test_split_root_unexpected_view_error_is_phase_safe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(
        adapter,
        "build_ephemeral_candidate_view",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("/private/path")),
    )

    assert adapter.main(["adapter", "--candidate-root", "/unused"]) == 3
    assert (
        capsys.readouterr().err
        == "arv001_split_root_unexpected_exception:ephemeral_view:RuntimeError\n"
    )
