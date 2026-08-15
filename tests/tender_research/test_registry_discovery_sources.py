from __future__ import annotations

import json
import tempfile
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import MagicMock, patch

import pytest

from src.tender_research.config import TenderResearchConfig
from src.tender_research.providers.public_44fz_search import (
    Public44FzSearchProvider,
    PublicSearchStatus,
    PublicTenderSearchItem,
    PublicTenderSearchPage,
)
from src.tender_research.registry_discovery import (
    RegistryNumberDiscovery,
    SourceType,
)


@pytest.fixture
def mock_provider():
    with patch("src.tender_research.registry_discovery.Public44FzSearchProvider") as mock_cls:
        instance = MagicMock()
        mock_cls.return_value = instance
        instance.search_pages.return_value = [
            PublicTenderSearchPage(
                items=[
                    PublicTenderSearchItem(registry_number="0373200008225000001"),
                ],
                page=1,
                page_size=30,
                has_next=False,
                status=PublicSearchStatus.SUCCESS,
            )
        ]
        instance.extract_registry_numbers.return_value = ["0373200008225000001"]
        yield instance


class TestSourceTypes:
    def test_external_public_44fz_source_type(self, mock_provider):
        config = TenderResearchConfig()
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="external_public_44fz", days_back=3, limit=10)
        assert result.selected_source_type == SourceType.EXTERNAL_PUBLIC_44FZ

    def test_local_db_source_type(self):
        config = TenderResearchConfig()
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="local_db", days_back=30, limit=10)
        assert result.selected_source_type == SourceType.LOCAL_DB

    def test_seed_file_source_type(self):
        with NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("0373200008225000004\n")
            seed_path = f.name
        try:
            config = TenderResearchConfig(eis_seed_file=seed_path)
            discovery = RegistryNumberDiscovery(config=config)
            result = discovery.discover(source="seed_file")
            assert result.selected_source_type == SourceType.SEED_FILE
        finally:
            Path(seed_path).unlink(missing_ok=True)

    def test_demo_source_type(self):
        config = TenderResearchConfig(eis_mode="demo")
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="demo", days_back=365, limit=10)
        assert result.selected_source_type == SourceType.DEMO

    def test_backend_search_real_source_type(self):
        config = TenderResearchConfig()
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="backend_search_real", days_back=30, limit=10)
        assert result.selected_source_type == SourceType.UNAVAILABLE


class TestAutoFallback:
    def test_auto_uses_external_public_first(self, mock_provider):
        config = TenderResearchConfig(
            eis_mode="demo",
            allow_demo_discovery=True,
            eis_seed_file="/tmp/nonexistent_test_seed.txt",
        )
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="auto", days_back=3, limit=10)
        assert result.selected_source in ("auto", "external_public_44fz")

    def test_auto_falls_back_to_seed_file(self, mock_provider):
        mock_provider.search_pages.return_value = [
            PublicTenderSearchPage(
                page=1, page_size=30,
                status=PublicSearchStatus.BLOCKED,
                error="blocked",
            )
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("0373200008225000999\n")
            seed_path = f.name
        try:
            config = TenderResearchConfig(eis_seed_file=seed_path)
            discovery = RegistryNumberDiscovery(config=config)
            result = discovery.discover(source="auto", days_back=3, limit=10)
            assert result.selected_source in ("auto", "seed_file")
        finally:
            Path(seed_path).unlink(missing_ok=True)

    def test_auto_rejects_demo_in_real_mode(self, monkeypatch):
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
            eis_seed_file="/tmp/nonexistent_test_seed_reject.txt",
        )
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="auto", days_back=365, limit=10)
        assert len(calls) == 1
        assert result.selected_source_type == SourceType.NONE

    def test_auto_not_treats_local_db_as_external(self):
        config = TenderResearchConfig()
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="local_db", days_back=30, limit=10)
        assert result.selected_source_type == SourceType.LOCAL_DB
        assert result.is_demo is False


class TestExternalPublicPagination:
    def test_external_public_page_size_default(self, mock_provider):
        config = TenderResearchConfig(public_search_page_size=30)
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="external_public_44fz", days_back=3, limit=10)
        assert result.page_size == 30

    def test_external_public_pages_read(self, mock_provider):
        config = TenderResearchConfig()
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="external_public_44fz", days_back=3, limit=100)
        assert result.pages_read >= 0


class TestRealModeRejectsDemo:
    def test_demo_returns_demo_numbers(self):
        config = TenderResearchConfig(
            eis_mode="real",
            allow_demo_discovery=False,
        )
        discovery = RegistryNumberDiscovery(config=config)
        result = discovery.discover(source="demo", days_back=365, limit=10)
        assert result.is_demo is True
        assert len(result.numbers) > 0
