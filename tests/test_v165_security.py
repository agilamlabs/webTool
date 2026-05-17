"""v1.6.5 critical security fixes.

Covers three SSRF / cookie-isolation gaps that v1.6.4 left open:

- C1: cross-domain cookie leak in ``_cookies_for_session`` -- httpx
  flat-dict cookies were sent to every host, including hosts the
  cookie's domain attribute did NOT cover.
- C2: ``classify_url`` HEAD probe fired for denied / private-IP
  URLs because the entry-point domain gate was missing.
- C3: Playwright download paths (``_do_save_page`` and
  ``_download_with_playwright``) wrote redirected content to disk
  without re-validating the post-redirect URL against the safety
  policy.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from web_agent.config import AppConfig, DownloadConfig, SafetyConfig
from web_agent.downloader import Downloader
from web_agent.models import FetchStatus
from web_agent.web_fetcher import WebFetcher

# ----------------------------------------------------------------------
# C1: cookie isolation
# ----------------------------------------------------------------------


def _names_in_jar(jar: httpx.Cookies) -> set[str]:
    """Return cookie names present in the jar (regardless of domain)."""
    return {c.name for c in jar.jar}


@pytest.mark.asyncio
async def test_cookies_for_session_filters_to_target_host():
    """A cookie set for bank.com must NOT be exposed when the target is attacker.com."""
    fake_ctx = MagicMock()
    fake_ctx.cookies = AsyncMock(
        return_value=[
            {"name": "bank_session", "value": "secret", "domain": "bank.com"},
            {"name": "attacker_pref", "value": "x", "domain": "attacker.com"},
        ]
    )
    fake_sessions = MagicMock()
    fake_sessions.get = MagicMock(return_value=fake_ctx)

    fetcher = WebFetcher(MagicMock(), AppConfig(), sessions=fake_sessions)

    jar = await fetcher._cookies_for_session("sid", "https://attacker.com/page")
    names = _names_in_jar(jar)

    assert "bank_session" not in names, "bank.com cookie leaked to attacker.com jar"
    assert "attacker_pref" in names


@pytest.mark.asyncio
async def test_cookies_for_session_subdomain_match():
    """Cookie for .example.com is sent to api.example.com (subdomain)."""
    fake_ctx = MagicMock()
    fake_ctx.cookies = AsyncMock(
        return_value=[{"name": "session", "value": "x", "domain": ".example.com"}]
    )
    fake_sessions = MagicMock()
    fake_sessions.get = MagicMock(return_value=fake_ctx)

    fetcher = WebFetcher(MagicMock(), AppConfig(), sessions=fake_sessions)

    jar = await fetcher._cookies_for_session("sid", "https://api.example.com/page")
    assert "session" in _names_in_jar(jar)


@pytest.mark.asyncio
async def test_cookies_for_session_exact_host_match_with_no_domain_attr():
    """Domainless cookies pin to the target host (Playwright per-host cookie)."""
    fake_ctx = MagicMock()
    fake_ctx.cookies = AsyncMock(return_value=[{"name": "csrf", "value": "abc", "domain": ""}])
    fake_sessions = MagicMock()
    fake_sessions.get = MagicMock(return_value=fake_ctx)

    fetcher = WebFetcher(MagicMock(), AppConfig(), sessions=fake_sessions)

    jar = await fetcher._cookies_for_session("sid", "https://example.com/page")
    assert "csrf" in _names_in_jar(jar)


@pytest.mark.asyncio
async def test_cookies_for_session_unrelated_domain_is_dropped():
    """parent.com cookie is dropped when target is api.other.com."""
    fake_ctx = MagicMock()
    fake_ctx.cookies = AsyncMock(
        return_value=[{"name": "session", "value": "x", "domain": "parent.com"}]
    )
    fake_sessions = MagicMock()
    fake_sessions.get = MagicMock(return_value=fake_ctx)

    fetcher = WebFetcher(MagicMock(), AppConfig(), sessions=fake_sessions)

    jar = await fetcher._cookies_for_session("sid", "https://api.other.com/page")
    assert "session" not in _names_in_jar(jar)


@pytest.mark.asyncio
async def test_cookies_for_session_empty_jar_when_no_session():
    """No session_id and no SessionManager => empty jar (never raises)."""
    fetcher = WebFetcher(MagicMock(), AppConfig())
    jar = await fetcher._cookies_for_session(None, "https://x.com/page")
    assert _names_in_jar(jar) == set()


@pytest.mark.asyncio
async def test_cookies_for_session_empty_jar_for_unparseable_target():
    """Garbage target_url => empty jar, no raise."""
    fake_ctx = MagicMock()
    fake_ctx.cookies = AsyncMock(
        return_value=[{"name": "x", "value": "y", "domain": "example.com"}]
    )
    fake_sessions = MagicMock()
    fake_sessions.get = MagicMock(return_value=fake_ctx)

    fetcher = WebFetcher(MagicMock(), AppConfig(), sessions=fake_sessions)

    jar = await fetcher._cookies_for_session("sid", "not a url")
    assert _names_in_jar(jar) == set()


@pytest.mark.asyncio
async def test_cookies_for_session_returns_jar_type():
    """Helper now returns httpx.Cookies (not dict). Type guard for callers."""
    fetcher = WebFetcher(MagicMock(), AppConfig())
    jar = await fetcher._cookies_for_session(None, "https://x.com")
    assert isinstance(jar, httpx.Cookies)


# ----------------------------------------------------------------------
# C2: classify_url pre-gate
# ----------------------------------------------------------------------


class _FailingHttpxClient:
    """An httpx.AsyncClient stand-in that raises if HEAD is ever invoked."""

    instance_count = 0

    def __init__(self, *_a, **_k):
        type(self).instance_count += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def head(self, _url):  # pragma: no cover - assertion is the test
        raise AssertionError("HEAD must not be called for a pre-gated URL")


@pytest.mark.asyncio
async def test_classify_url_blocks_denied_domain_before_head():
    """A denied-domain URL never fires a HEAD request."""
    config = AppConfig(safety=SafetyConfig(denied_domains=["evil.example.com"]))
    fetcher = WebFetcher(MagicMock(), config)

    _FailingHttpxClient.instance_count = 0
    with patch.object(httpx, "AsyncClient", _FailingHttpxClient):
        result = await fetcher.classify_url("https://evil.example.com/file")

    assert result == "unknown"
    assert _FailingHttpxClient.instance_count == 0, (
        "AsyncClient was constructed even though the URL is on a denied domain"
    )


@pytest.mark.asyncio
async def test_classify_url_blocks_private_ip_before_head():
    """AWS IMDS / RFC1918 URLs never fire a HEAD request."""
    config = AppConfig(safety=SafetyConfig(block_private_ips=True))
    fetcher = WebFetcher(MagicMock(), config)

    _FailingHttpxClient.instance_count = 0
    with patch.object(httpx, "AsyncClient", _FailingHttpxClient):
        result = await fetcher.classify_url("http://169.254.169.254/latest/meta-data/")

    assert result == "unknown"
    assert _FailingHttpxClient.instance_count == 0


@pytest.mark.asyncio
async def test_classify_url_still_classifies_extension_for_denied_domain():
    """Extension-based classification ALSO short-circuits via the pre-gate.

    Even though .pdf is a known binary extension, a denied-domain URL is
    treated as 'unknown' so callers fall through to a real fetch which
    will produce a BLOCKED result -- consistent with the SSRF mitigation
    (don't reveal anything about denied URLs to the classifier output).
    """
    config = AppConfig(safety=SafetyConfig(denied_domains=["evil.example.com"]))
    fetcher = WebFetcher(MagicMock(), config)

    result = await fetcher.classify_url("https://evil.example.com/payload.pdf")
    assert result == "unknown"


# ----------------------------------------------------------------------
# C3: Playwright download paths re-validate post-redirect URL
# ----------------------------------------------------------------------


def _make_downloader(tmp_path: Path) -> Downloader:
    config = AppConfig(
        download=DownloadConfig(download_dir=str(tmp_path)),
        safety=SafetyConfig(denied_domains=["evil.example.com"]),
    )
    return Downloader(MagicMock(), config)


@pytest.mark.asyncio
async def test_save_page_blocks_redirect_to_denied_host(tmp_path: Path):
    """_do_save_page returns BLOCKED if page.url drifts to a denied host."""
    downloader = _make_downloader(tmp_path)

    fake_page = MagicMock()
    fake_page.goto = AsyncMock(return_value=MagicMock(headers={"content-type": "text/html"}))
    # type-level property so accessing fake_page.url returns the redirected URL
    type(fake_page).url = property(lambda _self: "https://evil.example.com/landing")
    fake_page.content = AsyncMock(return_value="<html>evil content</html>")

    fp = tmp_path / "out.html"
    result = await downloader._do_save_page(fake_page, "https://allowed.com/r", fp)

    assert result.status == FetchStatus.BLOCKED
    assert "evil.example.com" in (result.error_message or "")
    assert not fp.exists(), "redirected content was written to disk"
    fake_page.content.assert_not_called()


@pytest.mark.asyncio
async def test_save_page_allows_when_redirect_stays_in_policy(tmp_path: Path):
    """_do_save_page proceeds when the post-redirect URL is still allowed."""
    downloader = _make_downloader(tmp_path)

    fake_page = MagicMock()
    fake_page.goto = AsyncMock(return_value=MagicMock(headers={"content-type": "text/html"}))
    type(fake_page).url = property(lambda _self: "https://allowed.com/landing")
    fake_page.content = AsyncMock(return_value="<html>ok</html>")

    fp = tmp_path / "out.html"
    result = await downloader._do_save_page(fake_page, "https://allowed.com/r", fp)

    assert result.status == FetchStatus.SUCCESS
    assert fp.exists()


def test_blocked_by_redirect_helper_returns_none_for_allowed(tmp_path: Path):
    downloader = _make_downloader(tmp_path)
    assert downloader._blocked_by_redirect("https://allowed.com/file", tmp_path / "x") is None


def test_blocked_by_redirect_helper_returns_blocked_for_denied(tmp_path: Path):
    downloader = _make_downloader(tmp_path)
    res = downloader._blocked_by_redirect("https://evil.example.com/file", tmp_path / "x")
    assert res is not None
    assert res.status == FetchStatus.BLOCKED
    assert "evil.example.com" in (res.error_message or "")


@pytest.mark.asyncio
async def test_download_with_playwright_blocks_redirect_via_download_url(tmp_path: Path):
    """_download_with_playwright (ephemeral branch) refuses to save when
    download.url is on a denied host."""
    downloader = _make_downloader(tmp_path)

    # Fake Download with a redirected origin
    fake_download = MagicMock()
    fake_download.url = "https://evil.example.com/payload.exe"
    fake_download.save_as = AsyncMock()

    # download_info.value is awaitable (returns the Download)
    async def _get_value():
        return fake_download

    fake_download_info = MagicMock()
    type(fake_download_info).value = property(lambda _self: _get_value())

    class _FakeExpectDownload:
        async def __aenter__(self):
            return fake_download_info

        async def __aexit__(self, *_a):
            return False

    fake_page = MagicMock()
    fake_page.expect_download = MagicMock(return_value=_FakeExpectDownload())
    fake_page.goto = AsyncMock()
    fake_page.close = AsyncMock()

    fake_ctx = MagicMock()
    fake_ctx.new_page = AsyncMock(return_value=fake_page)

    class _FakeBmContext:
        async def __aenter__(self):
            return fake_ctx

        async def __aexit__(self, *_a):
            return False

    downloader._bm = MagicMock()
    downloader._bm.new_context = MagicMock(return_value=_FakeBmContext())

    fp = tmp_path / "evil.exe"
    result = await downloader._download_with_playwright(
        "https://allowed.com/redirect", fp, session_id=None
    )

    assert result.status == FetchStatus.BLOCKED
    assert "evil.example.com" in (result.error_message or "")
    fake_download.save_as.assert_not_called()
    assert not fp.exists()


@pytest.mark.asyncio
async def test_download_with_playwright_session_blocks_redirect_via_download_url(
    tmp_path: Path,
):
    """Same SSRF check on the session branch of _download_with_playwright."""
    downloader = _make_downloader(tmp_path)

    fake_download = MagicMock()
    fake_download.url = "https://evil.example.com/payload.exe"
    fake_download.save_as = AsyncMock()

    async def _get_value():
        return fake_download

    fake_download_info = MagicMock()
    type(fake_download_info).value = property(lambda _self: _get_value())

    class _FakeExpectDownload:
        async def __aenter__(self):
            return fake_download_info

        async def __aexit__(self, *_a):
            return False

    fake_page = MagicMock()
    fake_page.expect_download = MagicMock(return_value=_FakeExpectDownload())
    fake_page.goto = AsyncMock()
    fake_page.close = AsyncMock()

    fake_session_ctx = MagicMock()
    fake_session_ctx.new_page = AsyncMock(return_value=fake_page)

    fake_sessions = MagicMock()
    fake_sessions.get = MagicMock(return_value=fake_session_ctx)
    fake_sessions.touch = MagicMock()
    downloader._sessions = fake_sessions

    fp = tmp_path / "evil.exe"
    result = await downloader._download_with_playwright(
        "https://allowed.com/redirect", fp, session_id="sid"
    )

    assert result.status == FetchStatus.BLOCKED
    assert "evil.example.com" in (result.error_message or "")
    fake_download.save_as.assert_not_called()
    assert not fp.exists()
