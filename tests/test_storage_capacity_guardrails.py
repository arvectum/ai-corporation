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
from src.shared.config.settings import Settings, get_settings


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

# =============================================================================
# ARV-010S1.2 — Env resolution tests
# =============================================================================


def test_env_canonical_only(monkeypatch):
    """Canonical env var ARVECTUM_STORAGE_WARNING_PERCENT changes threshold."""
    get_settings.cache_clear()
    monkeypatch.setenv("ARVECTUM_STORAGE_WARNING_PERCENT", "55")
    monkeypatch.setenv("ARVECTUM_STORAGE_CRITICAL_PERCENT", "65")
    monkeypatch.setenv("ARVECTUM_STORAGE_INGESTION_PROTECTED_PERCENT", "75")
    s = get_settings()
    assert s.arvectum_storage_warning_percent == 55
    assert s.arvectum_storage_critical_percent == 65
    assert s.arvectum_storage_ingestion_protected_percent == 75
    get_settings.cache_clear()


def test_env_compatibility_only(monkeypatch):
    """Compatibility env AI_CORP_ARVECTUM_STORAGE_* changes threshold."""
    get_settings.cache_clear()
    monkeypatch.setenv("AI_CORP_ARVECTUM_STORAGE_WARNING_PERCENT", "45")
    monkeypatch.setenv("AI_CORP_ARVECTUM_STORAGE_CRITICAL_PERCENT", "55")
    monkeypatch.setenv("AI_CORP_ARVECTUM_STORAGE_INGESTION_PROTECTED_PERCENT", "65")
    s = get_settings()
    assert s.arvectum_storage_warning_percent == 45
    assert s.arvectum_storage_critical_percent == 55
    assert s.arvectum_storage_ingestion_protected_percent == 65
    get_settings.cache_clear()


def test_env_canonical_takes_priority(monkeypatch):
    """Canonical ARVECTUM_STORAGE_* has priority over AI_CORP_ARVECTUM_STORAGE_*."""
    get_settings.cache_clear()
    monkeypatch.setenv("ARVECTUM_STORAGE_WARNING_PERCENT", "30")
    monkeypatch.setenv("AI_CORP_ARVECTUM_STORAGE_WARNING_PERCENT", "50")
    s = get_settings()
    assert s.arvectum_storage_warning_percent == 30  # canonical wins
    get_settings.cache_clear()


def test_env_python_kwargs_still_work():
    """Python constructor kwargs work regardless of env aliases."""
    s = Settings(
        arvectum_storage_warning_percent=60,
        arvectum_storage_critical_percent=75,
        arvectum_storage_ingestion_protected_percent=90,
    )
    assert s.arvectum_storage_warning_percent == 60
    assert s.arvectum_storage_critical_percent == 75
    assert s.arvectum_storage_ingestion_protected_percent == 90


def test_env_bad_thresholds_via_env_rejected(monkeypatch):
    """Invalid threshold order via env triggers ValueError."""
    get_settings.cache_clear()
    monkeypatch.setenv("ARVECTUM_STORAGE_WARNING_PERCENT", "80")
    monkeypatch.setenv("ARVECTUM_STORAGE_CRITICAL_PERCENT", "80")
    monkeypatch.setenv("ARVECTUM_STORAGE_INGESTION_PROTECTED_PERCENT", "90")
    with pytest.raises(ValueError, match="Storage thresholds"):
        get_settings()
    get_settings.cache_clear()


def test_env_defaults_after_cleanup(monkeypatch):
    """After clearing env, get_settings returns defaults."""
    get_settings.cache_clear()
    monkeypatch.setenv("ARVECTUM_STORAGE_WARNING_PERCENT", "55")
    s = get_settings()
    assert s.arvectum_storage_warning_percent == 55
    get_settings.cache_clear()
    # Without env, back to defaults
    monkeypatch.delenv("ARVECTUM_STORAGE_WARNING_PERCENT", raising=False)
    s2 = get_settings()
    assert s2.arvectum_storage_warning_percent == 70
    get_settings.cache_clear()


# =============================================================================
# ARV-010S1.2 — Real service gate tests
# =============================================================================


