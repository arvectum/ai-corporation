from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalysisStatus(StrEnum):
    SUCCESS = "success"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    BUDGET_EXCEEDED = "budget_exceeded"
    INVALID_RESPONSE = "invalid_response"
    VALIDATION_FAILED = "validation_failed"


class SupportStatus(StrEnum):
    SUPPORTED = "supported"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REJECTED = "rejected"


class ConfidenceBasis(StrEnum):
    DIRECT_EXACT_EVIDENCE = "direct_exact_evidence"
    MULTIPLE_EXACT_EVIDENCE = "multiple_exact_evidence"
    INCOMPLETE_EVIDENCE = "incomplete_evidence"
    CONFLICTING_EVIDENCE = "conflicting_evidence"
    PROVIDER_ONLY_ASSERTION = "provider_only_assertion"
    PROHIBITED_DECISION = "prohibited_decision"


class BudgetStatus(StrEnum):
    WITHIN_BUDGET = "within_budget"
    EXCEEDED = "exceeded"
    UNKNOWN_USAGE = "unknown_usage"


class EvidenceFragmentInput(ContractModel):
    document_id: str = Field(min_length=1)
    document_name: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    locator: dict[str, Any] = Field(default_factory=dict)
    text: str = Field(min_length=1)


class EvidenceFragment(ContractModel):
    fragment_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_id: str = Field(min_length=1)
    document_name: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    locator: dict[str, Any] = Field(default_factory=dict)
    text: str = Field(min_length=1)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DataHandlingReport(ContractModel):
    mode: str = "allowlisted_redacted"
    redaction_applied: bool = False
    redaction_count: int = Field(default=0, ge=0)
    input_chars_before: int = Field(default=0, ge=0)
    input_chars_after: int = Field(default=0, ge=0)
    selected_fields: list[str] = Field(default_factory=list)


class EvidencePacket(ContractModel):
    customer_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    procurement_case_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    registry_number: str = Field(min_length=1)
    fragments: list[EvidenceFragment] = Field(min_length=1)
    data_handling: DataHandlingReport
    packet_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvidenceReference(ContractModel):
    procurement_case_id: str = Field(min_length=1)
    registry_number: str = Field(min_length=1)
    fragment_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_id: str = Field(min_length=1)
    document_name: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    locator: dict[str, Any] = Field(default_factory=dict)
    quote: str = Field(min_length=1)
    quote_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompactWireEvidenceFragment(ContractModel):
    fragment_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    document_order: int = Field(ge=0)
    chunk_index: int = Field(ge=0)
    text: str = Field(min_length=1)


class CompactWireEvidenceReference(ContractModel):
    fragment_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    quote: str = Field(min_length=1)


class CompactWireProviderClaim(ContractModel):
    claim_id: str = Field(min_length=1)
    field_path: str = Field(min_length=1)
    value: Any
    provider_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_references: list[CompactWireEvidenceReference] = Field(default_factory=list)


class CompactWireProviderResponse(ContractModel):
    claims: list[CompactWireProviderClaim] = Field(default_factory=list)


class ProviderClaim(ContractModel):
    claim_id: str = Field(min_length=1)
    field_path: str = Field(min_length=1)
    value: Any
    provider_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_references: list[EvidenceReference] = Field(default_factory=list)


