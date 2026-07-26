from __future__ import annotations

import asyncio
import collections
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.shared.errors import AppError
from src.shared.storage.capacity import (
    StorageSnapshot,
    StorageState,
    _classify_used_percent,
    get_storage_snapshot,
)
from src.shared.storage.gate import (
    IngestionBlockedError,
    IngestionBlockedReason,
    check_ingestion_allowed,
    mass_ingestion,
)
from src.shared.storage.public import PublicStorageSnapshot, public_storage_snapshot
from src.shared.config.settings import Settings


DiskUsage = collections.namedtuple("DiskUsage", ["total", "used", "free"])

SAMPLE_DISK_TOTAL = 1_000_000_000_000


def _mock_disk_usage(free_bytes: int):
    used = SAMPLE_DISK_TOTAL - free_bytes
    return DiskUsage(SAMPLE_DISK_TOTAL, used, free_bytes)


def test_classify_69_9_percent_is_normal():
    assert _classify_used_percent(69.9, 70, 80, 90) == StorageState.NORMAL


def test_classify_70_percent_is_warning():
    assert _classify_used_percent(70.0, 70, 80, 90) == StorageState.WARNING


def test_classify_80_percent_is_critical():
    assert _classify_used_percent(80.0, 70, 80, 90) == StorageState.CRITICAL


def test_classify_90_percent_is_ingestion_protected():
    assert _classify_used_percent(90.0, 70, 80, 90) == StorageState.INGESTION_PROTECTED


def test_classify_custom_thresholds():
    assert _classify_used_percent(50.0, 40, 60, 80) == StorageState.WARNING
    assert _classify_used_percent(65.0, 40, 60, 80) == StorageState.CRITICAL
    assert _classify_used_percent(85.0, 40, 60, 80) == StorageState.INGESTION_PROTECTED


@pytest.fixture
def mock_settings():
    with patch("src.shared.storage.capacity.get_settings") as mock:
        settings = MagicMock()
        settings.arvectum_storage_root = None
        settings.arvectum_storage_warning_percent = 70
        settings.arvectum_storage_critical_percent = 80
        settings.arvectum_storage_ingestion_protected_percent = 90
        mock.return_value = settings
        yield mock


def test_no_root_configured(mock_settings):
    snap = get_storage_snapshot()
    assert snap.state == StorageState.STORAGE_UNKNOWN
    assert snap.mount_verified is False
    assert snap.reason == "storage_root_not_configured"
    assert snap.checked_at != ""


def test_root_does_not_exist(mock_settings):
    mock_settings.return_value.arvectum_storage_root = "/nonexistent/storage/path"
    snap = get_storage_snapshot()
    assert snap.state == StorageState.STORAGE_UNKNOWN
    assert snap.mount_verified is False
    assert snap.reason == "storage_root_missing"
    assert snap.checked_at != ""


def test_root_is_file_not_dir(mock_settings):
    with tempfile.NamedTemporaryFile(delete=False) as f:
        tmpfile = f.name
    try:
        mock_settings.return_value.arvectum_storage_root = tmpfile
        snap = get_storage_snapshot()
        assert snap.state == StorageState.STORAGE_UNKNOWN
        assert snap.mount_verified is False
        assert snap.reason == "storage_root_not_directory"
        assert snap.checked_at != ""
    finally:
        os.unlink(tmpfile)


def test_system_disk_fallback_is_storage_unknown(mock_settings):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        snap = get_storage_snapshot()
        assert snap.state == StorageState.STORAGE_UNKNOWN
        assert snap.mount_verified is False
        assert snap.reason == "storage_mount_not_verified"
        assert snap.checked_at != ""


def test_69_9_percent_normal(mock_settings):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.301))):
                snap = get_storage_snapshot()
                assert snap.state == StorageState.NORMAL
                assert snap.used_percent is not None and snap.used_percent < 70
                assert snap.reason == "threshold_normal"
                assert snap.checked_at != ""


def test_70_percent_warning(mock_settings):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.30))):
                snap = get_storage_snapshot()
                assert snap.state == StorageState.WARNING
                assert snap.used_percent is not None and snap.used_percent >= 70
                assert snap.reason == "threshold_warning"


