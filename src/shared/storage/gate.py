from __future__ import annotations

import asyncio
import inspect
import logging
from enum import Enum
from functools import wraps

from src.shared.errors import AppError
from src.shared.storage.capacity import StorageState, get_storage_snapshot

logger = logging.getLogger(__name__)


class IngestionBlockedReason(str, Enum):
    STORAGE_CAPACITY_PROTECTION_ACTIVE = "STORAGE_CAPACITY_PROTECTION_ACTIVE"
    STORAGE_STATE_UNKNOWN = "STORAGE_STATE_UNKNOWN"


class StorageGateError(AppError):
    status_code = 503

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class IngestionBlockedError(StorageGateError):
    status_code = 503

    def __init__(self, reason: IngestionBlockedReason) -> None:
        self.reason = reason
        super().__init__(
            message=f"Ingestion blocked: {reason.value}",
            code=reason.value,
        )


def check_ingestion_allowed() -> None:
    snap = get_storage_snapshot()
    if snap.state == StorageState.INGESTION_PROTECTED:
        raise IngestionBlockedError(IngestionBlockedReason.STORAGE_CAPACITY_PROTECTION_ACTIVE)
    if snap.state == StorageState.STORAGE_UNKNOWN:
        raise IngestionBlockedError(IngestionBlockedReason.STORAGE_STATE_UNKNOWN)


MASS_INGESTION_TAG = "_mass_ingestion"


def mass_ingestion(func):
    if inspect.iscoroutinefunction(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            check_ingestion_allowed()
            return await func(*args, **kwargs)
        async_wrapper.__dict__[MASS_INGESTION_TAG] = True
        return async_wrapper
    else:
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            check_ingestion_allowed()
            return func(*args, **kwargs)
        sync_wrapper.__dict__[MASS_INGESTION_TAG] = True
        return sync_wrapper


def is_mass_ingestion_operation(func) -> bool:
    return getattr(func, MASS_INGESTION_TAG, False)
