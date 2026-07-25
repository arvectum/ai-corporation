#!/usr/bin/env python3
"""Print a sanitized, read-only map of the local PostgreSQL topology."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from src.modules.production_llm_analysis.runtime_db_topology import (
    collect_runtime_database_topology,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--target-container", default="arvectum-postgres")
    parser.add_argument("--docker-context", default="desktop-linux")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    try:
        report = collect_runtime_database_topology(
            env_file=args.env_file,
            target_container=args.target_container,
            docker_context=args.docker_context,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except subprocess.CalledProcessError:
        print("runtime_database_topology_docker_failed", file=sys.stderr)
        return 3
    except (OSError, ValueError):
        print("runtime_database_topology_failed", file=sys.stderr)
        return 3
    except Exception:
        print("runtime_database_topology_failed", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
