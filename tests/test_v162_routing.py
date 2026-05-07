"""Tests for v1.6.2 smart binary routing + extensionless URL detection.

Covers issues #1, #2, #3, #10. All mock-based -- no network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from web_agent.web_fetcher import (
    _content_type_is_binary,
    _disposition_is_attachment,
    _is_download_url,
)

# ----------------------------------------------------------------------
# Header-based binary detection (used by HEAD probe)
# ----------------------------------------------------------------------


def test_content_type_pdf_is_binary():
    assert _content_type_is_binary("application/pdf") is True


def test_content_type_pdf_with_charset_is_binary():
    assert _content_type_is_binary("application/pdf; charset=utf-8") is True


def test_content_type_xlsx_is_binary():
    assert (
        _content_type_is_binary("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        is True
    )


def test_content_type_docx_is_binary():
    assert (
        _content_type_is_binary(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        is True
    )


def test_content_type_csv_is_binary():
    assert _content_type_is_binary("text/csv") is True


def test_content_type_octet_stream_is_binary():
    assert _content_type_is_binary("application/octet-stream") is True


def test_content_type_html_is_not_binary():
    assert _content_type_is_binary("text/html") is False
    assert _content_type_is_binary("text/html; charset=utf-8") is False


def test_content_type_none_is_not_binary():
    assert _content_type_is_binary(None) is False


def test_disposition_attachment_detection():
    assert _disposition_is_attachment('attachment; filename="x.pdf"') is True
    assert _disposition_is_attachment("ATTACHMENT") is True
    assert _disposition_is_attachment("inline") is False
    assert _disposition_is_attachment(None) is False


# ----------------------------------------------------------------------
# Extension-based detection
# ----------------------------------------------------------------------


def test_is_download_url_pdf():
    assert _is_download_url("https://x.com/report.pdf") is True


def test_is_download_url_xlsx():
    assert _is_download_url("https://x.com/sheet.xlsx") is True


def test_is_download_url_docx():
    assert _is_download_url("https://x.com/letter.docx") is True


def test_is_download_url_csv():
    assert _is_download_url("https://x.com/data.csv") is True


def test_is_download_url_extensionless():
    """Extensionless URLs are NOT detected by URL alone -- HEAD probe handles them."""
    assert _is_download_url("https://x.com/download/123") is False


def test_is_download_url_html():
    assert _is_download_url("https://x.com/page.html") is False


# ----------------------------------------------------------------------
# Smart routing in fetch_and_extract: extensionless URLs
# (covers issues #1 + #2 + #10 -- HEAD-based detection of binary content)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extensionless_pdf_routes_to_fetch_binary():
    """A URL with no extension but Content-Type: application/pdf must route to fetch_binary."""
    from web_agent.agent import Agent
    from web_agent.config import AppConfig
    from web_agent.models import FetchResult, FetchStatus

    config = AppConfig()
    agent = Agent(config)

    # Mock both fetch and fetch_binary on the fetcher; mock classify_url
    # to return 'binary' (the real probe would do HEAD).
    agent._fetcher.classify_url = AsyncMock(return_value="binary")
    agent._fetcher.fetch_binary = AsyncMock(
        return_value=FetchResult(
            url="https://x.com/download/123",
            final_url="https://x.com/download/123",
            status=FetchStatus.SUCCESS,
            binary=b"%PDF-1.4 fake",
            content_type="application/pdf",
        )
    )
    agent._fetcher.fetch = AsyncMock()  # should NOT be called

    # bypass actual lifecycle by stubbing the relevant pieces
    agent._bm.start = AsyncMock()
    agent._bm.stop = AsyncMock()

    result = await agent.fetch_and_extract("https://x.com/download/123")
    agent._fetcher.fetch_binary.assert_called_once()
    agent._fetcher.fetch.assert_not_called()
    # Extraction method depends on whether pypdf is available -- either pdf or none
    assert result.extraction_method in ("pdf", "none")


@pytest.mark.asyncio
async def test_extensionless_html_routes_to_fetch():
    """Extensionless URL with text/html Content-Type stays on the HTML fetch path."""
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
            html="<html><body><p>Hello world.</p></body></html>",
        )
    )
    agent._fetcher.fetch_binary = AsyncMock()  # must not be called

    agent._bm.start = AsyncMock()
    agent._bm.stop = AsyncMock()

    await agent.fetch_and_extract("https://x.com/article")
    agent._fetcher.fetch.assert_called_once()
    agent._fetcher.fetch_binary.assert_not_called()


@pytest.mark.asyncio
async def test_known_extension_skips_probe():
    """URL with .pdf extension routes to fetch_binary without probing."""
    from web_agent.agent import Agent
    from web_agent.config import AppConfig
    from web_agent.models import FetchResult, FetchStatus

    agent = Agent(AppConfig())
    classify_called = False

    async def mock_classify(_url):
        nonlocal classify_called
        classify_called = True
        return "binary"

    agent._fetcher.classify_url = mock_classify
    agent._fetcher.fetch_binary = AsyncMock(
        return_value=FetchResult(
            url="https://x.com/a.pdf",
            final_url="https://x.com/a.pdf",
            status=FetchStatus.SUCCESS,
            binary=b"fake",
            content_type="application/pdf",
        )
    )
    agent._fetcher.fetch = AsyncMock()

    agent._bm.start = AsyncMock()
    agent._bm.stop = AsyncMock()

    await agent.fetch_and_extract("https://x.com/a.pdf")
    assert classify_called is False, "classify_url should not be called for known extensions"
    agent._fetcher.fetch_binary.assert_called_once()


@pytest.mark.asyncio
async def test_binary_probe_disabled_param():
    """Passing binary_probe=False skips HEAD even for extensionless URLs."""
    from web_agent.agent import Agent
    from web_agent.config import AppConfig
    from web_agent.models import FetchResult, FetchStatus

    agent = Agent(AppConfig())
    classify_called = False

    async def mock_classify(_url):
        nonlocal classify_called
        classify_called = True
        return "binary"

    agent._fetcher.classify_url = mock_classify
    agent._fetcher.fetch = AsyncMock(
        return_value=FetchResult(
            url="https://x.com/article",
            final_url="https://x.com/article",
            status=FetchStatus.SUCCESS,
            html="<html><body>x</body></html>",
        )
    )
    agent._fetcher.fetch_binary = AsyncMock()

    agent._bm.start = AsyncMock()
    agent._bm.stop = AsyncMock()

    await agent.fetch_and_extract("https://x.com/article", binary_probe=False)
    assert classify_called is False
    agent._fetcher.fetch.assert_called_once()


# ----------------------------------------------------------------------
# fetch_binary streams + caps file size
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_binary_size_cap_enforced(tmp_path):
    """fetch_binary aborts and returns HTTP_ERROR when streamed size exceeds cap."""
    from web_agent.config import AppConfig, DownloadConfig
    from web_agent.models import FetchStatus
    from web_agent.web_fetcher import WebFetcher

    # Force a very small cap (1 MB) and stream chunks larger than that
    config = AppConfig(download=DownloadConfig(max_file_size_mb=1, download_dir=str(tmp_path)))

    bm = MagicMock()
    fetcher = WebFetcher(bm, config)

    # Build a stream of fake 100 KB chunks (12 chunks = 1.2 MB > 1 MB cap)
    big_chunks = [b"a" * (100 * 1024)] * 12

    class FakeStream:
        def __init__(self):
            self.url = "https://x.com/big.pdf"
            self.status_code = 200
            self.headers = {"content-type": "application/pdf"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self, chunk_size=8192):
            for c in big_chunks:
                yield c

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, _method, _url):
            return FakeStream()

    with patch("httpx.AsyncClient", FakeClient):
        result = await fetcher.fetch_binary("https://x.com/big.pdf")

    assert result.status == FetchStatus.HTTP_ERROR
    assert "MB cap" in (result.error_message or "")


# ----------------------------------------------------------------------
# Cookie sharing from Playwright session into httpx
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cookies_for_session_reads_from_context():
    from web_agent.config import AppConfig
    from web_agent.web_fetcher import WebFetcher

    bm = MagicMock()
    sessions = MagicMock()
    fake_ctx = MagicMock()
    fake_ctx.cookies = AsyncMock(
        return_value=[
            {"name": "auth", "value": "abc123", "domain": "x.com"},
            {"name": "session", "value": "xyz", "domain": "x.com"},
        ]
    )
    sessions.get = MagicMock(return_value=fake_ctx)

    fetcher = WebFetcher(bm, AppConfig(), sessions=sessions)
    cookies = await fetcher._cookies_for_session("session-1")
    assert cookies == {"auth": "abc123", "session": "xyz"}


@pytest.mark.asyncio
async def test_cookies_for_session_returns_empty_when_no_session_id():
    from web_agent.config import AppConfig
    from web_agent.web_fetcher import WebFetcher

    fetcher = WebFetcher(MagicMock(), AppConfig())
    assert await fetcher._cookies_for_session(None) == {}


@pytest.mark.asyncio
async def test_cookies_for_session_returns_empty_on_get_failure():
    from web_agent.config import AppConfig
    from web_agent.web_fetcher import WebFetcher

    sessions = MagicMock()
    sessions.get = MagicMock(side_effect=KeyError("no such session"))
    fetcher = WebFetcher(MagicMock(), AppConfig(), sessions=sessions)
    assert await fetcher._cookies_for_session("missing") == {}
