from __future__ import annotations

import hashlib
import json

import pytest

from scripts.arv001 import application_workflow
from scripts.arv001 import run_complete_corpus_acceptance as runner
from scripts.arv001 import run_complete_corpus_acceptance_split_roots as adapter
from scripts.arv001.complete_corpus_contract import AcceptanceBlocked
from scripts.arv001.corpus_hash_resolver import (
    BoundCorpusHashResolver,
    resolve_corpus_hash_profile,
)


def _physical() -> list[dict]:
    return [
        {
            "ordinal": 2,
            "original_name": "Бета.docx",
            "sha256": "b" * 64,
            "size_bytes": 202,
            "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "document_kind": "contract_draft",
            "source_type": "eis_attachment",
            "parse_status": "extracted",
            "parse_failure_reason": None,
        },
        {
            "ordinal": 1,
            "original_name": "Альфа.xml",
            "sha256": "a" * 64,
            "size_bytes": 101,
            "content_type": "application/xml",
            "document_kind": "notice",
            "source_type": "getdocs_ip",
            "parse_status": "extracted",
            "parse_failure_reason": None,
        },
    ]


def _stable_expected(physical: list[dict]) -> str:
    projected = [
        {
            "original_name": item["original_name"],
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
        }
        for item in physical
    ]
    projected.sort(key=lambda item: item["original_name"])
    payload = json.dumps(
        projected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_resolver_finds_stable_projection_and_ignores_input_order():
    physical = _physical()
    expected = _stable_expected(physical)

    first = resolve_corpus_hash_profile(physical, expected)
    second = resolve_corpus_hash_profile(list(reversed(physical)), expected)

    assert first.sha256 == expected
    assert first.fields == ("original_name", "sha256", "size_bytes")
    assert first.serialization == "canonical_compact"
    assert second == first


def test_resolver_rejects_wrong_approved_hash_instead_of_falling_back():
    with pytest.raises(AcceptanceBlocked, match="canonical_corpus_sha_mismatch"):
        resolve_corpus_hash_profile(_physical(), "0" * 64)


def test_bound_resolver_requires_same_profile_after_round_trip():
    physical = _physical()
    expected = _stable_expected(physical)
    resolver = BoundCorpusHashResolver(expected)

    assert resolver(physical) == expected
    assert resolver(list(reversed(physical))) == expected
    assert resolver.profile is not None
    assert resolver.profile.fields == ("original_name", "sha256", "size_bytes")


def test_split_adapter_patches_both_pre_and_post_persistence_hash_calls(monkeypatch):
    physical = _physical()
    expected = _stable_expected(physical)
    original_runner_hash = runner._corpus_hash
    original_workflow_hash = application_workflow.corpus_hash

    def fake_main() -> int:
        assert runner._corpus_hash(physical) == expected
        assert application_workflow.corpus_hash(list(reversed(physical))) == expected
        return 0

    monkeypatch.setattr(runner, "main", fake_main)
    result, resolver = adapter._delegate_with_bound_hash(
        ["arv001-test"], expected
    )

    assert result == 0
    assert resolver.profile is not None
    assert runner._corpus_hash is original_runner_hash
    assert application_workflow.corpus_hash is original_workflow_hash


def test_resolver_requires_identity_fields():
    with pytest.raises(AcceptanceBlocked, match="corpus_identity_field_missing"):
        resolve_corpus_hash_profile(
            [{"original_name": "A.xml", "size_bytes": 1}],
            "0" * 64,
        )
