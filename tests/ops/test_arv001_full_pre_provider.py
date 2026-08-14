from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.arv001.full_pre_provider import (
    _check_protected_drift,
    _copy_snapshot,
    _failure,
    _PhaseRecorder,
    _prepare_payload_error,
    _private_staging_root,
    _result,
    _verify_prepared_database,
    _write_prepared_state_manifest,
)


def _payload() -> dict[str, object]:
    return {
        "status": "application_prepared",
        "marker": "ARV-001_APPLICATION_PREPARED",
        "head_sha": "a" * 40,
        "physical_file_count": 10,
        "logical_document_count": 6,
        "mapped_file_count": 10,
        "extracted_document_count": 10,
        "prepared_chunk_count": 233,
        "post_persistence_gate5_ready": True,
        "controlled_preflight_invocations": 1,
        "controlled_provider_invocations": 0,
        "provider_generation_calls": 0,
        "production_db_mutations": 0,
        "old_arv003_mutations": 0,
        "git_mutations": 0,
    }


def test_prepare_payload_requires_all_exact_acceptance_values() -> None:
    assert _prepare_payload_error(_payload(), "a" * 40) is None


def test_prepare_payload_rejects_wrong_gate_and_unexpected_field() -> None:
    bad = _payload()
    bad["post_persistence_gate5_ready"] = False
    assert _prepare_payload_error(bad, "a" * 40) == "child_post_persistence_gate5_ready_invalid"
    bad = _payload()
    bad["extra"] = "not allowed"
    assert _prepare_payload_error(bad, "a" * 40) == "child_payload_schema_invalid"


def test_prepared_state_manifest_is_atomic_and_does_not_overwrite(tmp_path) -> None:
    (tmp_path / "prepared.sqlite3").write_bytes(b"sqlite")
    assert _write_prepared_state_manifest(tmp_path, _payload())
    assert not _write_prepared_state_manifest(tmp_path, _payload())
    assert (tmp_path / "prepared-state-manifest.json").is_file()


def test_closed_failure_result_marks_only_actual_failed_phase() -> None:
    recorder = _PhaseRecorder()
    recorder.passed("repository")
    result = _failure(
        head_sha="a" * 40,
        phase="gguf_validation",
        code="approved_gguf_validation_failed",
        recorder=recorder,
    )

    assert set(result) == {"schema_version", "status", "head_sha", "phases", "counters", "acceptance"}
    assert result["phases"][0]["status"] == "PASS"
    assert result["phases"][3] == {
        "phase": "gguf_validation", "status": "FAIL", "reason_codes": ["approved_gguf_validation_failed"],
    }
    assert result["phases"][4]["status"] == "SKIPPED_DEPENDENCY"


def test_closed_pass_result_requires_explicit_phase_completion() -> None:
    recorder = _PhaseRecorder()
    recorder.passed("repository")
    result = _result(head_sha="a" * 40, recorder=recorder, status="PASS")
    assert result["status"] == "PASS"
    assert result["phases"][1]["status"] == "SKIPPED_DEPENDENCY"


def test_private_staging_is_outside_repository_and_no_overwrite(tmp_path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    private = tmp_path / "private"
    staging, final = _private_staging_root(private, repository)
    assert staging is not None and final is not None
    assert staging.stat().st_mode & 0o777 == 0o700
    staging.rmdir()
    final.mkdir()
    blocked, _ = _private_staging_root(private, repository)
    assert blocked is None
    assert _private_staging_root(repository / "inside", repository) == (None, None)


def test_verify_prepared_database_requires_real_sqlite_shape(tmp_path) -> None:
    database = tmp_path / "prepared.sqlite3"
    database.write_bytes(b"not sqlite")
    # Passing dummy descriptor and data_dir
    assert not _verify_prepared_database(database, MagicMock(), tmp_path)


def test_check_protected_drift_detects_changes() -> None:
    with patch("subprocess.run") as mock_run:
        # Mock successful (no drift) result for most paths
        mock_run.return_value = MagicMock(returncode=0)

        drift, migration_drift = _check_protected_drift(Path("/repo"), "old", "new")
        assert not drift
        assert not migration_drift

        # Mock drift in document_ingestion
        def side_effect(cmd, **kwargs):
            if "src/modules/document_ingestion/" in cmd:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        mock_run.side_effect = side_effect
        drift, migration_drift = _check_protected_drift(Path("/repo"), "old", "new")
        assert drift
        assert not migration_drift

        # Mock migration drift
        def migration_side_effect(cmd, **kwargs):
            if "migrations/" in cmd:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0)

        mock_run.side_effect = migration_side_effect
        drift, migration_drift = _check_protected_drift(Path("/repo"), "old", "new")
        assert drift
        assert migration_drift


def test_copy_snapshot_verifies_sha(tmp_path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "prepared.sqlite3").write_bytes(b"database content")

    staging = tmp_path / "staging"
    staging.mkdir()

    import hashlib
    expected_sha = hashlib.sha256(b"database content").hexdigest()

    assert _copy_snapshot(source, staging, expected_sha)
    assert (staging / "prepared.sqlite3").read_bytes() == b"database content"

    assert not _copy_snapshot(source, staging, "wrong sha")
