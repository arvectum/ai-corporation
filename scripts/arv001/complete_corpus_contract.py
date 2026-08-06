"""Pure local contracts for the ARV-001 complete-corpus acceptance run."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

DEFAULT_REGISTRY_NUMBER = "0388100001826000047"
DEFAULT_CORPUS_SHA256 = "6557c0fa0dcc85bbab1a1e72a556505734c65eea6a29e649082eafbe80dc1d0a"
DEFAULT_POLICY_SHA256 = "2fcb1db44eee3df5762410f892ad1f806221e811e356df4863108a3213db41d0"
DEFAULT_PROVIDER = "openai_compatible"
DEFAULT_MODEL = "arvectum-gemma4-12b-it-qat-q4_0"
DEFAULT_CUSTOMER_NAME = "ARV-001 Complete Corpus Local Acceptance"
DEFAULT_PROJECT_NAME = "ARV-001 Complete Corpus Acceptance"
REQUIRED_ARTIFACTS = (
    "physical-files.json",
    "metadata.json",
    "logical-documents.json",
    "document-set-summary.json",
    "deterministic-parse-summary.json",
    "intake-summary.json",
)
UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
HASH_PATTERN = re.compile(r"\b[0-9a-f]{64}\b", re.IGNORECASE)


class AcceptanceBlocked(RuntimeError):
    """Fail-closed local acceptance error."""


@dataclass(frozen=True)
class ChunkDraft:
    index: int
    text: str
    text_hash: str
    char_start: int
    char_end: int
    token_estimate: int


@dataclass(frozen=True)
class PreparedDocument:
    original_name: str
    stored_name: str
    path: Path
    sha256: str
    size_bytes: int
    content_type: str | None
    document_kind: str | None
    source_type: str | None
    source_url: str | None
    text: str
    chunks: tuple[ChunkDraft, ...]
    corpus_descriptor: dict[str, Any]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_json_sha256(value: Any) -> str:
    return sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AcceptanceBlocked(f"artifact_unreadable:{path.name}") from exc
    except json.JSONDecodeError as exc:
        raise AcceptanceBlocked(f"artifact_invalid_json:{path.name}") from exc


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def artifact_shape(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {
            "type": "array",
            "count": len(value),
            "item_keys": sorted(
                {
                    key
                    for item in value
                    if isinstance(item, dict)
                    for key in item
                }
            ),
        }
    if isinstance(value, dict):
        result: dict[str, Any] = {
            "type": "object",
            "count": len(value),
            "keys": sorted(value),
        }
        files = value.get("files")
        if isinstance(files, list):
            result["file_count"] = len(files)
            result["file_item_keys"] = sorted(
                {
                    key
                    for item in files
                    if isinstance(item, dict)
                    for key in item
                }
            )
        return result
    return {"type": type(value).__name__}


def load_candidate(candidate_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    values: dict[str, Any] = {}
    shapes: dict[str, Any] = {}
    for name in REQUIRED_ARTIFACTS:
        path = candidate_root / name
        if not path.is_file():
            raise AcceptanceBlocked(f"required_artifact_missing:{name}")
        values[name] = read_json(path)
        shapes[name] = artifact_shape(values[name])
    return values, shapes


def corpus_hash(physical: Sequence[dict[str, Any]]) -> str:
    ordered = sorted(physical, key=lambda item: str(item.get("original_name") or ""))
    return canonical_json_sha256(ordered)


def validate_document_set(values: dict[str, Any], expected_count: int) -> None:
    summary = values["document-set-summary.json"]
    if not isinstance(summary, dict):
        raise AcceptanceBlocked("document_set_summary_invalid")
    status = summary.get("status") or summary.get("document_set_status")
    if status != "complete" or summary.get("analysis_allowed") is not True:
        raise AcceptanceBlocked("document_set_not_complete")
    if int(summary.get("physical_file_count") or 0) != expected_count:
        raise AcceptanceBlocked("document_set_physical_count_mismatch")
    logical = values["logical-documents.json"]
    if not isinstance(logical, list) or len(logical) != 6:
        raise AcceptanceBlocked("logical_document_count_mismatch")
    names = "\n".join(
        str(item.get("name") or item.get("file") or "").lower()
        for item in logical
        if isinstance(item, dict)
    )
    required = (
        ("извещение",),
        ("описание объекта", "техничес"),
        ("обоснование нмцк",),
        ("составу заявки", "требования к заявке"),
        ("проект контракта",),
        ("обеспечения исполнения контракта",),
    )
    if any(not any(value in names for value in alternatives) for alternatives in required):
        raise AcceptanceBlocked("logical_document_group_missing")


def _resolve_regular_file(root: Path, stored_name: str) -> Path:
    relative = Path(stored_name)
    if relative.is_absolute() or ".." in relative.parts:
        raise AcceptanceBlocked("stored_name_unsafe")
    direct = root / relative
    candidates = [direct] if direct.exists() else list(root.rglob(relative.name))
    candidates = [item for item in candidates if item.is_file()]
    if any(item.is_symlink() for item in candidates):
        raise AcceptanceBlocked("stored_file_symlink_forbidden")
    unique = sorted({item.resolve() for item in candidates})
    if len(unique) != 1:
        raise AcceptanceBlocked("stored_file_mapping_not_unique")
    path = unique[0]
    if root.resolve() not in path.parents:
        raise AcceptanceBlocked("stored_file_outside_intake_root")
    return path


def _fixed_chunks(text: str, *, size: int, overlap: int) -> tuple[ChunkDraft, ...]:
    if size <= 0 or overlap < 0 or overlap >= size:
        raise AcceptanceBlocked("invalid_chunk_policy")
    result: list[ChunkDraft] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        raw = text[start:end]
        left = len(raw) - len(raw.lstrip())
        trimmed = raw.rstrip()
        char_start = start + left
        char_end = start + len(trimmed)
        if char_end > char_start:
            value = text[char_start:char_end]
            result.append(
                ChunkDraft(
                    index=len(result),
                    text=value,
                    text_hash=sha256_bytes(value.encode("utf-8")),
                    char_start=char_start,
                    char_end=char_end,
                    token_estimate=max(1, (len(value) + 3) // 4),
                )
            )
        if end >= len(text):
            break
        start = end - overlap
    if not result:
        raise AcceptanceBlocked("document_chunks_empty")
    return tuple(result)


def prepare_documents(
    *,
    physical: list[dict[str, Any]],
    metadata: dict[str, Any],
    intake_root: Path,
    max_chars: int,
    chunk_size: int,
    chunk_overlap: int,
) -> list[PreparedDocument]:
    from src.tender_research.document_text_extractor import EXTRACTED_STATUS, extract_text

    metadata_files = metadata.get("files") if isinstance(metadata, dict) else None
    if not isinstance(metadata_files, list) or any(
        not isinstance(item, dict) for item in metadata_files
    ):
        raise AcceptanceBlocked("metadata_files_invalid")
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in metadata_files:
        by_name.setdefault(str(item.get("original_name") or ""), []).append(item)
    prepared: list[PreparedDocument] = []
    used_storage: set[str] = set()
    ordered = sorted(
        physical, key=lambda item: str(item.get("original_name") or "")
    )
    for ordinal, descriptor in enumerate(ordered, start=1):
        original_name = str(descriptor.get("original_name") or "").strip()
        candidates = by_name.get(original_name, [])
        if not original_name or len(candidates) != 1:
            raise AcceptanceBlocked("metadata_original_name_mapping_not_unique")
        metadata_item = candidates[0]
        stored_name = str(metadata_item.get("stored_name") or "").strip()
        if not stored_name or stored_name in used_storage:
            raise AcceptanceBlocked("metadata_stored_name_invalid_or_duplicate")
        path = _resolve_regular_file(intake_root, stored_name)
        actual_hash = sha256_file(path)
        actual_size = path.stat().st_size
        if actual_hash != str(descriptor.get("sha256") or "").lower():
            raise AcceptanceBlocked("source_file_sha256_mismatch")
        if actual_size != int(descriptor.get("size_bytes") or -1):
            raise AcceptanceBlocked("source_file_size_mismatch")
        status, text = extract_text(str(path), max_chars=max_chars)
        if status != EXTRACTED_STATUS or not text.strip():
            extension = path.suffix.lower() or "none"
            raise AcceptanceBlocked(
                "document_text_extraction_failed:"
                f"ordinal={ordinal}:ext={extension}:status={status}"
            )
        prepared.append(
            PreparedDocument(
                original_name=original_name,
                stored_name=stored_name,
                path=path,
                sha256=actual_hash,
                size_bytes=actual_size,
                content_type=descriptor.get("content_type"),
                document_kind=descriptor.get("document_kind"),
                source_type=descriptor.get("source_type"),
                source_url=metadata_item.get("source_url"),
                text=text,
                chunks=_fixed_chunks(text, size=chunk_size, overlap=chunk_overlap),
                corpus_descriptor=dict(descriptor),
            )
        )
        used_storage.add(stored_name)
    metadata_names = {str(item.get("original_name") or "") for item in metadata_files}
    physical_names = {item.original_name for item in prepared}
    if len(prepared) != len(physical) or metadata_names != physical_names:
        raise AcceptanceBlocked("physical_metadata_mapping_incomplete")
    return prepared


def validate_customer_report(html: str, registry_number: str) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).lower()
    required = {
        "registry_number": (registry_number.lower(),),
        "diesel_fuel": ("дизель",),
        "quantity_140": ("140",),
        "nmck": ("25 200 000", "25200000", "25 200 000"),
        "okpd2": ("19.20.21.300",),
        "technical_document": ("описание объекта закупки", "техническ"),
        "contract_draft": ("проект контракта",),
        "payment": ("оплат",),
        "acceptance": ("приемк", "приёмк"),
        "security": ("обеспечен",),
        "liability": ("ответствен", "штраф"),
    }
    missing = [
        name
        for name, alternatives in required.items()
        if not any(value in text for value in alternatives)
    ]
    if missing:
        raise AcceptanceBlocked(
            "customer_report_required_content_missing:" + ",".join(missing)
        )
    forbidden = (
        "проект контракта отсутствует",
        "проект контракта не найден",
        "отдельное техническое задание или описание объекта закупки не найдено",
        "пять структурированных файлов",
        "/users/",
        "/volumes/",
        "run_id",
        "customer_id",
        "evidence_id",
        "storage_key",
    )
    if (
        any(value in text for value in forbidden)
        or UUID_PATTERN.search(html)
        or HASH_PATTERN.search(html)
    ):
        raise AcceptanceBlocked("customer_report_private_or_stale_content_detected")
    return {
        "required_groups_present": sorted(required),
        "forbidden_content_present": False,
    }
