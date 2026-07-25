from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path


_RTF_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


class TokenMetadata:
    def __init__(self, token: str, byte_size: int):
        self._token = token
        self.byte_size = byte_size
        self._whitespace_removed = False
        self._quotes_removed = False
        self._rtf_detected = False
        self._uuid_extracted_from_rtf = False
        self._normalized = self._normalize(token)

    def _normalize(self, raw: str) -> str:
        s = raw
        if s.startswith("\ufeff"):
            s = s.removeprefix("\ufeff")
        self._whitespace_removed = bool(s != s.strip())
        s = s.strip()
        self._quotes_removed = False
        if (s.startswith('"') and s.endswith('"')) or (
            s.startswith("'") and s.endswith("'")
        ):
            s = s[1:-1]
            self._quotes_removed = True
            s = s.strip()
        s = s.replace("\r\n", "").replace("\r", "").replace("\n", "")
        self._rtf_detected = bool(re.search(r"\\rtf1?", s[:50]))
        if self._rtf_detected:
            found = _RTF_UUID_PATTERN.findall(s)
            if found:
                self._uuid_extracted_from_rtf = True
                s = found[0]
        return s

    @property
    def normalized(self) -> str:
        return self._normalized

    @property
    def normalized_length(self) -> int:
        return len(self._normalized)

    @property
    def empty(self) -> bool:
        return len(self._normalized) == 0

    @property
    def uuid_like(self) -> bool:
        try:
            uuid.UUID(self._normalized)
            return True
        except (ValueError, AttributeError):
            return False

    @property
    def contains_whitespace(self) -> bool:
        return bool(self._whitespace_removed)

    @property
    def quotes_removed(self) -> bool:
        return bool(self._quotes_removed)

    @property
    def rtf_detected(self) -> bool:
        return bool(self._rtf_detected)

    @property
    def uuid_extracted_from_rtf(self) -> bool:
        return bool(self._uuid_extracted_from_rtf)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self._normalized.encode("utf-8")).hexdigest()

    def clear(self) -> None:
        self._token = ""
        self._normalized = ""

    def to_dict(self) -> dict:
        return {
            "present": True,
            "byte_size": self.byte_size,
            "normalized_length": self.normalized_length,
            "uuid_like": self.uuid_like,
            "whitespace_removed": self.contains_whitespace,
            "quotes_removed": self.quotes_removed,
            "rtf_detected": self.rtf_detected,
            "uuid_extracted_from_rtf": self.uuid_extracted_from_rtf,
            "sha256": self.sha256,
        }

    def __del__(self):
        self.clear()


def read_token_file(path: str | Path) -> TokenMetadata:
    p = Path(path).expanduser().resolve()
    raw = p.read_bytes()
    byte_size = len(raw)
    token_str = raw.decode("utf-8", errors="replace")
    meta = TokenMetadata(token_str, byte_size)
    del token_str
    del raw
    return meta


def print_safe_metadata(path: str | Path) -> dict:
    meta = read_token_file(path)
    result = meta.to_dict()
    meta.clear()
    return result
