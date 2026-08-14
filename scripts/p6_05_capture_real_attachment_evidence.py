from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.p6_05_exact_attachment_evidence import (
    EXPECTED_DOCUMENT_COUNT,
    NOTICE_NUMBER,
    ExactAttachmentEvidenceBlocked,
    build_exact_attachment_evidence,
    write_exact_attachment_evidence,
)
from src.modules.tender_operator_agent_demo.procurement_intake_service import (
    create_run_from_eis_docs_archive,
)
from src.modules.tender_operator_agent_demo.schemas import EisDocsArchiveRunRequest
from src.modules.tender_operator_agent_demo.upload_service_legacy import (
    get_demo_run_input_dir,
    get_demo_run_procurement_dir,
    load_demo_run_metadata,
)


def _print(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main() -> int:
    """Future L7 operator runner. It must not be executed by L6/CI."""

    run_id: str | None = None
    try:
        result = create_run_from_eis_docs_archive(
            EisDocsArchiveRunRequest(
                reestr_number=NOTICE_NUMBER,
                law="44fz",
                subsystem_type="PRIZ",
                method="getDocsByReestrNumber",
                download_archive=True,
                analyze_after_download=False,
            )
        )
        run_id = result.run_id
        metadata = load_demo_run_metadata(run_id)
        manifest = build_exact_attachment_evidence(
            metadata,
            input_dir=get_demo_run_input_dir(run_id),
        )
        output_path = (
            get_demo_run_procurement_dir(run_id)
            / "p6-05-exact-attachment-evidence.json"
        )
        write_exact_attachment_evidence(manifest, output_path=output_path)
        _print(
            {
                "status": manifest["status"],
                "expected_document_count": manifest["expected_document_count"],
                "exact_document_count": manifest["exact_document_count"],
                "missing_names": manifest["missing_names"],
                "duplicate_names": manifest["duplicate_names"],
                "manifest_sha256": manifest["manifest_sha256"],
                "evidence_path": str(output_path),
                "external_actions": False,
            }
        )
        return 0
    except ExactAttachmentEvidenceBlocked as exc:
        _print(
            {
                "status": "FAIL_CLOSED_EXACT_ATTACHMENT_EVIDENCE",
                "failure_code": exc.code,
                "expected_document_count": EXPECTED_DOCUMENT_COUNT,
                "exact_document_count": exc.exact_document_count,
                "missing_names": list(exc.missing_names),
                "duplicate_names": list(exc.duplicate_names),
                "external_actions": False,
            }
        )
        return 2
    except Exception as exc:  # noqa: BLE001
        _print(
            {
                "status": "FAIL_CLOSED_EXACT_ATTACHMENT_EVIDENCE",
                "failure_code": f"runtime_error:{type(exc).__name__}",
                "expected_document_count": EXPECTED_DOCUMENT_COUNT,
                "exact_document_count": 0,
                "missing_names": [],
                "duplicate_names": [],
                "external_actions": False,
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())