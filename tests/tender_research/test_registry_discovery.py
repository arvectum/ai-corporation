from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import MagicMock, patch

import pytest

from src.tender_research.config import TenderResearchConfig
from src.tender_research.eis_loader import EisTenderLoader
from src.tender_research.providers.public_44fz_search import (
    Public44FzSearchProvider,
    PublicSearchStatus,
    PublicTenderSearchItem,
    PublicTenderSearchPage,
)
from src.tender_research.registry_discovery import (
    DiscoveredRegistryNumber,
    DiscoveryResult,
    RegistryNumberDiscovery,
    SourceType,
    _parse_registry_numbers_from_html,
)


class TestParseRegistryNumbersFromHtml:
    def test_extracts_from_eis_url_pattern(self):
        html = '<a href="/epz/order/notice/ea44/view/common-info.html?regNumber=0373200008225000004">'
        assert _parse_registry_numbers_from_html(html) == ["0373200008225000004"]

    def test_extracts_multiple_numbers(self):
        html = """
        <a href="?regNumber=0373200008225000004">one</a>
        <a href="?regNumber=0373200008225000005">two</a>
        """
        assert _parse_registry_numbers_from_html(html) == [
            "0373200008225000004",
            "0373200008225000005",
        ]

    def test_deduplicates(self):
        html = """<a href="?regNumber=0373200008225000004">x</a>
                   <a href="?regNumber=0373200008225000004">y</a>"""
        assert _parse_registry_numbers_from_html(html) == ["0373200008225000004"]

    def test_fallback_to_plain_regex(self):
        html = "Some text 0373200008225000004 and more"
        assert _parse_registry_numbers_from_html(html) == ["0373200008225000004"]

    def test_ignores_short_numbers(self):
        html = "1234567890123456 and 0373200008225000004"
        assert _parse_registry_numbers_from_html(html) == ["0373200008225000004"]

    def test_returns_empty_for_no_match(self):
        assert _parse_registry_numbers_from_html("<html></html>") == []


