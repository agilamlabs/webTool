"""v1.7.0 follow-up: Downloader proxy egress (B2) + metrics (R2) tests.

Fully offline. The httpx download path (Strategy 1) is exercised by patching
``httpx.AsyncClient`` in the ``web_agent.downloader`` namespace (mirrors the
``web_agent.web_fetcher`` patch idiom in tests/test_proxy_fingerprint.py); the
metrics assertions read ``MetricsRegistry.snapshot()`` exactly as
tests/test_metrics.py does. Nothing here hits the network or launches a real
Chromium.

Coverage:
- Proxy (B2): proxy configured -> the AsyncClient in ``_download_httpx``
  receives ``proxy=<url>``; proxy unset -> no ``proxy`` kwarg at all.
- Metrics (R2): a successful httpx download increments ``download_total`` +
  ``download_outcome{status=success}`` and observes ``download_bytes``; a
  blocked download (domain / allow_downloads gate) increments
  ``download_total`` + ``download_outcome{status=blocked}`` and observes no
  bytes; the disabled registry records nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest
from web_agent.config import AppConfig, ProxyConfig
from web_agent.downloader import Downloader
from web_agent.metrics import MetricsRegistry, get_metrics, noop_registry
from web_agent.models import FetchStatus

# ======================================================================
# httpx AsyncClient fake (captures constructor kwargs + canned stream)
# ======================================================================


class _FakeStreamResp:
    """Minimal async-CM stand-in for an httpx streaming response."""

    def __init__(self, *, url: str, headers: dict[str, str], status_code: int = 200) -> None:
        self.url = url
        self.headers = headers
        self.status_code = status_code

    async def __aenter__(self) -> _FakeStreamResp:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self, chunk_size: int = 8192) -> Any:
        for chunk in (b"%PDF-1.7 fake-body",):
            yield chunk


class _FakeAsyncClient:
    """Captures AsyncClient constructor kwargs and yields a canned stream
    response. Records the last instance's kwargs on the class for asserts."""

    last_kwargs: ClassVar[dict[str, Any]] = {}
    stream_headers: ClassVar[dict[str, str]] = {"content-type": "application/pdf"}

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None

    def stream(self, method: str, url: str, headers: dict[str, str] | None = None) -> _FakeStreamResp:
        return _FakeStreamResp(url=url, headers=type(self).stream_headers)


def _downloader(
    *,
    proxy_kwargs: dict[str, Any] | None = None,
    metrics: MetricsRegistry | None = None,
    tmp_path: Path,
) -> Downloader:
    """Build a Downloader with a mocked browser manager and no robots /
    rate-limiter so the httpx path is reached directly."""
    proxy = ProxyConfig(**proxy_kwargs) if proxy_kwargs else ProxyConfig()
    config = AppConfig(base_dir=str(tmp_path), proxy=proxy)
    return Downloader(MagicMock(), config, metrics=metrics)


# ======================================================================
# B2: proxy threading on the httpx download path (_download_httpx)
# ======================================================================


@pytest.mark.asyncio
async def test_download_httpx_proxy_passed_when_set(tmp_path: Path) -> None:
    """Proxy configured -> the Strategy-1 AsyncClient gets proxy=<url>."""
    dl = _downloader(
        proxy_kwargs={"server": "http://127.0.0.1:8080", "username": "u", "password": "p"},
        tmp_path=tmp_path,
    )
    _FakeAsyncClient.last_kwargs = {}
    _FakeAsyncClient.stream_headers = {"content-type": "application/pdf"}
    with patch("web_agent.downloader.httpx.AsyncClient", _FakeAsyncClient):
        result = await dl.download("https://example.com/report.pdf")

    assert result.status == FetchStatus.SUCCESS
    assert _FakeAsyncClient.last_kwargs.get("proxy") == "http://u:p@127.0.0.1:8080"


@pytest.mark.asyncio
async def test_download_httpx_proxy_socks5_no_auth(tmp_path: Path) -> None:
    """A scheme-only proxy URL is forwarded verbatim (no userinfo)."""
    dl = _downloader(proxy_kwargs={"server": "socks5://10.0.0.5:1080"}, tmp_path=tmp_path)
    _FakeAsyncClient.last_kwargs = {}
    _FakeAsyncClient.stream_headers = {"content-type": "application/pdf"}
    with patch("web_agent.downloader.httpx.AsyncClient", _FakeAsyncClient):
        await dl.download("https://example.com/report.pdf")

    assert _FakeAsyncClient.last_kwargs.get("proxy") == "socks5://10.0.0.5:1080"


