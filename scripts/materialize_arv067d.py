#!/usr/bin/env python3
"""Materialize ARV-067D assets from a one-shot checked payload."""
from __future__ import annotations

import base64
import hashlib
import io
import tarfile
from pathlib import Path

ARCHIVE_SHA256 = "c88586eaacd0c9a1789de86a196edcfb8440052735c03d211d1bcc9f19c93e3b"


def main() -> int:
    root = Path.cwd().resolve()
    chunk_dir = root / "scripts" / "arv067d_payload"
    encoded = "".join(path.read_text(encoding="utf-8") for path in sorted(chunk_dir.glob("*.txt")))
    payload = base64.b64decode(encoded.encode("ascii"), validate=True)
    actual = hashlib.sha256(payload).hexdigest()
    if actual != ARCHIVE_SHA256:
        raise RuntimeError(f"archive checksum mismatch: {actual}")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            target = (root / member.name).resolve()
            if root not in target.parents:
                raise RuntimeError(f"unsafe archive path: {member.name}")
            if not member.isfile():
                raise RuntimeError(f"unexpected archive member: {member.name}")
        archive.extractall(root, members=members, filter="data")
    print(f"ARV-067D materialized {len(members)} files; archive_sha256={actual}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