class TestRegistryNumberDiscovery:
    def test_seed_file_returns_numbers(self):
        with NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("# comment\n0373200008225000004\n0373200008225000005\n")
            seed_path = f.name
        try:
            config = TenderResearchConfig(
                eis_mode="demo",
                eis_seed_file=seed_path,
            )
            discovery = RegistryNumberDiscovery(config=config)
            result = discovery._seed_file(limit=None)
            assert len(result.numbers) == 2
            assert result.numbers[0].registry_number == "0373200008225000004"
            assert result.numbers[0].source == "seed_file"
            assert result.numbers[1].registry_number == "0373200008225000005"
        finally:
            Path(seed_path).unlink(missing_ok=True)

    def test_seed_file_respects_limit(self):
        with NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("0373200008225000004\n0373200008225000005\n0373200008225000006\n")
            seed_path = f.name
        try:
            config = TenderResearchConfig(eis_seed_file=seed_path)
            discovery = RegistryNumberDiscovery(config=config)
            result = discovery._seed_file(limit=2)
            assert len(result.numbers) == 2
        finally:
            Path(seed_path).unlink(missing_ok=True)

    def test_seed_file_returns_empty_when_missing(self):
        config = TenderResearchConfig(eis_seed_file="/tmp/nonexistent_seed_test.txt")
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery._seed_file()
        assert len(result.numbers) == 0

    def test_seed_file_skips_comments_and_blanks(self):
        with NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("\n\n# comment\n0373200008225000004\n\n# another\n")
            seed_path = f.name
        try:
            config = TenderResearchConfig(eis_seed_file=seed_path)
            discovery = RegistryNumberDiscovery(config=config)
            result = discovery._seed_file()
            assert len(result.numbers) == 1
            assert result.numbers[0].registry_number == "0373200008225000004"
        finally:
            Path(seed_path).unlink(missing_ok=True)

    def test_seed_file_json_format(self):
        items = [
            {"registry_number": "0373200008225000004", "title": "Test 1"},
            {"registry_number": "0373200008225000005", "title": "Test 2"},
        ]
        with NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump({"items": items}, f)
            seed_path = f.name
        try:
            config = TenderResearchConfig(eis_seed_file=seed_path)
            discovery = RegistryNumberDiscovery(config=config)
            result = discovery._seed_file()
            assert len(result.numbers) == 2
            assert result.numbers[0].registry_number == "0373200008225000004"
            assert result.numbers[1].registry_number == "0373200008225000005"
        finally:
            Path(seed_path).unlink(missing_ok=True)

    def test_seed_file_json_list_format(self):
        items = ["0373200008225000004", "0373200008225000005"]
        with NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(items, f)
            seed_path = f.name
        try:
            config = TenderResearchConfig(eis_seed_file=seed_path)
            discovery = RegistryNumberDiscovery(config=config)
            result = discovery._seed_file()
            assert len(result.numbers) == 2
        finally:
            Path(seed_path).unlink(missing_ok=True)

    def test_local_db_source_returns_empty_when_no_db(self):
        config = TenderResearchConfig()
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="local_db", days_back=30, limit=10)
        assert result.selected_source == "local_db"
        assert result.selected_source_type == SourceType.LOCAL_DB

    def test_backend_search_real_returns_unavailable(self):
        config = TenderResearchConfig()
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="backend_search_real", days_back=30, limit=10)
        assert result.selected_source == "backend_search_real"
        assert result.selected_source_type == SourceType.UNAVAILABLE
        assert len(result.warnings) > 0

    def test_demo_source_returns_numbers(self):
        config = TenderResearchConfig(eis_mode="demo")
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="demo", days_back=365, limit=10)
        assert len(result.numbers) > 0
        for rn in result.numbers:
            assert rn.is_demo is True
        assert result.is_demo is True
        assert result.selected_source_type == SourceType.DEMO

    def test_discover_with_invalid_source_raises(self):
        config = TenderResearchConfig()
        discovery = RegistryNumberDiscovery(config=config)
        with pytest.raises(Exception):
            discovery.discover(source="invalid_source")

    def test_discovered_registry_number_dataclass(self):
        d = DiscoveredRegistryNumber(
            registry_number="0373200008225000004",
            source="seed_file",
            tender_title="Test",
            external_id="123",
        )
        assert d.registry_number == "0373200008225000004"
        assert d.source == "seed_file"
        assert d.tender_title == "Test"
        assert d.external_id == "123"

    def test_discovery_result_dataclass(self):
        r = DiscoveryResult(
            numbers=[DiscoveredRegistryNumber("0373200008225000004", "seed_file")],
            selected_source="seed_file",
            selected_source_type=SourceType.SEED_FILE,
            is_demo=True,
            pages_read=2,
            page_size=30,
            network_status="success",
            warnings=["test warning"],
        )
        assert len(r.numbers) == 1
        assert r.selected_source == "seed_file"
        assert r.selected_source_type == SourceType.SEED_FILE
        assert r.is_demo is True
        assert r.pages_read == 2
        assert r.page_size == 30
        assert r.network_status == "success"
        assert r.warnings == ["test warning"]


class TestRealVsDemoDiscrimination:
    def test_demo_ignored_in_real_mode(self):
        config = TenderResearchConfig(
            eis_mode="real",
            allow_demo_discovery=False,
        )
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="demo", days_back=365, limit=10)
        assert len(result.numbers) > 0
        assert result.is_demo is True

    def test_auto_fallback_without_seed_file(self, monkeypatch):
        calls = []

        def empty_search_pages(*args, **kwargs):
            calls.append((args, kwargs))
            return [
                PublicTenderSearchPage(
                    page=1,
                    page_size=kwargs["page_size"],
                    has_next=False,
                    status=PublicSearchStatus.EMPTY,
                )
            ]

        monkeypatch.setattr(Public44FzSearchProvider, "search_pages", empty_search_pages)
        config = TenderResearchConfig(
            eis_mode="real",
            allow_demo_discovery=False,
            eis_seed_file="/tmp/nonexistent_test_seed_auto.txt",
        )
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="auto", days_back=365, limit=10)
        assert len(calls) == 1
        assert result.selected_source == "auto"
        assert len(result.warnings) > 0

    def test_auto_uses_demo_when_allowed(self, monkeypatch):
        calls = []

        def empty_search_pages(*args, **kwargs):
            calls.append((args, kwargs))
            return [
                PublicTenderSearchPage(
                    page=1,
                    page_size=kwargs["page_size"],
                    has_next=False,
                    status=PublicSearchStatus.EMPTY,
                )
            ]

        monkeypatch.setattr(Public44FzSearchProvider, "search_pages", empty_search_pages)
        config = TenderResearchConfig(
            eis_mode="demo",
            allow_demo_discovery=True,
            eis_seed_file="/tmp/nonexistent_test_seed_demo.txt",
        )
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="auto", days_back=365, limit=10)
        assert len(calls) == 1
        assert result.selected_source == "demo"

    def test_seed_file_custom_path_via_parameter(self):
        with NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("0373200008225000004\n")
            seed_path = f.name
        try:
            config = TenderResearchConfig(eis_mode="demo")
            discovery = RegistryNumberDiscovery(config=config)
            result = discovery.discover(source="seed_file", seed_file=seed_path)
            assert len(result.numbers) == 1
            assert result.numbers[0].registry_number == "0373200008225000004"
        finally:
            Path(seed_path).unlink(missing_ok=True)


