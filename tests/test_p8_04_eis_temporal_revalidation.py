from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.p6_05_exact_attachment_evidence import (
    EXPECTED_DOCUMENT_NAMES,
    NOTICE_NUMBER,
    _canonical_json_bytes,
)
from scripts.p8_04_eis_temporal_revalidation import (
    P6_BASELINE_MANIFEST_SHA256,
    P804TemporalRevalidationBlocked,
    aggregate_result,
    build_comparison_manifest,
    build_fresh_snapshot,
    compare_document_sets,
    load_and_verify_baseline,
    verify_baseline_manifest,
)


def _synthetic_sha256(name: str) -> str:
    return hashlib.sha256(f"synthetic:{name}".encode()).hexdigest()


def _baseline_manifest() -> dict:
    documents = [
        {
            "index": index,
            "name": name,
            "sha256": _synthetic_sha256(name),
            "size_bytes": index * 1000,
            "artifact_id": f"artifact/content-sha256:{_synthetic_sha256(name)}",
            "source_locator": f"eis-getdocs://notice/{NOTICE_NUMBER}/ref/fake/document/{index:02d}",
            "external_source_authority": "ЕИС / zakupki.gov.ru",
            "external_source_reference": f"44fz-notice:{NOTICE_NUMBER}",
            "external_source_version": "fake-ref",
            "retrieved_at": "2026-08-15T14:15:06.928193+00:00",
        }
        for index, name in enumerate(EXPECTED_DOCUMENT_NAMES, start=1)
    ]
    body = {
        "schema_version": "p6.05-exact-attachment-evidence-v1",
        "purpose": "exact-tender-attachment-evidence",
        "status": "PASS_EXACT_ATTACHMENT_EVIDENCE",
        "notice_number": NOTICE_NUMBER,
        "expected_document_count": 7,
        "exact_document_count": 7,
        "missing_names": [],
        "duplicate_names": [],
        "external_actions": False,
        "external_source_authority": "ЕИС / zakupki.gov.ru",
        "external_source_reference": f"44fz-notice:{NOTICE_NUMBER}",
        "external_source_version": "fake-ref",
        "retrieved_at": "2026-08-15T14:15:06.928193+00:00",
        "documents": documents,
    }
    return body


def _canonical_baseline() -> dict:
    body = _baseline_manifest()
    manifest_sha256 = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    return {**body, "manifest_sha256": manifest_sha256, "manifest_integrity_ref": f"sha256:{manifest_sha256}"}


def _rehash(manifest: dict, **overrides: object) -> dict:
    body = {
        key: value
        for key, value in manifest.items()
        if key not in ("manifest_sha256", "manifest_integrity_ref")
    }
    body.update(overrides)
    manifest_sha256 = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    return {**body, "manifest_sha256": manifest_sha256, "manifest_integrity_ref": f"sha256:{manifest_sha256}"}


def _fresh_manifest(*, mutate: str | None = None, add: str | None = None, drop: str | None = None) -> dict:
    documents = list(_baseline_manifest()["documents"])
    if mutate:
        for item in documents:
            if item["name"] == mutate:
                item["sha256"] = _synthetic_sha256(mutate + "-changed")
    if drop:
        documents = [item for item in documents if item["name"] != drop]
    if add:
        documents.append(
            {
                "index": len(documents) + 1,
                "name": add,
                "sha256": _synthetic_sha256(add),
                "size_bytes": 42,
                "artifact_id": f"artifact/content-sha256:{_synthetic_sha256(add)}",
                "source_locator": f"eis-getdocs://notice/{NOTICE_NUMBER}/ref/fake/document/x",
                "external_source_authority": "ЕИС / zakupki.gov.ru",
                "external_source_reference": f"44fz-notice:{NOTICE_NUMBER}",
                "external_source_version": "fake-fresh",
                "retrieved_at": "2026-08-20T10:00:00+00:00",
            }
        )
    body = {
        "schema_version": "p6.05-exact-attachment-evidence-v1",
        "purpose": "exact-tender-attachment-evidence",
        "status": "PASS_EXACT_ATTACHMENT_EVIDENCE",
        "notice_number": NOTICE_NUMBER,
        "expected_document_count": 7,
        "exact_document_count": len(documents),
        "missing_names": [],
        "duplicate_names": [],
        "external_actions": False,
        "external_source_authority": "ЕИС / zakupki.gov.ru",
        "external_source_reference": f"44fz-notice:{NOTICE_NUMBER}",
        "external_source_version": "fake-fresh",
        "retrieved_at": "2026-08-20T10:00:00+00:00",
        "documents": documents,
    }
    manifest_sha256 = hashlib.sha256(_canonical_json_bytes(body)).hexdigest()
    return {**body, "manifest_sha256": manifest_sha256, "manifest_integrity_ref": f"sha256:{manifest_sha256}"}


