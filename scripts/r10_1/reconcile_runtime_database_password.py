#!/usr/bin/env python3
"""Reconcile the persisted PostgreSQL role password with container configuration.

The command uses the container-local Unix socket to update the existing database
role password from the container's own POSTGRES_PASSWORD environment variable.
No credential is copied into command arguments, stdout, stderr, Git, or an
artifact. After a successful update it invokes the existing safe runtime database
recovery and sanitized Gate 5 preflight.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from src.modules.production_llm_analysis.runtime_db_password_reconcile import (
    reconcile_runtime_database_password,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--container", default="arvectum-postgres")
    parser.add_argument("--docker-context", default="desktop-linux")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the password reconciliation and repair the runtime database URL.",
    )
    return parser.parse_args()


def _exit_code(report: dict) -> int:
    status = report.get("final_status")
    if status == "GATE5_PREFLIGHT_READY":
        return 0
    if status == "LOCAL_SOCKET_RECOVERY_UNAVAILABLE":
        return 2
    if status in {
        "POSTGRES_PASSWORD_RECONCILIATION_APPLY_REQUIRED",
        "DATABASE_ACCESS_RESTORED_GATE5_CONFIGURATION_PENDING",
    }:
        return 5
    if status == "DATABASE_ACCESS_RESTORED_SCHEMA_MIGRATION_REQUIRED":
        return 6
    if status in {
        "POSTGRES_PASSWORD_RECONCILIATION_FAILED",
        "PASSWORD_RECONCILED_RUNTIME_RECOVERY_FAILED",
        "PASSWORD_RECONCILED_RUNTIME_ACCESS_NOT_RESTORED",
        "DATABASE_ACCESS_RESTORED_PREFLIGHT_NOT_RUN",
        "DATABASE_ACCESS_RESTORED_PREFLIGHT_DATABASE_FAILED",
    }:
        return 4
    return 3


def main() -> int:
    args = _arguments()
    try:
        report = reconcile_runtime_database_password(
            env_file=args.env_file,
            backup_dir=args.backup_dir,
            container=args.container,
            docker_context=args.docker_context,
            apply=args.apply,
            limit=args.limit,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return _exit_code(report)
    except subprocess.CalledProcessError:
        print("runtime_database_password_reconcile_docker_failed", file=sys.stderr)
        return 3
    except (OSError, ValueError):
        print("runtime_database_password_reconcile_failed", file=sys.stderr)
        return 3
    except Exception:
        print("runtime_database_password_reconcile_failed", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
