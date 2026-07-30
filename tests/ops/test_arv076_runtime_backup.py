from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "arv076_runtime_backup.py"
SPEC = importlib.util.spec_from_file_location("arv076_runtime_backup", MODULE_PATH)
assert SPEC and SPEC.loader
runtime_backup = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime_backup
SPEC.loader.exec_module(runtime_backup)


def test_redact_nested_yaml_and_environment_assignments() -> None:
    source = {
        "services": {
            "api": {
                "environment": [
                    "POSTGRES_PASSWORD=plain-text",
                    "OPENAI_API_KEY=sk-example",
                    "SAFE_VALUE=visible",
                    "DATABASE_URL=postgresql://user:password@db/app",
                ],
                "labels": {"token": "never-store", "owner": "ops"},
            }
        }
    }

    redacted = runtime_backup.redact(source)

    environment = redacted["services"]["api"]["environment"]
    assert "POSTGRES_PASSWORD=<redacted>" in environment
    assert "OPENAI_API_KEY=<redacted>" in environment
    assert "SAFE_VALUE=visible" in environment
    assert "DATABASE_URL=<redacted>" in environment
    assert redacted["services"]["api"]["labels"]["token"] == "<redacted>"
    assert redacted["services"]["api"]["labels"]["owner"] == "ops"


def test_manifest_verify_detects_tampering_and_unlisted_artifacts(tmp_path: Path) -> None:
    backup_set = tmp_path / "20260730T120000Z-deadbeef"
    backup_set.mkdir()
    (backup_set / "metadata").mkdir()
    (backup_set / "metadata" / "inventory.json").write_text("{}\n", encoding="utf-8")
    (backup_set / runtime_backup.BACKUP_COMPLETE).write_text("ok\n", encoding="utf-8")

    manifest = runtime_backup.build_manifest(
        backup_set,
        backup_id=backup_set.name,
        created_at="2026-07-30T12:00:00Z",
    )
    runtime_backup.write_manifest(backup_set, manifest)
    evidence = runtime_backup.verify_backup_set(backup_set)
    assert evidence["verified"] is True

    (backup_set / "metadata" / "inventory.json").write_text('{"changed": true}\n', encoding="utf-8")
    with pytest.raises(runtime_backup.BackupError, match="verification failed"):
        runtime_backup.verify_backup_set(backup_set)

    (backup_set / "metadata" / "inventory.json").write_text("{}\n", encoding="utf-8")
    (backup_set / "extra.txt").write_text("not listed\n", encoding="utf-8")
    with pytest.raises(runtime_backup.BackupError, match="unlisted_artifact"):
        runtime_backup.verify_backup_set(backup_set)


def test_manifest_rejects_incomplete_backup(tmp_path: Path) -> None:
    backup_set = tmp_path / "20260730T120000Z-deadbeef"
    backup_set.mkdir()
    (backup_set / "artifact.txt").write_text("data", encoding="utf-8")
    manifest = runtime_backup.build_manifest(
        backup_set,
        backup_id=backup_set.name,
        created_at="2026-07-30T12:00:00Z",
    )
    runtime_backup.write_manifest(backup_set, manifest)

    with pytest.raises(runtime_backup.BackupError, match="missing BACKUP_COMPLETE"):
        runtime_backup.verify_backup_set(backup_set)


def test_destination_rejects_colima_subtree_and_repository_subtree(tmp_path: Path) -> None:
    colima = tmp_path / "working" / "colima"
    repo = tmp_path / "repo"
    colima.mkdir(parents=True)
    repo.mkdir()

    with pytest.raises(runtime_backup.BackupError, match="independent"):
        runtime_backup.assert_independent_destination(
            colima / "backup",
            source_colima_home=colima,
            repository_root=repo,
            require_different_device=False,
        )

    with pytest.raises(runtime_backup.BackupError, match="Git repository"):
        runtime_backup.assert_independent_destination(
            repo / "output" / "backup",
            source_colima_home=colima,
            repository_root=repo,
            require_different_device=False,
        )


def test_parse_volume_container_requires_explicit_mapping() -> None:
    assert runtime_backup.parse_volume_container(["pgdata=postgres", "cache=redis"]) == {
        "pgdata": "postgres",
        "cache": "redis",
    }
    with pytest.raises(runtime_backup.BackupError, match="VOLUME=CONTAINER"):
        runtime_backup.parse_volume_container(["broken"])


