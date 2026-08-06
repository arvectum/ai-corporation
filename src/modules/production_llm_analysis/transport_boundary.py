"""Durable, sanitized transport-invocation boundary for the controlled runner.

A provider call that crosses the HTTP transport boundary must remain provable
even when the surrounding disposable partial stage is removed on failure.
These helpers persist a tiny sanitized marker immediately before the first
``HTTPClient.send`` and, on controlled failure, a separate sanitized failure
descriptor. Both live outside the disposable partial output directory and are
the durable basis for ``AUTHORIZATION_CONSUMED`` determination.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

BOUNDARY_SCHEMA_VERSION = "arv001.transport-boundary.v1"
MARKER_FILENAME = "transport-started.marker.json"
FAILURE_DESCRIPTOR_FILENAME = "controlled-failure.descriptor.json"


class TransportBoundaryError(RuntimeError):
    """Fail-closed error for durable transport-boundary persistence."""


def boundary_root(output_root: Path) -> Path:
    """Durable sibling of the supplied output_root, never inside the partial stage."""
    target = output_root.resolve()
    return target.parent / f".{target.name}.transport-boundary"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_write(path: Path, value: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise TransportBoundaryError("boundary_root_unwritable") from exc
    staged = path.parent / f".{path.name}.partial.os_getpid_{os.getpid()}"
    try:
        staged.write_text(_canonical_json(value) + "\n", encoding="utf-8")
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    except OSError as exc:
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        raise TransportBoundaryError("boundary_marker_unwritable") from exc


def write_transport_start_marker(
    root: Path,
    *,
    execution_ordinal: int,
    batch_ordinal: int | None,
    attempt_ordinal: int | None,
    request_identity_hash: str,
) -> Path:
    """Record durable transport start, overwriting in place and always true.

    The marker stores only sanitized identifiers: schema version, execution /
    batch / attempt ordinals, a monotonic sequence and the request identity hash.
    It never contains the prompt, tender text, credential, provider body, URL or
    a private path.
    """
    marker = {
        "schema_version": BOUNDARY_SCHEMA_VERSION,
        "transport_started": True,
        "execution_ordinal": int(execution_ordinal),
        "batch_ordinal": int(batch_ordinal) if batch_ordinal is not None else None,
        "attempt_ordinal": (
            int(attempt_ordinal) if attempt_ordinal is not None else None
        ),
        "monotonic_ns": time.monotonic_ns(),
        "request_identity_hash": request_identity_hash,
    }
    path = root / MARKER_FILENAME
    _atomic_write(path, marker)
    return path


def authorization_consumed(root: Path) -> bool:
    """Return True when a durable transport-start marker confirms HTTP start."""
    try:
        marker = json.loads((root / MARKER_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if marker.get("schema_version") != BOUNDARY_SCHEMA_VERSION:
        return False
    return marker.get("transport_started") is True


def transport_started(root: Path) -> bool:
    """Alias matching the failure descriptor naming used by the reporter."""
    return authorization_consumed(root)


def _read_marker(root: Path) -> dict[str, Any] | None:
    try:
        marker = json.loads((root / MARKER_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if marker.get("schema_version") != BOUNDARY_SCHEMA_VERSION:
        return None
    return marker if marker.get("transport_started") is True else None


def write_controlled_failure_descriptor(
    root: Path,
    *,
    execution_ordinal: int | None = None,
    batch_ordinal: int | None = None,
    attempt_count: int | None = None,
    retry_count: int | None = None,
    sanitized_failure_code: str,
) -> Path:
    """Persist a sanitized controlled failure descriptor outside the partial stage."""
    marker = _read_marker(root)
    if execution_ordinal is None:
        execution_ordinal = (
            int(marker["execution_ordinal"]) if marker else None
        )
    if batch_ordinal is None:
        batch_ordinal = (
            int(marker["batch_ordinal"]) if marker and marker.get("batch_ordinal") is not None else None
        )
    descriptor = {
        "schema_version": BOUNDARY_SCHEMA_VERSION,
        "status": "controlled_provider_failure",
        "transport_started": authorization_consumed(root),
        "authorization_consumed": authorization_consumed(root),
        "sanitized_failure_code": sanitized_failure_code,
        "execution_ordinal": execution_ordinal,
        "batch_ordinal": batch_ordinal,
        "attempt_count": attempt_count if attempt_count is not None else 0,
        "retry_count": retry_count if retry_count is not None else 0,
        "raw_response_stored": False,
        "raw_provider_body_recorded": False,
        "raw_tender_text_recorded": False,
        "credential_value_recorded": False,
        "local_paths_recorded": False,
    }
    path = root / FAILURE_DESCRIPTOR_FILENAME
    _atomic_write(path, descriptor)
    return path


def load_authorization_state(root: Path) -> dict[str, Any]:
    """Read the durable marker and, when present, the failure descriptor."""
    result: dict[str, Any] = {
        "schema_version": BOUNDARY_SCHEMA_VERSION,
        "transport_started": authorization_consumed(root),
        "authorization_consumed": authorization_consumed(root),
    }
    descriptor_path = root / FAILURE_DESCRIPTOR_FILENAME
    if descriptor_path.exists():
        try:
            result["failure_descriptor"] = json.loads(
                descriptor_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            result["failure_descriptor"] = None
    return result