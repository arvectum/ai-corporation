"""Compatibility facade for tender report persistence and rendering.

The historical upload implementation remains intact in ``upload_service_legacy``.
This facade exposes the same API while routing R10.1 reports through a separate,
sanitized customer projection.
"""

from __future__ import annotations

import html
import re
from typing import Any

from src.modules.tender_operator_agent_demo import upload_service_legacy as _legacy

for _name, _value in vars(_legacy).items():
    if _name not in {"__name__", "__package__", "__loader__", "__spec__"}:
        globals().setdefault(_name, _value)

_ORIGINAL_BUILD_PRELIMINARY_PROCUREMENT_ANALYSIS = (
    _legacy._build_preliminary_procurement_analysis
)


def _liability_contract_highlight(contract_text: str) -> str | None:
    """Return a conservative grounded liability summary from the contract text."""

    lowered = contract_text.lower()
    has_liability = bool(
        re.search(r"ответственност[ьи]\s+(?:сторон|поставщика|заказчика)", lowered)
        or "ответственность сторон" in lowered
    )
    has_sanctions = bool(
        re.search(r"\b(?:штраф|штрафы|штрафа|штрафов|пеня|пени|неустойк)\w*", lowered)
    )
    if has_liability and has_sanctions:
        return (
            "Проект контракта содержит условия ответственности сторон и "
            "штрафные санкции за нарушение обязательств."
        )
    if has_sanctions:
        return (
            "Проект контракта содержит условия о штрафах, пенях или неустойке "
            "за нарушение обязательств."
        )
    if has_liability:
        return "Проект контракта содержит раздел об ответственности сторон."
    return None


def _build_preliminary_procurement_analysis(**kwargs: Any) -> dict[str, Any]:
    """Preserve legacy extraction and enrich only the R10.1 customer path."""

    result = _ORIGINAL_BUILD_PRELIMINARY_PROCUREMENT_ANALYSIS(**kwargs)
    metadata = kwargs.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("analysis_mode") != (
        "production_llm_r10_1"
    ):
        return result
    highlight = _liability_contract_highlight(
        str(kwargs.get("contract_draft_text") or "")
    )
    if not highlight:
        return result
    existing = [str(item) for item in result.get("contract_highlights", []) if item]
    normalized = {" ".join(item.lower().split()) for item in existing}
    if " ".join(highlight.lower().split()) not in normalized:
        existing.append(highlight)
    result["contract_highlights"] = existing[:8]
    return result


_legacy._build_preliminary_procurement_analysis = (
    _build_preliminary_procurement_analysis
)


def _is_r10_1_model(model: dict[str, Any]) -> bool:
    provenance = model.get("ai_runtime_provenance")
    return bool(
        isinstance(provenance, dict)
        and provenance.get("producer") == "production_llm_r10_1"
    )


def _default_okpd2(model: dict[str, Any]) -> str | None:
    codes = model.get("okpd2_codes")
    if isinstance(codes, list):
        values = [
            str(item.get("code") or "").strip()
            for item in codes
            if isinstance(item, dict) and item.get("code")
        ]
        if values:
            return "; ".join(dict.fromkeys(values))
    passport = model.get("procurement_passport")
    if isinstance(passport, dict):
        value = str(passport.get("okpd2") or "").strip()
        if value and "не указан" not in value.lower():
            return value
    return None


