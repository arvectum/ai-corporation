from __future__ import annotations

import os
from pathlib import Path

from scripts.arv001 import full_pre_provider_canonical as canonical


def test_exact_approved_gguf_hash_is_accepted(monkeypatch, tmp_path: Path) -> None:
    candidate = tmp_path / "gemma-4-12b-it-qat-q4_0.gguf"
    candidate.write_bytes(b"approved")
    monkeypatch.setattr(canonical, "_sha256", lambda _: canonical._APPROVED_GGUF_SHA256)

    profile, errors = canonical._validate_approved_gguf(candidate)

    assert errors == ()
    assert profile == {"gguf_sha256": canonical._APPROVED_GGUF_SHA256}


def test_unapproved_gguf_hash_is_rejected(monkeypatch, tmp_path: Path) -> None:
    candidate = tmp_path / "gemma-4-12b-it-qat-q4_0.gguf"
    candidate.write_bytes(b"other")
    monkeypatch.setattr(canonical, "_sha256", lambda _: "0" * 64)

    profile, errors = canonical._validate_approved_gguf(candidate)

    assert profile is None
    assert errors == ("approved_gguf_sha256_mismatch",)


def _approved_bundle(monkeypatch) -> None:
    monkeypatch.setattr(
        canonical,
        "canonical_runtime_bundle_tree",
        lambda _: (
            canonical._APPROVED_RUNTIME_BUNDLE_TREE_SHA256,
            canonical._APPROVED_RUNTIME.runtime_bundle_regular_file_count,
            canonical._APPROVED_RUNTIME.runtime_bundle_symlink_count,
        ),
    )


def test_exact_approved_llama_server_and_bundle_are_accepted(
    monkeypatch, tmp_path: Path
) -> None:
    candidate = tmp_path / "llama-server"
    candidate.write_bytes(b"approved-binary")
    os.chmod(candidate, 0o700)
    monkeypatch.setattr(
        canonical,
        "_sha256",
        lambda _: canonical._APPROVED_LLAMA_SERVER_SHA256,
    )
    _approved_bundle(monkeypatch)

    profile, errors = canonical._validate_approved_llama_server(candidate)

    assert errors == ()
    assert profile == {
        "binary_sha256": canonical._APPROVED_LLAMA_SERVER_SHA256,
        "binary_architecture": "arm64",
        "binary_version_sanitized": canonical._APPROVED_LLAMA_SERVER_SHA256,
    }


def test_unapproved_llama_server_hash_is_rejected(
    monkeypatch, tmp_path: Path
) -> None:
    candidate = tmp_path / "llama-server"
    candidate.write_bytes(b"other-binary")
    os.chmod(candidate, 0o700)
    monkeypatch.setattr(canonical, "_sha256", lambda _: "0" * 64)

    profile, errors = canonical._validate_approved_llama_server(candidate)

    assert profile is None
    assert errors == ("llama_server_sha256_mismatch",)


def test_approved_executable_with_substituted_bundle_is_rejected(
    monkeypatch, tmp_path: Path
) -> None:
    candidate = tmp_path / "llama-server"
    candidate.write_bytes(b"approved-binary")
    os.chmod(candidate, 0o700)
    monkeypatch.setattr(
        canonical,
        "_sha256",
        lambda _: canonical._APPROVED_LLAMA_SERVER_SHA256,
    )
    monkeypatch.setattr(
        canonical,
        "canonical_runtime_bundle_tree",
        lambda _: (
            "0" * 64,
            canonical._APPROVED_RUNTIME.runtime_bundle_regular_file_count,
            canonical._APPROVED_RUNTIME.runtime_bundle_symlink_count,
        ),
    )

    profile, errors = canonical._validate_approved_llama_server(candidate)

    assert profile is None
    assert errors == ("llama_runtime_bundle_sha256_mismatch",)


def test_non_executable_llama_server_is_rejected(tmp_path: Path) -> None:
    candidate = tmp_path / "llama-server"
    candidate.write_bytes(b"binary")
    os.chmod(candidate, 0o600)

    profile, errors = canonical._validate_approved_llama_server(candidate)

    assert profile is None
    assert errors == ("llama_server_not_executable",)


def test_exact_runtime_pair_defers_binary_validation(monkeypatch, tmp_path: Path) -> None:
    gguf = tmp_path / "model.gguf"
    binary = tmp_path / "llama-server"
    gguf.write_bytes(b"approved")
    binary.write_bytes(b"binary")
    monkeypatch.setattr(
        canonical,
        "_validate_approved_gguf",
        lambda _: ({"gguf_sha256": canonical._APPROVED_GGUF_SHA256}, ()),
    )

    assets, errors = canonical._locate_exact_runtime_assets(
        (),
        gguf_path=gguf,
        llama_server_path=binary,
    )

    assert errors == ()
    assert assets == (binary, gguf)


def test_exact_runtime_pair_rejects_unapproved_gguf(monkeypatch, tmp_path: Path) -> None:
    gguf = tmp_path / "model.gguf"
    binary = tmp_path / "llama-server"
    gguf.write_bytes(b"other")
    binary.write_bytes(b"binary")
    monkeypatch.setattr(
        canonical,
        "_validate_approved_gguf",
        lambda _: (None, ("approved_gguf_sha256_mismatch",)),
    )

    assets, errors = canonical._locate_exact_runtime_assets(
        (),
        gguf_path=gguf,
        llama_server_path=binary,
    )

    assert assets is None
    assert errors == ("approved_gguf_sha256_mismatch",)
