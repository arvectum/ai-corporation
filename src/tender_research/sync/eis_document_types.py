from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class EisDocumentType:
    subsystem: str
    document_type: str
    law: str
    business_meaning: str
    parser_supported: bool
    current: bool
    sample_count: int
    priority: int


EIS_DOCUMENT_TYPES: tuple[EisDocumentType, ...] = (
    EisDocumentType(
        subsystem="PRIZ",
        document_type="epNotificationEF2020",
        law="44-ФЗ",
        business_meaning="Electronic auction notice metadata (observed in live getDocsByOrgRegion archives)",
        parser_supported=True,
        current=True,
        sample_count=534,
        priority=10,
    ),
)


def write_document_type_coverage_report(path: str | Path = "tmp/eis_bulk/document_type_coverage.md") -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# EIS Document Type Coverage",
        "",
        "This registry only includes document types confirmed from reachable XSD/live archives/project templates.",
        "Unknown document types are intentionally not invented.",
        "",
        "| Subsystem | Document type | Law | Meaning | Parser | Current | Samples | Priority |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    for item in EIS_DOCUMENT_TYPES:
        lines.append(
            f"| `{item.subsystem}` | `{item.document_type}` | {item.law} | {item.business_meaning} | "
            f"{item.parser_supported} | {item.current} | {item.sample_count} | {item.priority} |"
        )
    lines.extend(
        [
            "",
            "## Minimal Current 44-FZ Notice Set",
            "",
            "- `PRIZ/epNotificationEF2020` is confirmed and parser-supported.",
            "- Additional current notice document types must be added only after confirmation from XSD, getNsi, or live archives.",
        ]
    )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target