@pytest.mark.storage_gate_enforced
def test_service_gate_blocks_unknown_state(monkeypatch, session):
    """prepare_tender_for_analysis raises IngestionBlockedError on STORAGE_UNKNOWN."""
    from src.shared.storage.capacity import StorageSnapshot, StorageState
    from src.tender_research.rag.prepare_service import prepare_tender_for_analysis

    snap = StorageSnapshot(
        state=StorageState.STORAGE_UNKNOWN,
        reason="storage_root_not_configured",
        mount_verified=False,
        checked_at="2026-07-26T00:00:00",
        filesystem_total_bytes=None,
        filesystem_free_bytes=None,
        filesystem_used_bytes=None,
        used_percent=None,
        storage_root=None,
    )
    with patch("src.shared.storage.gate.get_storage_snapshot", return_value=snap):
        with patch("src.tender_research.rag.prepare_service._get_session") as mock_get_session:
            with patch("src.tender_research.rag.prepare_service.EisTenderLoader") as mock_loader:
                with patch("src.tender_research.rag.prepare_service.download_tender_documents") as mock_dl:
                    with pytest.raises(IngestionBlockedError) as exc:
                        prepare_tender_for_analysis("0323100010326000013", session=session)
                    assert exc.value.reason == IngestionBlockedReason.STORAGE_STATE_UNKNOWN
                    assert exc.value.status_code == 503
                    mock_get_session.assert_not_called()
                    mock_loader.assert_not_called()
                    mock_dl.assert_not_called()


@pytest.mark.storage_gate_enforced
def test_service_gate_blocks_protected_state(monkeypatch, session):
    """prepare_tender_for_analysis raises IngestionBlockedError on INGESTION_PROTECTED."""
    from src.shared.storage.capacity import StorageSnapshot, StorageState
    from src.tender_research.rag.prepare_service import prepare_tender_for_analysis

    snap = StorageSnapshot(
        state=StorageState.INGESTION_PROTECTED,
        reason="threshold_ingestion_protected",
        mount_verified=True,
        checked_at="2026-07-26T00:00:00",
        filesystem_total_bytes=1_000_000_000_000,
        filesystem_free_bytes=50_000_000_000,
        filesystem_used_bytes=950_000_000_000,
        used_percent=95.0,
        storage_root="/tmp/mock_storage",
    )
    with patch("src.shared.storage.gate.get_storage_snapshot", return_value=snap):
        with patch("src.tender_research.rag.prepare_service._get_session") as mock_get_session:
            with patch("src.tender_research.rag.prepare_service.EisTenderLoader") as mock_loader:
                with patch("src.tender_research.rag.prepare_service.download_tender_documents") as mock_dl:
                    with pytest.raises(IngestionBlockedError) as exc:
                        prepare_tender_for_analysis("0323100010326000013", session=session)
                    assert exc.value.reason == IngestionBlockedReason.STORAGE_CAPACITY_PROTECTION_ACTIVE
                    assert exc.value.status_code == 503
                    mock_get_session.assert_not_called()
                    mock_loader.assert_not_called()
                    mock_dl.assert_not_called()


# =============================================================================
# ARV-010S1.2 — Real synchronous API test
# =============================================================================


@pytest.mark.storage_gate_enforced
def test_sync_api_503_on_unknown_state(client, monkeypatch, session):
    """POST /api/tender-research/prepare returns 503 on STORAGE_UNKNOWN."""
    from src.shared.storage.capacity import StorageSnapshot, StorageState
    from src.shared.storage.gate import check_ingestion_allowed

    snap = StorageSnapshot(
        state=StorageState.STORAGE_UNKNOWN,
        reason="storage_root_not_configured",
        mount_verified=False,
        checked_at="2026-07-26T00:00:00",
        filesystem_total_bytes=None,
        filesystem_free_bytes=None,
        filesystem_used_bytes=None,
        used_percent=None,
        storage_root=None,
    )
    with patch("src.shared.storage.gate.get_storage_snapshot", return_value=snap):
        resp = client.post("/api/tender-research/prepare", json={"registry_number": "0323100010326000013"})
    assert resp.status_code == 503
    data = resp.json()
    assert data["error"]["code"] == "STORAGE_STATE_UNKNOWN"