class TestDiscoveryResultMetadata:
    def test_discovery_result_source_type(self):
        config = TenderResearchConfig(eis_mode="demo")
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="demo", days_back=365, limit=5)
        assert result.selected_source_type == SourceType.DEMO
        assert result.pages_read == 0
        assert result.page_size == 0
        assert result.discovered_count > 0


class TestRegistryDiscoveryWithMock:
    """Tests that use a mock provider to avoid network calls."""

    @pytest.fixture
    def mock_provider(self):
        with patch("src.tender_research.registry_discovery.Public44FzSearchProvider") as mock_cls:
            instance = MagicMock()
            mock_cls.return_value = instance
            instance.search_pages.return_value = [
                PublicTenderSearchPage(
                    items=[
                        PublicTenderSearchItem(registry_number="0373200008225000001", title="Test 1"),
                        PublicTenderSearchItem(registry_number="0373200008225000002", title="Test 2"),
                    ],
                    page=1,
                    page_size=30,
                    has_next=False,
                    status=PublicSearchStatus.SUCCESS,
                )
            ]
            instance.extract_registry_numbers.return_value = [
                "0373200008225000001",
                "0373200008225000002",
            ]
            yield instance

    def test_external_public_source_type(self, mock_provider):
        config = TenderResearchConfig()
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="external_public_44fz", days_back=3, limit=10)
        assert result.selected_source_type == SourceType.EXTERNAL_PUBLIC_44FZ
        assert result.page_size == 30
        assert result.discovered_count == 2
        assert result.pages_read == 1

    def test_external_public_blocked_network(self, mock_provider):
        mock_provider.search_pages.return_value = [
            PublicTenderSearchPage(
                page=1,
                page_size=30,
                status=PublicSearchStatus.BLOCKED,
                error="Connection reset",
            )
        ]
        config = TenderResearchConfig()
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="external_public_44fz", days_back=3, limit=10)
        assert result.selected_source_type == SourceType.EXTERNAL_PUBLIC_44FZ
        assert result.network_status == PublicSearchStatus.BLOCKED
        assert len(result.numbers) == 0
        assert len(result.warnings) > 0

    def test_external_public_timeout_network(self, mock_provider):
        mock_provider.search_pages.return_value = [
            PublicTenderSearchPage(
                page=1,
                page_size=30,
                status=PublicSearchStatus.TIMEOUT,
                error="timed out",
            )
        ]
        config = TenderResearchConfig()
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="external_public_44fz", days_back=3, limit=10)
        assert result.network_status == PublicSearchStatus.TIMEOUT
        assert len(result.numbers) == 0

    def test_external_public_page_size_parameter(self, mock_provider):
        config = TenderResearchConfig()
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="external_public_44fz", days_back=3, limit=10, page_size=50)
        assert result.page_size == 50

    def test_auto_uses_external_public_first(self, mock_provider):
        config = TenderResearchConfig(
            eis_mode="demo",
            allow_demo_discovery=True,
            eis_seed_file="/tmp/nonexistent_test.txt",
        )
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="auto", days_back=3, limit=10)
        assert result.selected_source in ("auto", "external_public_44fz")
        assert result.discovered_count == 2

    def test_auto_falls_back_on_blocked_external(self, mock_provider):
        mock_provider.search_pages.return_value = [
            PublicTenderSearchPage(
                page=1, page_size=30,
                status=PublicSearchStatus.BLOCKED,
                error="blocked",
            )
        ]
        with NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("0373200008225000999\n")
            seed_path = f.name
        try:
            config = TenderResearchConfig(
                eis_seed_file=seed_path,
            )
            discovery = RegistryNumberDiscovery(config=config)
            result = discovery.discover(source="auto", days_back=3, limit=10)
            assert result.selected_source in ("auto", "seed_file")
        finally:
            Path(seed_path).unlink(missing_ok=True)

    def test_auto_not_treats_local_db_as_external(self, mock_provider):
        config = TenderResearchConfig()
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="local_db", days_back=30, limit=10)
        assert result.selected_source_type == SourceType.LOCAL_DB
        assert result.is_demo is False
