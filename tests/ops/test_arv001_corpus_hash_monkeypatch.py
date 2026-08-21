"""Regression tests for BoundCorpusHashResolver monkey-patch in the main runner.

Ensures the corpus-hash serialization mismatch between complete_corpus_contract.corpus_hash
(canonical_json_sha256 / no newline) and the baseline corpus SHA (canonical_compact_newline)
is resolved at the application_workflow persistence check without breaking legacy behavior.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from scripts.arv001 import application_workflow
from scripts.arv001 import complete_corpus_contract as _contract
from scripts.arv001 import run_complete_corpus_acceptance as runner
from scripts.arv001.complete_corpus_contract import (
    AcceptanceBlocked,
    canonical_json_sha256,
)
from scripts.arv001.corpus_hash_resolver import BoundCorpusHashResolver

_NAMES = [
    "01-doc.pdf",
    "02-spec.xlsx",
    "03-notice.xml",
    "04-draft.docx",
    "05-price.xlsx",
    "06-req.pdf",
    "07-form.doc",
    "08-appendix.pdf",
    "09-evaluation.xlsx",
    "10-protocol.pdf",
]


def _physical_10() -> list[dict]:
    return [
        {"original_name": n, "sha256": hashlib.sha256(n.encode()).hexdigest(), "size_bytes": len(n) * 100}
        for n in _NAMES
    ]


def _profile_newline_expected(physical: list[dict]) -> str:
    projected = [
        {
            "original_name": item["original_name"],
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
        }
        for item in physical
    ]
    projected.sort(key=lambda item: item["original_name"])
    payload = (
        json.dumps(projected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# (a) The frozen canonical_compact_newline corpus passes the persisted-corpus check.
def test_frozen_newline_corpus_matches_bound_resolver():
    physical = _physical_10()
    expected = _profile_newline_expected(physical)

    resolver = BoundCorpusHashResolver(expected)
    assert resolver(physical) == expected
    assert resolver.profile is not None
    assert resolver.profile.serialization == "canonical_compact_newline"
    assert resolver.profile.fields == ("original_name", "sha256", "size_bytes")


# (b) The legacy compact/no-newline hash remains unchanged.
def test_legacy_compact_hash_unchanged_by_newline_profile():
    physical = _physical_10()

    legacy = _contract.corpus_hash(physical)
    newline = _profile_newline_expected(physical)

    assert legacy != newline
    ordered = sorted(physical, key=lambda item: str(item.get("original_name") or ""))
    assert legacy == canonical_json_sha256(ordered)


# (c) application_workflow.corpus_hash is restored after successful main().
def test_workflow_hash_restored_after_success():
    physical = _physical_10()
    expected = _profile_newline_expected(physical)
    original_hash = application_workflow.corpus_hash
    assert original_hash is _contract.corpus_hash

    def fake_arguments():
        class Args:
            candidate_root = None
            database_path = None
            output_root = None
            expected_head = "a" * 40
            expected_corpus_sha = expected
            expected_policy_sha = "0" * 64
            registry_number = "0388100001826000047"
            customer_name = "Test"
            project_name = "Test"
            initialize_database = False
            execute_provider = False
            static_only = True
            prepare_only = False
            verify_pre_provider_stage_boundary = False
            private_verification_descriptor = None
        return Args()

    def fake_repository_root():
        return runner.Path("/tmp")

    def fake_git_preflight(repo_root, expected_head):
        return {"head_sha": expected_head, "branch": "test", "dirty": False}

    def fake_configure(args, repo_root):
        return {
            "candidate_root": runner.Path("/tmp/candidate"),
            "intake_root": runner.Path("/tmp/intake"),
            "database_path": runner.Path("/tmp/db.sqlite"),
            "policy_path": runner.Path("/tmp/policy.json"),
            "output_root": runner.Path("/tmp/output"),
        }

    def fake_initialize_local_runtime(paths, repo_root, initialize_database=False):
        pass

    logical_docs = [
        {"name": "Извещение о закупке", "type": "извещение", "file": _NAMES[0]},
        {"name": "Описание объекта закупки", "type": "техническая документация", "file": _NAMES[1]},
        {"name": "Обоснование НМЦК", "type": "ценовое обоснование", "file": _NAMES[2]},
        {"name": "Требования к составу заявки", "type": "требования к заявке", "file": _NAMES[3]},
        {"name": "Проект контракта", "type": "проект контракта", "file": _NAMES[4]},
        {"name": "Реквизиты обеспечения исполнения контракта", "type": "обеспечение", "file": _NAMES[5]},
    ]

    def fake_load_candidate(candidate_root):
        metadata = {
            "files": [{"original_name": n, "stored_name": n} for n in _NAMES]
        }
        return (
            {
                "physical-files.json": physical,
                "metadata.json": metadata,
                "intake-summary.json": {"corpus_sha256": expected},
                "deterministic-parse-summary.json": {},
                "logical-documents.json": logical_docs,
                "document-set-summary.json": {
                    "status": "complete",
                    "analysis_allowed": True,
                    "physical_file_count": 10,
                    "logical_document_count": 6,
                },
            },
            {},
        )

    saved = {
        "_arguments": runner._arguments,
        "_repository_root": runner._repository_root,
        "_git_preflight": runner._git_preflight,
        "_configure": runner._configure,
        "_initialize_local_runtime": runner._initialize_local_runtime,
        "load_candidate": runner.load_candidate,
        "_static_contract_preflight": runner._static_contract_preflight,
        "database_preflight": runner.database_preflight,
        "provider_preflight": runner.provider_preflight,
        "_prepare_documents": runner._prepare_documents,
    }
    try:
        runner._arguments = fake_arguments
        runner._repository_root = fake_repository_root
        runner._git_preflight = fake_git_preflight
        runner._configure = fake_configure
        runner._initialize_local_runtime = fake_initialize_local_runtime
        runner.load_candidate = fake_load_candidate
        runner._static_contract_preflight = lambda: {"schema_mismatches": []}
        runner.database_preflight = lambda: {"ready": True}
        runner.provider_preflight = lambda path, sha: {"approved": True}
        runner._prepare_documents = lambda **kw: []
        code = runner.main()
        assert code == 0
        assert application_workflow.corpus_hash is original_hash
    finally:
        for name, original in saved.items():
            setattr(runner, name, original)
        application_workflow.corpus_hash = original_hash


# (d) application_workflow.corpus_hash is restored after exception.
def test_workflow_hash_restored_after_exception():
    physical = _physical_10()
    expected = _profile_newline_expected(physical)
    original_hash = application_workflow.corpus_hash

    def fake_arguments():
        class Args:
            candidate_root = None
            database_path = None
            output_root = None
            expected_head = "a" * 40
            expected_corpus_sha = expected
            expected_policy_sha = "0" * 64
            registry_number = "0388100001826000047"
            customer_name = "Test"
            project_name = "Test"
            initialize_database = False
            execute_provider = False
            static_only = False
            prepare_only = False
            verify_pre_provider_stage_boundary = False
            private_verification_descriptor = None
        return Args()

    def fake_repository_root():
        return runner.Path("/tmp")

    def fake_git_preflight(repo_root, expected_head):
        return {"head_sha": expected_head, "branch": "test", "dirty": False}

    def fake_configure(args, repo_root):
        return {
            "candidate_root": runner.Path("/tmp/candidate"),
            "intake_root": runner.Path("/tmp/intake"),
            "database_path": runner.Path("/tmp/db.sqlite"),
            "policy_path": runner.Path("/tmp/policy.json"),
            "output_root": runner.Path("/tmp/output"),
        }

    def fake_initialize_local_runtime(paths, repo_root, initialize_database=False):
        pass

    def fake_load_candidate(candidate_root):
        raise OSError("test-induced-failure")

    saved = {
        "_arguments": runner._arguments,
        "_repository_root": runner._repository_root,
        "_git_preflight": runner._git_preflight,
        "_configure": runner._configure,
        "_initialize_local_runtime": runner._initialize_local_runtime,
        "load_candidate": runner.load_candidate,
        "_static_contract_preflight": runner._static_contract_preflight,
        "database_preflight": runner.database_preflight,
        "provider_preflight": runner.provider_preflight,
    }
    try:
        runner._arguments = fake_arguments
        runner._repository_root = fake_repository_root
        runner._git_preflight = fake_git_preflight
        runner._configure = fake_configure
        runner._initialize_local_runtime = fake_initialize_local_runtime
        runner.load_candidate = fake_load_candidate
        runner._static_contract_preflight = lambda: {"schema_mismatches": []}
        runner.database_preflight = lambda: {"ready": True}
        runner.provider_preflight = lambda path, sha: {"approved": True}
        code = runner.main()
        assert code == 3
        assert application_workflow.corpus_hash is original_hash
    finally:
        for name, original in saved.items():
            setattr(runner, name, original)
        application_workflow.corpus_hash = original_hash


# (e) Wrong/mismatched corpus still fails closed.
def test_mismatched_corpus_fails_closed():
    physical = _physical_10()
    wrong_expected = "0" * 64

    with pytest.raises(AcceptanceBlocked, match="canonical_corpus_sha_mismatch"):
        BoundCorpusHashResolver(wrong_expected)(physical)