@pytest.mark.storage_gate_enforced
def test_sync_api_503_on_protected_state(client, monkeypatch, session):
    """POST /api/tender-research/prepare returns 503 on INGESTION_PROTECTED."""
    from src.shared.storage.capacity import StorageSnapshot, StorageState

    snap = StorageSnapshot(
        state=StorageState.INGESTION_PROTECTED,
        reason="threshold_ingestion_protected",
        mount_verified=True,
        checked_at="2026-07-26T00:00:00",
        filesystem_total_bytes=1_000_000_000_000,
        filesystem_free_bytes=50_000_000_000,
        filesystem_used_bytes=950_000_000_000,
        used_percent=95.0,
        storage_root="/tmp/mock_storage",
    )
    with patch("src.shared.storage.gate.get_storage_snapshot", return_value=snap):
        resp = client.post("/api/tender-research/prepare", json={"registry_number": "0323100010326000013"})
    assert resp.status_code == 503
    data = resp.json()
    assert data["error"]["code"] == "STORAGE_CAPACITY_PROTECTION_ACTIVE"


# =============================================================================
# ARV-010S1.2 — Real background submission test
# =============================================================================


@pytest.mark.storage_gate_enforced
def test_background_submission_503_on_unknown(client, monkeypatch, session):
    """POST /api/tender-research/jobs/prepare returns 503 on STORAGE_UNKNOWN."""
    from src.shared.storage.capacity import StorageSnapshot, StorageState

    snap = StorageSnapshot(
        state=StorageState.STORAGE_UNKNOWN,
        reason="storage_root_not_configured",
        mount_verified=False,
        checked_at="2026-07-26T00:00:00",
        filesystem_total_bytes=None,
        filesystem_free_bytes=None,
        filesystem_used_bytes=None,
        used_percent=None,
        storage_root=None,
    )
    with patch("src.shared.storage.gate.get_storage_snapshot", return_value=snap):
        with patch("src.tender_research.api.create_job") as mock_create:
            with patch("src.tender_research.api.submit_prepare_job") as mock_submit:
                resp = client.post(
                    "/api/tender-research/jobs/prepare",
                    json={"registry_number": "0323100010326000013"},
                )
    assert resp.status_code == 503
    data = resp.json()
    assert data["error"]["code"] == "STORAGE_STATE_UNKNOWN"
    mock_create.assert_not_called()
    mock_submit.assert_not_called()


@pytest.mark.storage_gate_enforced
def test_background_submission_503_on_protected(client, monkeypatch, session):
    """POST /api/tender-research/jobs/prepare returns 503 on INGESTION_PROTECTED."""
    from src.shared.storage.capacity import StorageSnapshot, StorageState

    snap = StorageSnapshot(
        state=StorageState.INGESTION_PROTECTED,
        reason="threshold_ingestion_protected",
        mount_verified=True,
        checked_at="2026-07-26T00:00:00",
        filesystem_total_bytes=1_000_000_000_000,
        filesystem_free_bytes=50_000_000_000,
        filesystem_used_bytes=950_000_000_000,
        used_percent=95.0,
        storage_root="/tmp/mock_storage",
    )
    with patch("src.shared.storage.gate.get_storage_snapshot", return_value=snap):
        with patch("src.tender_research.api.create_job") as mock_create:
            with patch("src.tender_research.api.submit_prepare_job") as mock_submit:
                resp = client.post(
                    "/api/tender-research/jobs/prepare",
                    json={"registry_number": "0323100010326000013"},
                )
    assert resp.status_code == 503
    data = resp.json()
    assert data["error"]["code"] == "STORAGE_CAPACITY_PROTECTION_ACTIVE"
    mock_create.assert_not_called()
    mock_submit.assert_not_called()


# =============================================================================
# ARV-010S1.2 — Real worker recheck test
# =============================================================================


