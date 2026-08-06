"""Repository-owned, side-effect-free R10.1 controlled-map contract."""
from dataclasses import dataclass

R10_1_PROMPT_ID = "procurement-analysis"
R10_1_BATCH_PROMPT_VERSION = "r10.1-batched-compact-v3"
R10_1_OUTPUT_SCHEMA_ID = "production-llm-analysis"
R10_1_OUTPUT_SCHEMA_VERSION = "v2"
R10_1_GROUNDING_POLICY_VERSION = "grounding-v1"
R10_1_PROVIDER_WIRE_CONTRACT_VERSION = "compact-safe-v2"
R10_1_BATCH_PLAN_VERSION = "arv003-map-plan-v7"
R10_1_COMPACT_INPUT_FRAGMENT_FIELDS = ("fragment_id", "document_order", "chunk_index", "text")
R10_1_COMPACT_OUTPUT_REFERENCE_FIELDS = ("fragment_id", "quote")


@dataclass(frozen=True)
class R10_1MapContract:
    prompt_id: str = R10_1_PROMPT_ID
    prompt_version: str = R10_1_BATCH_PROMPT_VERSION
    output_schema_id: str = R10_1_OUTPUT_SCHEMA_ID
    output_schema_version: str = R10_1_OUTPUT_SCHEMA_VERSION
    grounding_policy_version: str = R10_1_GROUNDING_POLICY_VERSION
    provider_wire_contract_version: str = R10_1_PROVIDER_WIRE_CONTRACT_VERSION
    plan_version: str = R10_1_BATCH_PLAN_VERSION


R10_1_CONTROLLED_MAP_CONTRACT = R10_1MapContract()
