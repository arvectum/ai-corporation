"""Resolve the immutable ARV-001 corpus descriptor hash without trusting array order."""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass
from typing import Any, Sequence

from scripts.arv001.complete_corpus_contract import AcceptanceBlocked

_REQUIRED_IDENTITY_FIELDS = ("original_name", "sha256")
_MAX_DESCRIPTOR_FIELDS = 18


@dataclass(frozen=True)
class CorpusHashProfile:
    fields: tuple[str, ...]
    serialization: str
    sha256: str

    def sanitized(self) -> dict[str, Any]:
        return {
            "fields": list(self.fields),
            "serialization": self.serialization,
            "sha256": self.sha256,
            "ordering": "original_name_unicode_codepoint_ascending",
        }


def _serialize(value: Any, profile: str) -> bytes:
    if profile == "canonical_compact":
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    elif profile == "canonical_compact_newline":
        text = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
    elif profile == "sorted_default":
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif profile == "sorted_pretty_2_newline":
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    else:  # pragma: no cover - closed internal call set
        raise AssertionError(profile)
    return text.encode("utf-8")


def _project(
    physical: Sequence[dict[str, Any]], fields: Sequence[str]
) -> list[dict[str, Any]]:
    projected = [
        {field: item[field] for field in fields if field in item}
        for item in physical
    ]
    return sorted(projected, key=lambda item: str(item.get("original_name") or ""))


def _field_sets(physical: Sequence[dict[str, Any]]) -> list[tuple[str, ...]]:
    available = sorted({key for item in physical for key in item})
    if len(available) > _MAX_DESCRIPTOR_FIELDS:
        raise AcceptanceBlocked("corpus_descriptor_field_count_unbounded")
    if any(
        field not in item
        for item in physical
        for field in _REQUIRED_IDENTITY_FIELDS
    ):
        raise AcceptanceBlocked("corpus_identity_field_missing")
    optional = [field for field in available if field not in _REQUIRED_IDENTITY_FIELDS]
    result: list[tuple[str, ...]] = []
    for count in range(len(optional) + 1):
        for subset in itertools.combinations(optional, count):
            result.append(tuple(sorted((*_REQUIRED_IDENTITY_FIELDS, *subset))))
    return result


def resolve_corpus_hash_profile(
    physical: Sequence[dict[str, Any]], expected_sha256: str
) -> CorpusHashProfile:
    """Find the unique canonical descriptor projection bound to the approved SHA.

    The immutable intake already supplies the expected SHA. We never weaken that
    binding: candidate projections are accepted only when their bytes hash exactly
    to the approved value. Full source-file SHA and size checks remain separate.
    """

    if (
        not isinstance(physical, Sequence)
        or isinstance(physical, (str, bytes, bytearray))
        or not physical
        or any(not isinstance(item, dict) for item in physical)
    ):
        raise AcceptanceBlocked("physical_files_contract_invalid")
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise AcceptanceBlocked("expected_corpus_sha_invalid")

    matches: dict[bytes, CorpusHashProfile] = {}
    serializations = (
        "canonical_compact",
        "canonical_compact_newline",
        "sorted_default",
        "sorted_pretty_2_newline",
    )
    for fields in _field_sets(physical):
        payload = _project(physical, fields)
        for serialization in serializations:
            encoded = _serialize(payload, serialization)
            digest = hashlib.sha256(encoded).hexdigest()
            if digest == expected:
                matches.setdefault(
                    encoded,
                    CorpusHashProfile(
                        fields=fields,
                        serialization=serialization,
                        sha256=digest,
                    ),
                )

    if not matches:
        raise AcceptanceBlocked("canonical_corpus_sha_mismatch")
    if len(matches) != 1:
        raise AcceptanceBlocked("canonical_corpus_hash_profile_ambiguous")
    return next(iter(matches.values()))


class BoundCorpusHashResolver:
    """Callable hash contract reused before persistence and after DB round-trip."""

    def __init__(self, expected_sha256: str) -> None:
        self.expected_sha256 = expected_sha256
        self.profile: CorpusHashProfile | None = None

    def __call__(self, physical: Sequence[dict[str, Any]]) -> str:
        resolved = resolve_corpus_hash_profile(physical, self.expected_sha256)
        if self.profile is None:
            self.profile = resolved
        elif (
            resolved.fields != self.profile.fields
            or resolved.serialization != self.profile.serialization
            or resolved.sha256 != self.profile.sha256
        ):
            raise AcceptanceBlocked("persisted_corpus_hash_profile_changed")
        return resolved.sha256
