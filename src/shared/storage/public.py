from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

from src.shared.storage.capacity import StorageSnapshot, get_storage_snapshot


class PublicStorageSnapshot(BaseModel):
    filesystem_total_bytes: int | None = None
    filesystem_used_bytes: int | None = None
    filesystem_free_bytes: int | None = None
    used_percent: float | None = None
    state: str = "storage_unknown"
    checked_at: str | None = None
    mount_verified: bool = False
    reason: str = ""


def public_storage_snapshot() -> PublicStorageSnapshot:
    snap = get_storage_snapshot()
    return PublicStorageSnapshot(
        filesystem_total_bytes=snap.filesystem_total_bytes,
        filesystem_used_bytes=snap.filesystem_used_bytes,
        filesystem_free_bytes=snap.filesystem_free_bytes,
        used_percent=snap.used_percent,
        state=snap.state.value,
        checked_at=snap.checked_at,
        mount_verified=snap.mount_verified,
        reason=snap.reason,
    )