def _fixture(tmp_path: Path) -> tuple[dict, Path, dict[str, bytes]]:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    payloads: dict[str, bytes] = {}
    files = [
        {
            "original_name": "epNotification.xml",
            "stored_name": "extracted/epnotification.xml",
            "size_bytes": 21,
        }
    ]
    aux = input_dir / "extracted" / "epnotification.xml"
    aux.parent.mkdir(parents=True)
    aux.write_bytes(b"<notice>redacted</notice>")
    files[0]["size_bytes"] = aux.stat().st_size

    for index, name in enumerate(EXPECTED_DOCUMENT_NAMES, start=1):
        payload = f"synthetic-redacted-{index}".encode()
        payloads[name] = payload
        stored_name = f"docs/{index:02d}.bin"
        path = input_dir / stored_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        files.append(
            {
                "original_name": name,
                "stored_name": stored_name,
                "size_bytes": len(payload),
            }
        )

    metadata = {
        "created_at": "2026-08-20T10:00:00+00:00",
        "procurement_source": "zakupki_gov_ru_getdocs_ip",
        "procurement_id": NOTICE_NUMBER,
        "notice_number": NOTICE_NUMBER,
        "reestr_number": NOTICE_NUMBER,
        "procurement": {
            "procurement_id": NOTICE_NUMBER,
            "procurement_number": NOTICE_NUMBER,
        },
        "external_actions": False,
        "no_platform_submission": True,
        "no_email_sending": True,
        "no_digital_signature": True,
        "archive_downloaded": True,
        "archive_extraction_complete": True,
        "getdocs_status": "completed",
        "getdocs_ref_id": "ref-fresh-1234",
        "files": files,
    }
    return metadata, input_dir, payloads


def test_pinned_baseline_sha_is_well_formed() -> None:
    assert len(P6_BASELINE_MANIFEST_SHA256) == 64
    int(P6_BASELINE_MANIFEST_SHA256, 16)


def test_verify_baseline_accepts_canonical() -> None:
    manifest = _canonical_baseline()
    verify_baseline_manifest(manifest, expected_sha256=manifest["manifest_sha256"])


def test_verify_baseline_rejects_tampered_hash() -> None:
    manifest = _canonical_baseline()
    manifest["manifest_sha256"] = "0" * 64
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        verify_baseline_manifest(manifest, expected_sha256=manifest["manifest_sha256"])
    assert caught.value.code == "BLOCKED_BASELINE_EVIDENCE_MISSING"


def test_verify_baseline_rejects_wrong_schema() -> None:
    manifest = _canonical_baseline()
    manifest["schema_version"] = "p7.something-v1"
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        verify_baseline_manifest(manifest, expected_sha256=manifest["manifest_sha256"])
    assert caught.value.code == "BLOCKED_BASELINE_EVIDENCE_MISSING"


def test_verify_baseline_rejects_wrong_status() -> None:
    manifest = _canonical_baseline()
    manifest["status"] = "PASS_WRONG"
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        verify_baseline_manifest(manifest, expected_sha256=manifest["manifest_sha256"])
    assert caught.value.code == "BLOCKED_BASELINE_EVIDENCE_MISSING"


def test_verify_baseline_rejects_wrong_notice() -> None:
    manifest = _canonical_baseline()
    manifest["notice_number"] = "999"
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        verify_baseline_manifest(manifest, expected_sha256=manifest["manifest_sha256"])
    assert caught.value.code == "BLOCKED_BASELINE_EVIDENCE_MISSING"


def test_load_and_verify_baseline_missing_file(tmp_path: Path) -> None:
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        load_and_verify_baseline(tmp_path / "missing.json")
    assert caught.value.code == "BLOCKED_BASELINE_EVIDENCE_MISSING"


def test_load_and_verify_baseline_roundtrip(tmp_path: Path) -> None:
    manifest = _canonical_baseline()
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    loaded = load_and_verify_baseline(path, expected_sha256=manifest["manifest_sha256"])
    assert loaded["manifest_sha256"] == manifest["manifest_sha256"]


