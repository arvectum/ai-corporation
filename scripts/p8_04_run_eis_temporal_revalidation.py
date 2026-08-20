from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.p6_05_exact_attachment_evidence import (
    NOTICE_NUMBER,
    ExactAttachmentEvidenceBlocked,
)
from scripts.p8_04_eis_temporal_revalidation import (
    P6_BASELINE_MANIFEST_SHA256,
    P804TemporalRevalidationBlocked,
    build_comparison_manifest,
    build_fresh_snapshot,
    load_and_verify_baseline,
    write_manifest,
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


def _baseline_path() -> Path:
    raw = os.environ.get("P8_04_BASELINE_MANIFEST", "").strip()
    if not raw:
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_BASELINE_EVIDENCE_MISSING",
            detail="P8_04_BASELINE_MANIFEST env var not set",
        )
    return Path(raw)


def _preflight() -> None:
    if os.environ.get("ZAKUPKI_GOV_RU_SOAP_ENABLED") != "1":
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_EIS_NOT_ENABLED",
            detail="ZAKUPKI_GOV_RU_SOAP_ENABLED != 1",
        )
    if not os.environ.get("ZAKUPKI_GOV_RU_SOAP_TOKEN"):
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_EIS_CREDENTIAL_MISSING",
            detail="ZAKUPKI_GOV_RU_SOAP_TOKEN not set",
        )
    if os.environ.get("ARVECTUM_ETP_TLS_ENABLED") != "true":
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_TLS_NOT_ENABLED",
            detail="ARVECTUM_ETP_TLS_ENABLED != true",
        )
    if not os.environ.get("ARVECTUM_ETP_TLS_POLICY_PATH"):
        raise P804TemporalRevalidationBlocked(
            "BLOCKED_TLS_POLICY_MISSING",
            detail="ARVECTUM_ETP_TLS_POLICY_PATH not set",
        )


def main() -> int:
    run_id: str | None = None
    try:
        baseline = load_and_verify_baseline(_baseline_path())
        _preflight()

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
        fresh = build_fresh_snapshot(
            metadata,
            input_dir=get_demo_run_input_dir(run_id),
        )
        comparison = build_comparison_manifest(
            baseline,
            fresh,
            fresh_observed_at=metadata.get("created_at", ""),
        )

        procurement_dir = get_demo_run_procurement_dir(run_id)
        write_manifest(fresh, procurement_dir / "p8-04-fresh-observation.json")
        write_manifest(comparison, procurement_dir / "p8-04-comparison.json")

        _print(
            {
                "status": comparison["status"],
                "run_id": run_id,
                "notice_number": NOTICE_NUMBER,
                "baseline_manifest_sha256": P6_BASELINE_MANIFEST_SHA256,
                "fresh_manifest_sha256": fresh["manifest_sha256"],
                "comparison_manifest_sha256": comparison["manifest_sha256"],
                "aggregate_result": comparison["aggregate_result"],
                "evidence_completeness": comparison["evidence_completeness"],
                "fresh_retrieved_at": fresh["retrieved_at"],
                "external_actions": False,
            }
        )
        return 0
    except (P804TemporalRevalidationBlocked, ExactAttachmentEvidenceBlocked) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        _print(
            {
                "status": "FAIL_CLOSED",
                "failure_code": code,
                "run_id": run_id,
                "external_actions": False,
            }
        )
        return 2
    except Exception as exc:  # noqa: BLE001
        _print(
            {
                "status": "FAIL_CLOSED",
                "failure_code": f"runtime_error:{type(exc).__name__}",
                "run_id": run_id,
                "external_actions": False,
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
