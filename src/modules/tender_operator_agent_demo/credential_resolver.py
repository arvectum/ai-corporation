from __future__ import annotations

import hashlib
import os
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from scripts.eis_token_probe.read_secret_token import read_token_file


CredentialOwner = Literal["document_export", "individual"]
CredentialSource = Literal["env_var", "token_file", "legacy_env_var", "none"]


@dataclass(frozen=True)
class ResolvedCredential:
    configured: bool = False
    credential_owner: CredentialOwner = "document_export"
    source: CredentialSource = "none"
    token: str = field(default="", repr=False)
    token_format: str = "plain"
    normalized_length: int = 0
    sha256_fingerprint: str = ""
    legacy_fallback_used: bool = False
    warnings: list[str] = field(default_factory=list)


PLACEHOLDER_VALUES = frozenset({
    "",
    "replace_me",
    "replace_me_do_not_commit_real_token",
    "insert_token_here",
    "\u0432\u0441\u0442\u0430\u0432\u0438\u0442\u044c_\u0442\u043e\u043a\u0435\u043d_\u0441\u044e\u0434\u0430",
})


def _is_placeholder(value: str) -> bool:
    return value.strip().lower() in PLACEHOLDER_VALUES


def resolve_getdocsip_credential(
    *,
    allow_legacy_fallback: bool | None = None,
    token_file_path: str | Path | None = None,
) -> ResolvedCredential:
    """Resolve the active GetDocsIP credential with strict priority:

    1. ZAKUPKI_GOV_RU_DOCUMENT_EXPORT_TOKEN env var
    2. ZAKUPKI_GOV_RU_DOCUMENT_EXPORT_TOKEN_FILE → read_secret_token
    3. ZAKUPKI_GOV_RU_SOAP_TOKEN (legacy) only if allow_legacy_fallback=True
    """
    if allow_legacy_fallback is None:
        allow_legacy_fallback = _read_bool("EIS_ALLOW_LEGACY_INDIVIDUAL_TOKEN", False)

    warnings_list: list[str] = []

    # Priority 1: env var
    env_token = os.environ.get("ZAKUPKI_GOV_RU_DOCUMENT_EXPORT_TOKEN", "").strip()
    if env_token and not _is_placeholder(env_token):
        return ResolvedCredential(
            configured=True,
            credential_owner="document_export",
            source="env_var",
            token=env_token,
            token_format="plain",
            normalized_length=len(env_token),
            sha256_fingerprint=_sha256(env_token),
        )

    # Priority 2: token file
    file_path = token_file_path or os.environ.get("ZAKUPKI_GOV_RU_DOCUMENT_EXPORT_TOKEN_FILE", "")
    if file_path:
        resolved_path = Path(file_path).expanduser().resolve()
        if resolved_path.is_file():
            try:
                meta = read_token_file(str(resolved_path))
                token_val = meta.normalized
                if token_val and not _is_placeholder(token_val):
                    fmt = "rtf" if meta.rtf_detected else "plain"
                    if meta.rtf_detected:
                        warnings_list.append(
                            f"token_file_format=rtf for {resolved_path.name}; "
                            "recommend re-saving as plain UTF-8"
                        )
                    result = ResolvedCredential(
                        configured=True,
                        credential_owner="document_export",
                        source="token_file",
                        token=token_val,
                        token_format=fmt,
                        normalized_length=meta.normalized_length,
                        sha256_fingerprint=meta.sha256,
                        warnings=warnings_list,
                    )
                    meta.clear()
                    return result
                meta.clear()
            except Exception as exc:
                warnings_list.append(f"token_file_read_error: {exc}")

    # Priority 3: legacy individual token (only if allowed)
    if allow_legacy_fallback:
        legacy_token = os.environ.get("ZAKUPKI_GOV_RU_SOAP_TOKEN", "").strip()
        if legacy_token and not _is_placeholder(legacy_token):
            warnings_list.append(
                "using legacy individual token as fallback; "
                "set ZAKUPKI_GOV_RU_DOCUMENT_EXPORT_TOKEN to remove this warning"
            )
            return ResolvedCredential(
                configured=True,
                credential_owner="individual",
                source="legacy_env_var",
                token=legacy_token,
                token_format="plain",
                normalized_length=len(legacy_token),
                sha256_fingerprint=_sha256(legacy_token),
                legacy_fallback_used=True,
                warnings=warnings_list,
            )

    return ResolvedCredential(
        configured=False,
        warnings=warnings_list or ["no GetDocsIP credential configured"],
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
