from __future__ import annotations

import hashlib
import json

import pytest

from scripts.arv001 import complete_corpus_contract as contract
from scripts.arv001 import run_complete_corpus_acceptance as runner


def _canonical_sha(rows: list[dict[str, object]], *, newline: bool) -> str:
    ordered = sorted(rows, key=lambda item: str(item["original_name"]))
    payload = json.dumps(
        ordered,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if newline:
        payload += "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _physical_rows() -> list[dict[str, object]]:
    return [
        {
            "original_name": "technical.pdf",
            "sha256": hashlib.sha256(b"technical").hexdigest(),
            "size_bytes": 9,
        },
        {
            "original_name": "notice.xml",
            "sha256": hashlib.sha256(b"notice").hexdigest(),
            "size_bytes": 6,
        },
    ]


def test_acceptance_runner_uses_frozen_newline_corpus_profile() -> None:
    physical = _physical_rows()
    expected = _canonical_sha(physical, newline=True)

    assert contract.corpus_hash(physical) != expected
    assert runner._resolve_bound_corpus_hash(physical, expected) == expected


def test_legacy_corpus_hash_contract_remains_compact() -> None:
    physical = _physical_rows()

    assert contract.corpus_hash(physical) == _canonical_sha(physical, newline=False)


def test_acceptance_runner_rejects_legacy_compact_profile() -> None:
    physical = _physical_rows()
    legacy = _canonical_sha(physical, newline=False)

    with pytest.raises(
        contract.AcceptanceBlocked,
        match="canonical_corpus_hash_profile_mismatch",
    ):
        runner._resolve_bound_corpus_hash(physical, legacy)


def test_acceptance_runner_fails_closed_on_descriptor_drift() -> None:
    physical = _physical_rows()
    expected = _canonical_sha(physical, newline=True)
    physical[0]["sha256"] = hashlib.sha256(b"changed").hexdigest()

    with pytest.raises(contract.AcceptanceBlocked, match="canonical_corpus_sha_mismatch"):
        runner._resolve_bound_corpus_hash(physical, expected)
