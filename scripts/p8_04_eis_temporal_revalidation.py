from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Any, Final

from scripts.p6_05_exact_attachment_evidence import (
    NOTICE_NUMBER,
    SOURCE_AUTHORITY,
    _canonical_json_bytes,
    build_exact_attachment_evidence,
)

P8_04_SCHEMA_VERSION: Final = "p8.04-eis-temporal-revalidation-v1"
P6_BASELINE_MANIFEST_SHA256: Final = (
    "74e943d855406b04741f040fed271bddfaada9a9cc6e7da4501735a6e8725121"
)
P6_BASELINE_SCHEMA_VERSION: Final = "p6.05-exact-attachment-evidence-v1"
P6_BASELINE_STATUS: Final = "PASS_EXACT_ATTACHMENT_EVIDENCE"
PURPOSE: Final = "p8.04-eis-temporal-revalidation"


class P804TemporalRevalidationBlocked(RuntimeError):
    """Fail-closed P8.04 revalidation error with a safe structured code."""

    def __init__(self, code: str, *, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}".rstrip())


def _normalized_name(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "").strip())


def _verify_baseline_hash(manifest: dict[str, Any], expected_sha256: str) -> None:
    body = {
        key: value
        for key, value in manifest.items()
        if key not in ("manifest_sha256", "manifest_integrity_ref")
    }
    actual = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    if actual != expected_sha256:
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_BASELINE_EVIDENCE_MISSING",
            detail=f"baseline manifest SHA-256 mismatch (expected {expected_sha256}, got {actual})",
        )


def _verify_embedded_manifest_hash(manifest: dict[str, Any], *, label: str) -> None:
    """Recompute the canonical JSON body SHA-256 and compare with the manifest's own hash.

    This is a domain-neutral cryptographic integrity check on the exact observation
    manifest, independent of the cross-field comparison. Any mismatch fails closed.
    """
    body = {
        key: value
        for key, value in manifest.items()
        if key not in ("manifest_sha256", "manifest_integrity_ref")
    }
    actual = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    expected = manifest.get("manifest_sha256")
    if not isinstance(expected, str) or expected != actual:
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_MANIFEST_INTEGRITY_MISMATCH",
            detail=f"{label} manifest canonical-body SHA-256 mismatch",
        )


def verify_baseline_manifest(
    manifest: dict[str, Any],
    *,
    expected_sha256: str = P6_BASELINE_MANIFEST_SHA256,
) -> dict[str, Any]:
    """Validate the immutable P6 baseline manifest fail-closed.

    ``expected_sha256`` defaults to the owner-pinned P6.05-L7 attempt #2 manifest
    SHA-256 for live runs; callers testing the mechanism with synthetic fixtures
    pass the fixture's own canonical hash explicitly.
    """
    if not isinstance(manifest, dict):
        raise P804TemporalRevalidationBlocked("BLOCKED_BASELINE_EVIDENCE_MISSING", detail="not an object")
    if manifest.get("schema_version") != P6_BASELINE_SCHEMA_VERSION:
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_BASELINE_EVIDENCE_MISSING",
            detail=f"unexpected baseline schema_version {manifest.get('schema_version')!r}",
        )
    if manifest.get("status") != P6_BASELINE_STATUS:
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_BASELINE_EVIDENCE_MISSING",
            detail=f"baseline not exact-evidence status: {manifest.get('status')!r}",
        )
    if _normalized_name(manifest.get("notice_number")) != NOTICE_NUMBER:
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_BASELINE_EVIDENCE_MISSING",
            detail=f"baseline notice mismatch: {manifest.get('notice_number')!r}",
        )
    _verify_baseline_hash(manifest, expected_sha256)
    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_BASELINE_EVIDENCE_MISSING",
            detail="baseline contains no exact documents",
        )
    _document_set(manifest, label="baseline")
    return manifest


def load_and_verify_baseline(
    baseline_path: Path,
    *,
    expected_sha256: str = P6_BASELINE_MANIFEST_SHA256,
) -> dict[str, Any]:
    """Load and fail-closed verify the owner-local immutable P6 baseline manifest."""
    try:
        manifest = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_BASELINE_EVIDENCE_MISSING",
            detail=f"baseline manifest unreadable: {exc}",
        ) from exc
    return verify_baseline_manifest(manifest, expected_sha256=expected_sha256)


def build_fresh_snapshot(metadata: dict[str, Any], input_dir: Path) -> dict[str, Any]:
    """Build the fresh exact observation using the P6 exact-evidence builder."""
    return build_exact_attachment_evidence(metadata, input_dir=input_dir)


