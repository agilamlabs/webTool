"""Tests for the SearchProvider abstraction + concrete providers.

These are unit tests using mocked HTTP / mocked ddgs -- no real network.
The Playwright provider is exercised by the existing live integration
tests in test_agent.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from web_agent.config import AppConfig
from web_agent.models import SearchResponse, SearchResultItem
from web_agent.rate_limiter import RateLimiter
from web_agent.search_providers import (
    DDGSProvider,
    SearchProvider,
    SearXNGProvider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RecordingProvider(SearchProvider):
    """Fake provider for testing the chain orchestrator."""

    name = "fake"

    def __init__(
        self,
        results: list[SearchResultItem] | None = None,
        raises: Exception | None = None,
        available: bool = True,
    ) -> None:
        self._results = results or []
        self._raises = raises
        self._available = available
        self.call_count = 0

    @property
    def is_available(self) -> bool:
        return self._available

    async def search(self, query: str, max_results: int) -> SearchResponse:
        self.call_count += 1
        if self._raises is not None:
            raise self._raises
        return SearchResponse(
            query=query,
            total_results=len(self._results),
            results=self._results[:max_results],
        )


def _make_item(idx: int, title: str = "T", url: str | None = None) -> SearchResultItem:
    return SearchResultItem(
        position=idx,
        title=title,
        url=url or f"https://example.com/{idx}",
        displayed_url="example.com",
        snippet="snippet",
    )


# ---------------------------------------------------------------------------
# SearXNGProvider
# ---------------------------------------------------------------------------


class TestSearXNGProvider:
    @pytest.mark.asyncio
    async def test_unavailable_when_base_url_none(self) -> None:
        p = SearXNGProvider(base_url=None)
        assert not p.is_available
        # Calling search anyway returns empty -- never reaches network
        resp = await p.search("anything", 5)
        assert resp.total_results == 0

    @pytest.mark.asyncio
    async def test_available_when_base_url_set(self) -> None:
        p = SearXNGProvider(base_url="http://localhost:8888")
        assert p.is_available

    @pytest.mark.asyncio
    async def test_strips_trailing_slash(self) -> None:
        p = SearXNGProvider(base_url="http://localhost:8888/")
        assert p._base_url == "http://localhost:8888"

    @pytest.mark.asyncio
    async def test_parses_json_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        p = SearXNGProvider(base_url="http://localhost:8888")

        # Build a mock httpx.AsyncClient that returns canned JSON.
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {
                    "url": "https://wikipedia.org/Python",
                    "title": "Python (programming)",
                    "content": "Python is a programming language...",
                },
                {
                    "url": "https://python.org",
                    "title": "Python.org",
                    "content": "Official site",
                },
                # Should be skipped: scheme not http(s)
                {"url": "javascript:alert(1)", "title": "evil", "content": ""},
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("web_agent.search_providers.httpx.AsyncClient", return_value=mock_client):
            resp = await p.search("python", max_results=5)

        assert resp.total_results == 2
        assert resp.results[0].title == "Python (programming)"
        assert resp.results[0].url == "https://wikipedia.org/Python"
        assert resp.results[0].position == 1
        assert resp.results[1].url == "https://python.org"
        # All non-http schemes filtered out
        for r in resp.results:
            assert r.url.startswith(("http://", "https://"))

    @pytest.mark.asyncio
    async def test_returns_empty_on_http_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        p = SearXNGProvider(base_url="http://localhost:8888")

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

        with patch("web_agent.search_providers.httpx.AsyncClient", return_value=mock_client):
            resp = await p.search("python", max_results=5)

        # Connection error -> empty response, chain falls through
        assert resp.total_results == 0
        assert resp.query == "python"

    @pytest.mark.asyncio
    async def test_respects_max_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        p = SearXNGProvider(base_url="http://localhost:8888")

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {"url": f"https://e.com/{i}", "title": f"T{i}", "content": "x"} for i in range(10)
            ]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("web_agent.search_providers.httpx.AsyncClient", return_value=mock_client):
            resp = await p.search("x", max_results=3)

        assert resp.total_results == 3

    @pytest.mark.asyncio
    async def test_calls_rate_limiter(self) -> None:
        rl = RateLimiter(rps_per_host=100.0)  # high rate, just confirm acquire is called
        rl.acquire = AsyncMock()  # type: ignore[method-assign]
        p = SearXNGProvider(base_url="http://my-searxng.local", rate_limiter=rl)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": []}
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("web_agent.search_providers.httpx.AsyncClient", return_value=mock_client):
            await p.search("x", 5)

        rl.acquire.assert_awaited_once_with("my-searxng.local")


# ---------------------------------------------------------------------------
# DDGSProvider
# ---------------------------------------------------------------------------


class TestDDGSProvider:
    @pytest.mark.asyncio
    async def test_unavailable_when_package_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        p = DDGSProvider()
        # Force a fresh import attempt to fail.
        import builtins

        real_import = builtins.__import__

        def _raise(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "ddgs":
                raise ImportError("simulated missing ddgs")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _raise)
        # Reset the lazy probe so it re-runs the import.
        p._available = None
        assert not p.is_available

    @pytest.mark.asyncio
    async def test_available_when_package_present(self) -> None:
        # ddgs is installed in the dev env, so this should return True.
        p = DDGSProvider()
        assert p.is_available

    @pytest.mark.asyncio
    async def test_parses_ddgs_dicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        p = DDGSProvider()

        # Mock the DDGS class to return canned results
        class _MockDDGS:
            def __enter__(self) -> _MockDDGS:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def text(self, query: str, max_results: int = 10) -> list[dict]:
                return [
                    {
                        "title": "Python.org",
                        "href": "https://python.org",
                        "body": "Official Python site",
                    },
                    {
                        "title": "Wikipedia",
                        "href": "https://en.wikipedia.org/wiki/Python",
                        "body": "Encyclopedia entry",
                    },
                    # Should be filtered (non-http scheme)
                    {"title": "evil", "href": "javascript:alert()", "body": ""},
                ]

        with patch("ddgs.DDGS", _MockDDGS):
            resp = await p.search("python", max_results=5)

        assert resp.total_results == 2
        assert resp.results[0].title == "Python.org"
        assert resp.results[0].url == "https://python.org"
        for r in resp.results:
            assert r.url.startswith(("http://", "https://"))

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        p = DDGSProvider()

        class _BoomDDGS:
            def __enter__(self) -> _BoomDDGS:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def text(self, *args: Any, **kwargs: Any) -> list[dict]:
                raise RuntimeError("DDG changed their HTML")

        with patch("ddgs.DDGS", _BoomDDGS):
            resp = await p.search("python", max_results=5)

        # Exceptions are swallowed -> empty response, chain falls through
        assert resp.total_results == 0


# ---------------------------------------------------------------------------
# SearchEngine chain orchestrator
# ---------------------------------------------------------------------------


class TestSearchEngineChain:
    """Test the chain logic by injecting fake providers via monkeypatch."""

    @pytest.mark.asyncio
    async def test_first_provider_returns_results_skips_rest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from web_agent.search_engine import SearchEngine

        config = AppConfig()
        engine = SearchEngine(browser_manager=MagicMock(), config=config)

        first = _RecordingProvider(results=[_make_item(1), _make_item(2)])
        second = _RecordingProvider(results=[_make_item(3)])
        third = _RecordingProvider(results=[_make_item(4)])
        engine._providers = [first, second, third]

        resp = await engine.search("query", max_results=5)
        assert resp.total_results == 2
        assert first.call_count == 1
        assert second.call_count == 0
        assert third.call_count == 0

    @pytest.mark.asyncio
    async def test_falls_through_on_empty_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from web_agent.search_engine import SearchEngine

        engine = SearchEngine(browser_manager=MagicMock(), config=AppConfig())
        first = _RecordingProvider(results=[])  # empty
        second = _RecordingProvider(results=[_make_item(1)])  # non-empty
        engine._providers = [first, second]

        resp = await engine.search("query", max_results=5)
        assert resp.total_results == 1
        assert first.call_count == 1
        assert second.call_count == 1

    @pytest.mark.asyncio
    async def test_falls_through_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from web_agent.search_engine import SearchEngine

        engine = SearchEngine(browser_manager=MagicMock(), config=AppConfig())
        first = _RecordingProvider(raises=RuntimeError("boom"))
        second = _RecordingProvider(results=[_make_item(1)])
        engine._providers = [first, second]

        resp = await engine.search("query", max_results=5)
        assert resp.total_results == 1
        assert first.call_count == 1
        assert second.call_count == 1

    @pytest.mark.asyncio
    async def test_skips_unavailable_providers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from web_agent.search_engine import SearchEngine

        engine = SearchEngine(browser_manager=MagicMock(), config=AppConfig())
        first = _RecordingProvider(results=[_make_item(1)], available=False)
        second = _RecordingProvider(results=[_make_item(2)])
        engine._providers = [first, second]

        resp = await engine.search("query", max_results=5)
        assert resp.total_results == 1
        # Unavailable provider was skipped without invoking search()
        assert first.call_count == 0
        assert second.call_count == 1
        assert resp.results[0].url == "https://example.com/2"

    @pytest.mark.asyncio
    async def test_strict_raises_when_all_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from web_agent.exceptions import SearchError
        from web_agent.search_engine import SearchEngine

        engine = SearchEngine(browser_manager=MagicMock(), config=AppConfig())
        engine._providers = [
            _RecordingProvider(results=[]),
            _RecordingProvider(raises=Exception("boom")),
        ]

        with pytest.raises(SearchError):
            await engine.search("query", max_results=5, strict=True)

    @pytest.mark.asyncio
    async def test_default_provider_chain_built_from_config(self) -> None:
        from web_agent.search_engine import SearchEngine

        config = AppConfig(search={"providers": ["ddgs", "playwright"]})
        engine = SearchEngine(browser_manager=MagicMock(), config=config)
        # Catalog includes searxng/ddgs/playwright but only configured ones run
        names = [p.name for p in engine.providers]
        assert names == ["ddgs", "playwright"]

    @pytest.mark.asyncio
    async def test_unknown_provider_name_silently_dropped(self) -> None:
        from web_agent.search_engine import SearchEngine

        config = AppConfig(search={"providers": ["nonexistent", "ddgs"]})
        engine = SearchEngine(browser_manager=MagicMock(), config=config)
        names = [p.name for p in engine.providers]
        assert names == ["ddgs"]


# ---------------------------------------------------------------------------
# URL-as-query detection in Agent
# ---------------------------------------------------------------------------


class TestQueryIsUrl:
    def test_https_url_is_detected(self) -> None:
        from web_agent.agent import _query_is_url

        assert _query_is_url("https://example.com")
        assert _query_is_url("https://example.com/path?q=1")
        assert _query_is_url("  https://example.com  ")  # stripped

    def test_http_url_is_detected(self) -> None:
        from web_agent.agent import _query_is_url

        assert _query_is_url("http://example.com")

    def test_plain_query_not_detected(self) -> None:
        from web_agent.agent import _query_is_url

        assert not _query_is_url("python web scraping")
        assert not _query_is_url("FastAPI tutorial 2024")

    def test_url_inside_natural_language_not_detected(self) -> None:
        from web_agent.agent import _query_is_url

        # "fetch https://x" has whitespace -> treat as a search query
        assert not _query_is_url("fetch https://example.com please")
        assert not _query_is_url("https://example.com is great")

    def test_no_scheme_not_detected(self) -> None:
        from web_agent.agent import _query_is_url

        # Bare domain is ambiguous -- could be a query for that brand
        assert not _query_is_url("example.com")
        assert not _query_is_url("www.example.com/path")