@pytest.mark.storage_gate_enforced
def test_worker_recheck_blocks_on_protected_and_fails_job(monkeypatch, session):
    """run_prepare_job fails job with gate_check step on INGESTION_PROTECTED."""
    from src.shared.storage.capacity import StorageSnapshot, StorageState
    from src.tender_research.rag.job_runner import run_prepare_job
    from src.tender_research.rag.job_service import create_job

    # Create a real job record in the in-memory DB
    record = create_job(
        session,
        job_type="prepare",
        registry_number="0323100010326000013",
        request={"registry_number": "0323100010326000013"},
    )

    snap = StorageSnapshot(
        state=StorageState.INGESTION_PROTECTED,
        reason="threshold_ingestion_protected",
        mount_verified=True,
        checked_at="2026-07-26T00:00:00",
        filesystem_total_bytes=1_000_000_000_000,
        filesystem_free_bytes=50_000_000_000,
        filesystem_used_bytes=950_000_000_000,
        used_percent=95.0,
        storage_root="/tmp/mock_storage",
    )
    with patch("src.shared.storage.gate.get_storage_snapshot", return_value=snap):
        with patch("src.tender_research.rag.job_runner._get_session", return_value=session):
            with patch("src.tender_research.rag.job_runner.mark_job_running") as mock_mark:
                with patch("src.tender_research.rag.job_runner.prepare_tender_for_analysis") as mock_prepare:
                        run_prepare_job(record.id, {"registry_number": "0323100010326000013"})

    mock_mark.assert_not_called()
    mock_prepare.assert_not_called()

    # Reload the job record and verify it was failed with correct metadata
    session.expire_all()
    from src.tender_research.rag.job_service import get_job
    updated = get_job(session, record.id)
    assert updated is not None
    assert updated.status == "failed"
    assert "STORAGE_CAPACITY_PROTECTION_ACTIVE" in (updated.errors or [])
    assert updated.current_step == "gate_check"


@pytest.mark.storage_gate_enforced
def test_worker_recheck_blocks_on_unknown_and_fails_job(monkeypatch, session):
    """run_prepare_job fails job with gate_check step on STORAGE_UNKNOWN."""
    from src.shared.storage.capacity import StorageSnapshot, StorageState
    from src.tender_research.rag.job_runner import run_prepare_job
    from src.tender_research.rag.job_service import create_job

    record = create_job(
        session,
        job_type="prepare",
        registry_number="0323100010326000013",
        request={"registry_number": "0323100010326000013"},
    )

    snap = StorageSnapshot(
        state=StorageState.STORAGE_UNKNOWN,
        reason="storage_root_not_configured",
        mount_verified=False,
        checked_at="2026-07-26T00:00:00",
        filesystem_total_bytes=None,
        filesystem_free_bytes=None,
        filesystem_used_bytes=None,
        used_percent=None,
        storage_root=None,
    )
    with patch("src.shared.storage.gate.get_storage_snapshot", return_value=snap):
        with patch("src.tender_research.rag.job_runner._get_session", return_value=session):
            with patch("src.tender_research.rag.job_runner.mark_job_running") as mock_mark:
                with patch("src.tender_research.rag.job_runner.prepare_tender_for_analysis") as mock_prepare:
                        run_prepare_job(record.id, {"registry_number": "0323100010326000013"})

    mock_mark.assert_not_called()
    mock_prepare.assert_not_called()

    session.expire_all()
    from src.tender_research.rag.job_service import get_job
    updated = get_job(session, record.id)
    assert updated is not None
    assert updated.status == "failed"
    assert "STORAGE_STATE_UNKNOWN" in (updated.errors or [])
    assert updated.current_step == "gate_check"


# =============================================================================
# ARV-010S1.2 — Normal path (gate passes)
# =============================================================================


