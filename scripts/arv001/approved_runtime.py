"""Closed repository-owned identity contract for the ARV-001 local runtime."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HASH = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SCHEMA_VERSION = "arv001-approved-runtime-v2"
_TREE_HASH_ALGORITHM = "sha256-json-c14n-v1"
_RUNTIME_BUNDLE_ROOT_RULE = "official_archive_extraction_root"
_CONFIG_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "arv001"
    / "approved_local_runtime.json"
)
_FIELDS = {
    "schema_version", "provider", "model_alias", "model_family",
    "parameter_profile", "tuning_profile", "training_profile", "format",
    "quantization", "source_repository", "source_revision", "gguf_sha256",
    "llama_cpp_repository", "llama_cpp_release_tag", "llama_cpp_commit",
    "llama_cpp_build", "github_release_id", "github_asset_id",
    "github_asset_name", "github_asset_size", "github_asset_created_at",
    "github_asset_updated_at", "github_asset_digest", "archive_sha256",
    "llama_server_sha256", "llama_server_size",
    "runtime_bundle_tree_hash_algorithm", "runtime_bundle_root_rule",
    "runtime_bundle_tree_sha256", "runtime_bundle_regular_file_count",
    "runtime_bundle_symlink_count", "version_output_sha256",
    "help_output_sha256", "dependency_summary_sha256", "architecture",
    "codesign_status", "commit_verification_verified", "provenance_decision",
    "required_capabilities",
}


class ApprovedRuntimeContractError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ApprovedRuntimeContract:
    provider: str
    model_alias: str
    gguf_sha256: str
    llama_cpp_release_tag: str
    llama_cpp_commit: str
    llama_cpp_build: str
    github_release_id: int
    github_asset_id: int
    github_asset_name: str
    github_asset_size: int
    github_asset_digest: str
    archive_sha256: str
    llama_server_sha256: str
    llama_server_size: int
    runtime_bundle_tree_hash_algorithm: str
    runtime_bundle_root_rule: str
    runtime_bundle_tree_sha256: str
    runtime_bundle_regular_file_count: int
    runtime_bundle_symlink_count: int
    required_capabilities: tuple[str, ...]


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ApprovedRuntimeContractError(code)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def load_approved_runtime_contract(
    path: Path | None = None,
) -> ApprovedRuntimeContract:
    target = path or _CONFIG_PATH
    _require(not target.is_symlink() and target.is_file(), "runtime_contract_missing")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApprovedRuntimeContractError("runtime_contract_json_invalid") from exc
    _require(isinstance(value, dict), "runtime_contract_schema_invalid")
    _require(set(value) == _FIELDS, "runtime_contract_schema_invalid")
    _require(value["schema_version"] == _SCHEMA_VERSION, "runtime_contract_version_invalid")

    non_string = {
        "github_release_id", "github_asset_id", "github_asset_size",
        "llama_server_size", "runtime_bundle_regular_file_count",
        "runtime_bundle_symlink_count", "commit_verification_verified",
        "required_capabilities",
    }
    _require(
        all(isinstance(value[field], str) and value[field] for field in _FIELDS - non_string),
        "runtime_contract_type_invalid",
    )
    for field in (
        "github_release_id", "github_asset_id", "github_asset_size",
        "llama_server_size", "runtime_bundle_regular_file_count",
    ):
        _require(_positive_int(value[field]), "runtime_contract_type_invalid")
    _require(
        isinstance(value["runtime_bundle_symlink_count"], int)
        and not isinstance(value["runtime_bundle_symlink_count"], bool)
        and value["runtime_bundle_symlink_count"] >= 0,
        "runtime_contract_type_invalid",
    )
    _require(value["commit_verification_verified"] is True, "runtime_contract_commit_unverified")

    for field in (
        "gguf_sha256", "archive_sha256", "llama_server_sha256",
        "runtime_bundle_tree_sha256", "version_output_sha256",
        "help_output_sha256", "dependency_summary_sha256",
    ):
        _require(bool(_HASH.fullmatch(value[field])), "runtime_contract_hash_invalid")
    _require(bool(_COMMIT.fullmatch(value["source_revision"])), "runtime_contract_hash_invalid")
    _require(bool(_COMMIT.fullmatch(value["llama_cpp_commit"])), "runtime_contract_hash_invalid")

    _require(value["provider"] == "openai_compatible", "runtime_contract_provider_invalid")
    _require(value["llama_cpp_repository"] == "ggml-org/llama.cpp", "runtime_contract_source_invalid")
    _require(value["llama_cpp_release_tag"] == "b10240", "runtime_contract_release_invalid")
    _require(value["llama_cpp_build"] == "10240", "runtime_contract_release_invalid")
    _require(value["architecture"] == "arm64", "runtime_contract_architecture_invalid")
    _require(value["codesign_status"] == "VALID", "runtime_contract_codesign_invalid")
    _require(
        value["provenance_decision"] == "PASS_OFFICIAL_ARTIFACT_ATTESTED",
        "runtime_contract_provenance_invalid",
    )
    _require(
        value["github_asset_digest"] == f"sha256:{value['archive_sha256']}",
        "runtime_contract_asset_digest_mismatch",
    )
    _require(
        value["runtime_bundle_tree_hash_algorithm"] == _TREE_HASH_ALGORITHM,
        "runtime_contract_tree_algorithm_invalid",
    )
    _require(
        value["runtime_bundle_root_rule"] == _RUNTIME_BUNDLE_ROOT_RULE,
        "runtime_contract_bundle_root_rule_invalid",
    )

    expected_capabilities = (
        "loopback_only", "models_endpoint", "tokenize_endpoint",
        "openai_compatible_transport_boundary", "host_flag", "port_flag",
        "model_flag", "alias_flag",
    )
    capabilities = value["required_capabilities"]
    _require(
        isinstance(capabilities, list) and tuple(capabilities) == expected_capabilities,
        "runtime_contract_capabilities_invalid",
    )

    return ApprovedRuntimeContract(
        provider=value["provider"],
        model_alias=value["model_alias"],
        gguf_sha256=value["gguf_sha256"],
        llama_cpp_release_tag=value["llama_cpp_release_tag"],
        llama_cpp_commit=value["llama_cpp_commit"],
        llama_cpp_build=value["llama_cpp_build"],
        github_release_id=value["github_release_id"],
        github_asset_id=value["github_asset_id"],
        github_asset_name=value["github_asset_name"],
        github_asset_size=value["github_asset_size"],
        github_asset_digest=value["github_asset_digest"],
        archive_sha256=value["archive_sha256"],
        llama_server_sha256=value["llama_server_sha256"],
        llama_server_size=value["llama_server_size"],
        runtime_bundle_tree_hash_algorithm=value["runtime_bundle_tree_hash_algorithm"],
        runtime_bundle_root_rule=value["runtime_bundle_root_rule"],
        runtime_bundle_tree_sha256=value["runtime_bundle_tree_sha256"],
        runtime_bundle_regular_file_count=value["runtime_bundle_regular_file_count"],
        runtime_bundle_symlink_count=value["runtime_bundle_symlink_count"],
        required_capabilities=tuple(capabilities),
    )


def approved_runtime_bundle_root(
    server: Path,
    contract: ApprovedRuntimeContract,
) -> Path:
    """Return the extraction root whose relative paths were hashed in attestation."""

    _require(
        server.name == "llama-server" and not server.is_symlink(),
        "runtime_bundle_server_invalid",
    )
    bundle_directory = server.parent
    expected_name = f"llama-{contract.llama_cpp_release_tag}"
    _require(
        bundle_directory.name == expected_name
        and not bundle_directory.is_symlink()
        and bundle_directory.is_dir(),
        "runtime_bundle_directory_invalid",
    )
    extraction_root = bundle_directory.parent
    _require(
        not extraction_root.is_symlink() and extraction_root.is_dir(),
        "runtime_bundle_root_invalid",
    )
    try:
        entries = list(extraction_root.iterdir())
        canonical_bundle = bundle_directory.resolve(strict=True)
    except OSError as exc:
        raise ApprovedRuntimeContractError("runtime_bundle_root_invalid") from exc
    _require(
        len(entries) == 1
        and not entries[0].is_symlink()
        and entries[0].resolve(strict=True) == canonical_bundle,
        "runtime_bundle_root_shape_invalid",
    )
    return extraction_root


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalized_tree_root(root: Path) -> Path:
    """Accept either extraction root or the official top-level bundle directory."""

    contract = load_approved_runtime_contract()
    expected_name = f"llama-{contract.llama_cpp_release_tag}"
    if root.name == expected_name:
        server = root / "llama-server"
        if server.is_file() and not server.is_symlink():
            return approved_runtime_bundle_root(server, contract)
    return root


def canonical_runtime_bundle_tree(root: Path) -> tuple[str, int, int]:
    """Hash the official extraction tree using the attestation serialization."""

    root = _normalized_tree_root(root)
    _require(not root.is_symlink() and root.is_dir(), "runtime_bundle_root_invalid")
    try:
        canonical_root = root.resolve(strict=True)
    except OSError as exc:
        raise ApprovedRuntimeContractError("runtime_bundle_root_invalid") from exc

    records: list[dict[str, Any]] = []
    regular_files = 0
    symlinks = 0

    def visit(directory: Path) -> None:
        nonlocal regular_files, symlinks
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise ApprovedRuntimeContractError("runtime_bundle_unreadable") from exc
        for child in children:
            relative = child.relative_to(root).as_posix()
            try:
                metadata = child.lstat()
            except OSError as exc:
                raise ApprovedRuntimeContractError("runtime_bundle_unreadable") from exc
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    target = os.readlink(child)
                    target_path = Path(target)
                    _require(not target_path.is_absolute(), "runtime_bundle_symlink_invalid")
                    resolved = (child.parent / target_path).resolve(strict=True)
                except (OSError, ApprovedRuntimeContractError) as exc:
                    if isinstance(exc, ApprovedRuntimeContractError):
                        raise
                    raise ApprovedRuntimeContractError("runtime_bundle_symlink_invalid") from exc
                _require(
                    resolved == canonical_root or canonical_root in resolved.parents,
                    "runtime_bundle_symlink_invalid",
                )
                records.append({"path": relative, "target": target, "type": "symlink"})
                symlinks += 1
            elif stat.S_ISDIR(metadata.st_mode):
                visit(child)
            elif stat.S_ISREG(metadata.st_mode):
                records.append(
                    {
                        "mode": format(stat.S_IMODE(metadata.st_mode), "04o"),
                        "path": relative,
                        "sha256": _sha256_file(child),
                        "size_bytes": metadata.st_size,
                        "type": "file",
                    }
                )
                regular_files += 1
            else:
                raise ApprovedRuntimeContractError("runtime_bundle_object_invalid")

    visit(root)
    records.sort(key=lambda item: str(item["path"]))
    payload = json.dumps(
        records,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), regular_files, symlinks
