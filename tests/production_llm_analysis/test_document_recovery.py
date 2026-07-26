import struct
import zipfile
from pathlib import Path

import pytest

from src.modules.production_llm_analysis.document_recovery import (
    DocumentRecoveryRequest,
    _publish,
    _safe_entry,
    inspect_zip,
    validate_data_root,
)


def make_zip(path: Path, entries: list[tuple[str, bytes]]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return path


def test_inspect_zip_reports_exact_hash_and_safe_inventory(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "safe.zip", [("one.xml", b"<one/>"), ("two.xml", b"<two/>")]
    )

    inventory, matches = inspect_zip(archive)

    assert inventory.crc_valid
    assert inventory.unsafe_entries == 0
    assert inventory.regular_file_count == 2
    assert len(matches) == 2


@pytest.mark.parametrize("name", ["../escape.xml", "/absolute.xml", "C:/drive.xml"])
def test_inspect_zip_rejects_unsafe_paths(tmp_path: Path, name: str) -> None:
    archive = make_zip(tmp_path / "unsafe.zip", [(name, b"content")])

    inventory, matches = inspect_zip(archive)

    assert inventory.unsafe_entries > 0
    assert not matches


def test_inspect_zip_rejects_symlink_entry(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("link.xml")
    info.external_attr = (0o120777 << 16) | 0xA0000000
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(info, b"target")

    inventory, matches = inspect_zip(archive)

    assert inventory.symlink_entries == 1
    assert inventory.unsafe_entries > 0
    assert not matches


def test_inspect_zip_rejects_encrypted_entry(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "encrypted.zip", [("doc.xml", b"content")])
    data = bytearray(archive.read_bytes())
    local = data.index(b"PK\x03\x04")
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<H", data, local + 6, 1)
    struct.pack_into("<H", data, central + 8, 1)
    archive.write_bytes(data)

    inventory, _ = inspect_zip(archive)

    assert inventory.encrypted_entries == 1
    assert inventory.unsafe_entries > 0


def test_inspect_zip_rejects_duplicate_normalized_paths(tmp_path: Path) -> None:
    archive = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.xml", b"one")
        zf.writestr("a.xml", b"two")

    inventory, _ = inspect_zip(archive)

    assert inventory.duplicate_paths == 1
    assert inventory.unsafe_entries > 0


def test_validate_data_root_rejects_tmp_and_backup_overlap(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        validate_data_root(Path("/tmp/r10-data"), tmp_path / "backup")
    with pytest.raises(ValueError):
        validate_data_root(tmp_path / "data", tmp_path / "data")


def test_validate_data_root_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real-data"
    target.mkdir()
    link = tmp_path / "linked-data"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ValueError, match="non-symlink"):
        validate_data_root(link, tmp_path / "backup")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("doc.xml", True),
        ("nested/doc.xml", True),
        ("../doc.xml", False),
        ("/doc.xml", False),
        ("C:/doc.xml", False),
        ("bad\x00.xml", False),
    ],
)
def test_safe_entry_policy(name: str, expected: bool) -> None:
    assert _safe_entry(name) is expected


def test_inspect_zip_rejects_compression_bomb(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "bomb.zip", [("bomb.xml", b"0" * 500_000)])

    inventory, _ = inspect_zip(archive)

    assert inventory.unsafe_entries > 0


def test_inspect_zip_counts_nested_archive(tmp_path: Path) -> None:
    nested = make_zip(tmp_path / "nested.zip", [("inner.xml", b"<inner/>")])
    archive = tmp_path / "outer.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(nested, "bundle.zip")

    inventory, _ = inspect_zip(archive)

    assert inventory.nested_zip_count == 1
    assert inventory.nested_regular_file_count == 1


def test_inspect_zip_rejects_nested_nested_archive(tmp_path: Path) -> None:
    inner = make_zip(tmp_path / "inner.zip", [("doc.xml", b"doc")])
    nested = tmp_path / "nested.zip"
    with zipfile.ZipFile(nested, "w") as zf:
        zf.write(inner, "inner.zip")
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(nested, "bundle.zip")

    inventory, matches = inspect_zip(outer)

    assert inventory.nested_zip_count == 1
    assert inventory.unsafe_entries > 0
    assert not matches


def test_inspect_zip_rejects_crc_failure(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "crc.zip", [("doc.xml", b"content")])
    data = bytearray(archive.read_bytes())
    with zipfile.ZipFile(archive, "r") as zf:
        info = zf.infolist()[0]
    data[info.header_offset + 30 + len(info.filename)] ^= 0xFF
    archive.write_bytes(data)

    inventory, _ = inspect_zip(archive)

    assert inventory.crc_valid is False


def test_publish_uses_deterministic_replacement(tmp_path: Path) -> None:
    source_stage = tmp_path / "source-stage"
    text_stage = tmp_path / "text-stage"
    source_final = tmp_path / "root" / "source" / "sha.xml"
    text_final = tmp_path / "root" / "extracted" / "sha.txt"
    source_stage.write_bytes(b"xml")
    text_stage.write_text("text", encoding="utf-8")

    _publish(source_stage, text_stage, source_final, text_final)

    assert source_final.read_bytes() == b"xml"
    assert text_final.read_text(encoding="utf-8") == "text"
    assert not source_stage.exists()
    assert not text_stage.exists()


def test_recovery_request_defaults_to_dry_run(tmp_path: Path) -> None:
    request = DocumentRecoveryRequest(
        "registry", tmp_path / "env", tmp_path / "data", tmp_path / "backup"
    )

    assert request.apply is False
    assert request.build_chunks is False


def test_recovery_request_keeps_apply_explicit(tmp_path: Path) -> None:
    request = DocumentRecoveryRequest(
        "registry",
        tmp_path / "env",
        tmp_path / "data",
        tmp_path / "backup",
        apply=True,
    )

    assert request.apply is True
    assert request.build_chunks is False


def test_build_chunks_requires_apply(tmp_path: Path) -> None:
    from src.modules.production_llm_analysis.document_recovery import (
        recover_procurement_documents,
    )

    request = DocumentRecoveryRequest(
        "registry",
        tmp_path / "env",
        tmp_path / "data",
        tmp_path / "backup",
        build_chunks=True,
    )
    with pytest.raises(ValueError, match="requires --apply"):
        recover_procurement_documents(request)
