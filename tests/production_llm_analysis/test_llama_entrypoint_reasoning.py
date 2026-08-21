from pathlib import Path


def test_controlled_llama_entry_point_installs_schema_then_non_reasoning():
    source = Path(
        "scripts/r10_1/run_controlled_provider_evidence_llama_schema.py"
    ).read_text(encoding="utf-8")

    schema_call = source.index("install_llama_schema_constraint()")
    reasoning_call = source.index("install_llama_non_reasoning_mode()")
    manifest_call = source.index("install_llama_manifest_profile()")

    assert schema_call < reasoning_call < manifest_call


def test_canonical_controlled_runner_installs_non_reasoning_before_transport():
    source = Path("scripts/r10_1/run_controlled_provider_evidence.py").read_text(
        encoding="utf-8"
    )

    schema_call = source.index("install_llama_schema_constraint()")
    reasoning_call = source.index("install_llama_non_reasoning_mode()")
    policy_load = source.index("policy = load_approved_provider_policy")
    provider_factory = source.index("def provider_factory")

    assert schema_call < reasoning_call < policy_load < provider_factory
