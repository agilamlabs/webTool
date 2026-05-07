"""v1.6.3 routing fixes: direct-URL branch + classify_url(session_id) + url_ext_classification.

Covers issues #1, #2, #3.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.web_fetcher import _url_ext_classification

# ----------------------------------------------------------------------
# _url_ext_classification (#2 helper)
# ----------------------------------------------------------------------


def test_known_binary_extension():
    assert _url_ext_classification("https://x.com/report.pdf") == "binary"
    assert _url_ext_classification("https://x.com/sheet.xlsx") == "binary"
    assert _url_ext_classification("https://x.com/letter.docx") == "binary"
    assert _url_ext_classification("https://x.com/data.csv") == "binary"


def test_known_html_extension():
    assert _url_ext_classification("https://x.com/page.html") == "html"
    assert _url_ext_classification("https://x.com/page.htm") == "html"
    assert _url_ext_classification("https://x.com/page.aspx") == "html"
    assert _url_ext_classification("https://x.com/page.asp") == "html"
    assert _url_ext_classification("https://x.com/page.php") == "html"
    assert _url_ext_classification("https://x.com/page.jsp") == "html"
    assert _url_ext_classification("https://x.com/page.xhtml") == "html"


def test_extensionless_url_unknown():
    assert _url_ext_classification("https://x.com/download/123") == "unknown"
    assert _url_ext_classification("https://x.com/articles/foo") == "unknown"
    assert _url_ext_classification("https://x.com/") == "unknown"


def test_query_string_does_not_affect_classification():
    """Extensions are checked on path only, not query string."""
    assert _url_ext_classification("https://x.com/foo?file=report.pdf") == "unknown"
    assert _url_ext_classification("https://x.com/page.html?tracker=abc") == "html"


# ----------------------------------------------------------------------
# classify_url(session_id=...) -- #3
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_url_uses_session_cookies():
    """classify_url with session_id should pull cookies from the session."""
    from web_agent.config import AppConfig
    from web_agent.web_fetcher import WebFetcher

    bm = MagicMock()
    sessions = MagicMock()
    fake_ctx = MagicMock()
    fake_ctx.cookies = AsyncMock(return_value=[{"name": "auth", "value": "secret-token"}])
    sessions.get = MagicMock(return_value=fake_ctx)

    fetcher = WebFetcher(bm, AppConfig(), sessions=sessions)
    cookies = await fetcher._cookies_for_session("session-1")
    assert cookies == {"auth": "secret-token"}


@pytest.mark.asyncio
async def test_classify_url_extension_short_circuits_no_session_lookup():
    """When extension determines classification, no session probe is needed."""
    from web_agent.config import AppConfig
    from web_agent.web_fetcher import WebFetcher

    sessions = MagicMock()
    sessions.get = MagicMock(side_effect=AssertionError("should not be called"))
    fetcher = WebFetcher(MagicMock(), AppConfig(), sessions=sessions)
    # .pdf -> 'binary' without consulting sessions
    result = await fetcher.classify_url("https://x.com/a.pdf", session_id="anything")
    assert result == "binary"


# ----------------------------------------------------------------------
# Direct-URL branch in search_and_extract -- #1
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_and_extract_direct_pdf_url_routes_to_fetch_binary():
    """search_and_extract('https://x.com/a.pdf') must use fetch_binary, not fetch."""
    from web_agent.agent import Agent
    from web_agent.config import AppConfig
    from web_agent.models import FetchResult, FetchStatus

    agent = Agent(AppConfig())
    agent._fetcher.fetch_binary = AsyncMock(
        return_value=FetchResult(
            url="https://x.com/a.pdf",
            final_url="https://x.com/a.pdf",
            status=FetchStatus.SUCCESS,
            binary=b"%PDF-1.4 fake",
            content_type="application/pdf",
        )
    )
    agent._fetcher.fetch = AsyncMock()

    result = await agent.search_and_extract("https://x.com/a.pdf")
    agent._fetcher.fetch_binary.assert_called_once()
    agent._fetcher.fetch.assert_not_called()
    # AgentResult should have one page (the extracted PDF)
    assert len(result.pages) == 1
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].provider == "direct"


@pytest.mark.asyncio
async def test_search_and_extract_direct_extensionless_pdf_routes_to_fetch_binary():
    """search_and_extract on an extensionless PDF URL goes through HEAD probe."""
    from web_agent.agent import Agent
    from web_agent.config import AppConfig
    from web_agent.models import FetchResult, FetchStatus

    agent = Agent(AppConfig())
    agent._fetcher.classify_url = AsyncMock(return_value="binary")
    agent._fetcher.fetch_binary = AsyncMock(
        return_value=FetchResult(
            url="https://x.com/download/42",
            final_url="https://x.com/download/42",
            status=FetchStatus.SUCCESS,
            binary=b"%PDF-1.4 fake",
            content_type="application/pdf",
        )
    )
    agent._fetcher.fetch = AsyncMock()

    await agent.search_and_extract("https://x.com/download/42")
    agent._fetcher.classify_url.assert_called_once()
    agent._fetcher.fetch_binary.assert_called_once()
    agent._fetcher.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_search_and_extract_direct_html_url_uses_fetch():
    """Plain HTML URLs still go through the HTML fetch path."""
    from web_agent.agent import Agent
    from web_agent.config import AppConfig
    from web_agent.models import FetchResult, FetchStatus

    agent = Agent(AppConfig())
    agent._fetcher.classify_url = AsyncMock(return_value="html")
    agent._fetcher.fetch = AsyncMock(
        return_value=FetchResult(
            url="https://x.com/article",
            final_url="https://x.com/article",
            status=FetchStatus.SUCCESS,
            html="<html><body>Hi</body></html>",
        )
    )
    agent._fetcher.fetch_binary = AsyncMock()

    await agent.search_and_extract("https://x.com/article")
    agent._fetcher.fetch.assert_called_once()
    agent._fetcher.fetch_binary.assert_not_called()


# ----------------------------------------------------------------------
# Search-result smart classification (#2)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_results_extensionless_urls_get_probed():
    """Extensionless URLs in search results trigger HEAD probe in parallel."""
    from web_agent.agent import Agent
    from web_agent.config import AppConfig
    from web_agent.models import FetchResult, FetchStatus, SearchResponse, SearchResultItem

    agent = Agent(AppConfig())

    # Search returns one .html (no probe), one extensionless (probed -> html)
    agent._search.search = AsyncMock(
        return_value=SearchResponse(
            query="x",
            total_results=2,
            results=[
                SearchResultItem(
                    position=1,
                    title="A",
                    url="https://x.com/article.html",
                    provider="searxng",
                ),
                SearchResultItem(
                    position=2,
                    title="B",
                    url="https://x.com/page",
                    provider="searxng",
                ),
            ],
        )
    )
    agent._fetcher.classify_url = AsyncMock(return_value="html")
    agent._fetcher.fetch_many = AsyncMock(
        return_value=[
            FetchResult(
                url="https://x.com/article.html",
                final_url="https://x.com/article.html",
                status=FetchStatus.SUCCESS,
                html="<html><body>A</body></html>",
            ),
            FetchResult(
                url="https://x.com/page",
                final_url="https://x.com/page",
                status=FetchStatus.SUCCESS,
                html="<html><body>B</body></html>",
            ),
        ]
    )

    await agent.search_and_extract("x")
    # classify_url should be called exactly ONCE (only for the extensionless URL)
    assert agent._fetcher.classify_url.call_count == 1


@pytest.mark.asyncio
async def test_search_results_extensionless_pdf_promoted_to_download_candidate():
    """Extensionless PDF detected via HEAD probe lands in download_candidates."""
    from web_agent.agent import Agent
    from web_agent.config import AppConfig
    from web_agent.models import SearchResponse, SearchResultItem

    agent = Agent(AppConfig())
    agent._search.search = AsyncMock(
        return_value=SearchResponse(
            query="x",
            total_results=1,
            results=[
                SearchResultItem(
                    position=1,
                    title="report",
                    url="https://x.com/download/42",
                    provider="searxng",
                ),
            ],
        )
    )
    agent._fetcher.classify_url = AsyncMock(return_value="binary")
    agent._fetcher.fetch_many = AsyncMock(return_value=[])

    result = await agent.search_and_extract("x")
    assert len(result.download_candidates) == 1
    assert result.download_candidates[0].url == "https://x.com/download/42"


@pytest.mark.asyncio
async def test_search_results_html_extensions_skip_probe():
    """URLs with known HTML extensions never trigger the HEAD probe."""
    from web_agent.agent import Agent
    from web_agent.config import AppConfig
    from web_agent.models import FetchResult, FetchStatus, SearchResponse, SearchResultItem

    agent = Agent(AppConfig())
    agent._search.search = AsyncMock(
        return_value=SearchResponse(
            query="x",
            total_results=2,
            results=[
                SearchResultItem(
                    position=1,
                    title="A",
                    url="https://x.com/page1.html",
                    provider="searxng",
                ),
                SearchResultItem(
                    position=2,
                    title="B",
                    url="https://x.com/page2.aspx",
                    provider="searxng",
                ),
            ],
        )
    )

    classify_called = False

    async def mock_classify(url, *, session_id=None):
        nonlocal classify_called
        classify_called = True
        return "binary"

    agent._fetcher.classify_url = mock_classify
    agent._fetcher.fetch_many = AsyncMock(
        return_value=[
            FetchResult(
                url=u,
                final_url=u,
                status=FetchStatus.SUCCESS,
                html="<html><body>x</body></html>",
            )
            for u in ("https://x.com/page1.html", "https://x.com/page2.aspx")
        ]
    )

    await agent.search_and_extract("x")
    assert classify_called is False, "HTML-extension URLs should not be probed"


@pytest.mark.asyncio
async def test_search_results_probe_disabled_treats_extensionless_as_html():
    """When probe_binary_urls=False, extensionless URLs default to HTML path."""
    from web_agent.agent import Agent
    from web_agent.config import AppConfig
    from web_agent.models import FetchResult, FetchStatus, SearchResponse, SearchResultItem

    config = AppConfig(safety={"probe_binary_urls": False})
    agent = Agent(config)
    agent._search.search = AsyncMock(
        return_value=SearchResponse(
            query="x",
            total_results=1,
            results=[
                SearchResultItem(
                    position=1,
                    title="A",
                    url="https://x.com/page",
                    provider="searxng",
                ),
            ],
        )
    )
    agent._fetcher.classify_url = AsyncMock(side_effect=AssertionError("should not be called"))
    agent._fetcher.fetch_many = AsyncMock(
        return_value=[
            FetchResult(
                url="https://x.com/page",
                final_url="https://x.com/page",
                status=FetchStatus.SUCCESS,
                html="<html><body>A</body></html>",
            )
        ]
    )

    await agent.search_and_extract("x")
    # extensionless URL fell through to fetch_many without probe
    agent._fetcher.fetch_many.assert_called_once()
