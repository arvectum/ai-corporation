#!/usr/bin/env python3
"""Recover local PostgreSQL access without emitting credentials.

The command compares the configured runtime database URL with the existing
PostgreSQL container identity. With --repair it atomically updates only
AI_CORP_DATABASE_URL in the selected local env file after the container-derived
candidate has passed a live database probe. It never calls an LLM provider.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from src.modules.production_llm_analysis.runtime_db_recovery import (
    recover_runtime_database_access,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--container", default="arvectum-postgres")
    parser.add_argument("--docker-context", default="desktop-linux")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Atomically repair AI_CORP_DATABASE_URL after a successful probe.",
    )
    return parser.parse_args()


def _exit_code(report: dict) -> int:
    runtime_preflight = report.get("runtime_preflight")
    if report.get("selected_candidate") == "none":
        return 2
    if not isinstance(runtime_preflight, dict):
        return 2
    database = runtime_preflight.get("database") or {}
    if not database.get("connection_ready"):
        return 2
    if not database.get("schema_ready"):
        return 4
    return 0 if runtime_preflight.get("ready_for_controlled_execution") else 5


def main() -> int:
    args = _arguments()
    try:
        report = recover_runtime_database_access(
            env_file=args.env_file,
            container=args.container,
            docker_context=args.docker_context,
            repair=args.repair,
            limit=args.limit,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return _exit_code(report)
    except subprocess.CalledProcessError:
        print("runtime_database_recovery_docker_failed", file=sys.stderr)
        return 3
    except (OSError, ValueError):
        print("runtime_database_recovery_failed", file=sys.stderr)
        return 3
    except Exception:
        print("runtime_database_recovery_failed", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