class GroundedClaim(ContractModel):
    claim_id: str = Field(min_length=1)
    field_path: str = Field(min_length=1)
    value: Any
    support_status: SupportStatus
    evidence_references: list[EvidenceReference] = Field(default_factory=list)
    provider_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    validated_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_basis: ConfidenceBasis
    validation_errors: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ProviderPricing(ContractModel):
    input_cost_per_1k_tokens: float = Field(ge=0.0)
    output_cost_per_1k_tokens: float = Field(ge=0.0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    pricing_table_version: str = Field(min_length=1)


class BudgetLimits(ContractModel):
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    timeout_ms: int = Field(gt=0)
    max_retries: int = Field(default=0, ge=0, le=5)
    max_total_latency_ms: int = Field(gt=0)
    max_estimated_cost: float = Field(gt=0.0)
    chars_per_token_estimate: int = Field(default=4, gt=0)


class BudgetPolicy(ContractModel):
    limits: BudgetLimits
    pricing: ProviderPricing


class BudgetEvaluation(ContractModel):
    status: BudgetStatus
    estimated_input_tokens: int = Field(default=0, ge=0)
    estimated_output_tokens: int = Field(default=0, ge=0)
    actual_input_tokens: int | None = Field(default=None, ge=0)
    actual_output_tokens: int | None = Field(default=None, ge=0)
    estimated_cost: float | None = Field(default=None, ge=0.0)
    actual_or_reconciled_cost: float | None = Field(default=None, ge=0.0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    pricing_table_version: str = Field(min_length=1)
    total_latency_ms: int | None = Field(default=None, ge=0)
    reasons: list[str] = Field(default_factory=list)


class ProductionLLMAnalysisRequest(ContractModel):
    request_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    customer_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    procurement_case_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    registry_number: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    provider_wire_contract_version: str = Field(default="full-v1", min_length=1)
    model: str = Field(min_length=1)
    prompt_id: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    output_schema_id: str = Field(min_length=1)
    output_schema_version: str = Field(min_length=1)
    grounding_policy_version: str = Field(min_length=1)
    temperature: float = Field(default=0.0, ge=0.0, le=0.0)
    evidence_packet: EvidencePacket
    budget_policy: BudgetPolicy
    batch_plan_version: str | None = None
    batch_plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    batch_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    batch_ordinal: int | None = Field(default=None, ge=1)
    batch_count: int | None = Field(default=None, ge=1)
    corpus_evidence_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    map_mode: bool = False
    max_claims: int | None = Field(default=None, ge=1)
    allowed_field_paths: list[str] = Field(default_factory=list)
    context_profile: str | None = None
    tokenizer_identity: str | None = None
    evidence_budget: int | None = Field(default=None, ge=1)
    chat_template_overhead: int | None = Field(default=None, ge=1)
    execution_deadline_ms: int | None = Field(default=None, ge=1)


class ProviderAnalysisResponse(ContractModel):
    provider_request_id: str | None = None
    claims: list[ProviderClaim] = Field(default_factory=list)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    attempt_latencies_ms: list[int] = Field(default_factory=list)
    total_latency_ms: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    raw_response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ProductionLLMAnalysisResult(ContractModel):
    status: AnalysisStatus
    analysis_state: str = "needs_review"
    canonical_input_eligible: bool = False
    request_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: str
    provider_wire_contract_version: str = "full-v1"
    model: str
    provider_request_id: str | None = None
    prompt_id: str
    prompt_version: str
    output_schema_id: str
    output_schema_version: str
    grounding_policy_version: str
    evidence_packet_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_claims: list[GroundedClaim] = Field(default_factory=list)
    rejected_claims: list[GroundedClaim] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    budget: BudgetEvaluation
    retry_count: int = Field(default=0, ge=0)
    sanitized_error_code: str | None = None
    raw_response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    validated_result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_plan_version: str | None = None
    batch_plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    batch_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    batch_ordinal: int | None = Field(default=None, ge=1)
    batch_count: int | None = Field(default=None, ge=1)
    corpus_evidence_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    map_empty: bool = False
    batch_hashes: list[str] = Field(default_factory=list)
    batch_result_hashes: list[str] = Field(default_factory=list)
    provider_call_count: int = Field(default=0, ge=0)
    empty_batch_count: int = Field(default=0, ge=0)
    provider_request_ids: list[str] = Field(default_factory=list)
    final_request_body_hashes: list[str] = Field(default_factory=list)
    final_projected_request_tokens: list[int] = Field(default_factory=list)
    tokenizer_identity: str | None = None
    context_profile: str | None = None
    evidence_budget: int | None = Field(default=None, ge=1)
    chat_template_overhead: int | None = Field(default=None, ge=1)
    execution_deadline_ms: int | None = Field(default=None, ge=1)
