"""Server-owned procurement documents for a customer analysis run."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.tender_research.models import (
    ProcurementDocumentChunk,
    ProcurementTender,
    ProcurementTenderDocument,
)
from src.modules.procurement_analysis.document_roles import detect_document_role


@dataclass(frozen=True)
class CustomerRunInputs:
    registry_number: str
    documents: list[Any]
    source_document_ids: list[str]
    warnings: list[str]
    limitations: list[str]


def resolve_customer_run_inputs(
    session: Session, registry_number: str
) -> CustomerRunInputs:
    """Resolve persisted production-intake text; caller paths are never accepted."""
    tender = session.scalar(
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
    for row in rows:
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
                (f"{name}\0{text}").encode("utf-8")
            ).hexdigest()
        evidence_chunks = [
            {
                "document_id": document_identity,
                "document_name": name,
                "chunk_id": f"{document_identity}:chunk:{chunk.chunk_index}",
                "locator": {"role": detect_document_role(name), "chunk_index": chunk.chunk_index},
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
