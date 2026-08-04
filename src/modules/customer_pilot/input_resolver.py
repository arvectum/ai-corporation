"""Server-owned procurement documents for a customer analysis run."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.modules.procurement_analysis.document_roles import detect_document_role
from src.tender_research.models import (
    ProcurementDocumentChunk,
    ProcurementTender,
    ProcurementTenderDocument,
    TenderAnalysisRun,
)


@dataclass(frozen=True)
class CustomerRunInputs:
    registry_number: str
    documents: list[Any]
    source_document_ids: list[str]
    warnings: list[str]
    limitations: list[str]


def resolve_customer_run_inputs(
    session: Session, registry_number: str, *, _exact_tender: ProcurementTender | None = None
) -> CustomerRunInputs:
    """Resolve persisted production-intake text; caller paths are never accepted."""
    tender = _exact_tender or session.scalar(
        select(ProcurementTender)
        .where(
            (ProcurementTender.registry_number == registry_number)
            | (ProcurementTender.purchase_number == registry_number)
        )
        # External intake identity is stable across databases; a generated UUID is
        # only a final tie-breaker and must not choose a different tender merely
        # because equivalent rows were inserted in a different order.
        .order_by(
            ProcurementTender.updated_at.desc(),
            ProcurementTender.external_id.desc(),
            ProcurementTender.id.desc(),
        )
    )
    if not tender:
        raise HTTPException(
            409, "No persisted procurement intake is available for this registry number"
        )
    rows = session.scalars(
        select(ProcurementTenderDocument)
        .where(
            ProcurementTenderDocument.tender_id == tender.id,
            ProcurementTenderDocument.download_status.in_(
                ("downloaded", "completed", "ready")
            ),
        )
        .order_by(
            ProcurementTenderDocument.file_name.asc(),
            func.coalesce(ProcurementTenderDocument.document_identity_hash, "").asc(),
            func.coalesce(ProcurementTenderDocument.sha256, "").asc(),
            ProcurementTenderDocument.id.asc(),
        )
    ).all()
    from src.modules.procurement_analysis.frozen_types import AnalyzedDocument

    documents, identities = [], []
    for document_order, row in enumerate(rows, 1):
        chunks = session.scalars(
            select(ProcurementDocumentChunk)
            .where(ProcurementDocumentChunk.document_id == row.id)
            .order_by(
                ProcurementDocumentChunk.chunk_index.asc(),
                ProcurementDocumentChunk.id.asc(),
            )
        ).all()
        text = "\n\n".join(chunk.text for chunk in chunks if chunk.text)
        if not text:
            continue
        name = row.file_name
        # A database UUID is only a lookup key.  It cannot be provenance because
        # the same production intake imported into another database would then
        # produce a different frozen source graph.
        document_identity = row.document_identity_hash or row.sha256
        if not document_identity:
            document_identity = sha256(
                (f"{name}\0{text}").encode()
            ).hexdigest()
        evidence_chunks = [
            {
                "document_id": document_identity,
                "document_name": name,
                "chunk_id": sha256(
                    f"{document_identity}\0{chunk.chunk_index}\0{chunk.text_hash}".encode()
                ).hexdigest(),
                "locator": {
                    "document_order": document_order,
                    "role": detect_document_role(name),
                    "chunk_index": int(chunk.chunk_index),
                    "char_start": int(chunk.char_start),
                    "char_end": int(chunk.char_end),
                    "text_hash": chunk.text_hash,
                    "token_estimate": int(chunk.token_estimate),
                    **{
                        key: value for key, value in (chunk.raw_meta or {}).items()
                        if key in {"page", "section"} and isinstance(value, (str, int, float))
                    },
                },
                "text": chunk.text,
            }
            for chunk in chunks if chunk.text
        ]
        documents.append(
            AnalyzedDocument(
                name,
                "." + name.rsplit(".", 1)[-1].lower() if "." in name else ".txt",
                detect_document_role(name),
                text,
                True,
                [],
                "persisted_procurement_intake",
                document_identity,
                None,
                evidence_chunks,
            )
        )
        identities.append(document_identity)
    if not documents:
        raise HTTPException(
            409, "Persisted procurement intake has no usable extracted documents"
        )
    return CustomerRunInputs(registry_number, documents, identities, [], [])


def resolve_customer_run_inputs_for_analysis_run(
    session: Session, run: TenderAnalysisRun
) -> CustomerRunInputs:
    """Resolve only the tender persisted in this run's immutable intake binding.

    Legacy runs without an explicit tender identity fail closed: selecting the
    newest matching registry would silently substitute a different corpus.
    """
    try:
        binding = json.loads(run.metadata_json or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(409, "Analysis run intake binding is invalid") from exc
    tender_id = binding.get("arv001_tender_id") if isinstance(binding, dict) else None
    if not isinstance(tender_id, str) or not tender_id:
        raise HTTPException(409, "Analysis run intake binding is missing")
    tender = session.scalar(select(ProcurementTender).where(ProcurementTender.id == tender_id))
    if not tender or (tender.registry_number != run.registry_number and tender.purchase_number != run.registry_number):
        raise HTTPException(409, "Analysis run intake binding does not match registry")
    expected_documents = binding.get("arv001_document_identity_hashes")
    if not isinstance(expected_documents, list) or not expected_documents or not all(isinstance(item, str) for item in expected_documents):
        raise HTTPException(409, "Analysis run document binding is invalid")
    inputs = resolve_customer_run_inputs(session, run.registry_number, _exact_tender=tender)
    if inputs.source_document_ids != expected_documents:
        raise HTTPException(409, "Analysis run document binding does not match intake")
    return inputs
