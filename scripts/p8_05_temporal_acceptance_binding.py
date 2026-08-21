from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePath
from typing import Any, Callable, Final, Sequence

from scripts.arv001.complete_corpus_contract import (
    DEFAULT_CORPUS_SHA256,
    DEFAULT_POLICY_SHA256,
    DEFAULT_REGISTRY_NUMBER,
    AcceptanceBlocked,
    load_candidate,
    validate_document_set,
)
from scripts.arv001.corpus_hash_resolver import resolve_corpus_hash_profile
from src.modules.tender_operator_agent_demo.document_set_completeness import (
    build_document_set_summary,
)

P8_05_SCHEMA_VERSION: Final = "p8.05-temporal-acceptance-binding-v1"
P8_05_AUTH_SCHEMA_VERSION: Final = "p8.05-golden-run-authorization-v1"
P8_05_RESULT_SCHEMA_VERSION: Final = "p8.05-bound-acceptance-result-v1"
P8_05_PURPOSE: Final = "temporal-revalidation-to-acceptance-binding"
FROZEN_BASELINE_ID: Final = "arv001-v2-6557c0fa0dcc"
FROZEN_BASELINE_GENERATION: Final = 2
FROZEN_BASELINE_KIND: Final = "reproducible_real_eis_acceptance"
AUTHORIZED_STATUS: Final = "AUTHORIZED_FOR_GOLDEN_RUN"
REVALIDATION_PASS: Final = "REVALIDATION_PASS"
REVALIDATION_BLOCKED: Final = "REVALIDATION_BLOCKED"
DRIFT_UNCHANGED: Final = "UNCHANGED"
DRIFT_ACCEPTABLE: Final = "ACCEPTABLE_DRIFT"
DRIFT_BLOCKING: Final = "BLOCKING_DRIFT"
_REQUIRED_LOGICAL_GROUPS: Final = frozenset(
    {
        "notice",
        "technical_specification",
        "price_justification",
        "application_requirements",
        "contract_draft",
        "contract_performance_security",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SAFE_FAILURE = re.compile(r"^[A-Za-z0-9_.:-]{1,240}$")


class P805AcceptanceBindingBlocked(RuntimeError):
    """Fail-closed P8.05 error with a sanitized stable code."""

    def __init__(self, code: str, *, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}".rstrip())


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _sha256_text(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_hash(value: object, code: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256.fullmatch(text):
        raise P805AcceptanceBindingBlocked(code)
    return text


def load_and_verify_frozen_baseline(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise P805AcceptanceBindingBlocked(
            "BLOCKED_FROZEN_BASELINE_MISSING",
            detail=type(exc).__name__,
        ) from exc
    return verify_frozen_baseline(value)


def verify_frozen_baseline(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise P805AcceptanceBindingBlocked("BLOCKED_FROZEN_BASELINE_INVALID")
    corpus = value.get("corpus")
    policy = value.get("policy")
    source = value.get("source")
    document_set = value.get("document_set")
    provenance = value.get("provenance")
    if not all(
        isinstance(item, dict)
        for item in (corpus, policy, source, document_set, provenance)
    ):
        raise P805AcceptanceBindingBlocked("BLOCKED_FROZEN_BASELINE_INVALID")
    assert isinstance(corpus, dict)
    assert isinstance(policy, dict)
    assert isinstance(source, dict)
    assert isinstance(document_set, dict)
    assert isinstance(provenance, dict)

    expected_profile = {
        "fields": ["original_name", "sha256", "size_bytes"],
        "ordering": "original_name_unicode_codepoint_ascending",
        "serialization": "canonical_compact_newline",
    }
    checks = (
        value.get("schema_version") == "1.0",
        value.get("baseline_id") == FROZEN_BASELINE_ID,
        value.get("baseline_generation") == FROZEN_BASELINE_GENERATION,
        value.get("baseline_kind") == FROZEN_BASELINE_KIND,
        value.get("registry_number") == DEFAULT_REGISTRY_NUMBER,
        corpus.get("sha256") == DEFAULT_CORPUS_SHA256,
        corpus.get("physical_file_count") == 10,
        corpus.get("logical_document_count") == 6,
        corpus.get("hash_profile") == expected_profile,
        policy.get("sha256") == DEFAULT_POLICY_SHA256,
        source.get("type") == "EIS",
        source.get("method") == "getDocsByReestrNumber",
        source.get("law") == "44fz",
        source.get("subsystem_type") == "PRIZ",
        int(source.get("fresh_acquisitions") or 0) >= 2,
        source.get("source_identity_reproduced") is True,
        document_set.get("status") == "complete",
        document_set.get("analysis_allowed") is True,
        set(document_set.get("required_logical_groups") or ())
        == _REQUIRED_LOGICAL_GROUPS,
        provenance.get("fresh_reacquisition_performed") is True,
        provenance.get("fresh_reacquisition_matches_historical_corpus_identity")
        is True,
        provenance.get("corpus_identity_changed") is False,
    )
    if not all(checks):
        raise P805AcceptanceBindingBlocked("BLOCKED_FROZEN_BASELINE_CONTRACT_MISMATCH")
    _require_hash(corpus.get("sha256"), "BLOCKED_FROZEN_BASELINE_CORPUS_HASH_INVALID")
    _require_hash(policy.get("sha256"), "BLOCKED_FROZEN_BASELINE_POLICY_HASH_INVALID")
    return value


def _serialize_profile(value: Any, serialization: str) -> bytes:
    if serialization == "canonical_compact_newline":
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    if serialization == "canonical_compact":
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    raise P805AcceptanceBindingBlocked("BLOCKED_UNSUPPORTED_CORPUS_HASH_PROFILE")


def profile_corpus_hash(
    physical: Sequence[dict[str, Any]],
    *,
    fields: Sequence[str],
    serialization: str,
) -> str:
    if not physical or any(not isinstance(item, dict) for item in physical):
        raise P805AcceptanceBindingBlocked("BLOCKED_PHYSICAL_CORPUS_INVALID")
    projected: list[dict[str, Any]] = []
    for item in physical:
        if any(field not in item for field in fields):
            raise P805AcceptanceBindingBlocked("BLOCKED_CORPUS_IDENTITY_FIELD_MISSING")
        projected.append({field: item[field] for field in fields})
    projected.sort(key=lambda item: str(item.get("original_name") or ""))
    return hashlib.sha256(_serialize_profile(projected, serialization)).hexdigest()


def _normalized_name(value: object) -> str:
    return str(value or "").strip()


def _material_index(rows: Sequence[dict[str, Any]], *, label: str) -> dict[str, dict[str, Any]]:
    if not rows:
        raise P805AcceptanceBindingBlocked("BLOCKED_EMPTY_MATERIAL_CORPUS", detail=label)
    result: dict[str, dict[str, Any]] = {}
    for item in rows:
        if not isinstance(item, dict):
            raise P805AcceptanceBindingBlocked("BLOCKED_MALFORMED_MATERIAL_ENTRY", detail=label)
        name = _normalized_name(item.get("original_name"))
        digest = _require_hash(item.get("sha256"), "BLOCKED_MALFORMED_MATERIAL_HASH")
        size = item.get("size_bytes")
        if not name or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise P805AcceptanceBindingBlocked("BLOCKED_MALFORMED_MATERIAL_ENTRY", detail=label)
        if name in result:
            raise P805AcceptanceBindingBlocked("BLOCKED_DUPLICATE_MATERIAL_NAME", detail=label)
        result[name] = {
            "original_name": name,
            "sha256": digest,
            "size_bytes": size,
        }
    return result


def _resolve_fresh_file(input_dir: Path, stored_name: object) -> Path:
    text = str(stored_name or "").strip()
    relative = PurePath(text)
    if not text or relative.is_absolute() or ".." in relative.parts:
        raise P805AcceptanceBindingBlocked("BLOCKED_UNSAFE_FRESH_STORED_PATH")
    try:
        root = input_dir.resolve(strict=True)
    except OSError as exc:
        raise P805AcceptanceBindingBlocked("BLOCKED_FRESH_INPUT_ROOT_MISSING") from exc
    current = input_dir
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise P805AcceptanceBindingBlocked("BLOCKED_SYMLINKED_FRESH_EVIDENCE")
    try:
        candidate = (input_dir / text).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise P805AcceptanceBindingBlocked("BLOCKED_FRESH_EVIDENCE_FILE_MISSING") from exc
    if not candidate.is_file():
        raise P805AcceptanceBindingBlocked("BLOCKED_FRESH_EVIDENCE_NOT_FILE")
    return candidate


def _verify_fresh_source_context(metadata: dict[str, Any], registry_number: str) -> tuple[str, str]:
    procurement = metadata.get("procurement")
    procurement = procurement if isinstance(procurement, dict) else {}
    identifiers = {
        _normalized_name(metadata.get("reestr_number")),
        _normalized_name(metadata.get("notice_number")),
        _normalized_name(metadata.get("procurement_id")),
        _normalized_name(procurement.get("procurement_number")),
        _normalized_name(procurement.get("procurement_id")),
    }
    identifiers.discard("")
    if registry_number not in identifiers or any(item != registry_number for item in identifiers):
        raise P805AcceptanceBindingBlocked("BLOCKED_FRESH_REGISTRY_MISMATCH")
    checks = (
        metadata.get("procurement_source") == "zakupki_gov_ru_getdocs_ip",
        metadata.get("external_actions") is False,
        metadata.get("no_platform_submission") is True,
        metadata.get("no_email_sending") is True,
        metadata.get("no_digital_signature") is True,
        metadata.get("archive_downloaded") is True,
        metadata.get("archive_extraction_complete") is True,
        metadata.get("getdocs_status") == "completed",
    )
    if not all(checks):
        raise P805AcceptanceBindingBlocked("BLOCKED_FRESH_EIS_CONTEXT_INVALID")
    ref_id = _normalized_name(metadata.get("getdocs_ref_id"))
    retrieved_at = _normalized_name(metadata.get("created_at"))
    if not ref_id or not retrieved_at:
        raise P805AcceptanceBindingBlocked("BLOCKED_FRESH_EIS_PROVENANCE_MISSING")
    return ref_id, retrieved_at


def load_frozen_candidate_material(
    candidate_root: Path,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    try:
        values, _shapes = load_candidate(candidate_root)
        validate_document_set(values, 10)
    except AcceptanceBlocked as exc:
        raise P805AcceptanceBindingBlocked(
            "BLOCKED_FROZEN_CANDIDATE_INVALID",
            detail=str(exc),
        ) from exc
    physical = values.get("physical-files.json")
    logical = values.get("logical-documents.json")
    metadata = values.get("metadata.json")
    if (
        not isinstance(physical, list)
        or len(physical) != 10
        or not isinstance(logical, list)
        or len(logical) != 6
        or not isinstance(metadata, dict)
    ):
        raise P805AcceptanceBindingBlocked("BLOCKED_FROZEN_CANDIDATE_SHAPE_MISMATCH")

    expected = baseline["corpus"]["sha256"]
    try:
        resolved = resolve_corpus_hash_profile(physical, expected)
    except AcceptanceBlocked as exc:
        raise P805AcceptanceBindingBlocked(
            "BLOCKED_FROZEN_CANDIDATE_CORPUS_MISMATCH",
            detail=str(exc),
        ) from exc
    profile = baseline["corpus"]["hash_profile"]
    if (
        list(resolved.fields) != profile["fields"]
        or resolved.serialization != profile["serialization"]
        or resolved.sha256 != expected
    ):
        raise P805AcceptanceBindingBlocked("BLOCKED_FROZEN_CANDIDATE_PROFILE_MISMATCH")
    index = _material_index(physical, label="baseline")
    return {
        "physical": list(index.values()),
        "physical_file_count": len(index),
        "logical_document_count": len(logical),
        "corpus_sha256": expected,
        "retrieved_at": _normalized_name(metadata.get("created_at")) or None,
        "retrieval_ref_sha256": _sha256_text(metadata.get("getdocs_ref_id")),
    }


def build_fresh_material_snapshot(
    metadata: dict[str, Any],
    *,
    input_dir: Path,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise P805AcceptanceBindingBlocked("BLOCKED_FRESH_METADATA_INVALID")
    registry = str(baseline["registry_number"])
    ref_id, retrieved_at = _verify_fresh_source_context(metadata, registry)
    raw_files = metadata.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise P805AcceptanceBindingBlocked("BLOCKED_FRESH_FILES_MISSING")

    rows: list[dict[str, Any]] = []
    for item in raw_files:
        if not isinstance(item, dict):
            raise P805AcceptanceBindingBlocked("BLOCKED_FRESH_FILE_ENTRY_INVALID")
        name = _normalized_name(item.get("original_name"))
        if not name:
            raise P805AcceptanceBindingBlocked("BLOCKED_FRESH_FILE_NAME_MISSING")
        path = _resolve_fresh_file(input_dir, item.get("stored_name"))
        digest, size = _sha256_file(path)
        declared_size = item.get("size_bytes")
        if declared_size is not None and declared_size != size:
            raise P805AcceptanceBindingBlocked("BLOCKED_FRESH_FILE_SIZE_MISMATCH")
        rows.append({"original_name": name, "sha256": digest, "size_bytes": size})

    index = _material_index(rows, label="fresh")
    summary = build_document_set_summary(raw_files)
    logical_documents = summary.get("logical_documents")
    logical_documents = logical_documents if isinstance(logical_documents, list) else []
    logical_groups = {
        str(item.get("kind") or "")
        for item in logical_documents
        if isinstance(item, dict)
    }
    profile = baseline["corpus"]["hash_profile"]
    corpus_sha = profile_corpus_hash(
        list(index.values()),
        fields=profile["fields"],
        serialization=profile["serialization"],
    )
    return {
        "physical": list(index.values()),
        "physical_file_count": len(index),
        "logical_document_count": int(summary.get("logical_document_count") or 0),
        "logical_groups": sorted(logical_groups),
        "document_set_status": summary.get("status"),
        "analysis_allowed": summary.get("analysis_allowed") is True,
        "corpus_sha256": corpus_sha,
        "retrieved_at": retrieved_at,
        "retrieval_ref_sha256": _sha256_text(ref_id),
    }


def compare_material_snapshots(
    baseline_snapshot: dict[str, Any],
    fresh_snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    baseline = _material_index(baseline_snapshot.get("physical") or [], label="baseline")
    fresh = _material_index(fresh_snapshot.get("physical") or [], label="fresh")
    result: list[dict[str, Any]] = []
    for name in sorted(set(baseline) | set(fresh)):
        old = baseline.get(name)
        new = fresh.get(name)
        if old is None:
            classification = "ADDED"
        elif new is None:
            classification = "REMOVED"
        elif old["sha256"] != new["sha256"] or old["size_bytes"] != new["size_bytes"]:
            classification = "CHANGED"
        else:
            classification = "UNCHANGED"
        result.append(
            {
                "name": name,
                "classification": classification,
                "baseline_sha256": old["sha256"] if old else None,
                "fresh_sha256": new["sha256"] if new else None,
                "baseline_size_bytes": old["size_bytes"] if old else None,
                "fresh_size_bytes": new["size_bytes"] if new else None,
            }
        )
    return result


def classify_revalidation(
    baseline: dict[str, Any],
    baseline_snapshot: dict[str, Any],
    fresh_snapshot: dict[str, Any],
    entries: Sequence[dict[str, Any]],
) -> str:
    expected_corpus = baseline["corpus"]["sha256"]
    structural_ok = (
        fresh_snapshot.get("corpus_sha256") == expected_corpus
        and fresh_snapshot.get("physical_file_count") == baseline["corpus"]["physical_file_count"]
        and fresh_snapshot.get("logical_document_count") == baseline["corpus"]["logical_document_count"]
        and fresh_snapshot.get("document_set_status") == "complete"
        and fresh_snapshot.get("analysis_allowed") is True
        and set(fresh_snapshot.get("logical_groups") or ()) == _REQUIRED_LOGICAL_GROUPS
        and bool(entries)
        and all(item.get("classification") == "UNCHANGED" for item in entries)
    )
    if not structural_ok:
        return DRIFT_BLOCKING
    retrieval_changed = (
        baseline_snapshot.get("retrieved_at") != fresh_snapshot.get("retrieved_at")
        or baseline_snapshot.get("retrieval_ref_sha256")
        != fresh_snapshot.get("retrieval_ref_sha256")
    )
    return DRIFT_ACCEPTABLE if retrieval_changed else DRIFT_UNCHANGED


def build_revalidation_manifest(
    baseline: dict[str, Any],
    baseline_snapshot: dict[str, Any],
    fresh_snapshot: dict[str, Any],
) -> dict[str, Any]:
    entries = compare_material_snapshots(baseline_snapshot, fresh_snapshot)
    drift = classify_revalidation(baseline, baseline_snapshot, fresh_snapshot, entries)
    status = REVALIDATION_BLOCKED if drift == DRIFT_BLOCKING else REVALIDATION_PASS
    body: dict[str, Any] = {
        "schema_version": P8_05_SCHEMA_VERSION,
        "purpose": P8_05_PURPOSE,
        "status": status,
        "drift_classification": drift,
        "authorization_eligible": drift in {DRIFT_UNCHANGED, DRIFT_ACCEPTABLE},
        "baseline_id": baseline["baseline_id"],
        "baseline_generation": baseline["baseline_generation"],
        "baseline_descriptor_sha256": canonical_sha256(baseline),
        "registry_number": baseline["registry_number"],
        "baseline_corpus_sha256": baseline["corpus"]["sha256"],
        "fresh_corpus_sha256": fresh_snapshot.get("corpus_sha256"),
        "baseline_physical_file_count": baseline_snapshot.get("physical_file_count"),
        "fresh_physical_file_count": fresh_snapshot.get("physical_file_count"),
        "baseline_logical_document_count": baseline_snapshot.get("logical_document_count"),
        "fresh_logical_document_count": fresh_snapshot.get("logical_document_count"),
        "baseline_retrieved_at": baseline_snapshot.get("retrieved_at"),
        "fresh_retrieved_at": fresh_snapshot.get("retrieved_at"),
        "baseline_retrieval_ref_sha256": baseline_snapshot.get("retrieval_ref_sha256"),
        "fresh_retrieval_ref_sha256": fresh_snapshot.get("retrieval_ref_sha256"),
        "comparison_entries": list(entries),
        "evidence_completeness": "complete",
        "external_actions": False,
        "provider_generation_calls_before_authorization": 0,
    }
    digest = canonical_sha256(body)
    return {
        **body,
        "manifest_sha256": digest,
        "manifest_integrity_ref": f"sha256:{digest}",
    }


def build_authorization_manifest(
    baseline: dict[str, Any],
    revalidation: dict[str, Any],
    *,
    expected_head: str,
) -> dict[str, Any]:
    head = str(expected_head or "").strip().lower()
    if not _GIT_SHA.fullmatch(head):
        raise P805AcceptanceBindingBlocked("BLOCKED_EXPECTED_HEAD_INVALID")
    if (
        revalidation.get("schema_version") != P8_05_SCHEMA_VERSION
        or revalidation.get("status") != REVALIDATION_PASS
        or revalidation.get("authorization_eligible") is not True
        or revalidation.get("drift_classification") not in {DRIFT_UNCHANGED, DRIFT_ACCEPTABLE}
        or revalidation.get("registry_number") != baseline["registry_number"]
        or revalidation.get("baseline_corpus_sha256") != baseline["corpus"]["sha256"]
        or revalidation.get("fresh_corpus_sha256") != baseline["corpus"]["sha256"]
        or revalidation.get("baseline_descriptor_sha256") != canonical_sha256(baseline)
        or revalidation.get("manifest_sha256") != canonical_sha256(
            {
                key: value
                for key, value in revalidation.items()
                if key not in ("manifest_sha256", "manifest_integrity_ref")
            }
        )
    ):
        raise P805AcceptanceBindingBlocked("BLOCKED_REVALIDATION_NOT_AUTHORIZABLE")

    body: dict[str, Any] = {
        "schema_version": P8_05_AUTH_SCHEMA_VERSION,
        "status": AUTHORIZED_STATUS,
        "authorization_scope": "one-local-complete-corpus-generation",
        "generation_run_limit": 1,
        "expected_head": head,
        "baseline_id": baseline["baseline_id"],
        "baseline_descriptor_sha256": canonical_sha256(baseline),
        "registry_number": baseline["registry_number"],
        "corpus_sha256": baseline["corpus"]["sha256"],
        "policy_sha256": baseline["policy"]["sha256"],
        "revalidation_manifest_sha256": revalidation["manifest_sha256"],
        "revalidation_drift_classification": revalidation["drift_classification"],
        "provider_execution_authorized": True,
        "procurement_submission_authorized": False,
        "email_authorized": False,
        "digital_signature_authorized": False,
        "external_actions": False,
    }
    digest = canonical_sha256(body)
    return {
        **body,
        "manifest_sha256": digest,
        "manifest_integrity_ref": f"sha256:{digest}",
    }


def validate_authorization_manifest(
    authorization: dict[str, Any],
    *,
    expected_head: str,
) -> None:
    if not isinstance(authorization, dict):
        raise P805AcceptanceBindingBlocked("BLOCKED_AUTHORIZATION_INVALID")
    body = {
        key: value
        for key, value in authorization.items()
        if key not in ("manifest_sha256", "manifest_integrity_ref")
    }
    expected_digest = canonical_sha256(body)
    if (
        authorization.get("schema_version") != P8_05_AUTH_SCHEMA_VERSION
        or authorization.get("status") != AUTHORIZED_STATUS
        or authorization.get("generation_run_limit") != 1
        or authorization.get("expected_head") != expected_head
        or authorization.get("provider_execution_authorized") is not True
        or authorization.get("procurement_submission_authorized") is not False
        or authorization.get("email_authorized") is not False
        or authorization.get("digital_signature_authorized") is not False
        or authorization.get("external_actions") is not False
        or authorization.get("manifest_sha256") != expected_digest
    ):
        raise P805AcceptanceBindingBlocked("BLOCKED_AUTHORIZATION_INVALID")


def execute_authorized_once(
    authorization: dict[str, Any],
    command: Sequence[str],
    *,
    expected_head: str,
    env: dict[str, str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Consume one P8.05 authorization by starting exactly one acceptance subprocess."""

    validate_authorization_manifest(authorization, expected_head=expected_head)
    if not command:
        raise P805AcceptanceBindingBlocked("BLOCKED_ACCEPTANCE_COMMAND_MISSING")
    return runner(
        list(command),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def safe_child_failure(stderr: str) -> str:
    value = stderr.strip().splitlines()[-1].strip() if stderr.strip() else "acceptance_failed"
    return value if _SAFE_FAILURE.fullmatch(value) else "acceptance_failed"


def write_manifest(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
