"""v1.6.15: SearchEngine no longer spams "Skipping unavailable provider"
on every search.

Root cause: the default provider chain is ``["searxng", "ddgs",
"playwright"]`` and ``searxng_base_url`` defaults to ``None``, so the
SearXNG provider is permanently unavailable. The per-search loop logged
"Skipping unavailable provider: searxng" at DEBUG on every call, and
loguru surfaces DEBUG by default. The fix reports unavailable providers
ONCE at construction (with a hint) and skips them silently thereafter.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from loguru import logger
from web_agent.config import AppConfig
from web_agent.models import SearchResponse, SearchResultItem
from web_agent.search_engine import SearchEngine
from web_agent.search_providers import SearchProvider

_OLD_NOISE = "Skipping unavailable provider"


class _FakeProvider(SearchProvider):
    """Minimal in-memory provider for chain tests."""

    def __init__(self, name: str, available: bool, results: list[SearchResultItem]) -> None:
        self.name = name
        self._available = available
        self._results = results
        self.calls = 0

    @property
    def is_available(self) -> bool:
        return self._available

    async def search(self, query: str, max_results: int) -> SearchResponse:
        self.calls += 1
        return SearchResponse(
            query=query,
            total_results=len(self._results),
            results=self._results[:max_results],
        )


def _capture_debug() -> tuple[list[str], int]:
    """Attach a DEBUG-level loguru sink; return (messages, sink_id)."""
    msgs: list[str] = []
    sink_id = logger.add(lambda m: msgs.append(str(m)), level="DEBUG", format="{message}")
    return msgs, sink_id


class TestSearxngLogNoise:
    def test_unavailable_logged_once_at_construction_with_hint(self) -> None:
        # Default chain includes searxng; no searxng_base_url => unavailable.
        msgs, sink_id = _capture_debug()
        try:
            SearchEngine(browser_manager=MagicMock(), config=AppConfig())
        finally:
            logger.remove(sink_id)
        joined = "\n".join(msgs)
        assert "configured but unavailable" in joined
        assert "searxng" in joined
        assert "searxng_base_url" in joined  # actionable hint present
        # The old per-search noise must NOT be the mechanism here.
        assert _OLD_NOISE not in joined

    def test_configured_searxng_not_flagged_unavailable(self) -> None:
        msgs, sink_id = _capture_debug()
        try:
            SearchEngine(
                browser_manager=MagicMock(),
                config=AppConfig(search={"searxng_base_url": "http://localhost:8888"}),
            )
        finally:
            logger.remove(sink_id)
        joined = "\n".join(msgs)
        # searxng is available now, so it must not appear in any
        # "unavailable" construction line.
        assert "configured but unavailable" not in joined or "searxng" not in joined

    @pytest.mark.asyncio
    async def test_search_skips_unavailable_silently_every_call(self) -> None:
        engine = SearchEngine(browser_manager=MagicMock(), config=AppConfig())
        unavailable = _FakeProvider("searxng", available=False, results=[])
        available = _FakeProvider(
            "ddgs",
            available=True,
            results=[
                SearchResultItem(
                    position=1,
                    title="ok",
                    url="https://example.com/ok",
                    displayed_url="example.com",
                    snippet="",
                )
            ],
        )
        engine._providers = [unavailable, available]

        msgs, sink_id = _capture_debug()
        try:
            r1 = await engine.search("q", max_results=5)
            r2 = await engine.search("q2", max_results=5)
        finally:
            logger.remove(sink_id)

        # The available provider served both searches; the unavailable one
        # was skipped without ever being called.
        assert r1.total_results == 1
        assert r2.total_results == 1
        assert unavailable.calls == 0
        assert available.calls == 2
        # And the search loop emitted no per-call "skipping" noise.
        joined = "\n".join(msgs)
        assert _OLD_NOISE not in joined
