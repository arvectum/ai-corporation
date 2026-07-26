#!/usr/bin/env python3
"""Controlled procurement document recovery; dry-run unless --apply is supplied."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.modules.production_llm_analysis.document_recovery import (
    DocumentRecoveryRequest,
    _report,
    recover_procurement_documents,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--registry-number", required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--build-chunks", action="store_true")
    args = parser.parse_args()
    try:
        report = recover_procurement_documents(
            DocumentRecoveryRequest(
                registry_number=args.registry_number,
                env_file=args.env_file,
                data_root=args.data_root,
                backup_dir=args.backup_dir,
                apply=args.apply,
                build_chunks=args.build_chunks,
            )
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return (
            0
            if report.get("final_status")
            in {
                "RECOVERY_PREFLIGHT_READY",
                "DOCUMENTS_RESTORED_AND_CHUNKS_BUILT",
                "ALREADY_RECOVERED_AND_CHUNKED",
            }
            else 2
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps(
                _report(
                    classification="DOCUMENT_RECOVERY_REJECTED",
                    final_status="DOCUMENT_RECOVERY_REJECTED",
                    error_code=type(exc).__name__,
                ),
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
