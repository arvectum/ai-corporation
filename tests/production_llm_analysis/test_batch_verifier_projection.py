from dataclasses import dataclass

import pytest

from scripts.r10_1.verify_batch_audit_plans import _persisted_evidence_fragments


@dataclass
class _Document:
    evidence_chunks: list[dict] | None


def _chunk(index: int) -> dict:
    return {
        "document_id": "doc-1",
        "document_name": "document.txt",
        "chunk_id": f"chunk-{index}",
        "locator": {
            "document_order": 1,
            "chunk_index": index,
            "char_start": index * 10,
            "char_end": index * 10 + 9,
            "text_hash": f"hash-{index}",
            "token_estimate": 3,
            "role": "supporting",
        },
        "text": f"chunk text {index}",
    }


def test_verifier_projects_all_persisted_chunks() -> None:
    fragments = _persisted_evidence_fragments(
        [_Document([_chunk(0), _chunk(1)]), _Document([_chunk(2)])]
    )

    assert [fragment.chunk_id for fragment in fragments] == [
        "chunk-0",
        "chunk-1",
        "chunk-2",
    ]
    assert all(fragment.locator["chunk_index"] in {0, 1, 2} for fragment in fragments)


def test_verifier_never_falls_back_to_full_document_text() -> None:
    with pytest.raises(SystemExit, match="evidence_batch_plan_chunks_unavailable"):
        _persisted_evidence_fragments([_Document(None)])
