from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any, Final
from urllib.parse import quote

NOTICE_NUMBER: Final = "0344100006426000005"
EXPECTED_DOCUMENT_NAMES: Final = (
    "1. Расчет НМЦК1.xlsx",
    "1. Расчет НМЦК2.docx",
    "2. Проект контракта.docx",
    "3. Описание объекта закупки.docx",
    "4. Требования к содержанию, составу заявки.docx",
    "5. Реквизиты.docx",
    "6. Информация о поставке товара.docx",
)
EXPECTED_DOCUMENT_COUNT: Final = len(EXPECTED_DOCUMENT_NAMES)
SOURCE_AUTHORITY: Final = "ЕИС / zakupki.gov.ru"
SCHEMA_VERSION: Final = "p6.05-exact-attachment-evidence-v1"
PURPOSE: Final = "exact-tender-attachment-evidence"
_SAFE_REF_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


class ExactAttachmentEvidenceBlocked(RuntimeError):
    """Fail-closed product-side evidence error with safe structured details."""

    def __init__(
        self,
        code: str,
        *,
        missing_names: tuple[str, ...] = (),
        duplicate_names: tuple[str, ...] = (),
        exact_document_count: int = 0,
    ) -> None:
        self.code = code
        self.missing_names = missing_names
        self.duplicate_names = duplicate_names
        self.exact_document_count = exact_document_count
        super().__init__(code)


def _normalized_name(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "").strip())


def _require_bool(metadata: dict[str, Any], key: str, expected: bool) -> None:
    if metadata.get(key) is not expected:
        raise ExactAttachmentEvidenceBlocked(f"unsafe_{key}")


def _require_notice(metadata: dict[str, Any]) -> None:
    procurement = metadata.get("procurement")
    procurement = procurement if isinstance(procurement, dict) else {}
    candidates = {
        _normalized_name(metadata.get("reestr_number")),
        _normalized_name(metadata.get("notice_number")),
        _normalized_name(metadata.get("procurement_id")),
        _normalized_name(procurement.get("procurement_number")),
        _normalized_name(procurement.get("procurement_id")),
    }
    candidates.discard("")
    if NOTICE_NUMBER not in candidates or any(item != NOTICE_NUMBER for item in candidates):
        raise ExactAttachmentEvidenceBlocked("notice_mismatch")


