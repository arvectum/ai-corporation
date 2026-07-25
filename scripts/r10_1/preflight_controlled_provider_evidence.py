#!/usr/bin/env python3
"""Print a sanitized Gate 5 provider, database and run preflight report.

This command never calls a provider, reads document text, prints a credential,
or writes customer artifacts. It can resolve settings from one explicit local
env file without recording its path or values in the report.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.modules.production_llm_analysis.runtime_preflight import (
    collect_runtime_controlled_provider_preflight,
)
from src.shared.config.settings import Settings, get_settings


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Local settings file. Its path and values are never printed.",
    )
    return parser.parse_args()


def _settings(args: argparse.Namespace) -> tuple[Settings, str]:
    if args.env_file is None:
        return get_settings(), "default_settings_resolution"
    if not args.env_file.is_file():
        raise ValueError("preflight_env_file_missing")
    return (
        Settings(_env_file=args.env_file, _env_file_encoding="utf-8"),
        "explicit_env_file_plus_process_environment",
    )


def main() -> int:
    args = _arguments()
    try:
        settings, settings_source = _settings(args)
        report = collect_runtime_controlled_provider_preflight(
            settings,
            limit=args.limit,
        )
        report["settings_source"] = settings_source
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
