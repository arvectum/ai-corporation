from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from src.shared.config.settings import get_settings

logger = logging.getLogger(__name__)

UTC = timezone.utc


class StorageState(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    INGESTION_PROTECTED = "ingestion_protected"
    STORAGE_UNKNOWN = "storage_unknown"


_PUBLIC_REASONS = {
    "not_configured": "storage_root_not_configured",
    "missing": "storage_root_missing",
    "not_directory": "storage_root_not_directory",
    "mount_not_verified": "storage_mount_not_verified",
    "usage_unavailable": "storage_usage_unavailable",
    "normal": "threshold_normal",
    "warning": "threshold_warning",
    "critical": "threshold_critical",
    "ingestion_protected": "threshold_ingestion_protected",
}


def _public_reason(state: StorageState, detail_key: str) -> str:
    if state == StorageState.STORAGE_UNKNOWN:
        return _PUBLIC_REASONS.get(detail_key, "storage_root_not_configured")
    return _PUBLIC_REASONS.get(state.value, state.value)


@dataclass
class StorageSnapshot:
    storage_root: str | None = None
    filesystem_total_bytes: int | None = None
    filesystem_used_bytes: int | None = None
    filesystem_free_bytes: int | None = None
    used_percent: float | None = None
    state: StorageState = StorageState.STORAGE_UNKNOWN
    checked_at: str = ""
    mount_verified: bool = False
    reason: str = ""


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _resolve_storage_root() -> Path | None:
    settings = get_settings()
    raw = settings.arvectum_storage_root
    if not raw:
        logger.warning("ARVECTUM_STORAGE_ROOT is not configured")
        return None
    path = Path(raw).expanduser().resolve()
    return path


def _mount_verified(path: Path) -> bool:
    try:
        st_dev = os.stat(path).st_dev
        root_st_dev = os.stat("/").st_dev
        return st_dev != root_st_dev
    except OSError:
        return False


def _classify_used_percent(pct: float, warning_pct: float, critical_pct: float, ingestion_protected_pct: float) -> StorageState:
    if pct >= ingestion_protected_pct:
        return StorageState.INGESTION_PROTECTED
    if pct >= critical_pct:
        return StorageState.CRITICAL
    if pct >= warning_pct:
        return StorageState.WARNING
    return StorageState.NORMAL


def get_storage_snapshot() -> StorageSnapshot:
    settings = get_settings()
    storage_root = _resolve_storage_root()
    now = _utcnow_iso()

    if storage_root is None:
        return StorageSnapshot(
            state=StorageState.STORAGE_UNKNOWN,
            checked_at=now,
            reason=_public_reason(StorageState.STORAGE_UNKNOWN, "not_configured"),
        )

    if not storage_root.exists():
        logger.warning("Storage root does not exist: %s", storage_root)
        return StorageSnapshot(
            storage_root=str(storage_root),
            state=StorageState.STORAGE_UNKNOWN,
            checked_at=now,
            reason=_public_reason(StorageState.STORAGE_UNKNOWN, "missing"),
        )

    if not storage_root.is_dir():
        logger.warning("Storage root is not a directory: %s", storage_root)
        return StorageSnapshot(
            storage_root=str(storage_root),
            state=StorageState.STORAGE_UNKNOWN,
            checked_at=now,
            reason=_public_reason(StorageState.STORAGE_UNKNOWN, "not_directory"),
        )

    mv = _mount_verified(storage_root)
    if not mv:
        logger.warning("Storage root %s resolves to system disk; expected external mount", storage_root)
        return StorageSnapshot(
            storage_root=str(storage_root),
            state=StorageState.STORAGE_UNKNOWN,
            checked_at=now,
            mount_verified=False,
            reason=_public_reason(StorageState.STORAGE_UNKNOWN, "mount_not_verified"),
        )

    try:
        usage = shutil.disk_usage(storage_root)
    except OSError as exc:
        logger.error("disk_usage failed for %s: %s", storage_root, exc)
        return StorageSnapshot(
            storage_root=str(storage_root),
            state=StorageState.STORAGE_UNKNOWN,
            checked_at=now,
            mount_verified=True,
            reason=_public_reason(StorageState.STORAGE_UNKNOWN, "usage_unavailable"),
        )

    total = usage.total
    free = usage.free
    used = total - free
    used_percent = (used / total) * 100.0 if total > 0 else 0.0

    state = _classify_used_percent(
        used_percent,
        warning_pct=settings.arvectum_storage_warning_percent,
        critical_pct=settings.arvectum_storage_critical_percent,
        ingestion_protected_pct=settings.arvectum_storage_ingestion_protected_percent,
    )

    return StorageSnapshot(
        storage_root=str(storage_root),
        filesystem_total_bytes=total,
        filesystem_used_bytes=used,
        filesystem_free_bytes=free,
        used_percent=round(used_percent, 2),
        state=state,
        checked_at=now,
        mount_verified=mv,
        reason=_public_reason(state, state.value),
    )


def storage_metrics_dict() -> dict:
    snap = get_storage_snapshot()
    return {
        "storage_total_bytes": snap.filesystem_total_bytes,
        "storage_free_bytes": snap.filesystem_free_bytes,
        "storage_used_percent": snap.used_percent,
        "storage_state": snap.state.value,
        "ingestion_allowed": snap.state not in (StorageState.INGESTION_PROTECTED, StorageState.STORAGE_UNKNOWN),
    }