def _render_customer_report_html(model: dict[str, Any]) -> str:
    """Render only the sanitized customer projection for R10.1."""

    from src.modules.tender_operator_agent_demo.customer_report_contract import (
        build_customer_detail_projection,
    )
    from src.modules.tender_operator_agent_demo.report_model import (
        build_customer_report_projection,
    )

    projection = build_customer_report_projection(model)
    detail = build_customer_detail_projection(model)
    default_okpd2 = _default_okpd2(model)

    def esc(value: Any) -> str:
        fallback = "Данных недостаточно — требуется проверка"
        return html.escape(str(value if value not in (None, "") else fallback))

    def bullets(values: list[Any]) -> str:
        return "".join(f"<li>{esc(value)}</li>" for value in values)

    def requirement_rows(values: list[dict[str, str]]) -> str:
        return "".join(
            "<tr>"
            f"<td>{esc(item.get('title'))}</td>"
            f"<td>{esc(item.get('detail') or '—')}</td>"
            f"<td>{esc(item.get('source'))}</td>"
            "</tr>"
            for item in values
        )

    decision = projection["customer_decision"]
    documents = "".join(
        f"<li>{esc(item['name'])} ({esc(item['type'])})</li>"
        for item in projection["customer_documents"]
    )
    rows = "".join(
        "<tr>"
        f"<td>{esc(row['sequence'])}</td>"
        f"<td>{esc(row['original_name'])}</td>"
        f"<td>{esc(row['quantity_display'])}</td>"
        f"<td>{esc(row['unit_original'])}</td>"
        f"<td>{esc(row.get('okpd2') or default_okpd2 or 'Не извлечён')}</td>"
        f"<td>{esc('; '.join(row.get('characteristics') or []) or '—')}</td>"
        f"<td>{esc(row['source_display'])}</td>"
        "</tr>"
        for row in projection["line_items"]
    )
    evidence = bullets(
        [
            f"{item['document_label']} — {item['document_type']}, {item['location']}"
            for item in projection["evidence_map"]
        ]
    )
    risks = bullets(
        [
            f"{risk.get('risk') or risk.get('description')}: {risk.get('impact')}. "
            f"Что сделать: {risk.get('mitigation')}"
            for risk in projection["risks"]
        ]
    )
    questions = [
        item.get("question") if isinstance(item, dict) else item
        for item in projection["customer_questions"]
    ]

    economics = ""
    if projection.get("unit_economics"):
        item = projection["unit_economics"]
        value = f"{item['value']:,.2f}".replace(",", " ").replace(".", ",")
        economics = (
            "<section><h2>Экономический ориентир</h2>"
            "<p>НМЦК, делённая на подтверждённый объём, составляет "
            f"ориентировочно <strong>{value} ₽ за {esc(item['unit'])}</strong>."
            "</p><p>Это арифметический ориентир по НМЦК, а не "
            "подтверждённая закупочная себестоимость.</p></section>"
        )

    as_of = ""
    if projection.get("analysis_as_of") not in (
        None,
        "",
        "Данных недостаточно — требуется проверка",
    ):
        as_of = (
            "<p>Отчёт сформирован по состоянию на: "
            f"{esc(projection['analysis_as_of'])}</p>"
        )

    items_section = ""
    if rows:
        technical_note = (
            "<p>Подробные требования приведены ниже в разделе «Технические требования».</p>"
            if detail["has_grounded_requirements"]
            else "<p>Детальные характеристики требуют дополнительной ручной проверки по документам.</p>"
        )
        items_section = (
            "<section><h2>Состав и объём закупки</h2><div class='scroll'>"
            "<table><thead><tr><th>№</th><th>Наименование</th>"
            "<th>Количество</th><th>Единица</th><th>ОКПД2</th>"
            "<th>Ключевые характеристики</th>"
            "<th>Подтверждённый источник</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>{technical_note}</section>"
        )

    requirement_sections = ""
    for title, key in (
        ("Технические требования", "technical_requirements"),
        ("Требования к заявке и участнику", "application_requirements"),
        ("Прочие подтверждённые требования", "other_requirements"),
    ):
        values = detail[key]
        if values:
            requirement_sections += (
                f"<section><h2>{esc(title)}</h2><div class='scroll'>"
                "<table><thead><tr><th>Требование</th><th>Деталь</th>"
                f"<th>Источник</th></tr></thead><tbody>{requirement_rows(values)}"
                "</tbody></table></div></section>"
            )

    contract_sections = ""
    if detail["contract_term_groups"]:
        groups = "".join(
            f"<h3>{esc(group['title'])}</h3><ul>{bullets(group['items'])}</ul>"
            for group in detail["contract_term_groups"]
        )
        contract_sections = f"<section><h2>Условия контракта</h2>{groups}</section>"

    risks_section = (
        f"<section><h2>Риски, подтверждённые документами</h2><ul>{risks}</ul></section>"
        if risks
        else ""
    )
    if questions:
        questions_section = (
            f"<section><h2>Вопросы для уточнения</h2><ul>{bullets(questions)}</ul></section>"
        )
    elif projection.get("document_set_complete"):
        questions_section = (
            "<section><h2>Вопросы для уточнения</h2>"
            "<p>Дополнительные вопросы по результатам анализа не сформированы.</p></section>"
        )
    else:
        questions_section = (
            "<section><h2>Вопросы для уточнения</h2>"
            "<p>Сначала необходимо получить недостающие документы, затем повторить анализ.</p></section>"
        )
    evidence_section = (
        f"<section><h2>Источники</h2><ul>{evidence}</ul></section>"
        if evidence
        else ""
    )
    limitations = projection["corpus_limitations"]
    limitations_section = (
        "<section><h2>Ограничения комплекта документов</h2><ul>"
        f"{bullets(limitations)}</ul></section>"
        if limitations
        else ""
    )

    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Анализ закупки № {esc(projection.get('procurement_number'))}</title><style>body{{margin:0;background:#f5f8fa;color:#10243e;font:16px Arial,sans-serif}}main{{max-width:1180px;margin:auto;padding:24px}}section{{background:#fff;border:1px solid #dce5eb;border-radius:12px;padding:20px;margin:16px 0}}h1,h2{{color:#003b5c}}.decision{{border-left:6px solid #d08300}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:860px}}th,td{{border-bottom:1px solid #dce5eb;padding:9px;text-align:left;vertical-align:top}}th{{background:#e9f7f5}}</style></head><body><main>
<section><h1>Анализ закупки № {esc(projection.get('procurement_number'))}</h1><p>Отчёт для принятия решения об участии</p><details><summary>Документы комплекта ({esc(projection['documents_count'])})</summary><ul>{documents}</ul></details></section>
<section><h2>{esc(projection.get('procurement_title'))}</h2><p>Заказчик: {esc(projection.get('customer_name'))}</p><p>Дата публикации: {esc(projection.get('publication_datetime_display'))}</p><p>Окончание подачи заявок: {esc(projection.get('application_deadline_display'))}</p><p>НМЦК: {esc(projection.get('nmck'))} ₽</p><p>Место поставки: {esc(projection.get('delivery_place'))}</p>{as_of}</section>
<section class="decision"><h2>Решение: {esc(decision.get('recommendation'))}</h2><h3>Ключевые основания</h3><ul>{bullets(decision.get('reasons', []))}</ul><h3>Подтверждено документами</h3><ul>{bullets(decision.get('confirmed', []))}</ul>{('<h3>Не удалось оценить</h3><ul>' + bullets(decision.get('not_evaluated', [])) + '</ul>') if decision.get('not_evaluated') else ''}<p><strong>Следующее действие:</strong> {esc(decision.get('next_action'))}</p></section>
{items_section}{economics}{requirement_sections}{contract_sections}<section><h2>Коммерческие предложения</h2><p>Коммерческие предложения не загружены; экономика участия не рассчитана.</p></section>{risks_section}{questions_section}{evidence_section}{limitations_section}</main></body></html>'''


def _render_canonical_report_html(model: dict[str, Any]) -> str:
    """Preserve legacy rendering, with R10.1 routed to the customer view."""

    if _is_r10_1_model(model):
        return _render_customer_report_html(model)
    return _legacy._render_product_report_html(model, customer=False)


def _persist_outputs(
    run_id: str,
    metadata: dict[str, Any],
    outputs: dict[str, dict[str, Any]],
    steps: list[Any],
) -> None:
    from src.modules.procurement_analysis.frozen_producer import (
        persist_frozen_r7_outputs,
    )

    renderer = (
        _render_customer_report_html
        if metadata.get("analysis_mode") == "production_llm_r10_1"
        else _render_canonical_report_html
    )
    persist_frozen_r7_outputs(
        output_dir=_legacy._output_dir(run_id),
        run_id=run_id,
        metadata=metadata,
        outputs=outputs,
        steps=steps,
        render_html=renderer,
        now_factory=_legacy._safe_datetime,
    )


_legacy._render_customer_report_html = _render_customer_report_html
_legacy._render_canonical_report_html = _render_canonical_report_html
_legacy._persist_outputs = _persist_outputs
