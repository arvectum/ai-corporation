"""Customer-safe detail sections for complete-corpus procurement reports."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any


_CONTRACT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Оплата", ("оплат", "расчет", "расчёт", "аванс")),
    (
        "Поставка и приёмка",
        (
            "постав",
            "приемк",
            "приёмк",
            "акт прием",
            "акт приём",
            "документ о прием",
            "документ о приём",
            "разгруз",
        ),
    ),
    ("Обеспечение", ("обеспечен", "гарант")),
    ("Ответственность и штрафы", ("ответствен", "штраф", "пен", "неустой")),
    ("Расторжение", ("расторж", "односторон")),
)
_HASH = re.compile(r"^[0-9a-f]{64}(?:\.[a-z0-9]+)?$", re.IGNORECASE)
_UUID = re.compile(
    r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_text(value: Any) -> str:
    text = _text(value)
    if not text or _HASH.fullmatch(text) or _UUID.fullmatch(text):
        return ""
    if text.startswith(("/", "file:")) or "/Users/" in text or "/Volumes/" in text:
        return ""
    return text


def _safe_source(value: Any) -> str:
    text = _safe_text(value)
    if not text:
        return "Документы закупки"
    if text.lower() in {
        "адаптер раннера",
        "fallback-адаптер",
        "runner adapter",
        "customer_run",
    }:
        return "Документы закупки"
    if Path(text).name != text:
        return "Документы закупки"
    if _HASH.search(Path(text).stem) or _UUID.search(Path(text).stem):
        return "Документы закупки"
    return text


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _safe_text(value)
        key = " ".join(clean.lower().split())
        if not clean or key in seen:
            continue
        seen.add(key)
        result.append(clean)
    return result


def _requirement_rows(model: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in model.get("requirements", []):
        if isinstance(item, str):
            title = _safe_text(item)
            if title:
                rows.append(
                    {
                        "title": title,
                        "detail": "",
                        "type": "требование",
                        "source": "Документы закупки",
                    }
                )
            continue
        if not isinstance(item, dict):
            continue
        title = _safe_text(
            item.get("title") or item.get("requirement") or item.get("name")
        )
        detail = _safe_text(item.get("detail") or item.get("description"))
        if not title and not detail:
            continue
        rows.append(
            {
                "title": title or detail,
                "detail": detail if detail != title else "",
                "type": _safe_text(item.get("type")) or "требование",
                "source": _safe_source(item.get("source")),
            }
        )
    return rows


def _contract_terms(model: dict[str, Any]) -> list[str]:
    compatibility = model.get("compatibility_sections")
    highlights = (
        compatibility.get("contract_highlights", [])
        if isinstance(compatibility, dict)
        else []
    )
    values: list[str] = [
        _safe_text(item) for item in highlights if isinstance(item, (str, int, float))
    ]
    for risk in model.get("risks", []):
        if not isinstance(risk, dict):
            continue
        description = _safe_text(risk.get("description") or risk.get("risk"))
        clause = _safe_text(risk.get("clause"))
        impact = _safe_text(risk.get("impact"))
        joined = ". ".join(part for part in (clause, description, impact) if part)
        if joined:
            values.append(joined)
    return _dedupe(values)


def _group_contract_terms(values: list[str]) -> list[dict[str, Any]]:
    """Assign each term to the first matching semantic group exactly once."""

    buckets: dict[str, list[str]] = {title: [] for title, _markers in _CONTRACT_GROUPS}
    other: list[str] = []
    for value in values:
        lowered = value.lower()
        for title, markers in _CONTRACT_GROUPS:
            if any(marker in lowered for marker in markers):
                buckets[title].append(value)
                break
        else:
            other.append(value)
    groups = [
        {"title": title, "items": buckets[title]}
        for title, _markers in _CONTRACT_GROUPS
        if buckets[title]
    ]
    if other:
        groups.append({"title": "Прочие условия", "items": other})
    return groups


def build_customer_detail_projection(model: dict[str, Any]) -> dict[str, Any]:
    """Return grounded customer sections without IDs, hashes or source quotes."""

    requirements = _requirement_rows(model)
    technical = [
        row
        for row in requirements
        if any(
            marker in f"{row['type']} {row['title']} {row['detail']}".lower()
            for marker in (
                "техничес",
                "характерист",
                "качест",
                "гост",
                "товар",
                "спецификац",
                "описание объекта",
            )
        )
    ]
    application = [
        row
        for row in requirements
        if any(
            marker in f"{row['type']} {row['title']} {row['detail']}".lower()
            for marker in (
                "документ",
                "заявк",
                "квалификац",
                "участник",
                "декларац",
                "сертификат",
            )
        )
    ]
    if not technical:
        technical = requirements
    remaining = [row for row in requirements if row not in technical and row not in application]
    contract_terms = _contract_terms(model)
    return {
        "technical_requirements": technical,
        "application_requirements": application,
        "other_requirements": remaining,
        "contract_term_groups": _group_contract_terms(contract_terms),
        "has_grounded_requirements": bool(requirements),
        "has_contract_terms": bool(contract_terms),
    }