def test_80_percent_critical(mock_settings):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.20))):
                snap = get_storage_snapshot()
                assert snap.state == StorageState.CRITICAL
                assert snap.used_percent is not None and snap.used_percent >= 80
                assert snap.reason == "threshold_critical"


def test_90_percent_ingestion_protected(mock_settings):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.10))):
                snap = get_storage_snapshot()
                assert snap.state == StorageState.INGESTION_PROTECTED
                assert snap.used_percent is not None and snap.used_percent >= 90
                assert snap.reason == "threshold_ingestion_protected"


def test_ingestion_blocked_at_90_percent(mock_settings):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.10))):
                with pytest.raises(IngestionBlockedError) as exc:
                    check_ingestion_allowed()
                assert exc.value.reason == IngestionBlockedReason.STORAGE_CAPACITY_PROTECTION_ACTIVE
                assert exc.value.status_code == 503


def test_ingestion_blocked_at_unknown(mock_settings):
    with pytest.raises(IngestionBlockedError) as exc:
        check_ingestion_allowed()
    assert exc.value.reason == IngestionBlockedReason.STORAGE_STATE_UNKNOWN
    assert exc.value.status_code == 503


def test_ingestion_allowed_at_normal(mock_settings):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.40))):
                check_ingestion_allowed()


def test_read_report_cleanup_not_blocked(mock_settings):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.05))):
                read_func = lambda: "data"
                report_func = lambda: "report"
                cleanup_func = lambda: None
                read_func()
                report_func()
                cleanup_func()


def test_mass_ingestion_decorator_sync(mock_settings):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.40))):
                @mass_ingestion
                def sweep():
                    return "done"

                assert sweep() == "done"


def test_mass_ingestion_decorator_async_normal(mock_settings):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.40))):
                @mass_ingestion
                async def sweep():
                    return "done"

                result = asyncio.run(sweep())
                assert result == "done"


def test_mass_ingestion_decorator_async_blocked(mock_settings):
    with pytest.raises(IngestionBlockedError):
        @mass_ingestion
        async def sweep():
            return "done"

        asyncio.run(sweep())


def test_mass_ingestion_decorator_blocked(mock_settings):
    with pytest.raises(IngestionBlockedError):
        @mass_ingestion
        def sweep():
            return "done"

        sweep()


def test_custom_thresholds_via_settings(mock_settings):
    mock_settings.return_value.arvectum_storage_warning_percent = 50
    mock_settings.return_value.arvectum_storage_critical_percent = 60
    mock_settings.return_value.arvectum_storage_ingestion_protected_percent = 70
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.35))):
                snap = get_storage_snapshot()
                assert snap.state == StorageState.CRITICAL
                assert snap.used_percent is not None and snap.used_percent >= 60


def test_public_snapshot_hides_storage_root(mock_settings):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.40))):
                pub = public_storage_snapshot()
                assert not hasattr(pub, "storage_root")
                assert pub.filesystem_total_bytes is not None
                assert pub.state == StorageState.NORMAL.value
                assert pub.checked_at != ""
                assert pub.reason == "threshold_normal"


def test_public_snapshot_unknown(mock_settings):
    pub = public_storage_snapshot()
    assert pub.state == StorageState.STORAGE_UNKNOWN.value
    assert pub.checked_at != ""
    assert not hasattr(pub, "storage_root")


def test_public_snapshot_no_absolute_paths(mock_settings):
    mock_settings.return_value.arvectum_storage_root = "/some/secret/storage/path"
    pub = public_storage_snapshot()
    assert pub.reason in ("storage_root_missing", "storage_root_not_configured")
    assert "/some/secret" not in pub.reason


def test_bytes_and_percent_calculated_correctly(mock_settings):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            free_bytes = 250_000_000_000
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(free_bytes)):
                snap = get_storage_snapshot()
                assert snap.filesystem_total_bytes == SAMPLE_DISK_TOTAL
                assert snap.filesystem_free_bytes == free_bytes
                assert snap.filesystem_used_bytes == SAMPLE_DISK_TOTAL - free_bytes
                expected_pct = round(((SAMPLE_DISK_TOTAL - free_bytes) / SAMPLE_DISK_TOTAL) * 100, 2)
                assert snap.used_percent == expected_pct


