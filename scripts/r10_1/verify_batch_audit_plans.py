#!/usr/bin/env python3
"""Read-only reproduction of the approved ARV-003 32K/64K audit plans.

The command keeps source text in memory only, never prints it, and never writes
the audit artifacts.  It intentionally mirrors the pinned audit serialization
so a changed request contract produces a full-hash mismatch instead of a new
expected value.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from collections import Counter
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.modules.production_llm_analysis.evidence import build_evidence_packet, canonical_json_bytes
from src.modules.production_llm_analysis.schemas import EvidenceFragmentInput, ProviderClaim


REGISTRY = "0388100001826000047"
TOKENIZER = "/opt/homebrew/bin/llama-tokenize"
MODEL = "/Users/master/.ollama/models/blobs/sha256-1278394b693672ac2799eadc9a83fd98259a6a88a40acfb1dcaa6c6fc895a606"
CHAT_TEMPLATE_OVERHEAD = 32


def _sha(value: object) -> str:
    return sha256(canonical_json_bytes(value)).hexdigest()


def _tokenize(value: str) -> int:
    completed = subprocess.run(
        [TOKENIZER, "-m", MODEL, "--stdin", "--show-count"],
        input=value.encode("utf-8"), capture_output=True, check=True,
    )
    for line in reversed(completed.stdout.decode("utf-8", "replace").splitlines()):
        if line.startswith("Total number of tokens:"):
            return int(line.rsplit(":", 1)[1].strip())
    raise RuntimeError("evidence_batch_exact_tokenizer_unavailable")


def _psql_json(sql: str) -> object:
    completed = subprocess.run(
        ["docker", "exec", "arvectum-postgres", "psql", "-U", "arvectum", "-d", "arvectum", "-At", "-c", sql],
        capture_output=True, text=True, check=True,
    )
    return json.loads(completed.stdout.strip() or "null")


def _request_body(packet: object, output_tokens: int) -> dict:
    # The approved artifact was calculated before numeric source ordering was
    # introduced. Reproduce that artifact's legacy packet serialization here;
    # product code itself uses numeric ordering.
    fragments_sorted = sorted(packet.fragments, key=lambda item: (item.document_id, item.chunk_id, item.fragment_id))
    unsigned = packet.model_dump(mode="json", exclude={"packet_hash"})
    unsigned["fragments"] = [fragment.model_dump(mode="json") for fragment in fragments_sorted]
    packet = packet.model_copy(update={"fragments": fragments_sorted, "packet_hash": _sha(unsigned)})
    fragments = [
        {
            "fragment_id": fragment.fragment_id,
            "document_id": fragment.document_id,
            "document_name": fragment.document_name,
            "chunk_id": fragment.chunk_id,
            "locator": fragment.locator,
            "text": fragment.text,
            "text_sha256": fragment.text_sha256,
        }
        for fragment in packet.fragments
    ]
    task = {
        "prompt_id": "procurement-analysis",
        "prompt_version": "v1",
        "output_schema_id": "production-llm-analysis",
        "output_schema_version": "v1",
        "grounding_policy_version": "grounding-v1",
        "procurement_case_id": packet.procurement_case_id,
        "registry_number": packet.registry_number,
        "evidence_packet_hash": packet.packet_hash,
        "evidence_fragments": fragments,
        "output_contract": {
            "type": "object", "additionalProperties": False, "required": ["claims"],
            "properties": {"claims": {"type": "array", "items": ProviderClaim.model_json_schema()}},
        },
    }
    return {
        "model": "gemma4:12b", "temperature": 0, "max_tokens": output_tokens,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                "You are a controlled internal procurement analysis component. "
                "Return exactly one valid JSON object matching the supplied output contract. "
                "Use only supplied evidence fragments. Every factual claim must copy exact evidence "
                "identities, locator and quote. Return an empty claims array when evidence is insufficient. "
                "Do not authorize submission, signing, supplier outreach or any autonomous external action."
            )},
            {"role": "user", "content": canonical_json_bytes(task).decode("utf-8")},
        ],
    }


def _legacy_packet(packet: object) -> object:
    fragments = sorted(packet.fragments, key=lambda item: (item.document_id, item.chunk_id, item.fragment_id))
    unsigned = packet.model_dump(mode="json", exclude={"packet_hash"})
    unsigned["fragments"] = [fragment.model_dump(mode="json") for fragment in fragments]
    return packet.model_copy(update={"fragments": fragments, "packet_hash": _sha(unsigned)})


def _fragment(chunk: dict, document: dict) -> EvidenceFragmentInput:
    locator = dict(chunk.get("raw_meta") or {})
    locator.update({"chunk_index": chunk["chunk_index"], "char_start": chunk["char_start"], "char_end": chunk["char_end"]})
    return EvidenceFragmentInput(
        document_id=document["doc_id"], document_name=document["file_name"],
        chunk_id=chunk["chunk_id"], locator=locator, text=chunk["text"],
    )


def _plan(chunks: list[dict], documents: list[dict], output_tokens: int, safety: int, context: int, fixed_tokens: int) -> dict:
    by_id = {document["doc_id"]: document for document in documents}
    document_order = {document["doc_id"]: index + 1 for index, document in enumerate(documents)}
    items = []
    for ordinal, chunk in enumerate(chunks, 1):
        packet = build_evidence_packet(
            customer_id="c", project_id="p", procurement_case_id="k", run_id="r",
            registry_number=REGISTRY, fragments=[_fragment(chunk, by_id[chunk["doc_id"]])],
        )
        items.append((ordinal, chunk, packet.fragments[0]))
    batches = []
    cursor = 0
    rough_budget = context - output_tokens - safety - CHAT_TEMPLATE_OVERHEAD - fixed_tokens
    while cursor < len(items):
        selected = []
        rough_tokens = 0
        while cursor + len(selected) < len(items):
            item = items[cursor + len(selected)]
            rough_tokens += max(1, math.ceil((len(item[1]["text"].encode("utf-8")) + 420) / 1.5))
            if selected and rough_tokens > rough_budget:
                break
            selected.append(item)
        if not selected:
            raise RuntimeError("evidence_batch_oversized_chunk")

        def body_for(values):
            packet = build_evidence_packet(
                customer_id="c", project_id="p", procurement_case_id="k", run_id="r",
                registry_number=REGISTRY,
                fragments=[_fragment(item[1], by_id[item[1]["doc_id"]]) for item in values],
            )
            body = _request_body(packet, output_tokens)
            return _legacy_packet(packet), body

        packet, body = body_for(selected)
        projected = _tokenize(canonical_json_bytes(body).decode("utf-8")) + CHAT_TEMPLATE_OVERHEAD
        while projected + output_tokens + safety > context and len(selected) > 1:
            allowed = context - output_tokens - safety
            cut = max(1, math.ceil(len(selected) * (1 - allowed / projected)))
            del selected[-cut:]
            packet, body = body_for(selected)
            projected = _tokenize(canonical_json_bytes(body).decode("utf-8")) + CHAT_TEMPLATE_OVERHEAD
        if projected + output_tokens + safety > context:
            raise RuntimeError("evidence_batch_oversized_chunk")
        evidence_tokens = _tokenize(json.dumps(
            body["messages"][1]["content"], ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ))
        manifest = {
            "batch_ordinal": len(batches) + 1,
            "fragments": [
                {"fragment_id": packet.fragments[index].fragment_id, "text_sha256": packet.fragments[index].text_sha256,
                 "document_ordinal": document_order[item[1]["doc_id"]], "chunk_ordinal": item[0],
                 "persisted_chunk_index": item[1]["chunk_index"], "locator_present": bool(packet.fragments[index].locator)}
                for index, item in enumerate(selected)
            ],
            "projected_request_tokens": projected,
        }
        batches.append({
            "batch_ordinal": len(batches) + 1,
            "document_ordinals": sorted({document_order[item[1]["doc_id"]] for item in selected}),
            "chunk_count": len(selected), "evidence_tokens": evidence_tokens,
            "projected_request_tokens": projected, "output_reserve": output_tokens,
            "safety_margin": safety,
            "utilization_percent": round(projected / (context - output_tokens - safety) * 100, 2),
            "batch_hash": _sha(manifest),
            "fragment_ids": [item[2].fragment_id for item in selected],
            "first_chunk_ordinal": selected[0][0], "last_chunk_ordinal": selected[-1][0],
            "locator_coverage": all(bool(item[2].locator) for item in selected),
        })
        cursor += len(selected)
    return {"batches": batches, "source_chunks": len(items), "assigned_chunks": sum(item["chunk_count"] for item in batches)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-32k", type=Path, default=Path("/Users/master/.config/arvectum/arv003-batch-audit/arv003-batch-plan-32k.json"))
    parser.add_argument("--plan-64k", type=Path, default=Path("/Users/master/.config/arvectum/arv003-batch-audit/arv003-batch-plan-64k.json"))
    args = parser.parse_args()
    documents = _psql_json(f"""select coalesce(json_agg(x order by x.identity_hash, x.file_name, x.sha256, x.doc_id), '[]'::json) from (select d.id::text doc_id, d.file_name, coalesce(d.document_identity_hash,'') identity_hash, coalesce(d.sha256,'') sha256, coalesce(json_agg(json_build_object('chunk_id',c.id::text,'chunk_index',c.chunk_index,'text',c.text,'text_hash',c.text_hash,'char_start',c.char_start,'char_end',c.char_end,'token_estimate',c.token_estimate,'raw_meta',c.raw_meta) order by c.chunk_index,c.id) filter (where c.id is not null),'[]'::json) chunks from procurement_tenders t join procurement_tender_documents d on d.tender_id=t.id left join procurement_document_chunks c on c.document_id=d.id where t.registry_number='{REGISTRY}' and d.download_status in ('downloaded','completed','ready') group by d.id) x""")
    if not isinstance(documents, list) or len(documents) != 5:
        print(json.dumps({"code": "evidence_batch_plan_mismatch", "documents": len(documents) if isinstance(documents, list) else None}))
        return 2
    chunks = []
    for document in documents:
        for chunk in document.get("chunks") or []:
            chunk["doc_id"] = document["doc_id"]
            chunks.append(chunk)
    if len(chunks) != 1266:
        print(json.dumps({"code": "evidence_batch_plan_mismatch", "chunks": len(chunks)}))
        return 2
    full_32 = []
    for document in documents:
        text = "\n\n".join(chunk["text"] for chunk in document.get("chunks") or [])
        full_32.append(EvidenceFragmentInput(document_id=document["doc_id"], document_name=document["file_name"], chunk_id=f"{document['doc_id']}:fulltext:v1", locator={"role": "supporting", "segment": 0}, text=text))
    full_packet = build_evidence_packet(customer_id="c", project_id="p", procurement_case_id="k", run_id="r", registry_number=REGISTRY, fragments=full_32)
    fixed_packet = build_evidence_packet(customer_id="c", project_id="p", procurement_case_id="k", run_id="r", registry_number=REGISTRY, fragments=[EvidenceFragmentInput(document_id="document-ordinal", document_name="document", chunk_id="empty-evidence", locator={}, text="x")])
    fixed_body = _request_body(fixed_packet, 4096)
    fixed_body["messages"][1]["content"] = fixed_body["messages"][1]["content"].replace('"text":"x"', '"text":""')
    fixed_tokens = _tokenize(canonical_json_bytes(fixed_body).decode("utf-8"))
    specs = [(args.plan_32k, _plan(chunks, documents, 4096, 3277, 32768, fixed_tokens)), (args.plan_64k, _plan(chunks, documents, 8192, 6554, 65536, fixed_tokens))]
    outputs = []
    for path, actual in specs:
        expected = json.loads(path.read_text(encoding="utf-8"))
        actual_hash = _sha(actual)
        expected_hash = expected.get("plan_hash")
        outputs.append({"profile": expected.get("physical_context"), "batch_count": len(actual["batches"]), "assigned": actual["assigned_chunks"], "expected_hash": expected_hash, "actual_hash": actual_hash, "match": actual_hash == expected_hash})
    print(json.dumps({"documents": 5, "chunks": 1266, "plans": outputs}, sort_keys=True))
    return 0 if all(item["match"] for item in outputs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
