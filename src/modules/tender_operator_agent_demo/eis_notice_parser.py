from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse


def extract_notice_metadata(xml_text: str) -> dict[str, Any]:
    if not xml_text or not xml_text.strip():
        return {}

    result: dict[str, Any] = {}
    source_label = "электронное извещение ЕИС"

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}

    ns_map = _collect_namespaces(xml_text)

    result["nmck"] = _extract_value(
        root, ns_map,
        ("maxPrice", "initialPrice", "contractPrice", "initial_price", "price"),
        transform=_parse_price,
    )

    result["publication_date"] = _extract_value(
        root, ns_map,
        ("publishDTInEIS", "publishDate", "placingDate", "publicationDate", "publication_date", "createDate"),
        transform=_normalize_date,
    )

    result["submission_deadline"] = _extract_value(
        root, ns_map,
        ("endDT", "endDate", "applicationEndDate", "collectingEndDate", "submissionDeadline", "deadline"),
        transform=_normalize_date,
    )

    result["delivery_term"] = _extract_value(
        root, ns_map,
        ("deliveryTerm", "deliveryTermInfo", "deliveryPeriod", "deliveryDate"),
    )

    result["customer_name"] = _extract_value(
        root, ns_map,
        ("placer", "customer", "customerInfo", "fullName", "legalEntityName"),
    )

    result["customer_inn"] = _extract_value(
        root, ns_map,
        ("inn", "customerInn", "placerInn"),
    )
    result["customer_kpp"] = _extract_value(root, ns_map, ("KPP", "kpp", "customerKpp"))

    customer_node = next((node for node in root.iter() if _local_name(node.tag) == "customer"), None)
    if customer_node is not None:
        result["customer_name"] = _child_value(customer_node, ("fullName", "name")) or result.get("customer_name")
        result["customer_inn"] = _child_value(customer_node, ("INN", "inn")) or result.get("customer_inn")
        result["customer_kpp"] = _child_value(customer_node, ("KPP", "kpp")) or result.get("customer_kpp")

    delivery_places = _extract_delivery_places(root)
    if delivery_places:
        result["delivery_places"] = delivery_places
        result["delivery_place"] = delivery_places[0]

    okpd2_codes = _extract_okpd2_codes(root)
    if okpd2_codes:
        result["okpd2_codes"] = okpd2_codes

    result["procurement_subject"] = _extract_value(
        root, ns_map,
        ("subject", "lotSubject", "procurementSubject", "contractSubject", "lotObjectInfo"),
    )
    if not result.get("procurement_subject"):
        result["procurement_subject"] = _extract_value(
            root, ns_map, ("purchaseObjectInfo", "purchaseObject"),
        )

    procedure_type = _extract_value(
        root, ns_map,
        ("procedureType", "placingWayName", "purchaseMethod", "procedureTypeName"),
    )
    if procedure_type:
        result["procedure_type"] = procedure_type

    result["source_label"] = source_label

    if result.get("nmck") or result.get("publication_date") or result.get("submission_deadline") or result.get("customer_name"):
        result["_has_notice_data"] = True

    return result


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_value(node: ET.Element, names: tuple[str, ...]) -> str | None:
    for descendant in node.iter():
        if _local_name(descendant.tag) in names and descendant.text and descendant.text.strip():
            return descendant.text.strip()
    return None


def _extract_delivery_places(root: ET.Element) -> list[str]:
    values: list[str] = []
    for node in root.iter():
        if _local_name(node.tag) not in {"deliveryPlacesInfo", "deliveryPlaceInfo", "placeOfDelivery"}:
            continue
        for descendant in node.iter():
            if _local_name(descendant.tag) not in {"GARAddress", "deliveryPlace", "deliveryAddress", "placeOfDelivery"}:
                continue
            text = " ".join(descendant.itertext()).strip()
            if text and text not in values:
                values.append(text)
    return values


def _extract_okpd2_codes(root: ET.Element) -> list[dict[str, str]]:
    """Extract only explicit OKPD2 records from the structured EIS notice."""
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in root.iter():
        if _local_name(node.tag).lower() not in {"okpd2", "okpd2info"}:
            continue
        code = _child_value(node, ("OKPDCode", "okpdCode", "code"))
        name = _child_value(node, ("OKPDName", "okpdName", "name"))
        if not code or code in seen:
            continue
        seen.add(code)
        result.append({"code": code, "name": name or "Наименование не указано в извещении", "source_type": "structured_notice_xml"})
    return result


