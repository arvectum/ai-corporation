from __future__ import annotations

from typing import Any

from src.modules.production_llm_analysis import controlled_evidence
from src.modules.production_llm_analysis.evidence import canonical_sha256
from src.modules.production_llm_analysis.llama_schema_constraint import (
    _LLAMA_SCHEMA_PROFILE,
)

_ORIGINAL_BUILD_MANIFEST = (
    controlled_evidence.build_sanitized_controlled_evidence_manifest
)
_PATCH_MARKER = "_arv003_llama_manifest_profile_v1"


def _decorate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Add the process-local wire profile without exposing customer data."""

    payload = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    stable_identity = dict(payload.get("stable_identity") or {})
    stable_identity["llama_schema_profile"] = _LLAMA_SCHEMA_PROFILE
    payload["stable_identity"] = stable_identity

    wire_contract = dict(payload.get("wire_contract") or {})
    wire_contract.update(
        {
            "llama_schema_profile": _LLAMA_SCHEMA_PROFILE,
            "provider_claim_id_authority": False,
            "provider_claim_value_authority": False,
            "server_side_claim_identity": True,
            "server_side_claim_value_expansion": True,
            "server_side_quote_expansion": True,
        }
    )
    payload["wire_contract"] = wire_contract
    return {**payload, "manifest_hash": canonical_sha256(payload)}


def _build_manifest_with_llama_profile(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _decorate_manifest(_ORIGINAL_BUILD_MANIFEST(*args, **kwargs))


setattr(_build_manifest_with_llama_profile, _PATCH_MARKER, True)


def install_llama_manifest_profile() -> None:
    """Install the sanitized manifest decorator for the llama entry point only."""

    current = controlled_evidence.build_sanitized_controlled_evidence_manifest
    if bool(getattr(current, _PATCH_MARKER, False)):
        return
    controlled_evidence.build_sanitized_controlled_evidence_manifest = (
        _build_manifest_with_llama_profile
    )