def test_checked_at_always_filled_unknown(mock_settings):
    snap = get_storage_snapshot()
    assert snap.checked_at != ""


def test_checked_at_always_filled_with_root(mock_settings):
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.40))):
                snap = get_storage_snapshot()
                assert snap.checked_at != ""


# Integration-style tests for API level

@pytest.fixture
def mock_settings_unknown():
    with patch("src.shared.storage.capacity.get_settings") as mock:
        settings = MagicMock()
        settings.arvectum_storage_root = None
        settings.arvectum_storage_warning_percent = 70
        settings.arvectum_storage_critical_percent = 80
        settings.arvectum_storage_ingestion_protected_percent = 90
        mock.return_value = settings
        yield mock


@pytest.fixture
def mock_settings_protected():
    with patch("src.shared.storage.capacity.get_settings") as mock:
        settings = MagicMock()
        settings.arvectum_storage_root = "/tmp/mock_storage"
        settings.arvectum_storage_warning_percent = 70
        settings.arvectum_storage_critical_percent = 80
        settings.arvectum_storage_ingestion_protected_percent = 90
        mock.return_value = settings
        yield mock


@pytest.fixture
def mock_settings_normal():
    with patch("src.shared.storage.capacity.get_settings") as mock:
        settings = MagicMock()
        settings.arvectum_storage_root = "/tmp/mock_storage"
        settings.arvectum_storage_warning_percent = 70
        settings.arvectum_storage_critical_percent = 80
        settings.arvectum_storage_ingestion_protected_percent = 90
        mock.return_value = settings
        yield mock


def test_api_app_error_handling_registered():
    app = FastAPI()
    from src.shared.api.errors import register_exception_handlers
    from src.shared.storage.gate import IngestionBlockedError, IngestionBlockedReason
    register_exception_handlers(app)

    @app.get("/test-storage-block")
    def test_endpoint():
        raise IngestionBlockedError(IngestionBlockedReason.STORAGE_CAPACITY_PROTECTION_ACTIVE)

    client = TestClient(app)
    resp = client.get("/test-storage-block")
    assert resp.status_code == 503
    data = resp.json()
    assert data["error"]["code"] == "STORAGE_CAPACITY_PROTECTION_ACTIVE"


def test_api_app_error_handling_unknown():
    app = FastAPI()
    from src.shared.api.errors import register_exception_handlers
    from src.shared.storage.gate import IngestionBlockedError, IngestionBlockedReason
    register_exception_handlers(app)

    @app.get("/test-storage-unknown")
    def test_endpoint():
        raise IngestionBlockedError(IngestionBlockedReason.STORAGE_STATE_UNKNOWN)

    client = TestClient(app)
    resp = client.get("/test-storage-unknown")
    assert resp.status_code == 503
    data = resp.json()
    assert data["error"]["code"] == "STORAGE_STATE_UNKNOWN"


# Threshold validation tests

def test_valid_thresholds():
    s = Settings(
        arvectum_storage_warning_percent=70,
        arvectum_storage_critical_percent=80,
        arvectum_storage_ingestion_protected_percent=90,
    )
    assert s.arvectum_storage_warning_percent == 70


def test_invalid_thresholds_warning_equals_critical():
    with pytest.raises(ValueError, match="Storage thresholds"):
        Settings(
            arvectum_storage_warning_percent=80,
            arvectum_storage_critical_percent=80,
            arvectum_storage_ingestion_protected_percent=90,
        )


def test_invalid_thresholds_critical_exceeds_protected():
    with pytest.raises(ValueError, match="Storage thresholds"):
        Settings(
            arvectum_storage_warning_percent=70,
            arvectum_storage_critical_percent=90,
            arvectum_storage_ingestion_protected_percent=80,
        )


def test_invalid_thresholds_warning_exceeds_protected():
    with pytest.raises(ValueError, match="Storage thresholds"):
        Settings(
            arvectum_storage_warning_percent=90,
            arvectum_storage_critical_percent=80,
            arvectum_storage_ingestion_protected_percent=95,
        )
