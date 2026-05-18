"""v1.6.5 medium-severity fixes.

- M6: cache hit preserves original ``searched_at``; from_cache=True is
  the only staleness signal.
- M7: ``async_retry`` rejects ``max_retries=0`` at decorator-construction
  time instead of raising a bogus TypeError at call time.
- M8: ``find_and_download_file`` recovers extensionless binary URLs via
  HEAD probe.
- M9: ``_unwrap_search_url`` truncates oversized SERP queries.
- M10: dead ``_classify_message`` machinery removed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

# ----------------------------------------------------------------------
# M10: dead-code removal -- module-level symbols are gone
# ----------------------------------------------------------------------


def test_classify_message_helper_removed():
    """The deprecated message-prefix classifier is gone in v1.6.5."""
    import web_agent.agent as agent_mod

    assert not hasattr(agent_mod, "_classify_message")
    assert not hasattr(agent_mod, "_to_structured")
    assert not hasattr(agent_mod, "_MESSAGE_PREFIX_CODES")
    # The replacement is still here
    assert hasattr(agent_mod, "_MessageBag")


# ----------------------------------------------------------------------
# M6: cache preserves searched_at
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_cache_hit_preserves_searched_at():
    """A cached SearchResponse must come back with its ORIGINAL searched_at."""
    from web_agent.config import AppConfig
    from web_agent.models import SearchResponse, SearchResultItem
    from web_agent.search_engine import SearchEngine

    original_ts = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    cached_payload = {
        "query": "x",
        "total_results": 1,
        "results": [
            SearchResultItem(
                position=1, title="A", url="https://x.com/a", provider="searxng"
            ).model_dump()
        ],
        "searched_at": original_ts.isoformat(),
        "from_cache": False,
    }

    fake_cache = AsyncMock()
    fake_cache.get = AsyncMock(return_value=cached_payload)

    engine = SearchEngine.__new__(SearchEngine)  # bypass __init__
    engine._config = AppConfig()
    engine._cache = fake_cache
    engine._providers = []  # not reached on cache hit

    response: SearchResponse = await engine.search("x", 10)
    assert response.from_cache is True
    # The original searched_at must NOT be rewritten to "now"
    assert response.searched_at == original_ts


# ----------------------------------------------------------------------
# M7: async_retry rejects max_retries < 1 at construction
# ----------------------------------------------------------------------


def test_async_retry_rejects_zero_max_retries():
    from web_agent.utils import async_retry

    with pytest.raises(ValueError, match="max_retries must be >= 1"):
        async_retry(max_retries=0)


def test_async_retry_rejects_negative_max_retries():
    from web_agent.utils import async_retry

    with pytest.raises(ValueError, match="max_retries must be >= 1"):
        async_retry(max_retries=-1)


def test_async_retry_accepts_one_retry():
    from web_agent.utils import async_retry

    # Should not raise
    decorator = async_retry(max_retries=1)
    assert callable(decorator)


# ----------------------------------------------------------------------
# M9: _unwrap_search_url length cap
# ----------------------------------------------------------------------


def test_unwrap_search_url_truncates_oversize_query():
    from web_agent.agent import _MAX_UNWRAPPED_QUERY_LEN, _unwrap_search_url

    huge_q = "a" * 5000
    url = f"https://www.google.com/search?q={huge_q}"
    result = _unwrap_search_url(url)
    assert result is not None
    assert len(result) == _MAX_UNWRAPPED_QUERY_LEN


def test_unwrap_search_url_normal_query_unchanged():
    from web_agent.agent import _unwrap_search_url

    url = "https://www.google.com/search?q=python+web+scraping"
    result = _unwrap_search_url(url)
    assert result == "python web scraping"


def test_unwrap_search_url_empty_query_returns_none():
    from web_agent.agent import _unwrap_search_url

    assert _unwrap_search_url("https://www.google.com/search?q=") is None


def test_unwrap_search_url_non_serp_returns_none():
    from web_agent.agent import _unwrap_search_url

    assert _unwrap_search_url("https://example.com/?q=foo") is None


# ----------------------------------------------------------------------
# M8: find_and_download_file recovers extensionless binary URLs
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_and_download_file_recovers_extensionless_pdf(tmp_path):
    """Extensionless URLs whose HEAD says binary should be downloadable."""
    from web_agent.agent import Agent
    from web_agent.config import AppConfig, DownloadConfig
    from web_agent.models import (
        DownloadResult,
        FetchStatus,
        SearchResponse,
        SearchResultItem,
    )

    config = AppConfig(download=DownloadConfig(download_dir=str(tmp_path)))
    agent = Agent(config)
    # Search returns a single extensionless result
    agent._search.search = AsyncMock(
        return_value=SearchResponse(
            query="report",
            total_results=1,
            results=[
                SearchResultItem(
                    position=1,
                    title="Annual Report",
                    url="https://regulator.example.com/Archives/12345",
                    provider="searxng",
                )
            ],
        )
    )
    # v1.6.10: classify_url returns granular kinds; "pdf" satisfies
    # find_and_download_file's filter for file_types=["pdf"].
    agent._fetcher.classify_url = AsyncMock(return_value="pdf")
    # Downloader is mocked to assert that we got the extensionless URL
    agent._downloader.download = AsyncMock(
        return_value=DownloadResult(
            url="https://regulator.example.com/Archives/12345",
            filepath=str(tmp_path / "12345.pdf"),
            filename="12345.pdf",
            status=FetchStatus.SUCCESS,
        )
    )

    result = await agent.find_and_download_file("annual report", file_types=["pdf"])
    assert result.status == FetchStatus.SUCCESS
    agent._fetcher.classify_url.assert_called_once()
    agent._downloader.download.assert_called_once()
    args, _ = agent._downloader.download.call_args
    assert args[0] == "https://regulator.example.com/Archives/12345"


@pytest.mark.asyncio
async def test_find_and_download_file_skips_extensionless_when_probe_disabled(tmp_path):
    """When probe_binary_urls=False, extensionless URLs are NOT considered."""
    from web_agent.agent import Agent
    from web_agent.config import AppConfig, DownloadConfig, SafetyConfig
    from web_agent.models import (
        FetchStatus,
        SearchResponse,
        SearchResultItem,
    )

    config = AppConfig(
        download=DownloadConfig(download_dir=str(tmp_path)),
        safety=SafetyConfig(probe_binary_urls=False),
    )
    agent = Agent(config)
    agent._search.search = AsyncMock(
        return_value=SearchResponse(
            query="x",
            total_results=1,
            results=[
                SearchResultItem(
                    position=1,
                    title="x",
                    url="https://x.com/Archives/12345",
                    provider="searxng",
                )
            ],
        )
    )
    agent._fetcher.classify_url = AsyncMock()  # should NOT be called

    result = await agent.find_and_download_file("x", file_types=["pdf"])
    assert result.status == FetchStatus.NETWORK_ERROR
    assert "No file URL matching" in (result.error_message or "")
    agent._fetcher.classify_url.assert_not_called()


@pytest.mark.asyncio
async def test_find_and_download_file_extensionless_html_not_downloaded(tmp_path):
    """Extensionless URLs that classify as 'html' must NOT be downloaded."""
    from web_agent.agent import Agent
    from web_agent.config import AppConfig, DownloadConfig
    from web_agent.models import (
        FetchStatus,
        SearchResponse,
        SearchResultItem,
    )

    config = AppConfig(download=DownloadConfig(download_dir=str(tmp_path)))
    agent = Agent(config)
    agent._search.search = AsyncMock(
        return_value=SearchResponse(
            query="x",
            total_results=1,
            results=[
                SearchResultItem(
                    position=1,
                    title="x",
                    url="https://x.com/blog/post",
                    provider="searxng",
                )
            ],
        )
    )
    agent._fetcher.classify_url = AsyncMock(return_value="html")
    agent._downloader.download = AsyncMock()  # must not be called

    result = await agent.find_and_download_file("x", file_types=["pdf"])
    assert result.status == FetchStatus.NETWORK_ERROR
    agent._downloader.download.assert_not_called()