def test_no_change_when_fresh_matches_baseline() -> None:
    baseline = _canonical_baseline()
    fresh = _fresh_manifest()
    entries = compare_document_sets(baseline, fresh)
    assert aggregate_result(entries) == "NO_CHANGE"
    assert all(item["classification"] == "UNCHANGED" for item in entries)


def test_changed_document_detected() -> None:
    baseline = _canonical_baseline()
    fresh = _fresh_manifest(mutate="2. Проект контракта.docx")
    entries = compare_document_sets(baseline, fresh)
    changed = [e for e in entries if e["classification"] == "CHANGED"]
    assert len(changed) == 1
    assert changed[0]["name"] == "2. Проект контракта.docx"
    assert aggregate_result(entries) == "CHANGE_DETECTED"


def test_added_document_detected() -> None:
    baseline = _canonical_baseline()
    fresh = _fresh_manifest(add="7. Новый документ.docx")
    entries = compare_document_sets(baseline, fresh)
    added = [e for e in entries if e["classification"] == "ADDED"]
    assert len(added) == 1
    assert added[0]["name"] == "7. Новый документ.docx"
    assert aggregate_result(entries) == "CHANGE_DETECTED"


def test_removed_document_detected() -> None:
    baseline = _canonical_baseline()
    fresh = _fresh_manifest(drop="5. Реквизиты.docx")
    entries = compare_document_sets(baseline, fresh)
    removed = [e for e in entries if e["classification"] == "REMOVED"]
    assert len(removed) == 1
    assert removed[0]["name"] == "5. Реквизиты.docx"
    assert aggregate_result(entries) == "CHANGE_DETECTED"


def test_comparison_manifest_is_deterministic() -> None:
    baseline = _canonical_baseline()
    fresh = _fresh_manifest(mutate="3. Описание объекта закупки.docx")
    first = build_comparison_manifest(
        baseline, fresh, fresh_observed_at="2026-08-20T10:00:00+00:00"
    )
    second = build_comparison_manifest(
        baseline, fresh, fresh_observed_at="2026-08-20T10:00:00+00:00"
    )
    assert first == second
    assert first["aggregate_result"] == "CHANGE_DETECTED"
    assert first["baseline_manifest_sha256"] == baseline["manifest_sha256"]
    assert first["fresh_manifest_sha256"] == fresh["manifest_sha256"]
    assert first["status"] == "CHANGE_DETECTED"
    serialized = json.dumps(first, ensure_ascii=False)
    assert "token" not in serialized


def test_no_change_comparison_manifest() -> None:
    baseline = _canonical_baseline()
    fresh = _fresh_manifest()
    manifest = build_comparison_manifest(
        baseline, fresh, fresh_observed_at="2026-08-20T10:00:00+00:00"
    )
    assert manifest["aggregate_result"] == "NO_CHANGE"
    assert manifest["status"] == "NO_CHANGE"


def test_comparison_does_not_mutate_baseline() -> None:
    baseline = _canonical_baseline()
    before = json.dumps(baseline, ensure_ascii=False, sort_keys=True)
    fresh = _fresh_manifest(mutate="1. Расчет НМЦК1.xlsx")
    build_comparison_manifest(baseline, fresh, fresh_observed_at="x")
    after = json.dumps(baseline, ensure_ascii=False, sort_keys=True)
    assert before == after


def test_fresh_snapshot_fails_closed_on_missing_document(tmp_path: Path) -> None:
    metadata, input_dir, _ = _fixture(tmp_path)
    missing = EXPECTED_DOCUMENT_NAMES[3]
    metadata["files"] = [
        item for item in metadata["files"] if item.get("original_name") != missing
    ]
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        try:
            build_fresh_snapshot(metadata, input_dir=input_dir)
        except Exception as exc:
            raise P804TemporalRevalidationBlocked(
                "BLOCKED_FRESH_EVIDENCE_INCOMPLETE", detail=str(exc)
            ) from exc
    assert caught.value.code == "BLOCKED_FRESH_EVIDENCE_INCOMPLETE"


def test_aggregate_result_requires_all_unchanged() -> None:
    assert aggregate_result([{"classification": "UNCHANGED"}]) == "NO_CHANGE"
    assert aggregate_result([{"classification": "UNCHANGED"}, {"classification": "ADDED"}]) == "CHANGE_DETECTED"


def test_aggregate_result_fails_closed_on_empty() -> None:
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        aggregate_result([])
    assert caught.value.code == "BLOCKED_EMPTY_COMPARISON"


