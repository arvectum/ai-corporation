import os
from pathlib import Path

import pytest

from src.modules.tender_operator_agent_demo.procurement_schemas import (
    ProcurementSearchRequest,
)
from src.modules.tender_operator_agent_demo.settings import ZakupkiSoapSettings
from src.modules.tender_operator_agent_demo.zakupki_soap_client import (
    ZakupkiSoapClient,
    parse_attachments_response,
    parse_details_response,
    parse_search_response,
)
from src.modules.tender_operator_agent_demo.zakupki_soap_templates import (
    build_search_envelope,
)
from src.shared.network.etp_trust import HostPolicy, TrustPolicy

FIXTURES = Path(__file__).parent / "fixtures" / "zakupki_soap"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _settings(token: str = "test-token-value-not-real") -> ZakupkiSoapSettings:
    return ZakupkiSoapSettings(
        enabled=True, token=token, timeout_seconds=7, max_results=5
    )


def test_client_disabled_without_token():
    client = ZakupkiSoapClient(
        ZakupkiSoapSettings(enabled=True, token="replace_me_do_not_commit_real_token")
    )

    assert client.is_configured() is False
    assert client.search_procurements(ProcurementSearchRequest(query="кабель")) == []


def test_injected_policy_and_diagnostics_are_instance_scoped(tmp_path: Path):
    policy = TrustPolicy(
        enabled=True,
        proxy_bypass_enabled=True,
        hosts=(HostPolicy("example.test", direct_connection=True),),
    )
    client = ZakupkiSoapClient(
        _settings(),
        trust_policy=policy,
        diagnostics_dir=tmp_path / "diagnostics",
        runtime_status_enabled=False,
        transport=lambda *_args: "<Envelope />",
    )
    client._post_soap(
        "<Envelope />",
        soap_action=None,
        endpoint_url="https://example.test/soap",
        method_name="test",
    )
    assert not (tmp_path / "diagnostics").exists()


def test_token_not_in_repr_status_or_errors():
    settings = _settings(token="secret-token-value")

    def failing_transport(
        _envelope: str, _soap_action: str | None, _timeout: int
    ) -> str:
        raise RuntimeError("upstream rejected secret-token-value")

    client = ZakupkiSoapClient(settings, transport=failing_transport)

    assert "secret-token-value" not in repr(settings)
    assert "secret-token-value" not in str(settings.safe_status())
    with pytest.raises(RuntimeError) as excinfo:
        client.search_procurements(ProcurementSearchRequest(query="кабель"))
    assert "secret-token-value" not in str(excinfo.value)
    assert "[redacted]" in str(excinfo.value)


def test_search_envelope_contains_query_and_token_in_single_template_layer():
    envelope = build_search_envelope(
        ProcurementSearchRequest(query="шкаф управления", law="44-ФЗ", max_results=1),
        token="test-token-value-not-real",
    )

    assert "шкаф управления" in envelope
    assert "44-ФЗ" in envelope
    assert "test-token-value-not-real" in envelope
    assert "searchProcurements" in envelope


def test_mocked_search_xml_maps_to_normalized_results():
    results = parse_search_response(_fixture("search_response.xml"))

    assert len(results) == 1
    assert results[0].procurement_id == "eis-001"
    assert results[0].notice_number == "0373100000126000001"
    assert results[0].source == "zakupki_gov_ru_soap_legacy"
    assert results[0].can_download_attachments is True
    assert results[0].warnings == []


def test_mocked_details_xml_maps_to_details():
    details = parse_details_response(_fixture("details_response.xml"))

    assert details.procurement.procurement_id == "eis-001"
    assert details.procurement.customer_name == "Промышленный заказчик"
    assert (
        details.raw_source_summary
        == "Синтетическая карточка закупки для тестов SOAP parser."
    )


def test_real_shaped_search_xml_maps_dates_money_and_warnings():
    results = parse_search_response(_fixture("real_shaped_search_response.xml"))

    assert len(results) == 2
    assert results[0].procurement_id == "real-shaped-001"
    assert results[0].customer_name == 'АО "Демо-заказчик"'
    assert results[0].publication_date == "2026-06-24"
    assert results[0].deadline == "2026-06-30"
    assert results[0].initial_price == 12500000.50
    assert results[0].attachments_status == "downloadable"
    assert results[1].initial_price is None
    assert results[1].customer_name == "Не указан"
    assert results[1].attachments_status == "manual_upload_required"
    assert results[1].warnings


