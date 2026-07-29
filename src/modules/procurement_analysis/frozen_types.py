"""Storage-neutral values consumed by the frozen procurement pipeline."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AnalyzedDocument:
    display_name: str
    extension: str
    role: str
    text: str | None
    extracted_text_available: bool
    warnings: list[str]
    source: str
    file_id: str
    raw_content: bytes | None = None
    # Optional persisted chunk projection for R10.1.  The frozen R9 producer
    # intentionally ignores this field and therefore keeps its input contract.
    evidence_chunks: list[dict] | None = None