@pytest.mark.asyncio
async def test_download_httpx_no_proxy_kwarg_when_unset(tmp_path: Path) -> None:
    """No proxy configured -> the proxy key is omitted entirely (not None),
    so the host's real IP path is unchanged when no operator proxy is set."""
    dl = _downloader(tmp_path=tmp_path)
    assert dl._config.proxy.is_active() is False
    _FakeAsyncClient.last_kwargs = {}
    _FakeAsyncClient.stream_headers = {"content-type": "application/pdf"}
    with patch("web_agent.downloader.httpx.AsyncClient", _FakeAsyncClient):
        await dl.download("https://example.com/report.pdf")

    assert "proxy" not in _FakeAsyncClient.last_kwargs


# ======================================================================
# R2: download metrics instrumentation
# ======================================================================


@pytest.mark.asyncio
async def test_successful_download_increments_total_outcome_and_bytes(tmp_path: Path) -> None:
    reg = MetricsRegistry()
    dl = _downloader(metrics=reg, tmp_path=tmp_path)
    _FakeAsyncClient.last_kwargs = {}
    _FakeAsyncClient.stream_headers = {"content-type": "application/pdf"}
    with patch("web_agent.downloader.httpx.AsyncClient", _FakeAsyncClient):
        result = await dl.download("https://example.com/report.pdf")

    assert result.status == FetchStatus.SUCCESS
    snap = reg.snapshot()
    assert snap["counters"]["download_total"] == 1
    assert snap["counters"]["download_outcome{status=success}"] == 1
    # download_bytes observes the saved size (the canned body length).
    assert snap["distributions"]["download_bytes"]["sum"] == float(result.size_bytes)
    assert result.size_bytes == len(b"%PDF-1.7 fake-body")


@pytest.mark.asyncio
async def test_blocked_domain_increments_blocked_outcome_no_bytes(tmp_path: Path) -> None:
    reg = MetricsRegistry()
    config = AppConfig(base_dir=str(tmp_path), safety={"denied_domains": ["evil.com"]})
    dl = Downloader(MagicMock(), config, metrics=reg)

    result = await dl.download("https://evil.com/file.pdf")

    assert result.status == FetchStatus.BLOCKED
    snap = reg.snapshot()
    assert snap["counters"]["download_total"] == 1
    assert snap["counters"]["download_outcome{status=blocked}"] == 1
    # No success -> no bytes distribution recorded.
    assert "download_bytes" not in snap["distributions"]


@pytest.mark.asyncio
async def test_blocked_allow_downloads_increments_blocked_outcome(tmp_path: Path) -> None:
    reg = MetricsRegistry()
    config = AppConfig(base_dir=str(tmp_path), safety={"allow_downloads": False})
    dl = Downloader(MagicMock(), config, metrics=reg)

    result = await dl.download("https://example.com/file.pdf")

    assert result.status == FetchStatus.BLOCKED
    counters = reg.snapshot()["counters"]
    assert counters["download_total"] == 1
    assert counters["download_outcome{status=blocked}"] == 1


@pytest.mark.asyncio
async def test_disabled_registry_records_nothing(tmp_path: Path) -> None:
    reg = MetricsRegistry(enabled=False)
    config = AppConfig(base_dir=str(tmp_path), safety={"denied_domains": ["evil.com"]})
    dl = Downloader(MagicMock(), config, metrics=reg)

    await dl.download("https://evil.com/file.pdf")

    assert reg.snapshot()["counters"] == {}


def test_downloader_defaults_to_noop_registry(tmp_path: Path) -> None:
    """No metrics= passed -> the shared no-op registry (zero-cost), matching
    the WebFetcher / SearchEngine default-arg contract."""
    config = AppConfig(base_dir=str(tmp_path))
    dl = Downloader(MagicMock(), config)
    assert dl._metrics is noop_registry()
    assert get_metrics(None) is noop_registry()