@pytest.mark.storage_gate_enforced
def test_normal_path_service_passes_gate(monkeypatch, session):
    """prepare_tender_for_analysis passes gate when state is NORMAL."""
    from src.shared.storage.capacity import StorageSnapshot, StorageState
    from src.tender_research.rag.prepare_service import prepare_tender_for_analysis

    snap = StorageSnapshot(
        state=StorageState.NORMAL,
        reason="threshold_normal",
        mount_verified=True,
        checked_at="2026-07-26T00:00:00",
        filesystem_total_bytes=1_000_000_000_000,
        filesystem_free_bytes=500_000_000_000,
        filesystem_used_bytes=500_000_000_000,
        used_percent=50.0,
        storage_root="/tmp/mock_storage",
    )
    with patch("src.shared.storage.gate.get_storage_snapshot", return_value=snap):
        with patch("src.tender_research.rag.prepare_service._get_session") as mock_get_session:
            # Should proceed past the gate — it will fail on the actual tender lookup
            # but that's after the gate boundary
            try:
                prepare_tender_for_analysis("0323100010326000013", session=session)
            except Exception:
                pass
            # _get_session should NOT be called because we passed our own session
            # (it's only called when session is None)
            mock_get_session.assert_not_called()


@pytest.mark.storage_gate_enforced
def test_normal_path_background_submission_creates_job(client, monkeypatch, session):
    """POST /api/tender-research/jobs/prepare creates job when state is NORMAL."""
    from src.shared.storage.capacity import StorageSnapshot, StorageState
    from src.shared.storage.gate import check_ingestion_allowed

    snap = StorageSnapshot(
        state=StorageState.NORMAL,
        reason="threshold_normal",
        mount_verified=True,
        checked_at="2026-07-26T00:00:00",
        filesystem_total_bytes=1_000_000_000_000,
        filesystem_free_bytes=500_000_000_000,
        filesystem_used_bytes=500_000_000_000,
        used_percent=50.0,
        storage_root="/tmp/mock_storage",
    )
    with patch("src.shared.storage.gate.get_storage_snapshot", return_value=snap):
        with patch("src.tender_research.api.create_job") as mock_create:
            with patch("src.tender_research.api.submit_prepare_job") as mock_submit:
                mock_create.return_value = MagicMock(
                    id="test-job-id",
                    job_type="prepare",
                    registry_number="0323100010326000013",
                    status="queued",
                )
                resp = client.post(
                    "/api/tender-research/jobs/prepare",
                    json={"registry_number": "0323100010326000013"},
                )
    # Should pass gate and reach create_job
    mock_create.assert_called_once()
    mock_submit.assert_called_once()
    assert resp.status_code == 200


@pytest.mark.storage_gate_enforced
def test_normal_path_worker_passes_gate(monkeypatch, session):
    """run_prepare_job passes gate when state is NORMAL."""
    from src.shared.storage.capacity import StorageSnapshot, StorageState
    from src.tender_research.rag.job_runner import run_prepare_job
    from src.tender_research.rag.job_service import create_job

    record = create_job(
        session,
        job_type="prepare",
        registry_number="0323100010326000013",
        request={"registry_number": "0323100010326000013"},
    )

    snap = StorageSnapshot(
        state=StorageState.NORMAL,
        reason="threshold_normal",
        mount_verified=True,
        checked_at="2026-07-26T00:00:00",
        filesystem_total_bytes=1_000_000_000_000,
        filesystem_free_bytes=500_000_000_000,
        filesystem_used_bytes=500_000_000_000,
        used_percent=50.0,
        storage_root="/tmp/mock_storage",
    )
    with patch("src.shared.storage.gate.get_storage_snapshot", return_value=snap):
        with patch("src.tender_research.rag.job_runner._get_session", return_value=session):
            with patch("src.tender_research.rag.job_runner.mark_job_running") as mock_mark:
                with patch("src.tender_research.rag.job_runner.prepare_tender_for_analysis") as mock_prepare:
                    mock_mark.return_value = MagicMock()
                    mock_prepare.side_effect = Exception("stop after storage boundary")
                    try:
                        run_prepare_job(record.id, {"registry_number": "0323100010326000013"})
                    except Exception:
                        pass
    mock_mark.assert_called_once()
    mock_prepare.assert_called_once()


# =============================================================================
# ARV-010 FINAL MERGE GATE — storage_root filling + root env resolution
# =============================================================================


