from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.arv001.full_pre_provider import (
    _reconstruct_actual_batch_requests,
    CanonicalRequestReconstruction
)
from scripts.arv001.prepared_verification import PrivatePreparedVerificationDescriptor


def test_reconstruct_requests_selects_correct_run(tmp_path: Path) -> None:
    database = tmp_path / "prepared.sqlite3"
    policy = tmp_path / "policy.json"

    # Use real models to create schema
    from src.shared.db.base import Base
    from src.tender_research.models import ProcurementTender, TenderAnalysisRun, ProcurementTenderDocument, ProcurementDocumentChunk
    from src.modules.customer_pilot.models import ProcurementCase, PilotProject
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)

    from sqlalchemy.orm import sessionmaker
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Run 1: Unrelated
        r1 = TenderAnalysisRun(id="wrong-run", registry_number="r1", status="completed", metadata_json="{}", customer_id="c1", project_id="p1", procurement_case_id="case1")
        session.add(r1)

        # Run 2: Target
        target_run_id = "target-run"
        metadata = {"arv001_tender_id": "target-tender"}
        r2 = TenderAnalysisRun(
            id=target_run_id,
            customer_id="cust",
            project_id="proj",
            procurement_case_id="case",
            registry_number="reg",
            status="completed",
            metadata_json=json.dumps(metadata)
        )
        session.add(r2)

        t = ProcurementTender(id="target-tender", source="eis", external_id="ext-id", registry_number="reg", content_hash="corpus-sha", title="test")
        session.add(t)

        # Add a document and a chunk
        doc = ProcurementTenderDocument(
            id="d0",
            tender_id="target-tender",
            file_name="doc-00.txt",
            sha256="1",
            size_bytes=100,
            download_status="completed",
            text_extraction_status="extracted",
            raw_meta=json.dumps({"corpus_descriptor": {"original_name": "doc-00.txt", "sha256": "1", "size_bytes": 100}})
        )
        session.add(doc)

        chunk = ProcurementDocumentChunk(
            id="ch0",
            tender_id="target-tender",
            document_id="d0",
            chunk_index=0,
            text="fragment content",
            text_hash="1"*64,
            char_start=0,
            char_end=16,
            token_estimate=5
        )
        session.add(chunk)

        # Add Case and Project for ownership check
        cp = ProcurementCase(
            id="case",
            customer_id="cust",
            project_id="proj",
            current_run_id=target_run_id,
            procurement_number="reg",
            artifact_key="test-artifact-key"
        )
        pp = PilotProject(id="proj", customer_id="cust", name="test project", internal_slug="test-project")
        session.add(cp)
        session.add(pp)

        session.commit()

    # Policy
    policy_data = {
        "provider": "openai_compatible",
        "budget": {
            "limits": {"max_input_tokens": 1000, "max_output_tokens": 4096, "timeout_ms": 1, "max_total_latency_ms": 1, "max_estimated_cost": 1.0, "max_retries": 0},
            "pricing": {"input_cost_per_1k_tokens": 0.0, "output_cost_per_1k_tokens": 0.0, "currency": "USD", "pricing_table_version": "v1"}
        }
    }
    policy.write_text(json.dumps(policy_data))

    descriptor = MagicMock(spec=PrivatePreparedVerificationDescriptor)
    descriptor.target_run_id = target_run_id
    descriptor.customer_id = "cust"
    descriptor.project_id = "proj"
    descriptor.case_id = "case"
    descriptor.tender_id = "target-tender"
    descriptor.corpus_sha256 = "corpus-sha"
    descriptor.registry_identity_sha256 = hashlib.sha256(b"reg").hexdigest()

    tokenizer = MagicMock()
    tokenizer.identity = "test-tokenizer"
    tokenizer.persistent = True
    tokenizer.side_effect = lambda x: 10

    # Reconstruct
    reconstruction = _reconstruct_actual_batch_requests(database, policy, tokenizer=tokenizer, descriptor=descriptor)

    assert isinstance(reconstruction, CanonicalRequestReconstruction)
    assert reconstruction.target_run_binding_verified is True
    assert reconstruction.canonical_evidence_projection_match is True
    assert len(reconstruction.requests) > 0
    assert reconstruction.requests[0].registry_number == "reg"
    assert reconstruction.requests[0].customer_id == "cust"
