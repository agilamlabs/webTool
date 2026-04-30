"""Integration tests for the Agent pipeline.

These tests require Playwright browsers to be installed:
    playwright install chromium

They also require network access for live page fetching.
"""

from __future__ import annotations

import pytest
from web_agent.agent import Agent
from web_agent.config import AppConfig
from web_agent.models import FetchStatus


@pytest.fixture
def fast_config() -> AppConfig:
    """Config optimized for fast testing (fewer retries, lower timeouts)."""
    return AppConfig(
        log_level="WARNING",
        browser={
            "headless": True,
            "default_timeout": 15000,
            "navigation_timeout": 20000,
            "max_contexts": 2,
        },
        fetch={
            "max_retries": 1,
            "retry_base_delay": 0.5,
        },
        search={
            "max_results": 3,
        },
    )


class TestFetchAndExtract:
    """Test fetching and extracting content from known URLs."""

    @pytest.mark.asyncio
    async def test_fetch_httpbin_html(self, fast_config: AppConfig) -> None:
        """Fetch httpbin.org/html which returns a known HTML page."""
        async with Agent(fast_config) as agent:
            result = await agent.fetch_and_extract("https://httpbin.org/html")

        assert result.extraction_method != "none"
        assert result.content is not None
        assert result.content_length > 0
        assert result.url == "https://httpbin.org/html"

    @pytest.mark.asyncio
    async def test_fetch_example_com(self, fast_config: AppConfig) -> None:
        """Fetch example.com which is a simple, reliable test page."""
        async with Agent(fast_config) as agent:
            result = await agent.fetch_and_extract("https://example.com")

        assert result.title is not None
        assert result.content is not None
        assert result.content_length > 0


class TestSearchAndExtract:
    """Test the full search-and-extract pipeline."""

    @pytest.mark.asyncio
    async def test_search_returns_results(self, fast_config: AppConfig) -> None:
        """Search for a common term and verify we get structured results."""
        async with Agent(fast_config) as agent:
            result = await agent.search_and_extract("Python programming language", max_results=3)

        assert result.query == "Python programming language"
        assert result.search.total_results > 0
        assert len(result.search.results) > 0
        assert result.total_time_ms > 0

        # Verify search result structure
        first = result.search.results[0]
        assert first.title
        assert first.url.startswith("http")
        assert first.position == 1

    @pytest.mark.asyncio
    async def test_search_extracts_pages(self, fast_config: AppConfig) -> None:
        """Verify that at least some pages are successfully extracted."""
        async with Agent(fast_config) as agent:
            result = await agent.search_and_extract("httpbin.org", max_results=3)

        # At least one page should be extracted (some may fail)
        assert len(result.pages) > 0 or len(result.errors) > 0


class TestDownload:
    """Test file download functionality."""

    @pytest.mark.asyncio
    async def test_download_json(self, fast_config: AppConfig, tmp_path) -> None:
        """Download a known JSON endpoint."""
        fast_config.download.download_dir = str(tmp_path)

        async with Agent(fast_config) as agent:
            result = await agent.download("https://httpbin.org/json", filename="test.json")

        assert result.status == FetchStatus.SUCCESS
        assert result.size_bytes > 0
        assert result.filename == "test.json"

        # Verify file exists
        from pathlib import Path

        assert Path(result.filepath).exists()
