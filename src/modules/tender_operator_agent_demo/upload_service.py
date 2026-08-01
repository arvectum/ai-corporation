"""Compatibility facade for tender report persistence and rendering.

The historical upload implementation remains intact in ``upload_service_legacy``.
This facade exposes the same API while routing R10.1 reports through a separate,
sanitzed customer projection.
"""

from __future__ import annotations

import html
from typing import Any

from src.modules.tender_operator_agent_demo import upload_service_legacy as _legacy

for _name, _value in vars(_legacy).items():
    if _name not in {"__name__", "__package__", "__loader__", "__spec__"}:
        globals().setdefault(_name, _value)


def _is_r10_1_model(model: dict[str, Any]) -> bool:
    provenance = model.get("ai_runtime_provenance")
    return bool(
        isinstance(provenance, dict)
        and provenance.get("producer") == "production_llm_r10_1"
    )


def _render_customer_report_html(model: dict[str, Any]) -> str:
    """Render only the sanitized customer projection for R10.1."""
    from src.modules.tender_operator_agent_demo.report_model import (
        build_customer_report_projection,
    )

    projection = build_customer_report_projection(model)

    def esc(value: Any) -> str:
        fallback = "Данных недостаточно — требуется проверка"
        return html.escape(str(value if value not in (None, "") else fallback))

    def bullets(values: list[Any]) -> str:
        return "".join(f"<li>{esc(value)}</li>" for value in values)

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
        f"<td>{esc(row.get('okpd2') or 'Не извлечён')}</td>"
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
            f"{risk.get('risk')}: {risk.get('impact')}. "
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
        items_section = (
            "<section><h2>Состав и объём закупки</h2><div class='scroll'>"
            "<table><thead><tr><th>№</th><th>Наименование</th>"
            "<th>Количество</th><th>Единица</th><th>ОКПД2</th>"
            "<th>Подтверждённый источник</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>"
            "<p>Зимний класс, ГОСТ, экологический класс и другие "
            "детальные характеристики не извлечены из текущего комплекта "
            "документов.</p></section>"
        )

    risks_section = (
        f"<section><h2>Риски, подтверждённые документами</h2><ul>{risks}</ul></section>"
        if risks
        else ""
    )
    questions_section = (
        f"<section><h2>Вопросы для уточнения</h2><ul>{bullets(questions)}</ul></section>"
        if questions
        else "<section><h2>Вопросы для уточнения</h2><p>Сначала необходимо "
        "запросить отсутствующие документы. Предметные вопросы по договорным "
        "и техническим условиям будут сформированы после их получения.</p></section>"
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

    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Анализ закупки № {esc(projection.get('procurement_number'))}</title><style>body{{margin:0;background:#f5f8fa;color:#10243e;font:16px Arial,sans-serif}}main{{max-width:1180px;margin:auto;padding:24px}}section{{background:#fff;border:1px solid #dce5eb;border-radius:12px;padding:20px;margin:16px 0}}h1,h2{{color:#003b5c}}.decision{{border-left:6px solid #d08300}}.scroll{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;min-width:760px}}th,td{{border-bottom:1px solid #dce5eb;padding:9px;text-align:left;vertical-align:top}}th{{background:#e9f7f5}}</style></head><body><main>
<section><h1>Анализ закупки № {esc(projection.get('procurement_number'))}</h1><p>Отчёт для принятия решения об участии</p><details><summary>Файлы комплекта ({esc(projection['documents_count'])})</summary><ul>{documents}</ul></details></section>
<section><h2>{esc(projection.get('procurement_title'))}</h2><p>Заказчик: {esc(projection.get('customer_name'))}</p><p>Дата публикации: {esc(projection.get('publication_datetime_display'))}</p><p>Окончание подачи заявок: {esc(projection.get('application_deadline_display'))}</p><p>НМЦК: {esc(projection.get('nmck'))} ₽</p><p>Место поставки: {esc(projection.get('delivery_place'))}</p>{as_of}</section>
<section class="decision"><h2>Решение: {esc(decision.get('recommendation'))}</h2><h3>Ключевые основания</h3><ul>{bullets(decision.get('reasons', []))}</ul><h3>Подтверждено документами</h3><ul>{bullets(decision.get('confirmed', []))}</ul>{('<h3>Не удалось оценить</h3><ul>' + bullets(decision.get('not_evaluated', [])) + '</ul>') if decision.get('not_evaluated') else ''}<p><strong>Следующее действие:</strong> {esc(decision.get('next_action'))}</p></section>
{items_section}{economics}<section><h2>Коммерческие предложения</h2><p>Коммерческие предложения не загружены; экономика участия не рассчитана.</p></section>{risks_section}{questions_section}{evidence_section}{limitations_section}</main></body></html>'''


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
