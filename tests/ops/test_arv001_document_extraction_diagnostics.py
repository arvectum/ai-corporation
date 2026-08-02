from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.arv001.complete_corpus_contract import AcceptanceBlocked, prepare_documents
from src.tender_research import document_text_extractor as extractor


def test_prepare_documents_reports_ordinal_extension_and_status_without_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "private-intake-root" / "stored-requirements.doc"
    source.parent.mkdir()
    source.write_bytes(b"legacy-word-binary")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    monkeypatch.setattr(
        extractor,
        "extract_text",
        lambda local_path, max_chars: (extractor.EMPTY_STATUS, ""),
    )

    with pytest.raises(AcceptanceBlocked) as raised:
        prepare_documents(
            physical=[
                {
                    "original_name": "Requirements.doc",
                    "sha256": digest,
                    "size_bytes": source.stat().st_size,
                }
            ],
            metadata={
                "files": [
                    {
                        "original_name": "Requirements.doc",
                        "stored_name": source.name,
                    }
                ]
            },
            intake_root=source.parent,
            max_chars=100_000,
            chunk_size=64,
            chunk_overlap=8,
        )

    reason = str(raised.value)
    assert reason == (
        "document_text_extraction_failed:ordinal=1:ext=.doc:status=empty"
    )
    assert str(tmp_path) not in reason
    assert source.name not in reason
