from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s()\-]*\d{3}[\s()\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_LONG_ID_RE = re.compile(r"(?<!\d)\d{10,14}(?!\d)")
_TOKEN_RE = re.compile(r"(?i)(?:bearer|token|api[_-]?key|secret)\s*[:=]\s*[^\s,;]+")


def canonical_json_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def redact_text(value: str) -> str:
    text = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    text = _PHONE_RE.sub("[REDACTED_PHONE]", text)
    text = _LONG_ID_RE.sub("[REDACTED_ID]", text)
    return _TOKEN_RE.sub("[REDACTED_SECRET]", text)


def bounded_redacted_text(value: str, limit: int) -> tuple[str, bool]:
    redacted = redact_text(value)
    if len(redacted) <= limit:
        return redacted, False
    return redacted[:limit], True


def validate_identifier(value: str, *, label: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def tenant_partition(tenant_id: str) -> str:
    if not tenant_id or len(tenant_id) > 256:
        raise ValueError("invalid tenant id")
    return hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()[:24]


class ShadowAuditStore:
    def __init__(self, root: Path, *, max_payload_bytes: int) -> None:
        self.root = root.expanduser().resolve()
        self.max_payload_bytes = max_payload_bytes

    def audit_path(self, *, tenant_id: str, run_id: str) -> Path:
        validated_run_id = validate_identifier(run_id, label="run id")
        partition = tenant_partition(tenant_id)
        return self.root / partition / validated_run_id / "electrical-ontology-shadow.v1.json"

    def save(self, *, tenant_id: str, run_id: str, payload: dict[str, Any]) -> Path:
        path = self.audit_path(tenant_id=tenant_id, run_id=run_id)
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            default=str,
        ).encode("utf-8")
        if len(encoded) > self.max_payload_bytes:
            raise ValueError("shadow audit payload exceeds configured byte limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=".shadow-audit-",
            delete=False,
        ) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_path = Path(handle.name)
        temporary_path.replace(path)
        return path

    def load(self, *, tenant_id: str, run_id: str) -> dict[str, Any]:
        path = self.audit_path(tenant_id=tenant_id, run_id=run_id)
        if not path.is_file():
            raise FileNotFoundError(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("shadow audit payload is not an object")
        return payload