def test_real_shaped_details_response_maps_partial_details():
    details = parse_details_response(_fixture("real_shaped_details_response.xml"))

    assert details.procurement.procurement_id == "real-details-001"
    assert details.procurement.notice_number == "0373100000126000003"
    assert details.procurement.publication_date == "2026-06-23"
    assert details.procurement.deadline == "2026-06-27"
    assert details.procurement.initial_price == 4750000.00
    assert (
        details.raw_source_summary
        == "Детализированная карточка для real-shaped parser tests."
    )


def test_empty_search_response_does_not_crash():
    assert (
        parse_search_response('<?xml version="1.0"?><Envelope><Body/></Envelope>') == []
    )


def test_mocked_attachments_xml_maps_to_attachments():
    attachments = parse_attachments_response(_fixture("attachments_response.xml"))

    assert len(attachments) == 2
    assert attachments[0].attachment_id == "att-001"
    assert attachments[0].extension == ".txt"
    assert attachments[0].can_download is True


def test_real_shaped_attachments_response_maps_url_and_manual_fallback():
    attachments = parse_attachments_response(
        _fixture("real_shaped_attachments_response.xml")
    )

    assert len(attachments) == 2
    assert attachments[0].attachment_id == "doc-001"
    assert attachments[0].can_download is True
    assert attachments[1].attachment_id == "doc-002"
    assert attachments[1].can_download is False
    assert attachments[1].requires_manual_upload is True
    assert attachments[1].warnings


def test_soap_transport_uses_timeout_and_mocked_response():
    calls = []

    def transport(envelope: str, soap_action: str | None, timeout: int) -> str:
        calls.append((envelope, soap_action, timeout))
        return _fixture("search_response.xml")

    client = ZakupkiSoapClient(_settings(), transport=transport)
    results = client.search_procurements(ProcurementSearchRequest(query="кабель"))

    assert results
    assert calls[0][1] == "searchProcurements"
    assert calls[0][2] == 7


def test_soap_actions_can_be_configured():
    calls = []

    def transport(_envelope: str, soap_action: str | None, _timeout: int) -> str:
        calls.append(soap_action)
        return _fixture("search_response.xml")

    settings = ZakupkiSoapSettings(
        enabled=True,
        token="test-token-value-not-real",
        search_action="urn:custom-search",
        details_action="urn:custom-details",
        attachments_action="urn:custom-attachments",
    )
    client = ZakupkiSoapClient(settings, transport=transport)
    client.search_procurements(ProcurementSearchRequest(query="кабель"))

    assert calls == ["urn:custom-search"]


def test_debug_artifacts_are_sanitized(tmp_path, monkeypatch):
    diagnostics_dir = tmp_path / "soap_diagnostics"
    monkeypatch.setenv("AI_CORP_ZAKUPKI_SOAP_DIAGNOSTICS_DIR", str(diagnostics_dir))
    secret = "secret-token-value"
    settings = ZakupkiSoapSettings(enabled=True, token=secret, debug=True)

    def transport(_envelope: str, _soap_action: str | None, _timeout: int) -> str:
        return f"<response><token>{secret}</token></response>"

    client = ZakupkiSoapClient(settings, transport=transport)
    client.search_procurements(ProcurementSearchRequest(query="кабель"))

    request_dump = (diagnostics_dir / "last_request.xml").read_text(encoding="utf-8")
    response_dump = (diagnostics_dir / "last_response.xml").read_text(encoding="utf-8")
    assert secret not in request_dump
    assert secret not in response_dump
    assert "[redacted]" in request_dump
    assert "[redacted]" in response_dump


def test_xml_with_dtd_is_rejected():
    payload = """<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><foo>&xxe;</foo>"""

    with pytest.raises(ValueError, match="forbidden DTD"):
        parse_search_response(payload)


@pytest.mark.skipif(
    not os.getenv("ZAKUPKI_GOV_RU_SOAP_LIVE_TEST"), reason="live SOAP smoke is opt-in"
)
def test_live_zakupki_soap_search_smoke():
    settings = ZakupkiSoapSettings.from_env()
    client = ZakupkiSoapClient(settings)

    try:
        results = client.search_procurements(
            ProcurementSearchRequest(
                source="zakupki_gov_ru_soap_legacy",
                query="электротехническое оборудование",
                max_results=1,
            )
        )
    except RuntimeError as exc:
        message = str(exc)
        assert "SOAP-запрос к ЕИС завершился ошибкой:" in message
        assert "[redacted]" not in message
        assert "token" not in message.lower()
    else:
        assert isinstance(results, list)
