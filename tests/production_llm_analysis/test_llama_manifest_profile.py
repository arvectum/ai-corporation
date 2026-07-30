from src.modules.production_llm_analysis.evidence import canonical_sha256
from src.modules.production_llm_analysis.llama_manifest_profile import (
    _decorate_manifest,
)
from src.modules.production_llm_analysis.llama_schema_constraint import (
    _LLAMA_SCHEMA_PROFILE,
)


def test_llama_manifest_profile_is_sanitized_stable_and_rehashed():
    original = {
        "manifest_version": "test-v1",
        "stable_identity": {"provider": "openai_compatible"},
        "wire_contract": {
            "provider_wire_contract_version": "compact-safe-v1",
            "server_side_reference_expansion": True,
        },
        "safety": {"raw_provider_body_recorded": False},
        "manifest_hash": "stale",
    }

    decorated = _decorate_manifest(original)

    assert decorated["stable_identity"]["llama_schema_profile"] == (
        _LLAMA_SCHEMA_PROFILE
    )
    wire = decorated["wire_contract"]
    assert wire["llama_schema_profile"] == _LLAMA_SCHEMA_PROFILE
    assert wire["provider_claim_id_authority"] is False
    assert wire["provider_claim_value_authority"] is False
    assert wire["server_side_claim_identity"] is True
    assert wire["server_side_claim_value_expansion"] is True
    assert wire["server_side_quote_expansion"] is True
    assert decorated["safety"] == original["safety"]
    payload = {key: value for key, value in decorated.items() if key != "manifest_hash"}
    assert decorated["manifest_hash"] == canonical_sha256(payload)
    assert "stale" not in str(decorated)