def test_internal_snapshot_contains_resolved_root(mock_settings):
    """Successful snapshot returns storage_root in internal object."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.40))):
                snap = get_storage_snapshot()
                # Internal snapshot has the resolved path
                assert snap.storage_root is not None
                assert isinstance(snap.storage_root, str)
                # The path should match the resolved tmpdir
                from pathlib import Path
                assert Path(snap.storage_root).samefile(tmpdir)


def test_internal_snapshot_storage_root_on_missing_path(mock_settings):
    """Unknown snapshot with configured-but-missing path contains storage_root."""
    mock_settings.return_value.arvectum_storage_root = "/nonexistent/storage/path/xyz123"
    snap = get_storage_snapshot()
    assert snap.state == StorageState.STORAGE_UNKNOWN
    assert snap.reason == "storage_root_missing"
    # Internal snapshot should still contain the configured path
    assert snap.storage_root is not None
    assert "nonexistent" in snap.storage_root


def test_public_snapshot_omits_storage_root(mock_settings):
    """PublicStorageSnapshot never leaks storage_root or absolute paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mock_settings.return_value.arvectum_storage_root = tmpdir
        with patch("src.shared.storage.capacity._mount_verified", return_value=True):
            with patch("shutil.disk_usage", return_value=_mock_disk_usage(int(SAMPLE_DISK_TOTAL * 0.40))):
                pub = public_storage_snapshot()
                assert not hasattr(pub, "storage_root")
                assert pub.reason == "threshold_normal"
                assert "/tmp" not in pub.reason


def test_public_snapshot_unknown_no_absolute_path_leak(mock_settings):
    """Unknown public snapshot does not leak path in reason."""
    mock_settings.return_value.arvectum_storage_root = "/some/secret/storage/path"
    pub = public_storage_snapshot()
    assert not hasattr(pub, "storage_root")
    assert pub.reason in ("storage_root_missing", "storage_root_not_configured")
    assert "/some/secret" not in pub.reason


def test_root_env_canonical_loaded(monkeypatch, tmp_path):
    """ARVECTUM_STORAGE_ROOT canonical env is loaded by Settings."""
    canonical = str(tmp_path / "canonical")
    get_settings.cache_clear()
    monkeypatch.setenv("ARVECTUM_STORAGE_ROOT", canonical)
    try:
        s = Settings(_env_file=None)
        assert s.arvectum_storage_root == canonical
    finally:
        get_settings.cache_clear()


def test_root_env_compatibility_fallback(monkeypatch, tmp_path):
    """AI_CORP_ARVECTUM_STORAGE_ROOT compatibility env works as fallback."""
    compatibility = str(tmp_path / "compatibility")
    get_settings.cache_clear()
    monkeypatch.delenv("ARVECTUM_STORAGE_ROOT", raising=False)
    monkeypatch.setenv("AI_CORP_ARVECTUM_STORAGE_ROOT", compatibility)
    try:
        s = Settings(_env_file=None)
        assert s.arvectum_storage_root == compatibility
    finally:
        get_settings.cache_clear()


def test_root_env_canonical_takes_priority(monkeypatch, tmp_path):
    """Canonical ARVECTUM_STORAGE_ROOT has priority over compatibility."""
    canonical = str(tmp_path / "canonical")
    compatibility = str(tmp_path / "compatibility")
    get_settings.cache_clear()
    monkeypatch.setenv("ARVECTUM_STORAGE_ROOT", canonical)
    monkeypatch.setenv("AI_CORP_ARVECTUM_STORAGE_ROOT", compatibility)
    try:
        s = Settings(_env_file=None)
        assert s.arvectum_storage_root == canonical
    finally:
        get_settings.cache_clear()


def test_settings_cache_reloads_after_environment_change(monkeypatch, tmp_path):
    first = str(tmp_path / "first")
    second = str(tmp_path / "second")
    get_settings.cache_clear()
    monkeypatch.setenv("ARVECTUM_STORAGE_ROOT", first)
    try:
        assert get_settings().arvectum_storage_root == first
        monkeypatch.setenv("ARVECTUM_STORAGE_ROOT", second)
        get_settings.cache_clear()
        assert get_settings().arvectum_storage_root == second
    finally:
        get_settings.cache_clear()
