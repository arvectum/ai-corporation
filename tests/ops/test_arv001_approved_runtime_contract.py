from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.arv001.approved_runtime import (
    ApprovedRuntimeContractError,
    canonical_runtime_bundle_tree,
    load_approved_runtime_contract,
)


def test_repository_runtime_contract_is_closed_and_attested() -> None:
    contract = load_approved_runtime_contract()

    assert contract.provider == "openai_compatible"
    assert contract.llama_cpp_release_tag == "b10240"
    assert contract.llama_cpp_commit == "0b14b87d7c20cb753b94b96854dd7b45306fc696"
    assert contract.llama_cpp_build == "10240"
    assert contract.github_release_id == 364164655
    assert contract.github_asset_id == 499974413
    assert contract.archive_sha256 == (
        "771a9a9bc7c9c62d5a3e891f2b64c89bca4850a7c9aeaf0b0e3f26be216fd8c7"
    )
    assert contract.llama_server_sha256 == (
        "ff0e2445d93e2d6305c44cce6386db1020385194261dc184deaf0f37c7148d85"
    )
    assert contract.runtime_bundle_tree_sha256 == (
        "9fb4db02070f78327f8f57ee40a906ef7d13d13444cf0c491aec4ba22b413740"
    )
    assert contract.runtime_bundle_regular_file_count == 43
    assert contract.runtime_bundle_symlink_count == 18


def test_runtime_bundle_tree_matches_closed_canonical_serialization(
    tmp_path: Path,
) -> None:
    server = tmp_path / "llama-server"
    library = tmp_path / "libggml.dylib"
    server.write_bytes(b"server")
    library.write_bytes(b"library")
    os.chmod(server, 0o755)
    os.chmod(library, 0o644)
    (tmp_path / "libggml-current.dylib").symlink_to("libggml.dylib")

    digest, regular_files, symlinks = canonical_runtime_bundle_tree(tmp_path)

    records = [
        {
            "mode": "0644",
            "path": "libggml.dylib",
            "sha256": hashlib.sha256(b"library").hexdigest(),
            "size_bytes": 7,
            "type": "file",
        },
        {
            "path": "libggml-current.dylib",
            "target": "libggml.dylib",
            "type": "symlink",
        },
        {
            "mode": "0755",
            "path": "llama-server",
            "sha256": hashlib.sha256(b"server").hexdigest(),
            "size_bytes": 6,
            "type": "file",
        },
    ]
    records.sort(key=lambda item: item["path"])
    expected = hashlib.sha256(
        json.dumps(
            records,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    assert digest == expected
    assert regular_files == 2
    assert symlinks == 1


def test_runtime_bundle_tree_rejects_escaping_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-runtime-file"
    outside.write_bytes(b"outside")
    (tmp_path / "unsafe").symlink_to(outside)

    with pytest.raises(
        ApprovedRuntimeContractError,
        match="runtime_bundle_symlink_invalid",
    ):
        canonical_runtime_bundle_tree(tmp_path)