def _document_set(manifest: dict[str, Any], *, label: str) -> dict[str, dict[str, Any]]:
    """Index exact documents by normalized name, failing closed on any defect.

    Never silently overwrites a duplicate name and never trusts an empty or
    malformed document list, because either could mask real material drift.
    """
    documents = manifest.get("documents")
    if not isinstance(documents, list) or not documents:
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_EMPTY_DOCUMENTS",
            detail=f"{label} documents list is empty or missing",
        )
    by_name: dict[str, dict[str, Any]] = {}
    for item in documents:
        if not isinstance(item, dict):
            raise P804TemporalRevalidationBlocked(
                "BLOCKED_MALFORMED_DOCUMENT_ENTRY",
                detail=f"{label} contains a non-object document entry",
            )
        name = _normalized_name(item.get("name"))
        if not name:
            raise P804TemporalRevalidationBlocked(
                "BLOCKED_MALFORMED_DOCUMENT_ENTRY",
                detail=f"{label} contains a document entry without a name",
            )
        digest = item.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise P804TemporalRevalidationBlocked(
                "BLOCKED_MALFORMED_DOCUMENT_ENTRY",
                detail=f"{label} document {name!r} lacks a well-formed sha256",
            )
        try:
            int(digest, 16)
        except ValueError:
            raise P804TemporalRevalidationBlocked(
                "BLOCKED_MALFORMED_DOCUMENT_ENTRY",
                detail=f"{label} document {name!r} has a non-hex sha256",
            ) from None
        if name in by_name:
            raise P804TemporalRevalidationBlocked(
                "BLOCKED_DUPLICATE_DOCUMENT",
                detail=f"{label} contains duplicate document {name!r}",
            )
        by_name[name] = item
    return by_name


def _verify_completeness_metadata(
    manifest: dict[str, Any],
    *,
    label: str,
    document_count: int,
) -> None:
    """Fail closed when declared count/completeness fields contradict the documents."""
    exact = manifest.get("exact_document_count")
    if exact is not None and exact != document_count:
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_INCONSISTENT_COMPLETENESS",
            detail=f"{label} exact_document_count {exact!r} contradicts {document_count} documents",
        )
    duplicates = manifest.get("duplicate_names")
    if not isinstance(duplicates, list) or duplicates:
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_INCONSISTENT_COMPLETENESS",
            detail=f"{label} duplicate_names is not an empty list",
        )
    if manifest.get("external_actions") is not False:
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_EXTERNAL_ACTIONS",
            detail=f"{label} external_actions is not False",
        )


def compare_document_sets(
    baseline: dict[str, Any],
    fresh: dict[str, Any],
) -> list[dict[str, Any]]:
    """Deterministic per-document comparison across the two exact snapshots.

    Classifications: UNCHANGED, CHANGED, ADDED, REMOVED.
    """
    baseline_docs = _document_set(baseline, label="baseline")
    fresh_docs = _document_set(fresh, label="fresh")
    names = sorted(set(baseline_docs) | set(fresh_docs))
    entries: list[dict[str, Any]] = []
    for name in names:
        base = baseline_docs.get(name)
        cur = fresh_docs.get(name)
        if base is not None and cur is not None:
            if base.get("sha256") == cur.get("sha256"):
                classification = "UNCHANGED"
            else:
                classification = "CHANGED"
        elif cur is not None:
            classification = "ADDED"
        else:
            classification = "REMOVED"
        entries.append(
            {
                "name": name,
                "classification": classification,
                "baseline_sha256": base.get("sha256") if base else None,
                "fresh_sha256": cur.get("sha256") if cur else None,
            }
        )
    return entries


def aggregate_result(entries: list[dict[str, Any]]) -> str:
    """NO_CHANGE only when every material document is UNCHANGED.

    An empty comparison never means NO_CHANGE: it means the evidence was empty
    and the temporal revalidation must fail closed instead of passing.
    """
    if not entries:
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_EMPTY_COMPARISON",
            detail="empty comparison cannot derive NO_CHANGE",
        )
    if any(item["classification"] != "UNCHANGED" for item in entries):
        return "CHANGE_DETECTED"
    return "NO_CHANGE"


def build_comparison_manifest(
    baseline: dict[str, Any],
    fresh: dict[str, Any],
    *,
    fresh_observed_at: str,
) -> dict[str, Any]:
    """Deterministic P8.04 comparison manifest referencing both immutable snapshots.

    Fails closed on empty or malformed document sets, duplicate document names,
    internally inconsistent completeness metadata, or any canonical-body
    SHA-256 mismatch in either the baseline or the fresh observation manifest.
    """
    _verify_embedded_manifest_hash(baseline, label="baseline")
    _verify_embedded_manifest_hash(fresh, label="fresh")
    baseline_docs = _document_set(baseline, label="baseline")
    fresh_docs = _document_set(fresh, label="fresh")
    _verify_completeness_metadata(baseline, label="baseline", document_count=len(baseline_docs))
    _verify_completeness_metadata(fresh, label="fresh", document_count=len(fresh_docs))

    entries = compare_document_sets(baseline, fresh)
    aggregate = aggregate_result(entries)

    body: dict[str, Any] = {
        "schema_version": P8_04_SCHEMA_VERSION,
        "purpose": PURPOSE,
        "status": aggregate,
        "notice_number": NOTICE_NUMBER,
        "external_source_authority": SOURCE_AUTHORITY,
        "external_source_reference": f"44fz-notice:{NOTICE_NUMBER}",
        "baseline_schema_version": baseline.get("schema_version"),
        "baseline_manifest_sha256": baseline.get("manifest_sha256"),
        "baseline_retrieved_at": baseline.get("retrieved_at"),
        "fresh_schema_version": fresh.get("schema_version"),
        "fresh_manifest_sha256": fresh.get("manifest_sha256"),
        "fresh_retrieved_at": fresh.get("retrieved_at"),
        "fresh_observed_at": fresh_observed_at,
        "external_actions": False,
        "comparison_entries": entries,
        "aggregate_result": aggregate,
        "evidence_completeness": "complete",
    }
    manifest_sha256 = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    return {
        **body,
        "manifest_sha256": manifest_sha256,
        "manifest_integrity_ref": f"sha256:{manifest_sha256}",
    }


def write_manifest(manifest: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
