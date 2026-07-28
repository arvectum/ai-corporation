from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ChunkingConfig:
    chunk_size_chars: int = 1500
    overlap_chars: int = 200
    min_chunk_chars: int = 120


@dataclass(frozen=True)
class ChunkDraft:
    chunk_index: int
    text: str
    text_hash: str
    char_start: int
    char_end: int
    token_estimate: int


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00a0", " ")
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return _SPACE_RE.sub(" ", text).strip()


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def _deduplicate_and_reindex(drafts: list[ChunkDraft]) -> list[ChunkDraft]:
    unique: list[ChunkDraft] = []
    seen_hashes: set[str] = set()
    for draft in drafts:
        if draft.text_hash in seen_hashes:
            continue
        seen_hashes.add(draft.text_hash)
        unique.append(
            ChunkDraft(
                chunk_index=len(unique),
                text=draft.text,
                text_hash=draft.text_hash,
                char_start=draft.char_start,
                char_end=draft.char_end,
                token_estimate=draft.token_estimate,
            )
        )
    return unique


def chunk_text(text: str, config: ChunkingConfig) -> list[ChunkDraft]:
    cleaned = normalize_text(text)
    if len(cleaned) < config.min_chunk_chars:
        return []

    chunk_size = max(config.min_chunk_chars, config.chunk_size_chars)
    overlap = max(0, min(config.overlap_chars, chunk_size // 2))

    drafts: list[ChunkDraft] = []
    start = 0
    chunk_index = 0
    length = len(cleaned)

    while start < length:
        end = min(length, start + chunk_size)
        if end < length:
            window = cleaned[start:end]
            split_at = max(window.rfind(". "), window.rfind("\n"), window.rfind(" "))
            if split_at >= config.min_chunk_chars // 2:
                end = start + split_at + 1

        piece = cleaned[start:end].strip()
        if len(piece) >= config.min_chunk_chars:
            text_hash = hashlib.sha256(piece.encode("utf-8")).hexdigest()
            drafts.append(
                ChunkDraft(
                    chunk_index=chunk_index,
                    text=piece,
                    text_hash=text_hash,
                    char_start=start,
                    char_end=end,
                    token_estimate=estimate_tokens(piece),
                )
            )
            chunk_index += 1

        if end >= length:
            break
        next_start = max(end - overlap, start + 1)
        while next_start < length and cleaned[next_start].isspace():
            next_start += 1
        start = next_start

    return _deduplicate_and_reindex(drafts)
