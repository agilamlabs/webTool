"""v1.6.9 shared smart-binary routing tests.

Verifies that ``WebFetcher.fetch_smart`` consolidates the binary-vs-HTML
routing rules previously duplicated across Agent.fetch_and_extract,
Agent.search_and_extract (URL branch), Recipes.search_and_open_best_result,
and Recipes.web_research.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.config import AppConfig, SafetyConfig
from web_agent.models import FetchResult, FetchStatus
from web_agent.web_fetcher import WebFetcher


def _wf(probe_binary_urls: bool = True) -> WebFetcher:
    """Build a WebFetcher with stubbed deps so we can assert routing
    without touching real Playwright."""
    cfg = AppConfig(safety=SafetyConfig(probe_binary_urls=probe_binary_urls))
    wf = WebFetcher(config=cfg, browser_manager=MagicMock())
    wf.fetch = AsyncMock(  # type: ignore[method-assign]
        return_value=FetchResult(url="x", final_url="x", status=FetchStatus.SUCCESS, html="<html/>")
    )
    wf.fetch_binary = AsyncMock(  # type: ignore[method-assign]
        return_value=FetchResult(url="x", final_url="x", status=FetchStatus.SUCCESS, binary=b"%PDF")
    )
    wf.classify_url = AsyncMock(return_value="html")  # type: ignore[method-assign]
    return wf


@pytest.mark.asyncio
async def test_known_download_extension_routes_to_fetch_binary() -> None:
    wf = _wf()
    await wf.fetch_smart("https://example.com/report.pdf")
    wf.fetch_binary.assert_awaited_once()
    wf.fetch.assert_not_awaited()
    # No HEAD probe needed when extension is conclusive
    wf.classify_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_extensionless_html_routes_to_fetch_html() -> None:
    wf = _wf()
    wf.classify_url = AsyncMock(return_value="html")  # type: ignore[method-assign]
    await wf.fetch_smart("https://example.com/article")
    wf.fetch.assert_awaited_once()
    wf.fetch_binary.assert_not_awaited()
    # HEAD probe consulted (extension was ambiguous)
    wf.classify_url.assert_awaited_once()


@pytest.mark.asyncio
async def test_extensionless_binary_routes_to_fetch_binary_via_probe() -> None:
    """Regulator-dashboard pattern: /api/report has no .pdf extension
    but the server returns Content-Type: application/pdf.

    v1.6.10: classify_url now returns granular kinds ('pdf', 'xlsx',
    'binary_other', ...) instead of the literal 'binary'. The stub
    returns 'pdf' here; ``_is_binary_kind('pdf')`` is True so routing
    still hits fetch_binary.
    """
    wf = _wf()
    wf.classify_url = AsyncMock(return_value="pdf")  # type: ignore[method-assign]
    await wf.fetch_smart("https://example.com/api/report")
    wf.fetch_binary.assert_awaited_once()
    wf.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_binary_probe_disabled_routes_extensionless_to_html() -> None:
    """When binary_probe=False, skip the HEAD probe and default to HTML."""
    wf = _wf()
    wf.classify_url = AsyncMock(return_value="pdf")  # type: ignore[method-assign]
    await wf.fetch_smart("https://example.com/api/report", binary_probe=False)
    wf.fetch.assert_awaited_once()
    wf.fetch_binary.assert_not_awaited()
    wf.classify_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_safety_probe_binary_urls_false_skips_probe() -> None:
    """SafetyConfig.probe_binary_urls=False also bypasses the HEAD probe."""
    wf = _wf(probe_binary_urls=False)
    wf.classify_url = AsyncMock(return_value="pdf")  # type: ignore[method-assign]
    await wf.fetch_smart("https://example.com/api/report")
    wf.fetch.assert_awaited_once()
    wf.classify_url.assert_not_awaited()


@pytest.mark.asyncio
async def test_extensionless_binary_other_routes_to_fetch_binary() -> None:
    """v1.6.10: 'binary_other' (opaque attachment) routes through fetch_binary."""
    wf = _wf()
    wf.classify_url = AsyncMock(return_value="binary_other")  # type: ignore[method-assign]
    await wf.fetch_smart("https://example.com/api/blob")
    wf.fetch_binary.assert_awaited_once()
    wf.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_extensionless_html_classification_routes_to_fetch_html() -> None:
    """v1.6.10: 'html' classification correctly routes through fetch (not fetch_binary)."""
    wf = _wf()
    wf.classify_url = AsyncMock(return_value="html")  # type: ignore[method-assign]
    await wf.fetch_smart("https://example.com/api/page")
    wf.fetch.assert_awaited_once()
    wf.fetch_binary.assert_not_awaited()


@pytest.mark.asyncio
async def test_session_id_is_threaded_through() -> None:
    wf = _wf()
    await wf.fetch_smart("https://example.com/report.pdf", session_id="sess-abc")
    wf.fetch_binary.assert_awaited_once()
    _args, kwargs = wf.fetch_binary.call_args
    assert kwargs.get("session_id") == "sess-abc"
