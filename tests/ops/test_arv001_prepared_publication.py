from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts.arv001.prepared_publication import (
    PreparedPublicationError,
    publish_prepared_state,
    scan_public_values,
)


def _staging(tmp_path: Path) -> tuple[Path, Path]:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    os.chmod(private, 0o700)
    staging = private / ".prepared-state.partial.test"
    staging.mkdir(mode=0o700)
    (staging / "prepared.sqlite3").write_bytes(b"sqlite-state")
    (staging / "runtime-profile.json").write_text(
        json.dumps(
            {
                "version": "arv001-runtime-v1",
                "provider": "openai_compatible",
                "provider_generation_calls": 0,
            }
        ),
        encoding="utf-8",
    )
    (staging / "prepared-verification.json").write_text(
        json.dumps({"schema_version": "private-v1"}), encoding="utf-8"
    )
    application = staging / "application-data"
    snapshot = application / "customer-pilot" / "c" / "p" / "case" / "run" / "analysis"
    snapshot.mkdir(parents=True)
    (snapshot / "requirements.json").write_text("{}", encoding="utf-8")
    (snapshot / "canonical_report.json").write_text("{}", encoding="utf-8")
    (snapshot / "canonical-binding.manifest.json").write_text("{}", encoding="utf-8")
    return staging, private / "prepared-state"


def _manifest() -> dict[str, object]:
    return {
        "schema_version": "arv001-prepared-state-v1",
        "head_sha": "a" * 40,
        "corpus_sha256": "b" * 64,
        "policy_sha256": "c" * 64,
        "binary_sha256": "d" * 64,
        "gguf_sha256": "e" * 64,
        "tokenizer_identity_sha256": "f" * 64,
        "database_sha256": "placeholder-overwritten-by-test-fixture",
        "physical_document_count": 10,
        "logical_document_count": 6,
        "extracted_document_count": 10,
        "chunk_count": 233,
        "snapshot_binding_verified": True,
        "source_graph_binding_verified": True,
        "gate5_ready": True,
        "controlled_preflight_verified": True,
        "controlled_preflight_invocations": 1,
        "controlled_provider_invocations": 0,
        "provider_generation_calls": 0,
        "created_at": "2026-08-03T00:00:00+00:00",
    }


def _result() -> dict[str, object]:
    return {
        "schema_version": "arv001-full-pre-provider-v1",
        "status": "PASS",
        "head_sha": "a" * 40,
        "phases": [
            {
                "phase": "prepared_state_persistence",
                "status": "PASS",
                "reason_codes": [],
            },
            {"phase": "privacy_scan", "status": "PASS", "reason_codes": []},
            {"phase": "cleanup", "status": "PASS", "reason_codes": []},
        ],
        "counters": {
            "controlled_preflight_invocations": 1,
            "controlled_provider_invocations": 0,
            "provider_generation_calls": 0,
            "production_db_mutations": 0,
            "old_arv003_mutations": 0,
            "git_data_leaks": 0,
        },
        "acceptance": {"application_prepared": True},
    }


def test_transactional_publication_preserves_exact_result_and_modes(
    tmp_path: Path,
) -> None:
    staging, final = _staging(tmp_path)
    manifest = _manifest()
    import hashlib

    manifest["database_sha256"] = hashlib.sha256(b"sqlite-state").hexdigest()
    result = _result()

    published = publish_prepared_state(
        staging=staging,
        final=final,
        base_manifest=manifest,
        result=result,
    )

    assert published.result == result
    assert not staging.exists()
    assert final.stat().st_mode & 0o777 == 0o700
    assert (
        json.loads(
            (final / "sanitized-acceptance-result.json").read_text(encoding="utf-8")
        )
        == result
    )
    assert {item.name for item in final.iterdir()} == {
        "prepared.sqlite3",
        "application-data",
        "runtime-profile.json",
        "prepared-verification.json",
        "prepared-state-manifest.json",
        "sanitized-acceptance-result.json",
    }
    for path in final.rglob("*"):
        assert not path.is_symlink()
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)


def test_existing_final_is_never_overwritten(tmp_path: Path) -> None:
    staging, final = _staging(tmp_path)
    final.mkdir()
    marker = final / "marker"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(PreparedPublicationError) as raised:
        publish_prepared_state(
            staging=staging,
            final=final,
            base_manifest=_manifest(),
            result=_result(),
        )

    assert raised.value.code == "prepared_final_already_exists"
    assert marker.read_text(encoding="utf-8") == "keep"


def test_post_rename_tamper_removes_canonical_final(tmp_path: Path) -> None:
    staging, final = _staging(tmp_path)
    manifest = _manifest()
    import hashlib

    manifest["database_sha256"] = hashlib.sha256(b"sqlite-state").hexdigest()

    def fault(stage: str) -> None:
        if stage == "after_rename":
            (final / "runtime-profile.json").write_text("tampered", encoding="utf-8")

    with pytest.raises(PreparedPublicationError) as raised:
        publish_prepared_state(
            staging=staging,
            final=final,
            base_manifest=manifest,
            result=_result(),
            fault=fault,
        )

    assert raised.value.code == "prepared_post_rename_hash_mismatch"
    assert not final.exists()
    assert any(
        item.name.startswith(".prepared-state.quarantine.")
        for item in final.parent.iterdir()
    )


@pytest.mark.parametrize(
    ("stage", "code"),
    [
        ("before_manifest_write", "prepared_manifest_write_failed"),
        ("before_result_write", "prepared_result_write_failed"),
        ("before_rename", "prepared_rename_failed"),
        ("before_post_verify", "prepared_post_rename_hash_mismatch"),
    ],
)
def test_failure_injection_leaves_no_canonical_state(
    tmp_path: Path, stage: str, code: str
) -> None:
    staging, final = _staging(tmp_path)
    manifest = _manifest()
    import hashlib

    manifest["database_sha256"] = hashlib.sha256(b"sqlite-state").hexdigest()

    def fault(actual: str) -> None:
        if actual == stage:
            raise OSError("injected")

    with pytest.raises(PreparedPublicationError) as raised:
        publish_prepared_state(
            staging=staging,
            final=final,
            base_manifest=manifest,
            result=_result(),
            fault=fault,
        )

    assert raised.value.code == code
    assert not final.exists()
    assert not staging.exists()


def test_symlink_and_sqlite_sidecars_fail_closed(tmp_path: Path) -> None:
    staging, final = _staging(tmp_path)
    (staging / "prepared.sqlite3-wal").write_bytes(b"wal")
    with pytest.raises(PreparedPublicationError) as raised:
        publish_prepared_state(
            staging=staging,
            final=final,
            base_manifest=_manifest(),
            result=_result(),
        )
    assert raised.value.code == "prepared_input_file_set_invalid"


def test_privacy_gate_rejects_paths_credentials_uuid_and_registry() -> None:
    values = [
        "/Users/master/private",
        "Bearer abc.def",
        "11111111-1111-4111-8111-111111111111",
        "0388100001826000047",
    ]
    for value in values:
        with pytest.raises(
            PreparedPublicationError, match="prepared_privacy_violation"
        ):
            scan_public_values([value])


def test_privacy_gate_accepts_closed_sanitized_values() -> None:
    scan_public_values([_result(), {"sha256": "a" * 64, "status": "PASS"}])