def test_comparison_fails_closed_on_empty_baseline() -> None:
    baseline = _rehash(_canonical_baseline(), documents=[], exact_document_count=0)
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        build_comparison_manifest(baseline, _fresh_manifest(), fresh_observed_at="x")
    assert caught.value.code == "BLOCKED_EMPTY_DOCUMENTS"


def test_comparison_fails_closed_on_empty_fresh() -> None:
    fresh = _rehash(_fresh_manifest(), documents=[], exact_document_count=0)
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        build_comparison_manifest(_canonical_baseline(), fresh, fresh_observed_at="x")
    assert caught.value.code == "BLOCKED_EMPTY_DOCUMENTS"


def test_comparison_fails_closed_on_duplicate_baseline_name() -> None:
    baseline_docs = _canonical_baseline()["documents"]
    baseline = _rehash(
        _canonical_baseline(),
        documents=[*baseline_docs, *baseline_docs],
        exact_document_count=len(baseline_docs) * 2,
    )
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        build_comparison_manifest(baseline, _fresh_manifest(), fresh_observed_at="x")
    assert caught.value.code == "BLOCKED_DUPLICATE_DOCUMENT"


def test_comparison_fails_closed_on_duplicate_fresh_name() -> None:
    fresh_docs = _fresh_manifest()["documents"]
    fresh = _rehash(
        _fresh_manifest(),
        documents=[*fresh_docs, fresh_docs[0]],
        exact_document_count=len(fresh_docs) + 1,
    )
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        build_comparison_manifest(_canonical_baseline(), fresh, fresh_observed_at="x")
    assert caught.value.code == "BLOCKED_DUPLICATE_DOCUMENT"


def test_comparison_fails_closed_on_duplicate_baseline_identity() -> None:
    baseline_docs = _canonical_baseline()["documents"]
    twin = dict(baseline_docs[0])
    twin["name"] = "1. Расчет НМЦК1.xlsx"
    baseline = _rehash(
        _canonical_baseline(),
        documents=[*baseline_docs, twin],
        exact_document_count=len(baseline_docs) + 1,
    )
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        build_comparison_manifest(baseline, _fresh_manifest(), fresh_observed_at="x")
    assert caught.value.code == "BLOCKED_DUPLICATE_DOCUMENT"


def test_comparison_fails_closed_on_fresh_integrity_mismatch() -> None:
    fresh = _fresh_manifest()
    fresh["documents"][0]["sha256"] = "0" * 64
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        build_comparison_manifest(_canonical_baseline(), fresh, fresh_observed_at="x")
    assert caught.value.code == "BLOCKED_MANIFEST_INTEGRITY_MISMATCH"


def test_comparison_fails_closed_on_baseline_integrity_mismatch() -> None:
    baseline = _canonical_baseline()
    baseline["manifest_sha256"] = "0" * 64
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        build_comparison_manifest(baseline, _fresh_manifest(), fresh_observed_at="x")
    assert caught.value.code == "BLOCKED_MANIFEST_INTEGRITY_MISMATCH"


def test_comparison_fails_closed_on_inconsistent_exact_count() -> None:
    fresh = _rehash(_fresh_manifest(), exact_document_count=99)
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        build_comparison_manifest(_canonical_baseline(), fresh, fresh_observed_at="x")
    assert caught.value.code == "BLOCKED_INCONSISTENT_COMPLETENESS"


def test_comparison_fails_closed_on_declared_duplicates_metadata() -> None:
    fresh = _rehash(_fresh_manifest(), duplicate_names=["1. Расчет НМЦК1.xlsx"])
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        build_comparison_manifest(_canonical_baseline(), fresh, fresh_observed_at="x")
    assert caught.value.code == "BLOCKED_INCONSISTENT_COMPLETENESS"


def test_comparison_fails_closed_on_external_actions() -> None:
    fresh = _rehash(_fresh_manifest(), external_actions=True)
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        build_comparison_manifest(_canonical_baseline(), fresh, fresh_observed_at="x")
    assert caught.value.code == "BLOCKED_EXTERNAL_ACTIONS"


def test_no_stale_baseline_fallback_to_no_change() -> None:
    baseline = _canonical_baseline()
    baseline["manifest_sha256"] = "0" * 64
    with pytest.raises(P804TemporalRevalidationBlocked) as caught:
        build_comparison_manifest(baseline, _fresh_manifest(), fresh_observed_at="x")
    assert caught.value.code == "BLOCKED_MANIFEST_INTEGRITY_MISMATCH"
    assert caught.value.code != "BLOCKED_EMPTY_COMPARISON"
