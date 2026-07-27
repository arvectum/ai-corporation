from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from src.modules.electrical_ontology_shadow.assets import (
    ShadowAssetCompatibilityError,
    ShadowOntologySnapshot,
    load_shadow_snapshot,
)
from src.modules.electrical_ontology_shadow.audit import (
    ShadowAuditStore,
    bounded_redacted_text,
    canonical_json_hash,
    tenant_partition,
    validate_identifier,
)
from src.shared.config.settings import Settings, get_settings

_STATUS_DISABLED = "DISABLED"
_STATUS_BLOCKED = "BLOCKED"
_STATUS_COMPLETED = "SHADOW_COMPLETED"
_STATUS_SAFE_FAILURE = "SAFE_FAILURE"


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _demo_runs_root(repository_root: Path) -> Path:
    configured = os.environ.get("AI_CORP_TENDER_OPERATOR_DEMO_RUNS_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return repository_root / "company_agent_runs" / "tender_operator_demo"


def _audit_root(settings: Settings, repository_root: Path) -> Path:
    if settings.electrical_ontology_shadow_audit_root:
        return Path(settings.electrical_ontology_shadow_audit_root).expanduser().resolve()
    return repository_root / "company_agent_runs" / "electrical_ontology_shadow"


def _allowed_profiles(settings: Settings) -> frozenset[str]:
    return frozenset(
        item.strip()
        for item in settings.electrical_ontology_shadow_allowed_profiles.split(",")
        if item.strip()
    )


def _safe_summary(payload: dict[str, Any]) -> dict[str, Any]:
    gate = payload.get("gate") or {}
    metrics = payload.get("metrics") or {}
    versions = payload.get("versions") or {}
    return {
        "status": payload.get("status"),
        "lifecycle_stage": payload.get("lifecycle_stage"),
        "reason_codes": list(payload.get("reason_codes") or []),
        "candidate_count": int(metrics.get("candidate_count") or 0),
        "disagreement_count": int(metrics.get("disagreement_count") or 0),
        "uncertain_count": int(metrics.get("uncertain_count") or 0),
        "error_count": int(metrics.get("error_count") or 0),
        "latency_ms": float(metrics.get("latency_ms") or 0.0),
        "snapshot_root_hash": versions.get("snapshot_root_hash"),
        "release_gate_passed": bool(gate.get("release_gate_passed")),
        "independent_acceptance_complete": bool(
            gate.get("independent_acceptance_complete")
        ),
        "operator_approval_present": bool(gate.get("operator_approval_present")),
        "production_effect": False,
        "external_actions": False,
        "requires_review": True,
    }


def _base_audit(
    *,
    run_id: str,
    tenant_id: str,
    status: str,
    lifecycle_stage: str,
    reason_codes: list[str],
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    return {
        "audit_schema_version": "1.0.0",
        "audit_id": f"ARV-067I:{validate_identifier(run_id, label='run id')}:{now}",
        "created_at": now,
        "run_id": run_id,
        "tenant_partition": tenant_partition(tenant_id),
        "status": status,
        "lifecycle_stage": lifecycle_stage,
        "reason_codes": sorted(set(reason_codes)),
        "versions": {},
        "gate": {},
        "input": {},
        "results": [],
        "comparison": {
            "status": "not_comparable",
            "disagreement_item_ids": [],
            "operator_review_required": True,
        },
        "metrics": {
            "candidate_count": 0,
            "disagreement_count": 0,
            "uncertain_count": 0,
            "error_count": 0,
            "latency_ms": 0.0,
            "cost_units": 0,
        },
        "safety": {
            "primary_result_mutated": False,
            "pdf_or_export_mutated": False,
            "go_no_go_mutated": False,
            "external_actions": False,
            "platform_submission": False,
            "email_sending": False,
            "digital_signature": False,
            "production_promotion_allowed": False,
            "human_review_required": True,
        },
        "error": None,
    }


def _normalize(value: str) -> str:
    return " ".join(value.lower().replace("ё", "е").split())


def _contains_term(text: str, term: str) -> bool:
    normalized_term = _normalize(term)
    if not normalized_term:
        return False
    if len(normalized_term) <= 3:
        return bool(re.search(rf"(?<!\w){re.escape(normalized_term)}(?!\w)", text))
    return normalized_term in text


def _profile_score(profile: dict[str, Any], normalized_text: str) -> int:
    terms = list(profile.get("aliases") or []) + list(profile.get("canonical_marks") or [])
    score = 0
    for term in terms:
        if _contains_term(normalized_text, str(term)):
            score += 3 if str(term) in (profile.get("aliases") or []) else 1
    return score


def _iter_item_like(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        item_name = payload.get("display_name") or payload.get("official_name") or payload.get("name")
        if item_name and any(
            key in payload
            for key in (
                "attributes",
                "characteristics",
                "raw_fragment",
                "requested_attributes",
                "candidate_attributes",
            )
        ):
            yield payload
        for value in payload.values():
            yield from _iter_item_like(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _iter_item_like(value)


def _item_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("display_name", "official_name", "name", "raw_fragment"):
        value = item.get(key)
        if value:
            parts.append(str(value))
    characteristics = item.get("characteristics")
    if isinstance(characteristics, list):
        parts.extend(str(value) for value in characteristics)
    return " | ".join(parts)


def _select_profile(
    item_text: str,
    profiles: dict[str, dict[str, Any]],
    eligible_profile_ids: frozenset[str],
) -> dict[str, Any] | None:
    normalized_text = _normalize(item_text)
    ranked = sorted(
        (
            (_profile_score(profile, normalized_text), profile_id, profile)
            for profile_id, profile in profiles.items()
            if profile_id in eligible_profile_ids
        ),
        key=lambda row: (-row[0], row[1]),
    )
    if not ranked or ranked[0][0] <= 0:
        return None
    return ranked[0][2]


def _canonical_category_ids(payload: Any) -> frozenset[str]:
    values: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in {"category_id", "canonical_category_id", "target_category_id"}:
                if isinstance(value, str) and value.startswith("electrical."):
                    values.add(value)
            else:
                values.update(_canonical_category_ids(value))
    elif isinstance(payload, list):
        for value in payload:
            values.update(_canonical_category_ids(value))
    return frozenset(values)


def _build_candidates(
    *,
    snapshot: ShadowOntologySnapshot,
    eligible_profile_ids: frozenset[str],
    source_text: str,
    structured_payload: Any,
    max_items: int,
    snippet_limit: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_profiles: set[str] = set()

    for index, item in enumerate(_iter_item_like(structured_payload), start=1):
        if len(candidates) >= max_items:
            break
        raw_item_text = _item_text(item)
        profile = _select_profile(raw_item_text, snapshot.profiles, eligible_profile_ids)
        if profile is None:
            continue
        profile_id = str(profile["id"])
        requested = item.get("requested_attributes") or item.get("attributes") or {}
        candidate = item.get("candidate_attributes") or {}
        evidence_confirmed = bool(item.get("evidence_confirmed"))
        snippet, truncated = bounded_redacted_text(raw_item_text, snippet_limit)
        candidates.append(
            {
                "item_id": str(item.get("item_id") or item.get("source_record_id") or f"item-{index}"),
                "profile": profile,
                "requested_attributes": requested if isinstance(requested, dict) else {},
                "candidate_attributes": candidate if isinstance(candidate, dict) else {},
                "evidence_confirmed": evidence_confirmed,
                "source_snippet": snippet,
                "source_snippet_truncated": truncated,
                "source_kind": "structured_primary_output",
            }
        )
        seen_profiles.add(profile_id)

    normalized_source = _normalize(source_text)
    for profile_id in sorted(eligible_profile_ids):
        if len(candidates) >= max_items:
            break
        if profile_id in seen_profiles:
            continue
        profile = snapshot.profiles[profile_id]
        if _profile_score(profile, normalized_source) <= 0:
            continue
        title = str(profile.get("title_ru") or profile_id)
        snippet, truncated = bounded_redacted_text(title, snippet_limit)
        candidates.append(
            {
                "item_id": f"source-{profile_id}",
                "profile": profile,
                "requested_attributes": {},
                "candidate_attributes": {},
                "evidence_confirmed": False,
                "source_snippet": snippet,
                "source_snippet_truncated": truncated,
                "source_kind": "bounded_source_scan",
            }
        )
    return candidates


def _evaluate_candidate(
    snapshot: ShadowOntologySnapshot,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    profile = candidate["profile"]
    requested = candidate["requested_attributes"]
    offered = candidate["candidate_attributes"]
    if not requested or not offered:
        evaluation = {
            "profile_id": profile["id"],
            "status": "UNCERTAIN",
            "reason_codes": [
                "HUMAN_REVIEW_REQUIRED",
                "SHADOW_CANDIDATE_PRODUCT_NOT_PROVIDED",
                "SHADOW_STRUCTURED_COMPARISON_NOT_AVAILABLE",
            ],
            "requires_review": True,
            "critical_mismatch_attributes": [],
            "critical_missing_attributes": [],
            "required_issue_attributes": [],
            "optional_issue_attributes": [],
        }
    else:
        evaluation = snapshot.matcher.evaluate_profile(
            profile,
            requested,
            offered,
            evidence_confirmed=bool(candidate["evidence_confirmed"]),
        )
    return {
        "item_id": candidate["item_id"],
        "profile_id": profile["id"],
        "target_category_id": profile["target_category_id"],
        "status": evaluation["status"],
        "reason_codes": sorted(set(evaluation["reason_codes"])),
        "requires_review": True,
        "critical_mismatch_attributes": evaluation.get(
            "critical_mismatch_attributes", []
        ),
        "critical_missing_attributes": evaluation.get(
            "critical_missing_attributes", []
        ),
        "required_issue_attributes": evaluation.get("required_issue_attributes", []),
        "optional_issue_attributes": evaluation.get("optional_issue_attributes", []),
        "source_kind": candidate["source_kind"],
        "source_snippet": candidate["source_snippet"],
        "source_snippet_truncated": candidate["source_snippet_truncated"],
        "evidence_confirmed": bool(candidate["evidence_confirmed"]),
    }


def _persist_enabled_audit(
    *,
    audit: dict[str, Any],
    settings: Settings,
    repository_root: Path,
    tenant_id: str,
    run_id: str,
    audit_root: Path | None,
) -> Path:
    store = ShadowAuditStore(
        audit_root or _audit_root(settings, repository_root),
        max_payload_bytes=settings.electrical_ontology_shadow_max_audit_bytes,
    )
    return store.save(tenant_id=tenant_id, run_id=run_id, payload=audit)


def run_shadow_payload_safely(
    *,
    run_id: str,
    tenant_id: str,
    source_text: str,
    primary_result: Any,
    structured_payload: Any | None = None,
    settings: Settings | None = None,
    repository_root: Path | None = None,
    audit_root: Path | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    repository_root = (repository_root or _repository_root()).resolve()
    try:
        validate_identifier(run_id, label="run id")
        tenant_partition(tenant_id)
    except ValueError:
        return {
            "status": _STATUS_SAFE_FAILURE,
            "lifecycle_stage": "failed_safe",
            "reason_codes": ["SHADOW_INVALID_ISOLATION_IDENTIFIER"],
            "candidate_count": 0,
            "disagreement_count": 0,
            "uncertain_count": 0,
            "error_count": 1,
            "latency_ms": 0.0,
            "snapshot_root_hash": None,
            "release_gate_passed": False,
            "independent_acceptance_complete": False,
            "operator_approval_present": False,
            "production_effect": False,
            "external_actions": False,
            "requires_review": True,
        }
    started = time.perf_counter()
    primary_before_hash = canonical_json_hash(primary_result)

    if not settings.electrical_ontology_shadow_enabled:
        audit = _base_audit(
            run_id=run_id,
            tenant_id=tenant_id,
            status=_STATUS_DISABLED,
            lifecycle_stage="disabled",
            reason_codes=["SHADOW_FEATURE_FLAG_DISABLED"],
        )
        audit["metrics"]["latency_ms"] = round(
            (time.perf_counter() - started) * 1000,
            3,
        )
        return _safe_summary(audit)

    audit = _base_audit(
        run_id=run_id,
        tenant_id=tenant_id,
        status=_STATUS_BLOCKED,
        lifecycle_stage="blocked",
        reason_codes=[],
    )

    try:
        if settings.electrical_ontology_shadow_kill_switch:
            audit["reason_codes"] = ["SHADOW_KILL_SWITCH_ACTIVE"]
            audit["gate"] = {
                "release_gate_passed": False,
                "independent_acceptance_complete": False,
                "operator_approval_present": False,
                "allowlist_present": False,
            }
        else:
            policy_path = (
                Path(settings.electrical_ontology_shadow_policy_path).expanduser().resolve()
                if settings.electrical_ontology_shadow_policy_path
                else None
            )
            snapshot = load_shadow_snapshot(
                repository_root=repository_root,
                policy_path=policy_path,
            )
            allowed = _allowed_profiles(settings)
            eligible = frozenset(
                snapshot.accepted_profile_ids.intersection(allowed)
            )
            operator_approval_present = bool(
                settings.electrical_ontology_shadow_approval_id
            )
            audit["versions"] = {
                "policy_id": snapshot.policy_id,
                "policy_version": snapshot.policy_version,
                "ontology_registry_id": snapshot.registry_id,
                "ontology_version": snapshot.registry_version,
                "benchmark_id": snapshot.benchmark_id,
                "benchmark_version": snapshot.benchmark_version,
                "snapshot_root_hash": snapshot.snapshot_root_hash,
                "source_hashes": snapshot.source_hashes,
            }
            audit["gate"] = {
                "release_gate_passed": snapshot.release_gate_passed,
                "independent_acceptance_complete": (
                    snapshot.independent_acceptance_complete
                ),
                "operator_approval_present": operator_approval_present,
                "allowlist_present": bool(allowed),
                "allowed_profile_ids": sorted(allowed),
                "accepted_profile_ids": sorted(snapshot.accepted_profile_ids),
                "eligible_profile_ids": sorted(eligible),
            }
            gate_reasons: list[str] = []
            if not snapshot.release_gate_passed:
                gate_reasons.append("ARV067H_RELEASE_GATE_NOT_PASSED")
            if not snapshot.independent_acceptance_complete:
                gate_reasons.append("ARV067H_INDEPENDENT_ACCEPTANCE_INCOMPLETE")
            if not operator_approval_present:
                gate_reasons.append("SHADOW_OPERATOR_APPROVAL_MISSING")
            if not allowed:
                gate_reasons.append("SHADOW_PROFILE_ALLOWLIST_EMPTY")
            if allowed.difference(snapshot.profiles):
                gate_reasons.append("SHADOW_ALLOWLIST_UNKNOWN_PROFILE")
            if not eligible:
                gate_reasons.append("SHADOW_NO_ELIGIBLE_PROFILES")

            if gate_reasons:
                audit["reason_codes"] = sorted(set(gate_reasons))
            else:
                bounded_source, source_truncated = bounded_redacted_text(
                    source_text,
                    settings.electrical_ontology_shadow_max_source_chars,
                )
                payload_for_items = (
                    structured_payload
                    if structured_payload is not None
                    else primary_result
                )
                candidates = _build_candidates(
                    snapshot=snapshot,
                    eligible_profile_ids=eligible,
                    source_text=bounded_source,
                    structured_payload=payload_for_items,
                    max_items=settings.electrical_ontology_shadow_max_items,
                    snippet_limit=min(
                        600,
                        settings.electrical_ontology_shadow_max_source_chars,
                    ),
                )
                results = [
                    _evaluate_candidate(snapshot, candidate)
                    for candidate in candidates
                ]
                canonical_primary_categories = _canonical_category_ids(primary_result)
                disagreements = [
                    result["item_id"]
                    for result in results
                    if canonical_primary_categories
                    and result["target_category_id"]
                    not in canonical_primary_categories
                ]
                outcomes = Counter(result["status"] for result in results)
                audit["status"] = _STATUS_COMPLETED
                audit["lifecycle_stage"] = "shadow_runtime"
                audit["reason_codes"] = [
                    "SHADOW_RESULT_REVIEW_REQUIRED",
                    "SHADOW_NO_PRODUCTION_EFFECT",
                ]
                audit["input"] = {
                    "source_sha256": hashlib.sha256(
                        source_text.encode("utf-8", errors="ignore")
                    ).hexdigest(),
                    "bounded_source_sha256": hashlib.sha256(
                        bounded_source.encode("utf-8")
                    ).hexdigest(),
                    "bounded_source_chars": len(bounded_source),
                    "source_truncated": source_truncated,
                    "structured_item_candidates": len(candidates),
                    "raw_source_stored": False,
                }
                audit["results"] = results
                audit["comparison"] = {
                    "status": (
                        "compared"
                        if canonical_primary_categories
                        else "not_comparable"
                    ),
                    "primary_canonical_category_ids": sorted(
                        canonical_primary_categories
                    ),
                    "disagreement_item_ids": disagreements,
                    "operator_review_required": bool(disagreements) or bool(results),
                }
                audit["metrics"].update(
                    {
                        "candidate_count": len(results),
                        "disagreement_count": len(disagreements),
                        "uncertain_count": outcomes.get("UNCERTAIN", 0),
                        "error_count": 0,
                        "outcome_counts": dict(sorted(outcomes.items())),
                        "review_rate": 1.0 if results else 0.0,
                        "disagreement_rate": (
                            len(disagreements) / len(results)
                            if results
                            else 0.0
                        ),
                        "uncertain_rate": (
                            outcomes.get("UNCERTAIN", 0) / len(results)
                            if results
                            else 0.0
                        ),
                    }
                )

        primary_after_hash = canonical_json_hash(primary_result)
        audit["safety"]["primary_result_mutated"] = (
            primary_before_hash != primary_after_hash
        )
        if audit["safety"]["primary_result_mutated"]:
            audit["status"] = _STATUS_SAFE_FAILURE
            audit["lifecycle_stage"] = "failed_safe"
            audit["reason_codes"] = ["SHADOW_PRIMARY_RESULT_MUTATION_DETECTED"]
            audit["metrics"]["error_count"] = 1

    except ShadowAssetCompatibilityError as exc:
        audit["status"] = _STATUS_SAFE_FAILURE
        audit["lifecycle_stage"] = "failed_safe"
        audit["reason_codes"] = ["SHADOW_ASSET_COMPATIBILITY_ERROR"]
        audit["metrics"]["error_count"] = 1
        audit["error"] = {
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        }
    except Exception as exc:  # pragma: no cover - final isolation boundary
        audit["status"] = _STATUS_SAFE_FAILURE
        audit["lifecycle_stage"] = "failed_safe"
        audit["reason_codes"] = ["SHADOW_INTERNAL_ERROR_SAFE_STOP"]
        audit["metrics"]["error_count"] = 1
        audit["error"] = {
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        }

    audit["metrics"]["latency_ms"] = round(
        (time.perf_counter() - started) * 1000,
        3,
    )
    if audit["metrics"]["latency_ms"] > settings.electrical_ontology_shadow_timeout_ms:
        audit["reason_codes"] = sorted(
            set(audit["reason_codes"] + ["SHADOW_LATENCY_BUDGET_EXCEEDED"])
        )
        audit["comparison"]["operator_review_required"] = True

    audit["audit_payload_hash"] = canonical_json_hash(
        {key: value for key, value in audit.items() if key != "audit_payload_hash"}
    )
    try:
        _persist_enabled_audit(
            audit=audit,
            settings=settings,
            repository_root=repository_root,
            tenant_id=tenant_id,
            run_id=run_id,
            audit_root=audit_root,
        )
    except Exception as exc:  # audit failure must not affect the primary flow
        audit["status"] = _STATUS_SAFE_FAILURE
        audit["lifecycle_stage"] = "failed_safe"
        audit["reason_codes"] = sorted(
            set(audit["reason_codes"] + ["SHADOW_AUDIT_PERSISTENCE_FAILED"])
        )
        audit["metrics"]["error_count"] = max(
            1,
            int(audit["metrics"].get("error_count") or 0),
        )
        audit["error"] = {
            "type": type(exc).__name__,
            "message": str(exc)[:500],
        }
    return _safe_summary(audit)


def _read_json_if_present(path: Path) -> Any:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _collect_bounded_run_text(run_dir: Path, max_chars: int) -> str:
    chunks: list[str] = []
    remaining = max_chars
    for directory_name in ("normalized", "output"):
        directory = run_dir / directory_name
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if remaining <= 0:
                break
            if not path.is_file() or path.suffix.lower() not in {
                ".txt",
                ".md",
                ".json",
                ".csv",
                ".xml",
            }:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            chunk = text[:remaining]
            chunks.append(chunk)
            remaining -= len(chunk)
    return "\n\n".join(chunks)


def _update_demo_metadata_summary(run_dir: Path, summary: dict[str, Any]) -> None:
    metadata_path = run_dir / "metadata.json"
    if not metadata_path.is_file():
        return
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        return
    metadata["electrical_ontology_shadow"] = copy.deepcopy(summary)
    temporary = metadata_path.with_suffix(".json.shadow.tmp")
    temporary.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(metadata_path)


def run_shadow_for_saved_demo_run_safely(
    run_id: str,
    *,
    settings: Settings | None = None,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    try:
        settings = settings or get_settings()
        repository_root = (repository_root or _repository_root()).resolve()
        validate_identifier(run_id, label="run id")
        run_dir = _demo_runs_root(repository_root) / run_id
        tenant_id = "demo-local"
        metadata = _read_json_if_present(run_dir / "metadata.json") or {}
        if isinstance(metadata, dict) and metadata.get("tenant_id"):
            tenant_id = str(metadata["tenant_id"])

        primary_result = {
            "metadata": metadata,
            "requirements": _read_json_if_present(
                run_dir / "output" / "requirements.json"
            ),
            "final_recommendation": _read_json_if_present(
                run_dir / "output" / "final_recommendation.json"
            ),
            "trace": _read_json_if_present(run_dir / "output" / "trace.json"),
        }
        source_text = _collect_bounded_run_text(
            run_dir,
            settings.electrical_ontology_shadow_max_source_chars,
        )
        summary = run_shadow_payload_safely(
            run_id=run_id,
            tenant_id=tenant_id,
            source_text=source_text,
            primary_result=primary_result,
            structured_payload=primary_result.get("requirements"),
            settings=settings,
            repository_root=repository_root,
        )
        try:
            _update_demo_metadata_summary(run_dir, summary)
        except Exception:
            return {
                **summary,
                "status": _STATUS_SAFE_FAILURE,
                "reason_codes": sorted(
                    set(
                        summary.get("reason_codes", [])
                        + ["SHADOW_METADATA_SUMMARY_FAILED"]
                    )
                ),
                "production_effect": False,
                "external_actions": False,
                "requires_review": True,
            }
        return summary
    except Exception:
        return {
            "status": _STATUS_SAFE_FAILURE,
            "lifecycle_stage": "failed_safe",
            "reason_codes": ["SHADOW_BACKGROUND_ISOLATION_BOUNDARY"],
            "candidate_count": 0,
            "disagreement_count": 0,
            "uncertain_count": 0,
            "error_count": 1,
            "latency_ms": 0.0,
            "snapshot_root_hash": None,
            "release_gate_passed": False,
            "independent_acceptance_complete": False,
            "operator_approval_present": False,
            "production_effect": False,
            "external_actions": False,
            "requires_review": True,
        }


def get_shadow_summary_for_saved_demo_run(
    run_id: str,
    *,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    repository_root = (repository_root or _repository_root()).resolve()
    validate_identifier(run_id, label="run id")
    metadata_path = _demo_runs_root(repository_root) / run_id / "metadata.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(metadata_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    summary = metadata.get("electrical_ontology_shadow")
    if not isinstance(summary, dict):
        raise FileNotFoundError("shadow summary is not available")
    return copy.deepcopy(summary)