def test_retention_keep_uses_daily_weekly_monthly_buckets() -> None:
    base = dt.datetime(2026, 7, 30, 12, tzinfo=dt.UTC)
    backups = []
    for days_ago in range(45):
        timestamp = base - dt.timedelta(days=days_ago)
        path = Path(timestamp.strftime("%Y%m%dT120000Z") + f"-{days_ago:08x}")
        backups.append((path, timestamp))

    kept = runtime_backup.retention_keep(backups, daily=7, weekly=4, monthly=2)

    assert backups[0][0] in kept
    kept_dates = {timestamp.date() for path, timestamp in backups if path in kept}
    assert len({date for date in kept_dates if date >= (base - dt.timedelta(days=6)).date()}) == 7
    assert any(timestamp.month == 6 for path, timestamp in backups if path in kept)


def test_verify_evidence_is_json_serializable(tmp_path: Path) -> None:
    backup_set = tmp_path / "20260730T120000Z-deadbeef"
    backup_set.mkdir()
    (backup_set / "artifact.txt").write_text("data", encoding="utf-8")
    (backup_set / runtime_backup.BACKUP_COMPLETE).write_text("ok\n", encoding="utf-8")
    manifest = runtime_backup.build_manifest(
        backup_set,
        backup_id=backup_set.name,
        created_at="2026-07-30T12:00:00Z",
    )
    runtime_backup.write_manifest(backup_set, manifest)

    json.dumps(runtime_backup.verify_backup_set(backup_set))


def test_redact_credential_uri_even_under_generic_key() -> None:
    assert runtime_backup.redact({"endpoint": "postgresql://user:password@db/app"}) == {
        "endpoint": "<redacted-uri>"
    }


def test_select_keys_drops_labels_and_other_unapproved_metadata() -> None:
    rows = [{"ID": "abc", "Names": "db", "Labels": "secret_token=value", "Image": "postgres"}]
    assert runtime_backup.select_keys(rows, {"ID", "Names", "Image"}) == [
        {"ID": "abc", "Image": "postgres", "Names": "db"}
    ]


def test_compare_fingerprints_extension_version_differences_accepted() -> None:
    base = {
        "database": "testdb",
        "alembic_version": "001",
        "extensions": [
            {"name": "plpgsql", "version": "1.0"},
            {"name": "vector", "version": "0.8.5"},
        ],
        "tables": [{"schema": "public", "table": "items", "row_count": 10}],
    }
    same_versions = dict(base)
    assert runtime_backup.compare_fingerprints(base, same_versions) == []

    newer_image = {
        **base,
        "extensions": [
            {"name": "plpgsql", "version": "1.0"},
            {"name": "vector", "version": "0.8.6"},
        ],
    }
    assert runtime_backup.compare_fingerprints(base, newer_image) == []

    with_extra = {
        **base,
        "extensions": [
            {"name": "plpgsql", "version": "1.0"},
            {"name": "vector", "version": "0.8.6"},
            {"name": "uuid-ossp", "version": "1.4"},
        ],
    }
    assert runtime_backup.compare_fingerprints(base, with_extra) == []

    missing_ext = {
        **base,
        "extensions": [{"name": "plpgsql", "version": "1.0"}],
    }
    diffs = runtime_backup.compare_fingerprints(base, missing_ext)
    assert len(diffs) == 1
    assert diffs[0].startswith("extensions:")
    assert "vector" in diffs[0]


def test_compare_fingerprints_detects_table_and_alembic_differences() -> None:
    base = {
        "database": "testdb",
        "alembic_version": "001",
        "extensions": [{"name": "plpgsql", "version": "1.0"}],
        "tables": [{"schema": "public", "table": "items", "row_count": 10}],
    }
    diff_db = {**base, "database": "otherdb"}
    assert runtime_backup.compare_fingerprints(base, diff_db) == ["database"]

    diff_alembic = {**base, "alembic_version": "002"}
    assert runtime_backup.compare_fingerprints(base, diff_alembic) == ["alembic_version"]

    diff_table = {**base, "tables": [{"schema": "public", "table": "items", "row_count": 99}]}
    assert runtime_backup.compare_fingerprints(base, diff_table) == ["tables"]