def _require_retrieval_context(metadata: dict[str, Any]) -> tuple[str, str]:
    if metadata.get("procurement_source") != "zakupki_gov_ru_getdocs_ip":
        raise ExactAttachmentEvidenceBlocked("wrong_procurement_source")
    _require_notice(metadata)
    _require_bool(metadata, "external_actions", False)
    _require_bool(metadata, "no_platform_submission", True)
    _require_bool(metadata, "no_email_sending", True)
    _require_bool(metadata, "no_digital_signature", True)
    _require_bool(metadata, "archive_downloaded", True)
    _require_bool(metadata, "archive_extraction_complete", True)
    if metadata.get("getdocs_status") != "completed":
        raise ExactAttachmentEvidenceBlocked("getdocs_not_completed")

    ref_id = _normalized_name(metadata.get("getdocs_ref_id"))
    if not ref_id:
        raise ExactAttachmentEvidenceBlocked("missing_getdocs_ref_id")
    if not _SAFE_REF_ID.fullmatch(ref_id):
        raise ExactAttachmentEvidenceBlocked("unsafe_getdocs_ref_id")

    retrieved_at = _normalized_name(metadata.get("created_at"))
    try:
        parsed = datetime.fromisoformat(retrieved_at)
    except ValueError as exc:
        raise ExactAttachmentEvidenceBlocked("invalid_retrieval_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExactAttachmentEvidenceBlocked("invalid_retrieval_timestamp")
    return ref_id, retrieved_at


def _resolve_exact_file(input_dir: Path, stored_name: str) -> Path:
    stored = PurePath(stored_name)
    if stored.is_absolute() or ".." in stored.parts:
        raise ExactAttachmentEvidenceBlocked("unsafe_stored_path")

    root = input_dir.resolve(strict=True)
    candidate = input_dir / stored_name
    current = input_dir
    for part in stored.parts:
        current = current / part
        if current.is_symlink():
            raise ExactAttachmentEvidenceBlocked("symlinked_evidence_file")

    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise ExactAttachmentEvidenceBlocked("missing_or_outside_evidence_file") from exc
    if not resolved.is_file():
        raise ExactAttachmentEvidenceBlocked("evidence_path_is_not_file")
    return resolved


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_exact_attachment_evidence(
    metadata: dict[str, Any],
    *,
    input_dir: Path,
) -> dict[str, Any]:
    """Build a purpose-scoped exact-byte manifest from one completed EIS read-only run."""

    ref_id, retrieved_at = _require_retrieval_context(metadata)
    raw_files = metadata.get("files")
    if not isinstance(raw_files, list):
        raise ExactAttachmentEvidenceBlocked("files_metadata_missing")

    expected_by_name = {
        _normalized_name(name): name for name in EXPECTED_DOCUMENT_NAMES
    }
    matches: dict[str, list[dict[str, Any]]] = {
        name: [] for name in EXPECTED_DOCUMENT_NAMES
    }
    for item in raw_files:
        if not isinstance(item, dict):
            continue
        normalized = _normalized_name(item.get("original_name"))
        expected = expected_by_name.get(normalized)
        if expected is not None:
            matches[expected].append(item)

    missing = tuple(name for name in EXPECTED_DOCUMENT_NAMES if not matches[name])
    duplicates = tuple(name for name in EXPECTED_DOCUMENT_NAMES if len(matches[name]) > 1)
    exact_count = sum(1 for name in EXPECTED_DOCUMENT_NAMES if len(matches[name]) == 1)
    if duplicates:
        raise ExactAttachmentEvidenceBlocked(
            "duplicate_expected_document_names",
            missing_names=missing,
            duplicate_names=duplicates,
            exact_document_count=exact_count,
        )
    if missing:
        raise ExactAttachmentEvidenceBlocked(
            "missing_expected_documents",
            missing_names=missing,
            exact_document_count=exact_count,
        )

    documents: list[dict[str, Any]] = []
    for index, name in enumerate(EXPECTED_DOCUMENT_NAMES, start=1):
        item = matches[name][0]
        stored_name = _normalized_name(item.get("stored_name"))
        if not stored_name:
            raise ExactAttachmentEvidenceBlocked(
                "missing_stored_name",
                exact_document_count=index - 1,
            )
        exact_path = _resolve_exact_file(input_dir, stored_name)
        digest, size_bytes = _sha256_file(exact_path)
        declared_size = item.get("size_bytes")
        if declared_size is not None and declared_size != size_bytes:
            raise ExactAttachmentEvidenceBlocked(
                "size_mismatch",
                exact_document_count=index - 1,
            )

        documents.append(
            {
                "index": index,
                "name": name,
                "sha256": digest,
                "size_bytes": size_bytes,
                "artifact_id": f"artifact/content-sha256:{digest}",
                "source_locator": (
                    f"eis-getdocs://notice/{NOTICE_NUMBER}/ref/"
                    f"{quote(ref_id, safe='')}/document/{index:02d}"
                ),
                "external_source_authority": SOURCE_AUTHORITY,
                "external_source_reference": f"44fz-notice:{NOTICE_NUMBER}",
                "external_source_version": ref_id,
                "retrieved_at": retrieved_at,
            }
        )

    manifest_body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "purpose": PURPOSE,
        "status": "PASS_EXACT_ATTACHMENT_EVIDENCE",
        "notice_number": NOTICE_NUMBER,
        "expected_document_count": EXPECTED_DOCUMENT_COUNT,
        "exact_document_count": len(documents),
        "missing_names": [],
        "duplicate_names": [],
        "external_actions": False,
        "external_source_authority": SOURCE_AUTHORITY,
        "external_source_reference": f"44fz-notice:{NOTICE_NUMBER}",
        "external_source_version": ref_id,
        "retrieved_at": retrieved_at,
        "documents": documents,
    }
    manifest_sha256 = hashlib.sha256(_canonical_json_bytes(manifest_body)).hexdigest()
    return {
        **manifest_body,
        "manifest_sha256": manifest_sha256,
        "manifest_integrity_ref": f"sha256:{manifest_sha256}",
    }


def write_exact_attachment_evidence(
    manifest: dict[str, Any],
    *,
    output_path: Path,
) -> None:
    """Write local evidence atomically; never stores raw document bytes in the manifest."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_name(f".{output_path.name}.tmp")
    tmp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    tmp_path.replace(output_path)
