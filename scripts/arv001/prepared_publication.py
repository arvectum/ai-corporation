"""Transactional private publication for verified ARV-001 prepared state."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

_REQUIRED_INPUTS = {
    "prepared.sqlite3",
    "application-data",
    "runtime-profile.json",
    "prepared-verification.json",
}
_EXACT_FINAL_SET = _REQUIRED_INPUTS | {
    "prepared-state-manifest.json",
    "sanitized-acceptance-result.json",
}
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_PRIVATE_PATTERNS = (
    re.compile(r"/(?:Users|home)/[^\s\"']+"),
    re.compile(r"[A-Za-z]:\\[^\s\"']+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+\-/=]+"),
    re.compile(r"(?i)(?:api[_-]?key|authorization|credential|password|secret)\s*[:=]"),
    re.compile(r"(?i)(?:postgres(?:ql)?|sqlite(?:\+[^:]*)?)://"),
    re.compile(r"(?i)https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?"),
)
_ACCEPTED_REGISTRY_NUMBER = "0388100001826000047"


class PreparedPublicationError(RuntimeError):
    """Stable fail-closed publication error without private path disclosure."""

    def __init__(self, code: str, cleanup_code: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.cleanup_code = cleanup_code

    @property
    def reason_codes(self) -> tuple[str, ...]:
        values = [self.code]
        if self.cleanup_code:
            values.append(self.cleanup_code)
        return tuple(sorted(set(values)))


@dataclass(frozen=True)
class PreparedPublication:
    final_directory: Path
    manifest: dict[str, object]
    result: dict[str, object]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise PreparedPublicationError("prepared_path_inspection_failed") from exc


def _require_directory(path: Path, code: str) -> None:
    value = _lstat(path)
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISDIR(value.st_mode):
        raise PreparedPublicationError(code)


def _require_file(path: Path, code: str) -> None:
    value = _lstat(path)
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise PreparedPublicationError(code)


def _children(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise PreparedPublicationError("prepared_path_inspection_failed") from exc


def _walk(root: Path) -> list[Path]:
    _require_directory(root, "prepared_directory_invalid")
    result: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        for child in _children(current):
            value = _lstat(child)
            if stat.S_ISLNK(value.st_mode):
                raise PreparedPublicationError("prepared_symlink_detected")
            if stat.S_ISDIR(value.st_mode):
                result.append(child)
                stack.append(child)
            elif stat.S_ISREG(value.st_mode):
                result.append(child)
            else:
                raise PreparedPublicationError("prepared_unsafe_filesystem_object")
    return sorted(result, key=lambda item: (len(item.parts), str(item)))


def _tree_hash(root: Path) -> str:
    records: list[dict[str, object]] = []
    for path in _walk(root):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            records.append({"path": relative, "type": "directory"})
        else:
            records.append(
                {
                    "path": relative,
                    "type": "file",
                    "sha256": _sha256_file(path),
                    "size": path.stat().st_size,
                }
            )
    return _sha256_bytes(_json_bytes(records))


def _fsync_file(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError as exc:
        raise PreparedPublicationError("prepared_file_fsync_failed") from exc


def _fsync_directory(path: Path, *, parent: bool = False) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        code = (
            "prepared_parent_fsync_failed"
            if parent
            else "prepared_directory_fsync_failed"
        )
        raise PreparedPublicationError(code) from exc


def _call_fault(fault: Callable[[str], None] | None, stage: str, code: str) -> None:
    if fault is None:
        return
    try:
        fault(stage)
    except PreparedPublicationError:
        raise
    except BaseException as exc:
        raise PreparedPublicationError(code) from exc


def _write_atomic_json(
    path: Path,
    value: object,
    *,
    code: str,
    fault: Callable[[str], None] | None,
    stage: str,
) -> bytes:
    if path.exists() or path.is_symlink():
        raise PreparedPublicationError(code)
    payload = _json_bytes(value)
    temporary = path.with_name(f".{path.name}.partial.{secrets.token_urlsafe(8)}")
    try:
        _call_fault(fault, stage, code)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except PreparedPublicationError:
        temporary.unlink(missing_ok=True)
        raise
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise PreparedPublicationError(code) from exc
    return payload


def scan_public_values(
    values: Iterable[object], *, forbidden_literals: Iterable[str] = ()
) -> None:
    text = "\n".join(
        value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        for value in values
    )
    if _UUID.search(text) or _ACCEPTED_REGISTRY_NUMBER in text:
        raise PreparedPublicationError("prepared_privacy_violation")
    if any(pattern.search(text) for pattern in _PRIVATE_PATTERNS):
        raise PreparedPublicationError("prepared_privacy_violation")
    for literal in forbidden_literals:
        if isinstance(literal, str) and literal and literal in text:
            raise PreparedPublicationError("prepared_privacy_violation")


def _validate_top_level(root: Path, expected: set[str], code: str) -> None:
    actual = {item.name for item in _children(root)}
    if actual != expected:
        raise PreparedPublicationError(code)


def _apply_modes_and_fsync(root: Path) -> None:
    items = _walk(root)
    for path in items:
        if path.is_file():
            os.chmod(path, 0o600)
            _fsync_file(path)
    for path in sorted(
        (item for item in items if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        os.chmod(path, 0o700)
        _fsync_directory(path)
    os.chmod(root, 0o700)
    _fsync_directory(root)


def _reject_sqlite_sidecars(root: Path) -> None:
    if any(
        path.is_file() and (path.name.endswith("-wal") or path.name.endswith("-shm"))
        for path in _walk(root)
    ):
        raise PreparedPublicationError("prepared_sqlite_sidecar_present")


def _safe_remove_tree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    _require_directory(path, "prepared_cleanup_failed")
    for child in sorted(_walk(path), key=lambda item: len(item.parts), reverse=True):
        try:
            if child.is_dir():
                child.rmdir()
            else:
                child.unlink()
        except OSError as exc:
            raise PreparedPublicationError("prepared_cleanup_failed") from exc
    try:
        path.rmdir()
    except OSError as exc:
        raise PreparedPublicationError("prepared_cleanup_failed") from exc


def _cleanup_failed_publication(staging: Path, final: Path) -> str | None:
    cleanup_code: str | None = None
    if final.exists() or final.is_symlink():
        quarantine = (
            final.parent / f".prepared-state.quarantine.{secrets.token_urlsafe(10)}"
        )
        try:
            if quarantine.exists() or quarantine.is_symlink():
                raise OSError("quarantine collision")
            os.replace(final, quarantine)
            _fsync_directory(final.parent, parent=True)
        except (OSError, PreparedPublicationError):
            cleanup_code = "prepared_quarantine_failed"
            try:
                _safe_remove_tree(final)
                _fsync_directory(final.parent, parent=True)
            except PreparedPublicationError:
                cleanup_code = "prepared_cleanup_failed"
    if staging.exists() or staging.is_symlink():
        try:
            _safe_remove_tree(staging)
            _fsync_directory(staging.parent, parent=True)
        except PreparedPublicationError:
            cleanup_code = cleanup_code or "prepared_cleanup_failed"
    return cleanup_code


def _post_verify(
    final: Path,
    *,
    expected_manifest: dict[str, object],
    expected_result: dict[str, object],
) -> None:
    _require_directory(final, "prepared_post_rename_file_set_invalid")
    if stat.S_IMODE(final.stat().st_mode) != 0o700:
        raise PreparedPublicationError("prepared_post_rename_mode_invalid")
    _validate_top_level(
        final, _EXACT_FINAL_SET, "prepared_post_rename_file_set_invalid"
    )
    _reject_sqlite_sidecars(final)
    for path in _walk(final):
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_file() and mode != 0o600:
            raise PreparedPublicationError("prepared_post_rename_mode_invalid")
        if path.is_dir() and mode != 0o700:
            raise PreparedPublicationError("prepared_post_rename_mode_invalid")
    manifest_path = final / "prepared-state-manifest.json"
    result_path = final / "sanitized-acceptance-result.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedPublicationError("prepared_post_rename_hash_mismatch") from exc
    if manifest != expected_manifest or result != expected_result:
        raise PreparedPublicationError("prepared_post_rename_hash_mismatch")
    expected_hashes = {
        "database_sha256": _sha256_file(final / "prepared.sqlite3"),
        "runtime_profile_sha256": _sha256_file(final / "runtime-profile.json"),
        "prepared_verification_sha256": _sha256_file(
            final / "prepared-verification.json"
        ),
        "application_data_tree_sha256": _tree_hash(final / "application-data"),
        "sanitized_result_sha256": _sha256_file(result_path),
    }
    if any(manifest.get(key) != value for key, value in expected_hashes.items()):
        raise PreparedPublicationError("prepared_post_rename_hash_mismatch")


def publish_prepared_state(
    *,
    staging: Path,
    final: Path,
    base_manifest: dict[str, object],
    result: dict[str, object],
    forbidden_literals: Iterable[str] = (),
    fault: Callable[[str], None] | None = None,
) -> PreparedPublication:
    """Publish one verified private state or leave no canonical final directory."""
    if staging.parent != final.parent:
        raise PreparedPublicationError("prepared_publication_parent_mismatch")
    _require_directory(staging, "prepared_staging_invalid")
    _require_directory(staging.parent, "prepared_parent_invalid")
    if stat.S_IMODE(staging.parent.stat().st_mode) != 0o700:
        raise PreparedPublicationError("prepared_parent_mode_invalid")
    if final.exists() or final.is_symlink():
        raise PreparedPublicationError("prepared_final_already_exists")
    _validate_top_level(staging, _REQUIRED_INPUTS, "prepared_input_file_set_invalid")
    _reject_sqlite_sidecars(staging)
    for name in (
        "prepared.sqlite3",
        "runtime-profile.json",
        "prepared-verification.json",
    ):
        _require_file(staging / name, "prepared_input_file_set_invalid")
    _require_directory(staging / "application-data", "prepared_input_file_set_invalid")

    try:
        runtime_profile = json.loads(
            (staging / "runtime-profile.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparedPublicationError("prepared_runtime_profile_invalid") from exc

    result_bytes = _json_bytes(result)
    manifest = {
        **base_manifest,
        "exact_file_set": sorted(_EXACT_FINAL_SET),
        "runtime_profile_sha256": _sha256_file(staging / "runtime-profile.json"),
        "prepared_verification_sha256": _sha256_file(
            staging / "prepared-verification.json"
        ),
        "application_data_tree_sha256": _tree_hash(staging / "application-data"),
        "sanitized_result_sha256": _sha256_bytes(result_bytes),
    }
    scan_public_values(
        (result, manifest, runtime_profile, sorted(_EXACT_FINAL_SET)),
        forbidden_literals=forbidden_literals,
    )

    renamed = False
    try:
        _write_atomic_json(
            staging / "prepared-state-manifest.json",
            manifest,
            code="prepared_manifest_write_failed",
            fault=fault,
            stage="before_manifest_write",
        )
        _write_atomic_json(
            staging / "sanitized-acceptance-result.json",
            result,
            code="prepared_result_write_failed",
            fault=fault,
            stage="before_result_write",
        )
        _validate_top_level(
            staging, _EXACT_FINAL_SET, "prepared_input_file_set_invalid"
        )
        _apply_modes_and_fsync(staging)
        _call_fault(fault, "before_rename", "prepared_rename_failed")
        try:
            os.replace(staging, final)
        except OSError as exc:
            raise PreparedPublicationError("prepared_rename_failed") from exc
        renamed = True
        _call_fault(fault, "after_rename", "prepared_post_rename_hash_mismatch")
        _fsync_directory(final.parent, parent=True)
        _call_fault(fault, "before_post_verify", "prepared_post_rename_hash_mismatch")
        _post_verify(final, expected_manifest=manifest, expected_result=result)
        return PreparedPublication(final, manifest, result)
    except PreparedPublicationError as exc:
        cleanup_code = _cleanup_failed_publication(staging, final)
        raise PreparedPublicationError(exc.code, cleanup_code) from exc
    except BaseException as exc:
        cleanup_code = _cleanup_failed_publication(staging, final)
        code = (
            "prepared_post_rename_hash_mismatch"
            if renamed
            else "prepared_publication_failed"
        )
        raise PreparedPublicationError(code, cleanup_code) from exc
