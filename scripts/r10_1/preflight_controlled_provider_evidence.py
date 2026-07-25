#!/usr/bin/env python3
"""Print a sanitized Gate 5 provider/run preflight report.

This command never calls a provider, reads document text, prints a credential,
or writes customer artifacts. It only reports configuration readiness and
server-owned run/document/chunk identities required for a controlled execution.
"""
from __future__ import annotations

import argparse
import json
import sys

from src.modules.production_llm_analysis.controlled_preflight import (
    collect_controlled_provider_preflight,
)
from src.shared.config.settings import get_settings
from src.shared.db.session import SessionLocal


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    try:
        with SessionLocal() as session:
            report = collect_controlled_provider_preflight(
                session,
                get_settings(),
                limit=args.limit,
            )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["ready_for_controlled_execution"] else 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        print("controlled_provider_preflight_failed", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
