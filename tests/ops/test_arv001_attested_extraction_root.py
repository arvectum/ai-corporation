from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from scripts.arv001.approved_runtime import canonical_runtime_bundle_tree


def _expected_digest(extraction_root: Path) -> str:
    prefix = "llama-b10240"
    records = [
        {
            "mode": "0644",
            "path": f"{prefix}/libggml.dylib",
            "sha256": hashlib.sha256(b"library").hexdigest(),
            "size_bytes": 7,
            "type": "file",
        },
        {
            "path": f"{prefix}/libggml-current.dylib",
            "target": "libggml.dylib",
            "type": "symlink",
        },
        {
            "mode": "0755",
            "path": f"{prefix}/llama-server",
            "sha256": hashlib.sha256(b"server").hexdigest(),
            "size_bytes": 6,
            "type": "file",
        },
    ]
    records.sort(key=lambda item: item["path"])
    payload = json.dumps(
        records,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_bundle_directory_is_normalized_to_attested_extraction_root(
    tmp_path: Path,
) -> None:
    extraction_root = tmp_path / "extract"
    bundle = extraction_root / "llama-b10240"
    bundle.mkdir(parents=True)
    server = bundle / "llama-server"
    library = bundle / "libggml.dylib"
    server.write_bytes(b"server")
    library.write_bytes(b"library")
    os.chmod(server, 0o755)
    os.chmod(library, 0o644)
    (bundle / "libggml-current.dylib").symlink_to("libggml.dylib")

    from_extraction_root = canonical_runtime_bundle_tree(extraction_root)
    from_bundle_directory = canonical_runtime_bundle_tree(bundle)

    assert from_extraction_root == from_bundle_directory
    assert from_bundle_directory == (_expected_digest(extraction_root), 2, 1)


def test_non_official_directory_is_not_rebased(tmp_path: Path) -> None:
    bundle = tmp_path / "renamed-bundle"
    bundle.mkdir()
    server = bundle / "llama-server"
    server.write_bytes(b"server")
    os.chmod(server, 0o755)

    digest, regular_files, symlinks = canonical_runtime_bundle_tree(bundle)

    records = [
        {
            "mode": "0755",
            "path": "llama-server",
            "sha256": hashlib.sha256(b"server").hexdigest(),
            "size_bytes": 6,
            "type": "file",
        }
    ]
    expected = hashlib.sha256(
        json.dumps(
            records,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert (digest, regular_files, symlinks) == (expected, 1, 0)
