from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.p6_05_exact_attachment_evidence import (
    EXPECTED_DOCUMENT_NAMES,
    NOTICE_NUMBER,
    ExactAttachmentEvidenceBlocked,
    build_exact_attachment_evidence,
)


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
                "source_url": "https://zakupki.gov.ru/redacted?token=must-not-leak",
            }
        )

    metadata = {
        "created_at": "2026-08-14T18:00:00+00:00",
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
        "getdocs_ref_id": "ref-1234",
        "files": files,
    }
    return metadata, input_dir, payloads


def _blocked(code: str, func) -> ExactAttachmentEvidenceBlocked:
    with pytest.raises(ExactAttachmentEvidenceBlocked) as caught:
        func()
    assert caught.value.code == code
    return caught.value


def test_exact_seven_document_manifest_ignores_auxiliary_files(tmp_path: Path) -> None:
    metadata, input_dir, payloads = _fixture(tmp_path)

    manifest = build_exact_attachment_evidence(metadata, input_dir=input_dir)

    assert manifest["status"] == "PASS_EXACT_ATTACHMENT_EVIDENCE"
    assert manifest["expected_document_count"] == 7
    assert manifest["exact_document_count"] == 7
    assert manifest["missing_names"] == []
    assert manifest["duplicate_names"] == []
    assert manifest["external_actions"] is False
    assert [item["name"] for item in manifest["documents"]] == list(
        EXPECTED_DOCUMENT_NAMES
    )
    assert all("epNotification" not in item["name"] for item in manifest["documents"])
    for item in manifest["documents"]:
        assert item["sha256"] == hashlib.sha256(payloads[item["name"]]).hexdigest()
        assert item["artifact_id"] == f"artifact/content-sha256:{item['sha256']}"
        assert "token=" not in item["source_locator"]
        assert "zakupki.gov.ru/redacted" not in item["source_locator"]

    serialized = json.dumps(manifest, ensure_ascii=False)
    assert "synthetic-redacted-1" not in serialized
    assert "must-not-leak" not in serialized


def test_manifest_hash_is_deterministic_for_same_evidence(tmp_path: Path) -> None:
    metadata, input_dir, _ = _fixture(tmp_path)
    first = build_exact_attachment_evidence(metadata, input_dir=input_dir)
    second = build_exact_attachment_evidence(metadata, input_dir=input_dir)
    assert first == second


def test_missing_expected_document_fails_closed(tmp_path: Path) -> None:
    metadata, input_dir, _ = _fixture(tmp_path)
    missing = EXPECTED_DOCUMENT_NAMES[3]
    metadata["files"] = [
        item for item in metadata["files"] if item.get("original_name") != missing
    ]
    exc = _blocked(
        "missing_expected_documents",
        lambda: build_exact_attachment_evidence(metadata, input_dir=input_dir),
    )
    assert exc.missing_names == (missing,)
    assert exc.exact_document_count == 6


def test_duplicate_expected_document_name_fails_closed(tmp_path: Path) -> None:
    metadata, input_dir, _ = _fixture(tmp_path)
    duplicate = deepcopy(metadata["files"][1])
    duplicate["stored_name"] = "docs/duplicate.bin"
    (input_dir / "docs" / "duplicate.bin").write_bytes(b"duplicate")
    duplicate["size_bytes"] = len(b"duplicate")
    metadata["files"].append(duplicate)
    exc = _blocked(
        "duplicate_expected_document_names",
        lambda: build_exact_attachment_evidence(metadata, input_dir=input_dir),
    )
    assert exc.duplicate_names == (EXPECTED_DOCUMENT_NAMES[0],)


def test_path_traversal_fails_closed(tmp_path: Path) -> None:
    metadata, input_dir, _ = _fixture(tmp_path)
    metadata["files"][1]["stored_name"] = "../outside.bin"
    _blocked(
        "unsafe_stored_path",
        lambda: build_exact_attachment_evidence(metadata, input_dir=input_dir),
    )


def test_symlinked_exact_file_fails_closed(tmp_path: Path) -> None:
    metadata, input_dir, _ = _fixture(tmp_path)
    source = input_dir / "docs" / "01.bin"
    replacement = input_dir / "docs" / "real.bin"
    source.rename(replacement)
    try:
        source.symlink_to(replacement)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not available")
    metadata["files"][1]["size_bytes"] = replacement.stat().st_size
    _blocked(
        "symlinked_evidence_file",
        lambda: build_exact_attachment_evidence(metadata, input_dir=input_dir),
    )


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (lambda m: m.__setitem__("getdocs_ref_id", ""), "missing_getdocs_ref_id"),
        (lambda m: m.__setitem__("getdocs_ref_id", "ref?token=secret"), "unsafe_getdocs_ref_id"),
        (lambda m: m.__setitem__("reestr_number", "wrong"), "notice_mismatch"),
        (lambda m: m.__setitem__("external_actions", True), "unsafe_external_actions"),
        (lambda m: m.__setitem__("no_platform_submission", False), "unsafe_no_platform_submission"),
        (lambda m: m.__setitem__("no_email_sending", False), "unsafe_no_email_sending"),
        (lambda m: m.__setitem__("no_digital_signature", False), "unsafe_no_digital_signature"),
        (lambda m: m.__setitem__("archive_downloaded", False), "unsafe_archive_downloaded"),
        (
            lambda m: m.__setitem__("archive_extraction_complete", False),
            "unsafe_archive_extraction_complete",
        ),
        (lambda m: m.__setitem__("getdocs_status", "no_data"), "getdocs_not_completed"),
    ],
)
def test_retrieval_and_safety_guards_fail_closed(
    tmp_path: Path,
    mutator,
    code: str,
) -> None:
    metadata, input_dir, _ = _fixture(tmp_path)
    mutator(metadata)
    _blocked(code, lambda: build_exact_attachment_evidence(metadata, input_dir=input_dir))


def test_declared_size_mismatch_fails_closed(tmp_path: Path) -> None:
    metadata, input_dir, _ = _fixture(tmp_path)
    metadata["files"][1]["size_bytes"] += 1
    _blocked(
        "size_mismatch",
        lambda: build_exact_attachment_evidence(metadata, input_dir=input_dir),
    )