def _safe_notice_attachment_url(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("//"):
        return None
    if text.startswith("/"):
        return text
    parsed = urlparse(text)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        return None
    if hostname != "zakupki.gov.ru" and not hostname.endswith(".zakupki.gov.ru"):
        return None
    return text


def _node_attribute(node: ET.Element, names: tuple[str, ...]) -> str | None:
    allowed = {name.lower() for name in names}
    for key, value in node.attrib.items():
        if _local_name(key).lower() in allowed and str(value).strip():
            return str(value).strip()
    return None


def extract_notice_attachments(xml_text: str) -> list[dict[str, str | None]]:
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    attachments: list[dict[str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    candidate_tags = {
        "attachment", "attachmentinfo", "document", "documentinfo", "file", "fileinfo"
    }
    name_fields = ("fileName", "docName", "name", "documentName", "title")
    url_fields = ("url", "downloadUrl", "fileUrl", "href", "link")

    for node in root.iter():
        local = _local_name(node.tag).lower()
        if "attachment" not in local and local not in candidate_tags:
            continue
        name = _extract_child_text(node, name_fields) or _node_attribute(node, name_fields)
        url = _extract_child_text(node, url_fields) or _node_attribute(node, url_fields)
        if not url and node.text and node.text.strip().startswith(("http://", "https://", "/")):
            url = node.text.strip()
        safe_url = _safe_notice_attachment_url(url)
        if not safe_url:
            continue
        if not name:
            name = PurePosixPath(urlparse(safe_url).path).name or "document"
        key = (name.strip().lower(), safe_url)
        if key in seen:
            continue
        seen.add(key)
        attachments.append(
            {
                "name": name.strip(),
                "url": safe_url,
                "document_kind": _classify_notice_attachment_kind(name),
            }
        )
    return attachments


def merge_structured_metadata(
    notice_meta: dict[str, Any],
    card_meta: dict[str, Any],
    doc_meta: dict[str, Any],
) -> dict[str, Any]:
    priority_sources = [
        ("eis_notice", notice_meta),
        ("card", card_meta),
        ("documents", doc_meta),
    ]

    result: dict[str, Any] = {}

    fields = [
        ("nmck", "initial_price"),
        ("publication_date", "publication_date"),
        ("submission_deadline", "deadline"),
        ("delivery_term", "delivery_term"),
        ("customer_name", "customer_name"),
        ("customer_inn", "customer_inn"),
        ("customer_kpp", "customer_kpp"),
        ("delivery_place", "delivery_place"),
        ("okpd2_codes", "okpd2_codes"),
        ("procurement_subject", "procurement_subject"),
        ("procedure_type", "procedure_type"),
    ]

    source_labels = {
        "eis_notice": "электронное извещение ЕИС",
        "card": "карточка ЕИС",
        "documents": "документы закупки",
    }

    for notice_key, output_key in fields:
        for source_name, meta in priority_sources:
            value = meta.get(notice_key)
            if value is not None:
                result[output_key] = {
                    "value": value,
                    "source": source_name,
                    "source_label": source_labels.get(source_name, source_name),
                    "source_reference": f"{source_name}:{notice_key}",
                }
                break

    return result


def apply_structured_metadata_to_procurement(
    procurement_payload: dict[str, Any],
    structured: dict[str, Any],
) -> None:
    source_labels = set()
    for key, entry in structured.items():
        if isinstance(entry, dict) and "value" in entry:
            source_labels.add(entry.get("source_label", ""))

    if structured.get("initial_price") and isinstance(structured["initial_price"], dict):
        procurement_payload["initial_price"] = structured["initial_price"]["value"]
    if structured.get("publication_date") and isinstance(structured["publication_date"], dict):
        procurement_payload["publication_date"] = structured["publication_date"]["value"]
    if structured.get("deadline") and isinstance(structured["deadline"], dict):
        procurement_payload["deadline"] = structured["deadline"]["value"]
    if structured.get("delivery_term") and isinstance(structured["delivery_term"], dict):
        procurement_payload["delivery_term"] = structured["delivery_term"]["value"]
    if structured.get("delivery_place") and isinstance(structured["delivery_place"], dict):
        procurement_payload["delivery_place"] = structured["delivery_place"]["value"]
    for key in ("customer_name", "customer_inn", "customer_kpp"):
        if structured.get(key) and isinstance(structured[key], dict):
            procurement_payload[key] = structured[key]["value"]
    if structured.get("procedure_type") and isinstance(structured["procedure_type"], dict):
        procurement_payload["procedure_type"] = structured["procedure_type"]["value"]
    if structured.get("okpd2_codes") and isinstance(structured["okpd2_codes"], dict):
        procurement_payload["okpd2_codes"] = structured["okpd2_codes"]["value"]

    if source_labels:
        procurement_payload["structured_source_label"] = ", ".join(sorted(source_labels))

    if structured.get("nmck") and isinstance(structured["nmck"], dict):
        ns = structured["nmck"]
        if ns.get("source") == "eis_notice":
            procurement_payload["initial_price_source_priority"] = "eis_notice"
    if structured.get("publication_date") and isinstance(structured["publication_date"], dict):
        ns = structured["publication_date"]
        if ns.get("source") == "eis_notice":
            procurement_payload["publication_date_source_priority"] = "eis_notice"
    if structured.get("deadline") and isinstance(structured["deadline"], dict):
        ns = structured["deadline"]
        if ns.get("source") == "eis_notice":
            procurement_payload["deadline_source_priority"] = "eis_notice"


def build_notice_priority_prompt_section(procurement: dict[str, Any]) -> str:
    source_label = procurement.get("structured_source_label", "электронное извещение ЕИС")
    lines = ["Приоритетные сведения из электронного извещения ЕИС:"]
    nmck = procurement.get("initial_price")
    if nmck:
        lines.append(f"- НМЦК: {nmck} ₽")
    pub_date = procurement.get("publication_date")
    if pub_date:
        lines.append(f"- Дата публикации: {pub_date}")
    deadline = procurement.get("deadline")
    if deadline:
        lines.append(f"- Окончание подачи заявок: {deadline}")
    delivery = procurement.get("delivery_term")
    if delivery:
        lines.append(f"- Срок поставки: {delivery}")
    lines.append(f"- Источник: {source_label}")
    lines.append("")
    lines.append("Instruction: используй электронное извещение ЕИС как приоритетный источник структурных сведений. При расхождении с приложенными документами явно отметь расхождение и опирайся на извещение.")
    return "\n".join(lines)


def build_technical_documents_prompt_section(documents: list[dict[str, Any]]) -> str:
    if not documents:
        return ""

    lines = ["Документы технической части и состава поставки:"]
    added = 0
    for item in documents:
        role = str(item.get("role_hint") or item.get("document_kind") or "attachment").strip()
        if role not in {"technical_spec", "contract_draft", "specification", "procurement_object_description", "attachment", "supporting"}:
            continue
        label = str(item.get("display_name") or item.get("original_name") or "Документ").strip()
        source_type = str(item.get("source_type") or item.get("source") or "document").strip()
        lines.append(f"- {label} ({role}; источник: {source_type})")
        added += 1
        if added >= 12:
            break

    if added == 0:
        return ""

    lines.append("")
    lines.append("Instruction: для раздела состава поставки используй прежде всего ТЗ, описание объекта закупки, спецификации, приложения и таблицы. Электронное извещение используй для реквизитов закупки, но не как замену технического задания.")
    return "\n".join(lines)


def _collect_namespaces(xml_text: str) -> dict[str, str]:
    ns_map: dict[str, str] = {}
    for match in re.finditer(r'xmlns:?(\w*)\s*=\s*"([^"]+)"', xml_text):
        prefix = match.group(1) or "default"
        uri = match.group(2)
        ns_map[prefix] = uri
    return ns_map


def _extract_value(
    root: ET.Element,
    ns_map: dict[str, str],
    tag_names: tuple[str, ...],
    transform=None,
) -> Any:
    for tag in tag_names:
        for ns_prefix, ns_uri in ns_map.items():
            if ns_prefix == "default":
                full_tag = f"{{{ns_uri}}}{tag}"
            else:
                full_tag = f"{{{ns_uri}}}{tag}"
            found = root.find(f".//{full_tag}")
            if found is not None and found.text:
                val = found.text.strip()
                if transform:
                    val = transform(val)
                if val:
                    return val
        found = root.find(f".//{tag}")
        if found is not None and found.text:
            val = found.text.strip()
            if transform:
                val = transform(val)
            if val:
                return val
    return None


def _extract_child_text(node: ET.Element, tag_names: tuple[str, ...]) -> str | None:
    for child in node.iter():
        child_tag = child.tag.rsplit("}", 1)[-1]
        if child_tag in tag_names and child.text and child.text.strip():
            return child.text.strip()
    return None


def _classify_notice_attachment_kind(name: str) -> str:
    lowered = name.lower()
    if any(token in lowered for token in ("обоснование нмцк", "обоснование начальной", "расчет нмцк", "расчёт нмцк")):
        return "estimate"
    if any(token in lowered for token in ("техническое задание", "техзад", " тз", "тз ", "technical specification")):
        return "technical_specification"
    if any(token in lowered for token in ("описание объекта закупки", "описание товара", "описание работ", "описание услуг", "ооз")):
        return "procurement_object_description"
    if any(token in lowered for token in ("спецификац",)):
        return "specification"
    if any(token in lowered for token in ("проект контракта", "проект договора", "контракт", "договор", "agreement", "contract")):
        return "contract_draft"
    if any(token in lowered for token in ("смет",)):
        return "estimate"
    if any(token in lowered for token in ("форма",)):
        return "form"
    if any(token in lowered for token in ("извещение", "epnotification", "notice")):
        return "eis_notice"
    return "attachment"


def _parse_price(value: str) -> float | None:
    try:
        cleaned = value.replace("\xa0", "").replace(" ", "").replace(",", ".")
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _normalize_date(value: str) -> str | None:
    if not value:
        return None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?))?([+-]\d{2}:?\d{2})?", value)
    if match:
        date_value = f"{match[3]}.{match[2]}.{match[1]}"
        time_value = match[4]
        timezone_value = match[5]
        if time_value:
            date_value += f" {time_value}"
        if timezone_value:
            date_value += f" {timezone_value}"
        return date_value
    match = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", value)
    if match:
        return value
    return value
