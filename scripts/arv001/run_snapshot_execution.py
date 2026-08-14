#!/usr/bin/env python3
"""Read-only ARV-001 prepared snapshot re-execution runner.

Accepts either a fully attested prepared-state root (``--root``) or the three
prepared artifacts directly (``--database``, ``--data-dir``, ``--descriptor``).
Re-executes the exact run in read-only mode with no provider invocation and no
writes, printing a JSON result bound to the exact snapshot hash.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.arv001.snapshot_execution import (
    SnapshotExecutionError,
    execute_prepared_state_root,
    execute_snapshot,
)


def _json_default(value: object) -> object:
    if hasattr(value, "gate"):
        return {
            "gate": value.gate,
            "status": value.status,
            "detail": value.detail,
        }
    raise TypeError(f"cannot serialize: {type(value).__name__}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-execute an ARV-001 prepared snapshot read-only."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--root",
        help="published prepared-state root (attested publication)",
    )
    source.add_argument(
        "--database",
        help="path to prepared.sqlite3 (used with --data-dir and --descriptor)",
    )
    parser.add_argument(
        "--data-dir",
        help="path to application-data (consumer snapshot files)",
    )
    parser.add_argument(
        "--descriptor",
        help="path to prepared-verification.json (06 00 private descriptor)",
    )
    parser.add_argument("--head", required=True, help="expected repository head sha")
    parser.add_argument("--corpus", required=True, help="expected corpus sha256")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.root:
            result = execute_prepared_state_root(
                root=Path(args.root),
                expected_head=args.head,
                expected_corpus_sha=args.corpus,
            )
        else:
            if not args.data_dir or not args.descriptor:
                raise SnapshotExecutionError("snapshot_arguments_incomplete")
            result = execute_snapshot(
                database=Path(args.database),
                data_dir=Path(args.data_dir),
                descriptor_path=Path(args.descriptor),
                expected_head=args.head,
                expected_corpus_sha=args.corpus,
            )
    except SnapshotExecutionError as exc:
        print(f"snapshot_execution:{exc.code}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "snapshot_id": result.snapshot_id,
                "target_run_id": result.target_run_id,
                "snapshot_hash_bound": result.snapshot_hash_bound,
                "snapshot_hash": result.snapshot_hash,
                "recomputed_snapshot_hash": result.recomputed_snapshot_hash,
                "database_sha256": result.database_sha256,
                "verified": result.verified,
                "provider_invocations": result.provider_invocations,
                "write_count": result.write_count,
                "gates": list(result.gates),
            },
            sort_keys=True,
            indent=2,
            default=_json_default,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())