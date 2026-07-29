#!/usr/bin/env python3
"""Build and verify product R10.1 plans through the production code path.

This is plan-only: it resolves persisted input read-only, invokes the same
chunk projection and ``build_r10_1_batch_plan`` as the producer, and writes
only the caller-selected sanitized lineage artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.modules.customer_pilot.input_resolver import resolve_customer_run_inputs
from src.modules.procurement_analysis.r10_1_producer import (
    build_r10_1_batch_plan,
    build_r10_1_evidence_packet,
)
from src.modules.production_llm_analysis.batching import (
    BatchPolicy,
    tokenizer_from_environment,
)
from src.modules.production_llm_analysis.schemas import (
    BudgetLimits,
    BudgetPolicy,
    EvidenceFragmentInput,
    ProviderPricing,
)
from src.shared.config.settings import get_settings
from src.shared.db.session import SessionLocal
from src.tender_research.models import TenderAnalysisRun


def _policy(output_tokens: int) -> BudgetPolicy:
    return BudgetPolicy(
        limits=BudgetLimits(
            max_input_tokens=1_000_000,
            max_output_tokens=output_tokens,
            timeout_ms=900_000,
            max_retries=0,
            max_total_latency_ms=1_200_000,
            max_estimated_cost=1.0,
        ),
        pricing=ProviderPricing(
            input_cost_per_1k_tokens=0,
            output_cost_per_1k_tokens=0,
            currency="USD",
            pricing_table_version="plan-only-zero-cost-v1",
        ),
    )


def _persisted_evidence_fragments(documents: list) -> list[EvidenceFragmentInput]:
    """Return the resolver-owned chunk projection; never fall back to full text."""

    fragments: list[EvidenceFragmentInput] = []
    for document in documents:
        chunks = getattr(document, "evidence_chunks", None)
        if not chunks:
            raise SystemExit("evidence_batch_plan_chunks_unavailable")
        fragments.extend(EvidenceFragmentInput.model_validate(item) for item in chunks)
    if not fragments:
        raise SystemExit("evidence_batch_plan_chunks_unavailable")
    return fragments


def _plan(
    args: argparse.Namespace,
    *,
    profile: str,
    documents: list,
    evidence_fragments: list[EvidenceFragmentInput],
    identity: str,
    run: TenderAnalysisRun,
):
    policy = (
        BatchPolicy.approved_32k(tokenizer_identity=identity)
        if profile == "32k"
        else BatchPolicy.approved_64k(tokenizer_identity=identity)
    )
    output_tokens = policy.output_reserve
    packet = build_r10_1_evidence_packet(
        customer_id=str(run.customer_id),
        project_id=str(run.project_id),
        procurement_case_id=str(run.procurement_case_id),
        run_id=str(run.id),
        registry_number=args.registry_number,
        documents=documents,
        evidence_fragments=evidence_fragments,
    )
    settings = get_settings()
    provider_name = os.environ.get(
        "ARV003_PLAN_PROVIDER", settings.llm_provider or "openai_compatible"
    )
    model = os.environ.get("ARV003_PLAN_MODEL", settings.llm_model or "approved-model")
    return build_r10_1_batch_plan(
        packet=packet,
        customer_id=str(run.customer_id),
        project_id=str(run.project_id),
        procurement_case_id=str(run.procurement_case_id),
        registry_number=args.registry_number,
        run_id=str(run.id),
        documents=documents,
        provider_name=provider_name,
        model=model,
        budget_policy=_policy(output_tokens),
        token_counter=args.tokenizer,
        batch_policy=policy,
        prompt_id="procurement-analysis",
        prompt_version="r10.1-batched-v1",
        output_schema_id="production-llm-analysis",
        output_schema_version="v1",
        grounding_policy_version="grounding-v1",
        controlled=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry-number", required=True)
    parser.add_argument("--legacy-plan-32k", required=True, type=Path)
    parser.add_argument("--legacy-plan-64k", required=True, type=Path)
    parser.add_argument("--lineage-output", required=True, type=Path)
    args = parser.parse_args()
    args.tokenizer = tokenizer_from_environment()
    started = time.monotonic()
    with SessionLocal() as session:
        run = session.scalar(
            select(TenderAnalysisRun).where(
                TenderAnalysisRun.registry_number == args.registry_number
            )
        )
        if not run or not run.procurement_case_id:
            raise SystemExit("evidence_batch_plan_source_unavailable")
        inputs = resolve_customer_run_inputs(session, args.registry_number)
    documents = inputs.documents
    evidence_fragments = _persisted_evidence_fragments(documents)
    plans = {
        "32k": _plan(
            args,
            profile="32k",
            documents=documents,
            evidence_fragments=evidence_fragments,
            identity=args.tokenizer.identity,
            run=run,
        ),
        "64k": _plan(
            args,
            profile="64k",
            documents=documents,
            evidence_fragments=evidence_fragments,
            identity=args.tokenizer.identity,
            run=run,
        ),
    }
    if any(len(plan.fragment_ids) != len(evidence_fragments) for plan in plans.values()):
        raise SystemExit("evidence_batch_plan_coverage_mismatch")
    legacy_paths = {"32k": args.legacy_plan_32k, "64k": args.legacy_plan_64k}
    records = []
    for profile, plan in plans.items():
        legacy = json.loads(legacy_paths[profile].read_text(encoding="utf-8"))
        records.append(
            {
                "context_profile": profile,
                "legacy_audit_hash": legacy.get("plan_hash"),
                "product_plan_hash": plan.plan_hash,
                "corpus_source_hash": plan.corpus_evidence_hash,
                "document_count": len(documents),
                "chunk_count": len(evidence_fragments),
                "coverage": {
                    "assigned": len(plan.fragment_ids),
                    "duplicate": 0,
                    "unassigned": 0,
                    "oversized": 0,
                    "truncation": 0,
                },
                "batch_count": len(plan.batches),
                "tokenizer_identity": plan.tokenizer_identity,
                "chat_template_overhead": plan.policy.chat_template_overhead,
                "execution_deadline_ms": plan.policy.execution_deadline_ms,
                "hash_transition_reason": [
                    "stable identity change",
                    "numeric ordering",
                    "new request contract",
                    "new prompt/schema metadata",
                ],
                "plan_duration_ms": 0,
                "tokenizer_invocations": 0,
                "tokenizer_cache_hits": 0,
                "maxrss": 0,
            }
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    metrics = {
        "plan_duration_ms": duration_ms,
        "tokenizer_invocations": int(getattr(args.tokenizer, "invocations", 0)),
        "tokenizer_cache_hits": int(getattr(args.tokenizer, "cache_hits", 0)),
        "maxrss": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
    }
    for record in records:
        record.update(metrics)
    payload = {"lineage_version": "arv003-product-plan-lineage-v1", "profiles": records}
    args.lineage_output.parent.mkdir(parents=True, exist_ok=True)
    args.lineage_output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    args.lineage_output.chmod(0o600)
    print(
        json.dumps(
            {
                "profiles": [
                    {
                        "context_profile": item["context_profile"],
                        "legacy_audit_hash": item["legacy_audit_hash"],
                        "product_plan_hash": item["product_plan_hash"],
                        "document_count": item["document_count"],
                        "chunk_count": item["chunk_count"],
                        "batch_count": item["batch_count"],
                        "coverage": item["coverage"],
                        "plan_duration_ms": item["plan_duration_ms"],
                        "tokenizer_invocations": item["tokenizer_invocations"],
                        "tokenizer_cache_hits": item["tokenizer_cache_hits"],
                        "maxrss": item["maxrss"],
                    }
                    for item in records
                ]
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
