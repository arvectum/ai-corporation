from __future__ import annotations

import hashlib
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml


class ShadowAssetCompatibilityError(ValueError):
    """Raised when a version-pinned ontology or benchmark asset is incompatible."""


@dataclass(frozen=True)
class ShadowOntologySnapshot:
    policy_id: str
    policy_version: str
    registry_id: str
    registry_version: str
    benchmark_id: str
    benchmark_version: str
    profiles: dict[str, dict[str, Any]]
    accepted_profile_ids: frozenset[str]
    release_gate_passed: bool
    independent_acceptance_complete: bool
    source_hashes: dict[str, str]
    snapshot_root_hash: str
    matcher: ModuleType


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ShadowAssetCompatibilityError(f"required asset is missing: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ShadowAssetCompatibilityError(f"asset must contain a mapping: {path}")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _root_hash(source_hashes: dict[str, str]) -> str:
    canonical = "\n".join(f"{path}:{digest}" for path, digest in sorted(source_hashes.items()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_matcher(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("arv067i_wave1_matcher", path)
    if spec is None or spec.loader is None:
        raise ShadowAssetCompatibilityError(f"cannot load matcher: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "evaluate_profile", None)):
        raise ShadowAssetCompatibilityError("matcher does not expose evaluate_profile")
    return module


def _require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise ShadowAssetCompatibilityError(
            f"{label} mismatch: expected={expected!r}, actual={actual!r}"
        )


def load_shadow_snapshot(
    *,
    repository_root: Path,
    policy_path: Path | None = None,
) -> ShadowOntologySnapshot:
    repository_root = repository_root.resolve()
    electrical_dir = repository_root / "schemas" / "categories" / "electrical"
    resolved_policy_path = policy_path or electrical_dir / "shadow_runtime_policy.v1.yaml"
    policy = _read_yaml(resolved_policy_path)

    pins = policy.get("pins")
    if not isinstance(pins, dict):
        raise ShadowAssetCompatibilityError("shadow runtime policy has no pins")

    manifest_path = repository_root / str(pins["ontology_manifest_path"])
    release_report_path = repository_root / str(pins["benchmark_release_report_path"])
    acceptance_path = repository_root / str(pins["benchmark_acceptance_path"])
    matcher_path = repository_root / str(pins["matcher_path"])

    manifest = _read_yaml(manifest_path)
    release_report = _read_yaml(release_report_path)
    acceptance = _read_yaml(acceptance_path)

    _require_equal(
        manifest.get("registry_id"),
        pins.get("ontology_registry_id"),
        "ontology registry id",
    )
    _require_equal(
        str(manifest.get("version")),
        str(pins.get("ontology_version")),
        "ontology version",
    )
    _require_equal(
        release_report.get("benchmark_id"),
        pins.get("benchmark_id"),
        "benchmark id",
    )
    _require_equal(
        str(release_report.get("benchmark_version")),
        str(pins.get("benchmark_version")),
        "benchmark version",
    )

    expected_policy_ref = f"{pins['benchmark_id']}@{pins['benchmark_version']}"
    _require_equal(acceptance.get("policy_ref"), expected_policy_ref, "acceptance policy ref")

    profiles: dict[str, dict[str, Any]] = {}
    source_paths = [resolved_policy_path, manifest_path, release_report_path, acceptance_path]
    for relative_path in manifest.get("profile_files", []):
        fragment_path = electrical_dir / str(relative_path)
        fragment = _read_yaml(fragment_path)
        source_paths.append(fragment_path)
        for profile in fragment.get("profiles", []):
            profile_id = str(profile.get("id") or "")
            if not profile_id or profile_id in profiles:
                raise ShadowAssetCompatibilityError(
                    f"invalid or duplicate profile id: {profile_id!r}"
                )
            profiles[profile_id] = profile

    profile_count = int(release_report.get("profile_count") or 0)
    if profile_count != len(profiles):
        raise ShadowAssetCompatibilityError(
            f"profile count mismatch: release={profile_count}, loaded={len(profiles)}"
        )

    accepted_profile_ids = frozenset(
        str(row.get("profile_id"))
        for row in acceptance.get("profiles", [])
        if row.get("acceptance_status") == "accepted"
    )
    unknown_accepted = accepted_profile_ids.difference(profiles)
    if unknown_accepted:
        raise ShadowAssetCompatibilityError(
            f"acceptance references unknown profiles: {sorted(unknown_accepted)}"
        )

    acceptance_summary = acceptance.get("summary") or {}
    report_gates = release_report.get("gates") or {}
    independent_complete = bool(
        acceptance_summary.get("independent_acceptance_complete")
    )
    release_gate_passed = bool(
        release_report.get("status") == "RELEASE_ELIGIBLE"
        and report_gates.get("release_gate_passed") is True
        and report_gates.get("independent_acceptance_passed") is True
        and independent_complete
        and len(accepted_profile_ids) == len(profiles)
    )

    source_paths.append(matcher_path)
    source_hashes = {
        str(path.resolve().relative_to(repository_root)): _sha256(path)
        for path in source_paths
    }

    return ShadowOntologySnapshot(
        policy_id=str(policy["policy_id"]),
        policy_version=str(policy["version"]),
        registry_id=str(manifest["registry_id"]),
        registry_version=str(manifest["version"]),
        benchmark_id=str(release_report["benchmark_id"]),
        benchmark_version=str(release_report["benchmark_version"]),
        profiles=profiles,
        accepted_profile_ids=accepted_profile_ids,
        release_gate_passed=release_gate_passed,
        independent_acceptance_complete=independent_complete,
        source_hashes=source_hashes,
        snapshot_root_hash=_root_hash(source_hashes),
        matcher=_load_matcher(matcher_path),
    )
