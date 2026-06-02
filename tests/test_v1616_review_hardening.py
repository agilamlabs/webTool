"""v1.6.16 review-hardening follow-up tests (SSRF / egress-path cluster).

Covers the v1.6.16 fixes for the secondary egress paths that the prior
hardening passes missed. All unit-level -- no real browser, no real
network. httpx is faked with the same ``FakeClient`` / ``FakeStream``
pattern used by ``tests/test_v162_routing.py``; peer-IP and per-redirect
guards are exercised via mocks consistent with ``test_v1614``.

Finding -> test map:
  UT-1     : malformed host (idna UnicodeError) fails closed, not crash.
  FB-1     : fetch_binary blocks redirect-to-private + private peer IP.
  FC-1     : classify_url blocks redirect-to-private + private peer IP.
  FE-1     : per-host limiter re-acquired on every in-loop retry.
  DL-1     : security block from _download_httpx does NOT fall through.
  DL-2     : extension allowlist checks the saved filename + extensionless.
  DL-3     : oversized rendered DOM aborts before materializing in Python.
  ROBOTS-1 : per-host cache / lock dicts evict past a bound.
  ROBOTS-2 : robots.txt fetch skips a private/internal host.
  ROBOTS-3 : first lookup fetches regardless of process uptime (monotonic).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pydantic
import pytest
from web_agent import robots as robots_module
from web_agent import utils
from web_agent.config import (
    AppConfig,
    AutomationConfig,
    BrowserConfig,
    DownloadConfig,
    ExtractionConfig,
    FetchConfig,
    SafetyConfig,
    SearchConfig,
    WorkspaceConfig,
    _is_loopback_host,
)
from web_agent.exceptions import DomainNotAllowedError, NavigationError
from web_agent.models import (
    FetchResult,
    FetchStatus,
    KeyboardInput,
    ScreenshotInput,
    ScrollInput,
)
from web_agent.robots import RobotsChecker
from web_agent.utils import (
    _matches_domain,
    check_domain_allowed,
    httpx_peer_ip,
    is_private_address,
)


# ---------------------------------------------------------------------------
# Shared httpx fakes (mirror tests/test_v162_routing.py)
# ---------------------------------------------------------------------------
class _FakeNetworkStream:
    """Stand-in for httpcore's network_stream extension."""

    def __init__(self, peer_ip: str | None) -> None:
        self._peer_ip = peer_ip

    def get_extra_info(self, key: str) -> Any:
        if key == "server_addr" and self._peer_ip is not None:
            return (self._peer_ip, 0)
        return None


class _FakeStreamResponse:
    """Fake httpx streaming response for ``client.stream(...)``."""

    def __init__(
        self,
        *,
        url: str,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        peer_ip: str | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/pdf"}
        self._chunks = chunks if chunks is not None else [b"hello"]
        self.extensions: dict[str, Any] = {}
        if peer_ip is not None:
            self.extensions["network_stream"] = _FakeNetworkStream(peer_ip)

    async def __aenter__(self) -> _FakeStreamResponse:
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    async def aiter_bytes(self, chunk_size: int = 8192):
        for c in self._chunks:
            yield c


class _FakeHeadResponse:
    """Fake httpx response for ``client.head(...)``."""

    def __init__(
        self,
        *,
        url: str,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        peer_ip: str | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/pdf"}
        self.extensions: dict[str, Any] = {}
        if peer_ip is not None:
            self.extensions["network_stream"] = _FakeNetworkStream(peer_ip)


def _make_fake_client(*, stream_resp=None, head_resp=None):
    """Build a fake httpx.AsyncClient class returning the given responses.

    The fake honours ``event_hooks={"response": [...]}`` by invoking each
    response hook before returning -- so per-redirect validation that raises
    is exercised exactly as production httpx would invoke it.
    """

    class _FakeClient:
        def __init__(self, *a: Any, **k: Any) -> None:
            self._hooks = (k.get("event_hooks") or {}).get("response", [])

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *a: Any) -> bool:
            return False

        def stream(self, _method: str, _url: str, **_k: Any):
            assert stream_resp is not None
            return stream_resp

        async def head(self, _url: str, **_k: Any):
            assert head_resp is not None
            for hook in self._hooks:
                await hook(head_resp)
            return head_resp

    return _FakeClient


# ---------------------------------------------------------------------------
# UT-1: getaddrinfo UnicodeError (idna) must fail closed, not crash the gate
# ---------------------------------------------------------------------------
class TestUT1IdnaUnicodeErrorFailsClosed:
    @pytest.mark.parametrize(
        "host",
        [
            "\udce9xample.com",  # surrogate -> idna UnicodeEncodeError
            "a" * 300 + ".com",  # over-long label -> idna UnicodeError
        ],
    )
    def test_resolver_does_not_raise(self, host: str) -> None:
        utils._resolve_host_addresses.cache_clear()
        # Must not raise UnicodeError; returns empty tuple (unresolvable).
        assert utils._resolve_host_addresses(host) == ()

    @pytest.mark.parametrize(
        "host",
        ["\udce9xample.com", "a" * 300 + ".com"],
    )
    def test_is_private_address_does_not_raise(self, host: str) -> None:
        # Unresolvable -> cannot prove private -> False, no crash.
        assert is_private_address(host) is False

    def test_check_domain_allowed_blocks_unencodable_host_when_blocking_on(self) -> None:
        # Fail-closed: with SSRF protection on, an un-encodable host is
        # rejected (False) instead of raising UnicodeError out of the gate.
        bad = "http://\udce9xample.com/path"
        assert check_domain_allowed(bad, SafetyConfig(block_private_ips=True)) is False

    def test_check_domain_allowed_blocks_overlong_label(self) -> None:
        bad = "http://" + ("a" * 300) + ".com/"
        assert check_domain_allowed(bad, SafetyConfig(block_private_ips=True)) is False

    def test_valid_host_still_allowed(self) -> None:
        # Regression: a normal public host with an empty allow-list passes.
        assert check_domain_allowed(
            "http://example.com/", SafetyConfig(block_private_ips=True)
        ) is (check_domain_allowed("http://example.com/", SafetyConfig(block_private_ips=True)))
        # example.com is encodable and resolves public -> allowed.
        assert check_domain_allowed("http://example.com/", SafetyConfig(block_private_ips=True))

    def test_host_is_encodable_helper(self) -> None:
        assert utils._host_is_encodable("example.com") is True
        assert utils._host_is_encodable("\udce9xample.com") is False
        assert utils._host_is_encodable("a" * 300) is False
        assert utils._host_is_encodable("") is False


# ---------------------------------------------------------------------------
# Shared peer-IP helper (used by FB-1 / FC-1)
# ---------------------------------------------------------------------------
class TestHttpxPeerIpHelper:
    def test_reads_server_addr(self) -> None:
        resp = _FakeStreamResponse(url="https://x/y", peer_ip="10.0.0.5")
        assert httpx_peer_ip(resp) == "10.0.0.5"

    def test_returns_empty_when_no_extension(self) -> None:
        resp = _FakeStreamResponse(url="https://x/y", peer_ip=None)
        assert httpx_peer_ip(resp) == ""


# ---------------------------------------------------------------------------
# FB-1: fetch_binary post-connect peer-IP + per-redirect Location validation
# ---------------------------------------------------------------------------
class TestFB1FetchBinarySSRF:
    def _fetcher(self, monkeypatch: pytest.MonkeyPatch):
        from web_agent import web_fetcher
        from web_agent.web_fetcher import WebFetcher

        cfg = AppConfig(safety=SafetyConfig(block_private_ips=True))
        fetcher = WebFetcher(MagicMock(), cfg)
        return fetcher, web_fetcher

    @pytest.mark.asyncio
    async def test_private_peer_ip_blocks_after_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Host gate passes (public DNS) but the actual peer is private -> BLOCKED."""
        fetcher, web_fetcher = self._fetcher(monkeypatch)

        # check_domain_allowed sees a public host (let it pass), but the
        # connected peer rebinds to a private address.
        monkeypatch.setattr(web_fetcher, "check_domain_allowed", lambda *a, **k: True)
        resp = _FakeStreamResponse(url="https://good.example/file.pdf", peer_ip="169.254.169.254")
        monkeypatch.setattr("httpx.AsyncClient", _make_fake_client(stream_resp=resp))

        result = await fetcher.fetch_binary("https://good.example/file.pdf")
        assert result.status == FetchStatus.BLOCKED
        assert "private" in (result.error_message or "").lower()

    @pytest.mark.asyncio
    async def test_public_peer_ip_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: a public peer IP streams normally."""
        fetcher, web_fetcher = self._fetcher(monkeypatch)
        monkeypatch.setattr(web_fetcher, "check_domain_allowed", lambda *a, **k: True)
        resp = _FakeStreamResponse(
            url="https://good.example/file.pdf",
            peer_ip="93.184.216.34",
            chunks=[b"PDFDATA"],
        )
        monkeypatch.setattr("httpx.AsyncClient", _make_fake_client(stream_resp=resp))

        result = await fetcher.fetch_binary("https://good.example/file.pdf")
        assert result.status == FetchStatus.SUCCESS
        assert result.binary == b"PDFDATA"

    @pytest.mark.asyncio
    async def test_redirect_hook_blocks_disallowed_location(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 3xx Location to a denied host raises in the hook -> BLOCKED."""
        fetcher, web_fetcher = self._fetcher(monkeypatch)
        # Entry host allowed; the redirect Location is denied.
        denied = "http://169.254.169.254/latest/meta-data"

        def _gate(url: str, *a: Any, **k: Any) -> bool:
            return "169.254" not in url

        monkeypatch.setattr(web_fetcher, "check_domain_allowed", _gate)

        # Drive the event hook the way the production client would: a fake
        # client whose stream() runs the response hook against a 302.
        class _RedirectingStream(_FakeStreamResponse):
            pass

        hook_holder: dict[str, Any] = {}

        class _FakeClient:
            def __init__(self, *a: Any, **k: Any) -> None:
                hook_holder["hooks"] = (k.get("event_hooks") or {}).get("response", [])

            async def __aenter__(self) -> _FakeClient:
                return self

            async def __aexit__(self, *a: Any) -> bool:
                return False

            def stream(self, _m: str, _u: str, **_k: Any):
                redirect_resp = MagicMock()
                redirect_resp.status_code = 302
                redirect_resp.headers = {"location": denied}

                class _Ctx:
                    async def __aenter__(_self):  # noqa: N805
                        for h in hook_holder["hooks"]:
                            await h(redirect_resp)
                        return _RedirectingStream(url="https://good.example/x")

                    async def __aexit__(_self, *a):  # noqa: N805
                        return False

                return _Ctx()

        monkeypatch.setattr("httpx.AsyncClient", _FakeClient)

        result = await fetcher.fetch_binary("https://good.example/x")
        assert result.status == FetchStatus.BLOCKED
        assert "disallowed" in (result.error_message or "").lower()


# ---------------------------------------------------------------------------
# FC-1: classify_url post-connect peer-IP + per-redirect validation
# ---------------------------------------------------------------------------
class TestFC1ClassifyUrlSSRF:
    def _fetcher(self):
        from web_agent.web_fetcher import WebFetcher

        cfg = AppConfig(safety=SafetyConfig(block_private_ips=True, probe_binary_urls=True))
        return WebFetcher(MagicMock(), cfg)

    @pytest.mark.asyncio
    async def test_private_peer_ip_returns_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from web_agent import web_fetcher

        fetcher = self._fetcher()
        monkeypatch.setattr(web_fetcher, "check_domain_allowed", lambda *a, **k: True)
        head = _FakeHeadResponse(url="https://good.example/doc", peer_ip="127.0.0.1")
        monkeypatch.setattr("httpx.AsyncClient", _make_fake_client(head_resp=head))

        # Extensionless URL forces the HEAD probe.
        kind = await fetcher.classify_url("https://good.example/doc")
        assert kind == "unknown"

    @pytest.mark.asyncio
    async def test_redirect_to_denied_returns_unknown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from web_agent import web_fetcher

        fetcher = self._fetcher()

        def _gate(url: str, *a: Any, **k: Any) -> bool:
            return "169.254" not in url

        monkeypatch.setattr(web_fetcher, "check_domain_allowed", _gate)

        # HEAD returns a 302 hook to a denied Location -> the hook raises
        # NavigationError -> swallowed to 'unknown'.
        class _FakeClient:
            def __init__(self, *a: Any, **k: Any) -> None:
                self._hooks = (k.get("event_hooks") or {}).get("response", [])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def head(self, _url: str, **_k: Any):
                redirect = MagicMock()
                redirect.status_code = 301
                redirect.headers = {"location": "http://169.254.169.254/x"}
                for h in self._hooks:
                    await h(redirect)
                return _FakeHeadResponse(url="https://good.example/doc")

        monkeypatch.setattr("httpx.AsyncClient", _FakeClient)

        kind = await fetcher.classify_url("https://good.example/doc")
        assert kind == "unknown"

    @pytest.mark.asyncio
    async def test_public_peer_classifies_normally(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from web_agent import web_fetcher

        fetcher = self._fetcher()
        monkeypatch.setattr(web_fetcher, "check_domain_allowed", lambda *a, **k: True)
        head = _FakeHeadResponse(
            url="https://good.example/doc",
            headers={"content-type": "application/pdf"},
            peer_ip="93.184.216.34",
        )
        monkeypatch.setattr("httpx.AsyncClient", _make_fake_client(head_resp=head))

        kind = await fetcher.classify_url("https://good.example/doc")
        assert kind == "pdf"


# ---------------------------------------------------------------------------
# FE-1: rate limiter re-acquired on every in-loop retry
# ---------------------------------------------------------------------------
class TestFE1LimiterReacquiredPerRetry:
    @pytest.mark.asyncio
    async def test_acquire_called_once_per_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from web_agent.web_fetcher import WebFetcher

        # max_retries=3 so the failing fetch retries; acquire must be called
        # once per attempt (3x), not once for the whole fetch().
        cfg = AppConfig()
        cfg.fetch.max_retries = 3
        cfg.fetch.retry_base_delay = 0.0
        cfg.fetch.retry_max_delay = 0.0

        limiter = MagicMock()
        limiter.acquire = AsyncMock()

        fetcher = WebFetcher(MagicMock(), cfg, rate_limiter=limiter)

        # Make the navigation always raise a retryable error so the loop
        # exhausts all attempts. _navigate_and_extract is the inner call.
        attempts = {"n": 0}

        async def _boom(*a: Any, **k: Any):
            attempts["n"] += 1
            raise Exception("transient boom")

        monkeypatch.setattr(fetcher, "_navigate_and_extract", _boom)

        # Avoid the real browser: _fetch_with_retry uses self._bm.new_page().
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_page():
            yield MagicMock()

        fetcher._bm.new_page = MagicMock(side_effect=lambda *a, **k: _fake_page())

        result = await fetcher.fetch("https://good.example/page")

        # All three attempts ran, and the limiter was acquired once per attempt.
        assert attempts["n"] == 3
        assert limiter.acquire.await_count == 3
        assert result.status == FetchStatus.NETWORK_ERROR


# ---------------------------------------------------------------------------
# DL-1: SSRF/security block from _download_httpx must NOT fall through
# ---------------------------------------------------------------------------
class TestDL1SecurityBlockDoesNotFallThrough:
    def _downloader(self, tmp_path: Path):
        from web_agent.downloader import Downloader

        cfg = AppConfig(
            download=DownloadConfig(download_dir=str(tmp_path)),
            safety=SafetyConfig(allow_downloads=True),
        )
        return Downloader(MagicMock(), cfg)

    @pytest.mark.asyncio
    async def test_navigation_error_returns_blocked_no_playwright(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dl = self._downloader(tmp_path)

        async def _raise_nav(*a: Any, **k: Any):
            raise NavigationError("Redirect to disallowed URL blocked: http://169.254.169.254/")

        monkeypatch.setattr(dl, "_download_httpx", _raise_nav)

        # Sentinels: the Playwright strategies must NOT be invoked.
        save_page = AsyncMock()
        dl_play = AsyncMock()
        monkeypatch.setattr(dl, "_save_page_with_playwright", save_page)
        monkeypatch.setattr(dl, "_download_with_playwright", dl_play)

        result = await dl.download("https://good.example/file.pdf")

        assert result.status == FetchStatus.BLOCKED
        save_page.assert_not_called()
        dl_play.assert_not_called()

    @pytest.mark.asyncio
    async def test_domain_not_allowed_error_returns_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dl = self._downloader(tmp_path)

        async def _raise_dna(*a: Any, **k: Any):
            raise DomainNotAllowedError("nope", url="http://10.0.0.1/")

        monkeypatch.setattr(dl, "_download_httpx", _raise_dna)
        save_page = AsyncMock()
        monkeypatch.setattr(dl, "_save_page_with_playwright", save_page)
        monkeypatch.setattr(dl, "_download_with_playwright", AsyncMock())

        result = await dl.download("https://good.example/file.pdf")
        assert result.status == FetchStatus.BLOCKED
        save_page.assert_not_called()

    @pytest.mark.asyncio
    async def test_transient_error_still_falls_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuine transport error must STILL fall through to Playwright."""
        dl = self._downloader(tmp_path)

        async def _raise_transient(*a: Any, **k: Any):
            raise ConnectionError("connection reset")

        monkeypatch.setattr(dl, "_download_httpx", _raise_transient)

        async def _save_ok(*a: Any, **k: Any):
            from web_agent.models import DownloadResult

            return DownloadResult(
                url="https://good.example/page.html",
                filepath="x",
                filename="page.html",
                status=FetchStatus.SUCCESS,
            )

        save_page = AsyncMock(side_effect=_save_ok)
        monkeypatch.setattr(dl, "_save_page_with_playwright", save_page)

        # .html -> web page URL -> Strategy 2 (save_page)
        result = await dl.download("https://good.example/page.html")
        assert result.status == FetchStatus.SUCCESS
        save_page.assert_called_once()


# ---------------------------------------------------------------------------
# DL-2: allowed_extensions checks the saved filename, blocks extensionless
# ---------------------------------------------------------------------------
class TestDL2ExtensionAllowlist:
    def _downloader(self, tmp_path: Path):
        from web_agent.downloader import Downloader

        cfg = AppConfig(
            download=DownloadConfig(
                download_dir=str(tmp_path),
                allowed_extensions=[".pdf", ".csv"],
            ),
            safety=SafetyConfig(allow_downloads=True),
        )
        return Downloader(MagicMock(), cfg)

    @pytest.mark.asyncio
    async def test_extensionless_url_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dl = self._downloader(tmp_path)
        # If the allowlist were bypassed, _download_httpx would run; assert
        # it never does for a blocked extensionless name.
        monkeypatch.setattr(dl, "_download_httpx", AsyncMock())
        monkeypatch.setattr(dl, "_save_page_with_playwright", AsyncMock())
        result = await dl.download("https://good.example/download")  # no extension
        assert result.status == FetchStatus.BLOCKED
        assert "extensionless" in (result.error_message or "").lower()

    @pytest.mark.asyncio
    async def test_saved_filename_extension_checked_not_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """url ends .pdf but caller forces filename='payload.exe' -> BLOCKED on .exe."""
        dl = self._downloader(tmp_path)
        monkeypatch.setattr(dl, "_download_httpx", AsyncMock())
        result = await dl.download("https://good.example/a.pdf", filename="payload.exe")
        assert result.status == FetchStatus.BLOCKED
        assert ".exe" in (result.error_message or "").lower()

    @pytest.mark.asyncio
    async def test_allowed_extension_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dl = self._downloader(tmp_path)

        async def _ok(url: str, filepath: Path):
            from web_agent.models import DownloadResult

            return DownloadResult(
                url=url,
                filepath=str(filepath),
                filename=filepath.name,
                status=FetchStatus.SUCCESS,
            )

        monkeypatch.setattr(dl, "_download_httpx", AsyncMock(side_effect=_ok))
        result = await dl.download("https://good.example/report.pdf")
        assert result.status == FetchStatus.SUCCESS


# ---------------------------------------------------------------------------
# DL-3: oversized rendered DOM aborts before materializing in Python
# ---------------------------------------------------------------------------
class TestDL3PageSaveMemoryBound:
    @pytest.mark.asyncio
    async def test_oversized_dom_probe_aborts_before_content(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from web_agent import downloader as downloader_module
        from web_agent.downloader import Downloader

        cfg = AppConfig(
            download=DownloadConfig(download_dir=str(tmp_path), max_file_size_mb=1),
            safety=SafetyConfig(allow_downloads=True),
        )
        dl = Downloader(MagicMock(), cfg)

        monkeypatch.setattr(downloader_module, "check_domain_allowed", lambda *a, **k: True)

        # safe_page_content MUST NOT be called -- the probe aborts first.
        called = {"content": False}

        async def _should_not_run(*a: Any, **k: Any):
            called["content"] = True
            return ("x", "content")

        monkeypatch.setattr(downloader_module, "safe_page_content", _should_not_run)

        # Fake page: goto returns a response with no Content-Length; the
        # evaluate() probe reports a length far above the 1 MB cap.
        page = MagicMock()
        resp = MagicMock()
        resp.headers = {"content-type": "text/html"}
        page.goto = AsyncMock(return_value=resp)
        page.url = "https://good.example/huge"
        page.evaluate = AsyncMock(return_value=5 * 1024 * 1024)  # 5 MB chars

        filepath = tmp_path / "huge.html"
        result = await dl._do_save_page(page, "https://good.example/huge", filepath)

        assert result.status == FetchStatus.HTTP_ERROR
        assert "exceeds" in (result.error_message or "").lower()
        assert called["content"] is False, "DOM was materialized despite exceeding the cap"

    @pytest.mark.asyncio
    async def test_small_dom_proceeds_to_capture(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from web_agent import downloader as downloader_module
        from web_agent.downloader import Downloader

        cfg = AppConfig(
            download=DownloadConfig(download_dir=str(tmp_path), max_file_size_mb=1),
            safety=SafetyConfig(allow_downloads=True),
        )
        dl = Downloader(MagicMock(), cfg)
        monkeypatch.setattr(downloader_module, "check_domain_allowed", lambda *a, **k: True)

        async def _content(*a: Any, **k: Any):
            return ("<html>small</html>", "content")

        monkeypatch.setattr(downloader_module, "safe_page_content", _content)

        page = MagicMock()
        resp = MagicMock()
        resp.headers = {"content-type": "text/html"}
        page.goto = AsyncMock(return_value=resp)
        page.url = "https://good.example/small"
        page.evaluate = AsyncMock(return_value=20)  # tiny

        filepath = tmp_path / "small.html"
        result = await dl._do_save_page(page, "https://good.example/small", filepath)
        assert result.status == FetchStatus.SUCCESS


# ---------------------------------------------------------------------------
# ROBOTS-1: per-host cache / lock dicts evict past a bound
# ---------------------------------------------------------------------------
class TestRobots1Eviction:
    @pytest.mark.asyncio
    async def test_locks_bounded_by_maxsize(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(robots_module, "_ROBOTS_CACHE_MAXSIZE", 3)
        rc = RobotsChecker(user_agent="bot", ttl_seconds=1e9, block_private_ips=False)

        async def _stub(scheme: str, host: str):
            return None

        monkeypatch.setattr(rc, "_fetch_and_parse", _stub)

        for i in range(10):
            await rc.is_allowed(f"https://host{i}.example/p")

        # Never exceeds the cap; the lock dict is the heaviest map.
        assert len(rc._locks) <= 3
        assert len(rc._cache) <= 3
        # The most-recently-seen host survived; the oldest was evicted.
        assert "host9.example" in rc._locks
        assert "host0.example" not in rc._locks


# ---------------------------------------------------------------------------
# ROBOTS-2: robots.txt fetch skips a private/internal host
# ---------------------------------------------------------------------------
class TestRobots2PrivateHostSkipped:
    @pytest.mark.asyncio
    async def test_private_host_skips_fetch_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rc = RobotsChecker(user_agent="bot", block_private_ips=True)

        # Any attempt to construct an httpx client = a real fetch was tried.
        def _boom(*a: Any, **k: Any):
            raise AssertionError("robots.txt must not fetch a private host")

        monkeypatch.setattr(robots_module.httpx, "AsyncClient", _boom)

        result = await rc._fetch_and_parse("http", "169.254.169.254")
        assert result is None  # allow-all, fail-safe

    @pytest.mark.asyncio
    async def test_loopback_host_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rc = RobotsChecker(user_agent="bot", block_private_ips=True)
        monkeypatch.setattr(
            robots_module.httpx,
            "AsyncClient",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")),
        )
        assert await rc._fetch_and_parse("http", "127.0.0.1") is None

    @pytest.mark.asyncio
    async def test_opt_out_attempts_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """block_private_ips=False honours the operator opt-out (does fetch)."""
        rc = RobotsChecker(user_agent="bot", block_private_ips=False)
        attempted = {"n": 0}

        class _Client:
            def __init__(self, *a: Any, **k: Any) -> None:
                attempted["n"] += 1

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, _url: str, **_k: Any):
                resp = MagicMock()
                resp.status_code = 404
                return resp

        monkeypatch.setattr(robots_module.httpx, "AsyncClient", _Client)
        result = await rc._fetch_and_parse("http", "127.0.0.1")
        assert attempted["n"] == 1  # the fetch was attempted (not skipped)
        assert result is None  # 404 -> allow-all


# ---------------------------------------------------------------------------
# ROBOTS-3: first lookup fetches regardless of process uptime (monotonic)
# ---------------------------------------------------------------------------
class TestRobots3FirstLookupFetchesRegardlessOfUptime:
    @pytest.mark.asyncio
    async def test_first_lookup_fetches_when_uptime_below_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pin time.monotonic() BELOW the TTL -- mimics a machine with fewer
        # than ttl seconds of uptime. With the old ``(0.0, None)`` sentinel
        # and ``(monotonic() - cached_at) > ttl`` staleness test, the first
        # lookup NEVER fetched in this window, so robots.txt was silently
        # ignored (allow-all) for the first hour of uptime.
        monkeypatch.setattr(robots_module.time, "monotonic", lambda: 10.0)
        rc = RobotsChecker(user_agent="bot", ttl_seconds=3600.0, block_private_ips=False)

        calls = {"n": 0}

        async def _counting_stub(scheme: str, host: str):
            calls["n"] += 1
            return None

        monkeypatch.setattr(rc, "_fetch_and_parse", _counting_stub)

        await rc.is_allowed("https://example.com/a")
        assert calls["n"] == 1, "first lookup must fetch even when uptime < ttl"

        # Within TTL -> served from cache, no second fetch.
        await rc.is_allowed("https://example.com/b")
        assert calls["n"] == 1


# ===========================================================================
# Config / validation cluster (CO-1..CO-9, BR-3, BR-4, MO-1)
# ===========================================================================
#
# Finding -> test map:
#   CO-1 : bare unprefixed env vars do NOT disable fences on AppConfig();
#          correctly-prefixed nested vars still work.
#   CO-2 : deny-list entries with port / IPv6 brackets actually match.
#   CO-3 : max_contexts=0 (and negative) rejected.
#   CO-4 : negative / zero max_file_size_mb rejected.
#   CO-5 : partial retry override keeps the NAMED policy's other delays.
#   CO-7 : assorted security/throughput ints reject negative/zero.
#   CO-8 : _is_loopback_host recognises obfuscated loopback literals.
#   CO-9 : _resolve_paths uses the cross-platform absolute predicate.
#   BR-3 : KeyboardInput.repeat out-of-range rejected; in-range accepted.
#   BR-4 : ScrollInput.infinite_scroll_max out-of-range rejected.
#   MO-1 : FetchResult html/binary mutual-exclusivity; quality range.


# ---------------------------------------------------------------------------
# CO-1: bare env vars must NOT flip security fences; nested vars still work
# ---------------------------------------------------------------------------
class TestCO1EnvPrefixIsolation:
    def test_bare_block_private_ips_does_not_disable_fence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The scariest case in the report: a stray bare env var must not
        # silently disable SSRF protection on the documented default path.
        monkeypatch.setenv("BLOCK_PRIVATE_IPS", "false")
        assert AppConfig().safety.block_private_ips is True
        # standalone instantiation is equally protected.
        assert SafetyConfig().block_private_ips is True

    def test_bare_allow_upload_outside_download_dir_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ALLOW_UPLOAD_OUTSIDE_DOWNLOAD_DIR", "true")
        assert AppConfig().safety.allow_upload_outside_download_dir is False

    def test_bare_headless_and_safe_mode_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEADLESS", "false")
        monkeypatch.setenv("SAFE_MODE", "true")
        cfg = AppConfig()
        assert cfg.browser.headless is True
        assert cfg.safety.safe_mode is False

    def test_bare_execute_helpers_does_not_enable_python(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # WorkspaceConfig dodged the PATH foot-gun via a field rename but
        # still read other bare vars: a stray EXECUTE_HELPERS=true would
        # silently enable Python-helper execution on a default AppConfig().
        # (NB: we deliberately do not set a bare ``ENABLED`` here -- that
        # name collides with SkillsConfig's deprecated ``enabled`` alias,
        # an orthogonal concern. EXECUTE_HELPERS is the WorkspaceConfig-
        # specific security knob this fix is about.)
        monkeypatch.setenv("EXECUTE_HELPERS", "true")
        cfg = AppConfig()
        assert cfg.workspace.execute_helpers is False
        # standalone instantiation is equally protected.
        assert WorkspaceConfig().execute_helpers is False

    def test_nested_prefixed_workspace_var_still_works(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WEB_AGENT_WORKSPACE__EXECUTE_HELPERS", "true")
        assert AppConfig().workspace.execute_helpers is True

    def test_nested_prefixed_safety_var_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The correctly-namespaced nested var MUST still take effect via
        # AppConfig's env_nested_delimiter path.
        monkeypatch.setenv("WEB_AGENT_SAFETY__BLOCK_PRIVATE_IPS", "false")
        assert AppConfig().safety.block_private_ips is False

    def test_nested_prefixed_vars_across_sections(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEB_AGENT_BROWSER__HEADLESS", "false")
        monkeypatch.setenv("WEB_AGENT_DOWNLOAD__MAX_FILE_SIZE_MB", "7")
        monkeypatch.setenv("WEB_AGENT_DIAGNOSTICS__CAPTURE_NETWORK", "true")
        monkeypatch.setenv("WEB_AGENT_LOG_LEVEL", "DEBUG")
        cfg = AppConfig()
        assert cfg.browser.headless is False
        assert cfg.download.max_file_size_mb == 7
        assert cfg.diagnostics.capture_network is True
        assert cfg.log_level == "DEBUG"

    def test_standalone_subconfig_reads_its_own_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A standalone sub-config reads ONLY its WEB_AGENT_<SECTION>__ vars.
        monkeypatch.setenv("WEB_AGENT_SAFETY__ALLOW_DOWNLOADS", "false")
        assert SafetyConfig().allow_downloads is False
        # ...and ignores the bare name.
        monkeypatch.delenv("WEB_AGENT_SAFETY__ALLOW_DOWNLOADS", raising=False)
        monkeypatch.setenv("ALLOW_DOWNLOADS", "false")
        assert SafetyConfig().allow_downloads is True


# ---------------------------------------------------------------------------
# CO-2: deny-list entries with port / IPv6 brackets must actually match
# ---------------------------------------------------------------------------
class TestCO2DenyListPortAndBracketNormalization:
    @pytest.mark.parametrize(
        "raw, host",
        [
            ("evil.com:8443", "evil.com"),
            ("[::1]", "::1"),
            ("http://[2001:db8::1]:9999/x", "2001:db8::1"),
            ("localhost:8888", "localhost"),
            ("internal.svc:8080", "internal.svc"),
        ],
    )
    def test_denied_entry_matches_bare_host(self, raw: str, host: str) -> None:
        cfg = SafetyConfig(denied_domains=[raw])
        # exactly one normalized pattern, and it matches the bare host the
        # match-time comparator (_normalize_host) produces.
        assert len(cfg.denied_domains) == 1
        assert _matches_domain(host, cfg.denied_domains[0]) is True

    def test_subdomain_of_ported_deny_entry_matches(self) -> None:
        cfg = SafetyConfig(denied_domains=["evil.com:8443"])
        assert _matches_domain("api.evil.com", cfg.denied_domains[0]) is True

    def test_allowed_entry_with_port_normalized_too(self) -> None:
        cfg = SafetyConfig(allowed_domains=["good.com:443"])
        assert cfg.allowed_domains == ["good.com"]


# ---------------------------------------------------------------------------
# CO-3: max_contexts lower bound
# ---------------------------------------------------------------------------
class TestCO3MaxContextsLowerBound:
    @pytest.mark.parametrize("bad", [0, -1, -5])
    def test_non_positive_rejected(self, bad: int) -> None:
        with pytest.raises(pydantic.ValidationError):
            BrowserConfig(max_contexts=bad)

    def test_one_accepted(self) -> None:
        assert BrowserConfig(max_contexts=1).max_contexts == 1


# ---------------------------------------------------------------------------
# CO-4: max_file_size_mb lower bound
# ---------------------------------------------------------------------------
class TestCO4MaxFileSizeLowerBound:
    @pytest.mark.parametrize("bad", [0, -1, -100])
    def test_non_positive_rejected(self, bad: int) -> None:
        with pytest.raises(pydantic.ValidationError):
            DownloadConfig(max_file_size_mb=bad)

    def test_one_mb_accepted(self) -> None:
        assert DownloadConfig(max_file_size_mb=1).max_file_size_mb == 1


# ---------------------------------------------------------------------------
# CO-5: partial retry override keeps the NAMED policy's other delays
# ---------------------------------------------------------------------------
class TestCO5RetryPolicyPartialOverride:
    def test_paranoid_plus_max_retries_keeps_paranoid_delays(self) -> None:
        # paranoid = 5 / 2.0 / 60.0; overriding only max_retries must keep
        # paranoid's base/max delays (NOT balanced's 1.0/30.0).
        f = FetchConfig(retry_policy="paranoid", max_retries=9)
        assert f.max_retries == 9
        assert f.retry_base_delay == 2.0
        assert f.retry_max_delay == 60.0

    def test_fast_plus_base_delay_keeps_fast_other_fields(self) -> None:
        # fast = 1 / 0.5 / 5.0; overriding only base_delay keeps fast's
        # max_retries and max_delay.
        f = FetchConfig(retry_policy="fast", retry_base_delay=0.1)
        assert f.max_retries == 1
        assert f.retry_base_delay == 0.1
        assert f.retry_max_delay == 5.0

    def test_full_named_policy_applied_when_no_override(self) -> None:
        f = FetchConfig(retry_policy="paranoid")
        assert (f.max_retries, f.retry_base_delay, f.retry_max_delay) == (5, 2.0, 60.0)

    def test_balanced_with_partial_override_keeps_balanced_defaults(self) -> None:
        f = FetchConfig(retry_policy="balanced", max_retries=7)
        assert (f.max_retries, f.retry_base_delay, f.retry_max_delay) == (7, 1.0, 30.0)

    def test_all_three_overridden_under_named_policy(self) -> None:
        f = FetchConfig(
            retry_policy="paranoid",
            max_retries=2,
            retry_base_delay=0.25,
            retry_max_delay=3.0,
        )
        assert (f.max_retries, f.retry_base_delay, f.retry_max_delay) == (2, 0.25, 3.0)


# ---------------------------------------------------------------------------
# CO-7: assorted security/throughput int lower bounds
# ---------------------------------------------------------------------------
class TestCO7IntLowerBounds:
    def test_search_max_results_rejects_non_positive(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            SearchConfig(max_results=0)

    def test_search_timeout_rejects_non_positive(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            SearchConfig(searxng_timeout=0)

    def test_browser_viewport_rejects_non_positive(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            BrowserConfig(viewport_width=0)
        with pytest.raises(pydantic.ValidationError):
            BrowserConfig(viewport_height=-1)

    def test_browser_timeouts_reject_negative(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            BrowserConfig(default_timeout=-1)
        with pytest.raises(pydantic.ValidationError):
            BrowserConfig(slow_mo=-1)

    def test_extraction_min_content_length_rejects_negative(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            ExtractionConfig(min_content_length=-1)
        # zero remains a valid "accept any non-empty content" sentinel.
        assert ExtractionConfig(min_content_length=0).min_content_length == 0

    def test_automation_screenshot_quality_range(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            AutomationConfig(screenshot_quality=101)
        with pytest.raises(pydantic.ValidationError):
            AutomationConfig(screenshot_quality=-1)
        assert AutomationConfig(screenshot_quality=100).screenshot_quality == 100


# ---------------------------------------------------------------------------
# CO-8: _is_loopback_host recognises obfuscated loopback literals
# ---------------------------------------------------------------------------
class TestCO8LoopbackObfuscatedLiterals:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "127.0.0.2",
            "127.255.255.254",
            "::1",
            "[::1]",
            "localhost",
            "2130706433",  # decimal 127.0.0.1
            "0177.0.0.1",  # octal first octet
            "0x7f.0.0.1",  # hex first octet
            "127.1",  # short-form
        ],
    )
    def test_loopback_literals_recognised(self, host: str) -> None:
        assert _is_loopback_host(host) is True

    @pytest.mark.parametrize(
        "host",
        ["8.8.8.8", "10.0.0.1", "169.254.169.254", "example.com", "", None],
    )
    def test_non_loopback_rejected(self, host: str | None) -> None:
        assert _is_loopback_host(host) is False


# ---------------------------------------------------------------------------
# CO-9: _resolve_paths uses the cross-platform absolute predicate
# ---------------------------------------------------------------------------
class TestCO9CrossPlatformAbsolutePaths:
    def test_windows_absolute_output_dir_not_rejoined(self) -> None:
        # A Windows drive-rooted absolute path must be treated as absolute
        # regardless of the host OS (Path.is_absolute() would miss it on
        # POSIX), so it is NOT re-joined under base_dir.
        cfg = AppConfig(base_dir="/tmp/base", output_dir="C:/abs/out")
        assert cfg.output_dir == "C:/abs/out"

    def test_unc_path_not_rejoined(self) -> None:
        cfg = AppConfig(base_dir="/tmp/base", output_dir=r"\\server\share\out")
        assert cfg.output_dir == r"\\server\share\out"

    def test_relative_path_still_resolved_under_base(self) -> None:
        cfg = AppConfig(base_dir="/tmp/base", download=DownloadConfig(download_dir="rel/dl"))
        # Resolved to an absolute path anchored at the resolved base_dir.
        assert cfg.download.download_dir.endswith(str(Path("rel") / "dl"))
        assert Path(cfg.download.download_dir).is_absolute()


# ---------------------------------------------------------------------------
# BR-3: KeyboardInput.repeat bounds
# ---------------------------------------------------------------------------
class TestBR3KeyboardRepeatBounds:
    @pytest.mark.parametrize("bad", [0, -1, 101, 100_000_000])
    def test_out_of_range_rejected(self, bad: int) -> None:
        with pytest.raises(pydantic.ValidationError):
            KeyboardInput(key="Enter", repeat=bad)

    @pytest.mark.parametrize("ok", [1, 50, 100])
    def test_in_range_accepted(self, ok: int) -> None:
        assert KeyboardInput(key="Enter", repeat=ok).repeat == ok

    def test_default_is_one(self) -> None:
        assert KeyboardInput(key="Enter").repeat == 1


# ---------------------------------------------------------------------------
# BR-4: ScrollInput.infinite_scroll_max bounds
# ---------------------------------------------------------------------------
class TestBR4InfiniteScrollMaxBounds:
    @pytest.mark.parametrize("bad", [0, -1, 1001, 10_000_000])
    def test_out_of_range_rejected(self, bad: int) -> None:
        with pytest.raises(pydantic.ValidationError):
            ScrollInput(infinite_scroll_max=bad)

    @pytest.mark.parametrize("ok", [1, 10, 1000])
    def test_in_range_accepted(self, ok: int) -> None:
        assert ScrollInput(infinite_scroll_max=ok).infinite_scroll_max == ok

    def test_negative_delay_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            ScrollInput(infinite_scroll_delay_ms=-1)


# ---------------------------------------------------------------------------
# MO-1: FetchResult html/binary exclusivity + ScreenshotInput.quality range
# ---------------------------------------------------------------------------
class TestMO1ModelInvariants:
    def test_html_only_accepted(self) -> None:
        fr = FetchResult(url="u", final_url="u", status=FetchStatus.SUCCESS, html="<p>")
        assert fr.html is not None and fr.binary is None

    def test_binary_only_accepted(self) -> None:
        fr = FetchResult(url="u", final_url="u", status=FetchStatus.SUCCESS, binary=b"PDF")
        assert fr.binary is not None and fr.html is None

    def test_both_none_accepted_for_blocked(self) -> None:
        fr = FetchResult(url="u", final_url="u", status=FetchStatus.BLOCKED)
        assert fr.html is None and fr.binary is None

    def test_both_set_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            FetchResult(
                url="u",
                final_url="u",
                status=FetchStatus.SUCCESS,
                html="<p>",
                binary=b"PDF",
            )

    @pytest.mark.parametrize("bad", [-1, 101, 200])
    def test_screenshot_quality_out_of_range_rejected(self, bad: int) -> None:
        with pytest.raises(pydantic.ValidationError):
            ScreenshotInput(quality=bad)

    @pytest.mark.parametrize("ok", [0, 50, 100])
    def test_screenshot_quality_in_range_accepted(self, ok: int) -> None:
        assert ScreenshotInput(quality=ok).quality == ok

    def test_screenshot_quality_none_default(self) -> None:
        assert ScreenshotInput().quality is None


# ===========================================================================
# Arbitrary-write + skill-scope cluster (MC-1, GH-1, WS-1, EC-1)
# ===========================================================================
#
# Finding -> test map:
#   MC-1 : web_print_page_as_pdf contains output_path to screenshot_dir --
#          absolute / '..' paths rejected; a normal relative name still works
#          and reaches the page.pdf() write inside screenshot_dir.
#   GH-1 : the github_release_download sanitizer actually strips ``site:``
#          (and other field operators) + standalone boolean OR; ordinary
#          query text survives.
#   WS-1 : markdown_skills_only gate validates the NORMALIZED path, so a
#          ``..`` escape is blocked while legitimate domain-skills/ writes
#          are allowed.
#   EC-1 : EC host confinement matches the parsed hostname at label
#          boundaries -- ``ec.europa.eu.evil.com`` and
#          ``evil.com/?x=ec.europa.eu`` rejected; real EC host + subdomain
#          accepted.


# ---------------------------------------------------------------------------
# MC-1: web_print_page_as_pdf contains an LLM-controlled output_path
# ---------------------------------------------------------------------------
class TestMC1PdfOutputPathContained:
    """The MCP boundary must contain ``output_path`` to the screenshot dir.

    The underlying ``BrowserActions.print_page_as_pdf`` honours an absolute
    ``output_path`` verbatim (arbitrary file write). The fix validates +
    rewrites the path at the ``web_print_page_as_pdf`` MCP tool before
    calling down.
    """

    def _ctx_and_agent(self, tmp_path: Path):
        """Return (ctx, agent_mock, shot_dir) wired like FastMCP's lifespan."""
        from web_agent.models import ActionStatus, ScreenshotFormat, ScreenshotResult

        agent = MagicMock()
        agent._config = AppConfig(base_dir=str(tmp_path))
        # The PDF write lands in automation.screenshot_dir.
        shot_dir = Path(agent._config.automation.screenshot_dir)
        shot_dir.mkdir(parents=True, exist_ok=True)
        agent.print_page_as_pdf = AsyncMock(
            return_value=ScreenshotResult(
                url="https://example.com/",
                path=str(shot_dir / "ok.pdf"),
                format=ScreenshotFormat.PNG,
                status=ActionStatus.SUCCESS,
            )
        )
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"agent": agent}
        return ctx, agent, shot_dir

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_path",
        [
            "/etc/cron.d/evil.pdf",  # POSIX absolute
            r"C:\Users\me\Startup\evil.pdf",  # Windows drive-rooted
            r"\\server\share\evil.pdf",  # UNC
            "../../escape.pdf",  # parent traversal
            "domain/../../../escape.pdf",  # nested traversal escape
        ],
    )
    async def test_absolute_or_traversal_rejected(self, tmp_path: Path, bad_path: str) -> None:
        from web_agent.mcp_server import web_print_page_as_pdf

        ctx, agent, _shot = self._ctx_and_agent(tmp_path)
        out = await web_print_page_as_pdf(ctx, url="https://example.com/", output_path=bad_path)
        # Rejected at the boundary: FAILED, and the underlying write was
        # NEVER reached (so nothing could be written outside the dir).
        assert out["status"] == "failed"
        assert "output_path" in (out.get("error_message") or "")
        agent.print_page_as_pdf.assert_not_called()

    @pytest.mark.asyncio
    async def test_relative_name_passes_contained(self, tmp_path: Path) -> None:
        from web_agent.mcp_server import web_print_page_as_pdf

        ctx, agent, shot_dir = self._ctx_and_agent(tmp_path)
        out = await web_print_page_as_pdf(ctx, url="https://example.com/", output_path="report.pdf")
        assert out["status"] == "success"
        agent.print_page_as_pdf.assert_awaited_once()
        # The path handed down is relative (never absolute) and re-anchors
        # inside the screenshot dir, so it cannot hit the absolute bypass.
        kwargs = agent.print_page_as_pdf.await_args.kwargs
        passed = kwargs["output_path"]
        assert not Path(passed).is_absolute()
        assert utils.safe_join_path(shot_dir, passed).parent == shot_dir.resolve()

    @pytest.mark.asyncio
    async def test_none_output_path_passes_through(self, tmp_path: Path) -> None:
        from web_agent.mcp_server import web_print_page_as_pdf

        ctx, agent, _shot = self._ctx_and_agent(tmp_path)
        out = await web_print_page_as_pdf(ctx, url="https://example.com/")
        assert out["status"] == "success"
        # output_path stays None -- downstream picks its own contained name.
        assert agent.print_page_as_pdf.await_args.kwargs["output_path"] is None

    @pytest.mark.asyncio
    async def test_relative_name_writes_inside_screenshot_dir_end_to_end(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: the rewritten relative path reaches the real
        BrowserActions.print_page_as_pdf and Chromium's page.pdf() write
        target lands INSIDE screenshot_dir (the absolute bypass is never
        triggered)."""
        from web_agent.browser_actions import BrowserActions
        from web_agent.mcp_server import web_print_page_as_pdf

        cfg = AppConfig(base_dir=str(tmp_path))
        shot_dir = Path(cfg.automation.screenshot_dir)

        # Real BrowserActions with a faked session page.
        page = MagicMock()
        page.pdf = AsyncMock()
        type(page).url = property(lambda _self: "https://example.com/")
        fake_tab_mgr = MagicMock()
        fake_tab_mgr.get_or_current = MagicMock(return_value=page)
        fake_sessions = MagicMock()
        fake_sessions.get_tab_manager = MagicMock(return_value=fake_tab_mgr)
        fake_sessions.touch = MagicMock()
        ba = BrowserActions(MagicMock(), cfg, sessions=fake_sessions)

        agent = MagicMock()
        agent._config = cfg
        agent.print_page_as_pdf = ba.print_page_as_pdf  # real impl
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"agent": agent}

        out = await web_print_page_as_pdf(ctx, output_path="sub/report.pdf", session_id="sid")
        assert out["status"] == "success"
        page.pdf.assert_awaited_once()
        written = Path(page.pdf.await_args.kwargs["path"])
        assert shot_dir.resolve() in written.parents
        assert written.name == "report.pdf"

    @pytest.mark.asyncio
    async def test_absolute_output_path_never_reaches_real_write(self, tmp_path: Path) -> None:
        """The pre-fix arbitrary-write vector: an absolute output_path must
        NOT cause the real page.pdf() to be invoked with that absolute
        target."""
        from web_agent.browser_actions import BrowserActions
        from web_agent.mcp_server import web_print_page_as_pdf

        cfg = AppConfig(base_dir=str(tmp_path))
        page = MagicMock()
        page.pdf = AsyncMock()
        type(page).url = property(lambda _self: "https://example.com/")
        fake_tab_mgr = MagicMock()
        fake_tab_mgr.get_or_current = MagicMock(return_value=page)
        fake_sessions = MagicMock()
        fake_sessions.get_tab_manager = MagicMock(return_value=fake_tab_mgr)
        fake_sessions.touch = MagicMock()
        ba = BrowserActions(MagicMock(), cfg, sessions=fake_sessions)

        agent = MagicMock()
        agent._config = cfg
        agent.print_page_as_pdf = ba.print_page_as_pdf
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"agent": agent}

        evil = str(tmp_path / "outside" / "evil.pdf")
        out = await web_print_page_as_pdf(ctx, output_path=evil, session_id="sid")
        assert out["status"] == "failed"
        page.pdf.assert_not_called()


# ---------------------------------------------------------------------------
# GH-1: github_release_download query sanitizer actually strips site: + OR
# ---------------------------------------------------------------------------
class TestGH1QuerySanitizer:
    def _sanitize(self):
        from web_agent.builtin_skills.github_release_download import _sanitize_query_term

        return _sanitize_query_term

    @pytest.mark.parametrize(
        "payload",
        [
            "winx64 site:evil.com",
            "x OR site:evil.com",
            '" OR site:evil.com"',
            "SITE:evil.com",  # case-insensitive
            "site : evil.com",  # whitespace around colon
            "(a | b) site:evil.com",
        ],
    )
    def test_site_operator_stripped(self, payload: str) -> None:
        out = self._sanitize()(payload)
        # The ``site:`` operator token must be gone (no re-scoping left).
        assert "site:" not in out.lower()
        assert "site :" not in out.lower()

    @pytest.mark.parametrize(
        "payload",
        ["x OR y", "a AND b", "foo NOT bar", "OR OR OR"],
    )
    def test_boolean_operators_stripped(self, payload: str) -> None:
        out = self._sanitize()(payload)
        tokens = out.split()
        assert "OR" not in tokens
        assert "AND" not in tokens
        assert "NOT" not in tokens

    def test_field_operators_other_than_site_stripped(self) -> None:
        out = self._sanitize()("inurl:evil filetype:exe intitle:secret")
        low = out.lower()
        assert "inurl:" not in low
        assert "filetype:" not in low
        assert "intitle:" not in low

    @pytest.mark.parametrize(
        "ok",
        [
            "owner/name v1.2.3",
            "normal-string_v1.0",
            "orchestra transformer",  # contains 'or'/'and' as substrings
            "windows x64 release",
            "scaffolding",  # contains 'fold' / 'and'? no -- guards substrings
        ],
    )
    def test_legitimate_text_preserved(self, ok: str) -> None:
        # Ordinary words (incl. ones that merely contain 'or'/'and') survive,
        # modulo whitespace collapsing.
        out = self._sanitize()(ok)
        assert out == " ".join(ok.split())

    def test_scope_escape_payload_collapses_to_bare_text(self) -> None:
        # The end-to-end intent: a prompt-injected scope escape leaves only
        # harmless plain text -- no operator the engine would honour.
        out = self._sanitize()('" OR site:evil.com"')
        assert out == "evil.com"

    def test_empty_string(self) -> None:
        assert self._sanitize()("") == ""


# ---------------------------------------------------------------------------
# WS-1: markdown_skills_only gate validates the NORMALIZED path
# ---------------------------------------------------------------------------
class TestWS1MarkdownSkillsOnlyNormalizedGate:
    def _ws(self, tmp_path: Path, mode: str = "markdown_skills_only"):
        from web_agent.config import WorkspaceConfig
        from web_agent.workspace import Workspace

        cfg = AppConfig(
            base_dir=str(tmp_path),
            workspace=WorkspaceConfig(enabled=True, mode=mode),
        )
        return Workspace(cfg)

    @pytest.mark.parametrize(
        "escape_path",
        [
            "domain-skills/../notes/x.md",
            "domain-skills/../evil.md",
            "domain-skills/sub/../../notes/y.md",
        ],
    )
    def test_dotdot_escape_blocked(self, tmp_path: Path, escape_path: str) -> None:
        from web_agent.workspace import WorkspaceError

        ws = self._ws(tmp_path)
        with pytest.raises(WorkspaceError):
            ws.write_file(escape_path, "x")

    def test_write_skill_dotdot_escape_blocked(self, tmp_path: Path) -> None:
        # write_skill('../notes/x') -> write_file('domain-skills/../notes/x.md')
        from web_agent.workspace import WorkspaceError

        ws = self._ws(tmp_path)
        with pytest.raises(WorkspaceError):
            ws.write_skill("../notes/x", "x")

    def test_legitimate_skill_path_allowed(self, tmp_path: Path) -> None:
        ws = self._ws(tmp_path)
        p = ws.write_skill("sec.gov/filing_search", "# skill")
        assert p.is_file()
        # Landed under domain-skills/ as documented.
        from web_agent.workspace import SKILLS_DIR

        rel = p.relative_to(ws.root())
        assert rel.parts[0] == SKILLS_DIR
        assert rel.suffix == ".md"

    def test_plain_md_under_skills_dir_allowed(self, tmp_path: Path) -> None:
        ws = self._ws(tmp_path)
        p = ws.write_file("domain-skills/note.md", "# x")
        assert p.is_file()

    def test_non_md_still_blocked(self, tmp_path: Path) -> None:
        from web_agent.workspace import WorkspaceError

        ws = self._ws(tmp_path)
        with pytest.raises(WorkspaceError):
            ws.write_file("domain-skills/x.py", "x")

    def test_md_outside_skills_dir_still_blocked(self, tmp_path: Path) -> None:
        from web_agent.workspace import WorkspaceError

        ws = self._ws(tmp_path)
        with pytest.raises(WorkspaceError):
            ws.write_file("notes/x.md", "x")

    def test_reviewed_helpers_normalized_dotdot_helpers_py_blocked(self, tmp_path: Path) -> None:
        # The reviewed_python_helpers gate must also see the normalized path:
        # 'domain-skills/../helpers.py' resolves to root helpers.py, but a
        # normalized 'sub/../helpers.py' equally must not smuggle a non-root
        # .py past the gate. Confirm a normalized non-root .py is blocked.
        from web_agent.workspace import WorkspaceError

        ws = self._ws(tmp_path, mode="reviewed_python_helpers")
        with pytest.raises(WorkspaceError):
            ws.write_file("notes/../sub/helpers.py", "x")

    def test_reviewed_helpers_root_helpers_py_allowed(self, tmp_path: Path) -> None:
        ws = self._ws(tmp_path, mode="reviewed_python_helpers")
        p = ws.write_file("helpers.py", "x = 1")
        assert p.is_file() and p.name == "helpers.py"


# ---------------------------------------------------------------------------
# EC-1: EC host confinement matches the parsed hostname at label boundaries
# ---------------------------------------------------------------------------
class TestEC1HostnameConfinement:
    def _filter(self):
        """Return a predicate mirroring the skill's in-loop host gate."""
        from web_agent.builtin_skills.ec_europa_document_search import _EC_HOSTS
        from web_agent.utils import _matches_domain, _normalize_host

        def accepted(url: str) -> bool:
            host = _normalize_host(url)
            return bool(host) and any(_matches_domain(host, d) for d in _EC_HOSTS)

        return accepted

    @pytest.mark.parametrize(
        "url",
        [
            "https://ec.europa.eu.evil.com/x",  # suffix-spoof
            "https://evil.com/?x=ec.europa.eu",  # query-string substring
            "https://attacker.com/ec.europa.eu",  # path substring
            "https://notec.europa.eu.evil/",  # label-boundary spoof
            "https://evil.com/#ec.europa.eu",  # fragment substring
            "not a url",
        ],
    )
    def test_spoofed_hosts_rejected(self, url: str) -> None:
        assert self._filter()(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "https://ec.europa.eu/info/doc",
            "https://finance.ec.europa.eu/policy",  # subdomain of ec.europa.eu
            "https://eur-lex.europa.eu/legal-content/x",
            "https://sub.eur-lex.europa.eu/x",  # subdomain of allowed host
            "http://EC.EUROPA.EU/x",  # case-insensitive
        ],
    )
    def test_real_ec_hosts_and_subdomains_accepted(self, url: str) -> None:
        assert self._filter()(url) is True

    @pytest.mark.asyncio
    async def test_run_drops_spoofed_and_keeps_real(self, monkeypatch) -> None:
        """End-to-end through run(): a spoofed host in the result set is
        dropped; only the genuine EC host survives into the output."""
        from web_agent.builtin_skills import ec_europa_document_search as ec
        from web_agent.models import AgentResult, ExtractionResult, SearchResponse

        real = ExtractionResult(
            url="https://ec.europa.eu/info/real",
            title="Real EC doc",
            content="body",
            extraction_method="trafilatura",
        )
        spoof = ExtractionResult(
            url="https://ec.europa.eu.evil.com/fake",
            title="Spoofed",
            content="evil",
            extraction_method="trafilatura",
        )

        agent = MagicMock()
        agent.search_and_extract = AsyncMock(
            return_value=AgentResult(
                query="q", search=SearchResponse(query="q"), pages=[spoof, real]
            )
        )

        out = await ec.run(agent, "https://ec.europa.eu", {"query": "policy"})
        import json as _json

        docs = _json.loads(out["documents"])
        urls = [d["url"] for d in docs]
        assert "https://ec.europa.eu/info/real" in urls
        assert "https://ec.europa.eu.evil.com/fake" not in urls
        assert out["count"] == "1"


# ===========================================================================
# Recipes egress + concurrency cluster (REC-1, REC-2)
# ===========================================================================
#
# Finding -> test map:
#   REC-1 : fill_form_and_extract repeats the WebFetcher.fetch SSRF re-checks
#           after its raw page.goto -- a post-navigation redirect to a denied
#           host OR a DNS-rebind to a private peer is blocked (returns
#           extraction_method="none"), and never reaches the form-fill /
#           extract steps. The legitimate (public, no-redirect) path still
#           extracts content.
#   REC-2 : web_research bounds its fetch_smart fan-out with the SAME
#           per-session semaphore fetch_many uses
#           (BrowserConfig.max_pages_per_session_fetch), so at most N
#           navigations run concurrently on one session/context -- while all
#           pages still complete and a single failure does not abort the run.


def _form_recipes(
    *,
    page: Any,
    config: AppConfig,
    sessions: Any = None,
) -> Any:
    """Build a Recipes whose BrowserManager.new_page() yields ``page``.

    Mirrors ``tests/test_v1614_pipeline.py::_make_recipes_with_mock_page`` --
    a real ContentExtractor so the happy path traverses the actual
    extraction chain; search/fetcher/downloader are stubbed since
    fill_form_and_extract drives the page directly on the path under test.
    """
    from web_agent.content_extractor import ContentExtractor
    from web_agent.recipes import Recipes

    class _NewPageCM:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return page

        async def __aexit__(self, *a: Any) -> None:
            return None

    bm = MagicMock()
    bm.new_page = MagicMock(side_effect=lambda *a, **k: _NewPageCM())

    return Recipes(
        search=MagicMock(),
        fetcher=MagicMock(),
        extractor=ContentExtractor(config),
        downloader=MagicMock(),
        config=config,
        browser_manager=bm,
        sessions=sessions,
    )


def _form_page(*, final_url: str, response_url: str, peer_ip: str | None = None) -> Any:
    """A fake Playwright Page for fill_form_and_extract's _drive().

    ``goto`` returns a Response whose ``.url`` is ``response_url`` and whose
    ``server_addr()`` reports ``peer_ip`` (mirrors the real C-1(b) rebind
    signal). ``page.url`` is a property (as on real Playwright pages).
    """
    response = MagicMock()
    response.url = response_url
    response.status = 200
    if peer_ip is not None:
        response.server_addr = AsyncMock(return_value={"ipAddress": peer_ip, "port": 443})
    else:
        response.server_addr = AsyncMock(return_value=None)

    page = MagicMock()
    page.goto = AsyncMock(return_value=response)
    page.wait_for_load_state = AsyncMock()
    type(page).url = property(lambda _self: final_url)
    return page


# ---------------------------------------------------------------------------
# REC-1: fill_form_and_extract repeats the post-goto SSRF re-checks
# ---------------------------------------------------------------------------
class TestREC1FillFormSSRFRecheck:
    @pytest.mark.asyncio
    async def test_redirect_to_denied_host_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Entry URL passes the up-front gate, but the navigation lands on a
        denied host (3xx / meta-refresh) -> extraction_method='none', and the
        form-fill / extract steps are never reached."""
        from web_agent import recipes as recipes_module
        from web_agent.models import FormFilterSpec

        entry = "https://good.example/form"
        denied = "http://169.254.169.254/latest/meta-data"

        # Up-front gate (recipes.py:823) allows the entry URL; the post-goto
        # candidate (the redirect target) is denied.
        def _gate(url: str, *a: Any, **k: Any) -> bool:
            return "169.254" not in url

        monkeypatch.setattr(recipes_module, "check_domain_allowed", _gate)

        # safe_page_content / extractor must NEVER run on the blocked path.
        def _boom_capture(*a: Any, **k: Any):
            raise AssertionError("safe_page_content reached despite redirect block")

        monkeypatch.setattr(recipes_module, "safe_page_content", _boom_capture)

        page = _form_page(final_url=denied, response_url=denied, peer_ip="93.184.216.34")
        recipes = _form_recipes(page=page, config=AppConfig(safety=SafetyConfig()))

        result = await recipes.fill_form_and_extract(entry, FormFilterSpec())
        assert result.extraction_method == "none"
        assert result.url == entry

    @pytest.mark.asyncio
    async def test_private_peer_ip_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Host gate passes (public final URL) but the actual connected peer
        rebinds to a private/IMDS address -> blocked via the same
        _response_peer_is_private helper WebFetcher.fetch uses."""
        from web_agent import recipes as recipes_module
        from web_agent.models import FormFilterSpec

        entry = "https://good.example/form"

        # Every URL passes the host gate; the block must come purely from the
        # post-connect peer-IP check (real _response_peer_is_private helper).
        monkeypatch.setattr(recipes_module, "check_domain_allowed", lambda *a, **k: True)

        def _boom_capture(*a: Any, **k: Any):
            raise AssertionError("safe_page_content reached despite private-peer block")

        monkeypatch.setattr(recipes_module, "safe_page_content", _boom_capture)

        # final_url == entry so the redirect loop is a no-op; only the peer IP
        # (169.254.169.254 = AWS IMDS) trips the guard.
        page = _form_page(final_url=entry, response_url=entry, peer_ip="169.254.169.254")
        recipes = _form_recipes(
            page=page, config=AppConfig(safety=SafetyConfig(block_private_ips=True))
        )

        result = await recipes.fill_form_and_extract(entry, FormFilterSpec())
        assert result.extraction_method == "none"
        assert result.url == entry

    @pytest.mark.asyncio
    async def test_block_private_ips_off_skips_peer_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Operator opt-out: with block_private_ips=False the peer-IP guard is
        not consulted (mirrors WebFetcher.fetch), so a private peer on an
        allow-listed, non-redirecting host still extracts."""
        from web_agent import recipes as recipes_module
        from web_agent.models import FormFilterSpec

        entry = "https://good.example/results"
        monkeypatch.setattr(recipes_module, "check_domain_allowed", lambda *a, **k: True)

        real_html = (
            "<html><head><title>Results</title></head><body><main>"
            "<p>Post-submit content with sufficient length so the extraction "
            "chain (trafilatura / bs4 / raw) returns a real method rather than "
            "bailing on min_content_length. Several sentences ensure a "
            "meaningful body is captured for the citation.</p>"
            "<p>Second paragraph adds further body text for the extractor.</p>"
            "</main></body></html>"
        )

        async def _capture(_page: Any, **_k: Any):
            return (real_html, "content")

        monkeypatch.setattr(recipes_module, "safe_page_content", _capture)

        page = _form_page(final_url=entry, response_url=entry, peer_ip="169.254.169.254")
        recipes = _form_recipes(
            page=page, config=AppConfig(safety=SafetyConfig(block_private_ips=False))
        )

        result = await recipes.fill_form_and_extract(entry, FormFilterSpec())
        # Not the SSRF short-circuit: a real extractor method + content.
        assert result.extraction_method != "none"
        assert result.content_length > 0

    @pytest.mark.asyncio
    async def test_public_no_redirect_extracts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression: a public host with no redirect and a public peer IP
        extracts content normally (the REC-1 guard does not over-block)."""
        from web_agent import recipes as recipes_module
        from web_agent.models import FormFilterSpec

        entry = "https://good.example/results"
        monkeypatch.setattr(recipes_module, "check_domain_allowed", lambda *a, **k: True)

        real_html = (
            "<html><head><title>Results</title></head><body><main>"
            "<p>This is the real post-submit content with enough text for the "
            "extraction chain to succeed. We add multiple sentences so "
            "trafilatura / bs4 / raw can pull a meaningful body out of the "
            "markup instead of failing on the minimum content length.</p>"
            "<p>Second paragraph contributes additional body text.</p>"
            "</main></body></html>"
        )

        async def _capture(_page: Any, **_k: Any):
            return (real_html, "content")

        monkeypatch.setattr(recipes_module, "safe_page_content", _capture)

        page = _form_page(final_url=entry, response_url=entry, peer_ip="93.184.216.34")
        recipes = _form_recipes(
            page=page, config=AppConfig(safety=SafetyConfig(block_private_ips=True))
        )

        result = await recipes.fill_form_and_extract(entry, FormFilterSpec())
        assert result.extraction_method != "none"
        assert result.content_length > 0


# ---------------------------------------------------------------------------
# REC-2: web_research bounds the fetch_smart fan-out by the session semaphore
# ---------------------------------------------------------------------------
class TestREC2WebResearchBoundedConcurrency:
    @staticmethod
    def _search_engine(urls: list[str]) -> Any:
        """A fake SearchEngine.search returning the given URLs as results."""
        from web_agent.models import SearchResponse, SearchResultItem

        async def _search(query: str, max_results: int = 10, **_k: Any) -> SearchResponse:
            items = [
                SearchResultItem(position=i + 1, title=f"r{i}", url=u, provider="searxng")
                for i, u in enumerate(urls)
            ]
            return SearchResponse(query=query, total_results=len(items), results=items)

        eng = MagicMock()
        eng.search = _search
        return eng

    @staticmethod
    def _extractor() -> Any:
        """A fake ContentExtractor.extract returning non-empty HTML content."""
        from web_agent.models import ExtractionResult

        def _extract(fr: Any) -> ExtractionResult:
            return ExtractionResult(
                url=fr.url,
                title="t",
                content="body text",
                extraction_method="raw",
                content_length=9,
            )

        ex = MagicMock()
        ex.extract = MagicMock(side_effect=_extract)
        return ex

    def _recipes(self, urls: list[str], bound: int) -> Any:
        from web_agent.config import BrowserConfig
        from web_agent.recipes import Recipes

        cfg = AppConfig(browser=BrowserConfig(max_pages_per_session_fetch=bound))
        return Recipes(
            search=self._search_engine(urls),
            fetcher=MagicMock(),
            extractor=self._extractor(),
            downloader=MagicMock(),
            config=cfg,
            browser_manager=MagicMock(),
            sessions=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_session_path_never_exceeds_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An instrumented fetch_smart tracks live concurrency; with a
        session_id the peak must never exceed max_pages_per_session_fetch,
        and every target page must still complete."""
        import asyncio

        from web_agent import recipes as recipes_module
        from web_agent.models import FetchResult, FetchStatus

        bound = 3
        n_pages = 12
        urls = [f"https://host{i}.example/page" for i in range(n_pages)]

        # All URLs pass the host gate (so all reach the fan-out, none filtered).
        monkeypatch.setattr(recipes_module, "check_domain_allowed", lambda *a, **k: True)

        live = 0
        peak = 0
        completed: list[str] = []
        lock = asyncio.Lock()

        async def _fake_fetch_smart(url: str, *, session_id: Any = None, **_k: Any) -> FetchResult:
            nonlocal live, peak
            async with lock:
                live += 1
                peak = max(peak, live)
            try:
                # Yield enough times for other gated tasks to pile up if the
                # semaphore were missing.
                for _ in range(5):
                    await asyncio.sleep(0)
                await asyncio.sleep(0.01)
                return FetchResult(
                    url=url,
                    final_url=url,
                    status=FetchStatus.SUCCESS,
                    html="<html><body>ok</body></html>",
                )
            finally:
                async with lock:
                    live -= 1
                    completed.append(url)

        recipes = self._recipes(urls, bound=bound)
        recipes._fetcher.fetch_smart = _fake_fetch_smart

        result = await recipes.web_research("q", max_pages=n_pages, session_id="sid")

        assert peak <= bound, f"fan-out concurrency {peak} exceeded bound {bound}"
        assert peak > 1, "test did not actually exercise concurrency"
        # Every page completed and produced a citation (none dropped).
        assert sorted(completed) == sorted(urls)
        assert len(result.citations) == n_pages
        assert result.pages_visited == n_pages

    @pytest.mark.asyncio
    async def test_single_failure_does_not_abort_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """return_exceptions=True is preserved: one fetch_smart raising must
        not abort the whole research run -- the other pages still produce
        citations and the failure is surfaced as a diagnostic."""
        import asyncio

        from web_agent import recipes as recipes_module
        from web_agent.models import FetchResult, FetchStatus

        urls = [f"https://host{i}.example/page" for i in range(4)]
        monkeypatch.setattr(recipes_module, "check_domain_allowed", lambda *a, **k: True)

        async def _fake_fetch_smart(url: str, *, session_id: Any = None, **_k: Any) -> FetchResult:
            await asyncio.sleep(0)
            if url.endswith("host1.example/page"):
                raise RuntimeError("transient boom")
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.SUCCESS,
                html="<html><body>ok</body></html>",
            )

        recipes = self._recipes(urls, bound=2)
        recipes._fetcher.fetch_smart = _fake_fetch_smart

        result = await recipes.web_research("q", max_pages=4, session_id="sid")

        # 3 of 4 succeeded; the run was NOT aborted by the single failure.
        assert len(result.citations) == 3
        urls_seen = {c.url for c in result.citations}
        assert "https://host1.example/page" not in urls_seen
        # The failure is surfaced (a diagnostic / warning, not a crash).
        assert any("host1.example" in w for w in result.warnings)

    @pytest.mark.asyncio
    async def test_ephemeral_path_unbounded_gather_still_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No session_id -> the ephemeral path (bounded by max_contexts in
        BrowserManager) is left as-is; all pages still complete. Confirms the
        REC-2 gate only wraps the session path, matching fetch_many."""
        import asyncio

        from web_agent import recipes as recipes_module
        from web_agent.models import FetchResult, FetchStatus

        urls = [f"https://host{i}.example/page" for i in range(6)]
        monkeypatch.setattr(recipes_module, "check_domain_allowed", lambda *a, **k: True)

        seen_session_ids: list[Any] = []

        async def _fake_fetch_smart(url: str, *, session_id: Any = None, **_k: Any) -> FetchResult:
            seen_session_ids.append(session_id)
            await asyncio.sleep(0)
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.SUCCESS,
                html="<html><body>ok</body></html>",
            )

        recipes = self._recipes(urls, bound=2)
        recipes._fetcher.fetch_smart = _fake_fetch_smart

        result = await recipes.web_research("q", max_pages=6, session_id=None)

        assert len(result.citations) == 6
        assert all(sid is None for sid in seen_session_ids)


# ===========================================================================
# Redaction / replay-fidelity / audit cluster (AG-1, AG-2, AG-3/TRACE-2,
# TRACE-1, TRACE-3)
# ===========================================================================
#
# Finding -> test map:
#   AG-1     : a CancelledError raised during the search_and_extract HEAD
#              probe gather PROPAGATES (is re-raised), not swallowed as
#              "default to HTML". A generic Exception still defaults to HTML.
#   AG-2     : apply_domain_skill redacts sensitive skill `inputs`
#              (password/token/...) in the audit record; non-sensitive keys
#              and the value handed to the skill runner are untouched.
#   AG-3 +   : replay_trace detects the ***REDACTED*** sentinel: with no
#   TRACE-2    override it SKIPS the action (+ logger.warning) instead of
#              typing the sentinel; a supplied ``secrets`` mapping re-injects
#              the real value. End-to-end record->replay confirmed.
#   TRACE-1  : an ``action.evaluate`` expression is redacted in the trace
#              (was previously written verbatim).
#   TRACE-3  : the per-session ``_counters`` map is FIFO-bounded.


def _agent(tmp_path: Path, **cfg_overrides: Any) -> Any:
    """Construct a real Agent (no browser launched) for unit tests.

    Mirrors ``tests/test_v168_trace_replay.py`` -- the Agent is built but
    ``__aenter__`` is never called, so no Playwright process starts. Callers
    stub the specific collaborator they exercise (``_actions``,
    ``_fetcher``, ``_search``, ``_skills``).
    """
    from web_agent import Agent

    cfg = AppConfig(base_dir=str(tmp_path), **cfg_overrides)
    return Agent(cfg)


# ---------------------------------------------------------------------------
# AG-1: search_and_extract HEAD-probe gather re-raises CancelledError
# ---------------------------------------------------------------------------
class TestAG1ProbeCancellationPropagates:
    @staticmethod
    def _search_engine_one(url: str) -> Any:
        """Fake SearchEngine.search returning a single extensionless URL.

        An extensionless URL classifies as 'unknown' -> lands in
        ``unknown_items`` -> enters the probe_binary_urls gather branch.
        """
        from web_agent.models import SearchResponse, SearchResultItem

        async def _search(query: str, max_results: Any = None, **_k: Any) -> SearchResponse:
            item = SearchResultItem(position=1, title="r", url=url, provider="searxng")
            return SearchResponse(query=query, total_results=1, results=[item])

        eng = MagicMock()
        eng.search = _search
        return eng

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A child probe task cancelled mid-gather must propagate out of
        search_and_extract -- not be absorbed as 'probe failed -> HTML'."""
        import asyncio

        from web_agent import agent as agent_module

        url = "https://good.example/doc"  # extensionless -> unknown -> probe
        agent = _agent(tmp_path)
        agent._search = self._search_engine_one(url)
        # All hosts pass the gate so the URL reaches the probe branch.
        monkeypatch.setattr(agent_module, "check_domain_allowed", lambda *a, **k: True)

        async def _cancelled(*a: Any, **k: Any) -> Any:
            raise asyncio.CancelledError()

        agent._fetcher.classify_url = _cancelled
        # Sentinel: if cancellation were swallowed, the pipeline would
        # proceed to fetch_many. It must NOT be reached.
        agent._fetcher.fetch_many = AsyncMock(
            side_effect=AssertionError("pipeline continued after cancellation")
        )

        with pytest.raises(asyncio.CancelledError):
            await agent.search_and_extract("q")
        agent._fetcher.fetch_many.assert_not_called()

    @pytest.mark.asyncio
    async def test_generic_probe_exception_still_defaults_to_html(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: an ordinary (non-Cancelled) probe failure is still
        swallowed and the URL defaults to the HTML path (reaches fetch_many),
        exactly as before -- the AG-1 fix only re-raises CancelledError."""
        from web_agent import agent as agent_module
        from web_agent.models import AgentResult

        url = "https://good.example/doc"
        agent = _agent(tmp_path)
        agent._search = self._search_engine_one(url)
        monkeypatch.setattr(agent_module, "check_domain_allowed", lambda *a, **k: True)

        async def _boom(*a: Any, **k: Any) -> Any:
            raise RuntimeError("transient probe failure")

        agent._fetcher.classify_url = _boom
        # The swallowed-then-HTML path lands the item in fetch_many; stub it
        # so we can assert it WAS reached (no exception escapes).
        reached = {"n": 0}

        async def _fetch_many(items: Any, **_k: Any) -> list[Any]:
            reached["n"] = len(list(items))
            return []

        agent._fetcher.fetch_many = _fetch_many

        result = await agent.search_and_extract("q")
        assert isinstance(result, AgentResult)
        assert reached["n"] == 1, "generic probe failure should default the URL to HTML/fetch_many"


# ---------------------------------------------------------------------------
# AG-2: apply_domain_skill redacts sensitive skill inputs in the audit log
# ---------------------------------------------------------------------------
class TestAG2SkillInputsRedactedInAudit:
    @pytest.mark.asyncio
    async def test_sensitive_inputs_redacted_in_audit_record(self, tmp_path: Path) -> None:
        """A skill input named password/token/api_key is written to the audit
        log as the redaction sentinel; benign keys survive; the UN-redacted
        dict still reaches the skill runner."""
        import json as _json

        from web_agent.config import AuditConfig
        from web_agent.models import SkillApplicationResult
        from web_agent.trace_recorder import _REDACTED

        agent = _agent(tmp_path, audit=AuditConfig(enabled=True))
        assert agent._audit.enabled is True

        # Stub the runner so no real skill is required; capture what it sees.
        seen: dict[str, Any] = {}

        async def _apply(_self: Any, url: str, name: str, inputs: dict[str, Any]) -> Any:
            seen["inputs"] = inputs
            return SkillApplicationResult(
                skill_name=name, domain="sec.gov", url=url, succeeded=True
            )

        agent._skills.apply = _apply  # type: ignore[assignment]

        raw_inputs = {
            "username": "alice",
            "password": "hunter2",
            "api_token": "tok_abc",
            "AUTHORIZATION": "Bearer xyz",
            "page": 3,
        }
        await agent.apply_domain_skill("https://sec.gov", "filing_search", raw_inputs)

        # The runner received the REAL, un-redacted values.
        assert seen["inputs"] == raw_inputs
        assert seen["inputs"]["password"] == "hunter2"

        # The audit record redacted exactly the sensitive values.
        line = agent._audit.path.read_text(encoding="utf-8").strip().splitlines()[0]
        entry = _json.loads(line)
        assert entry["method"] == "apply_domain_skill"
        logged = entry["args"]["inputs"]
        assert logged["username"] == "alice"  # benign preserved
        assert logged["page"] == 3
        assert logged["password"] == _REDACTED
        assert logged["api_token"] == _REDACTED
        assert logged["AUTHORIZATION"] == _REDACTED  # case-insensitive key match
        # The plaintext secret must not appear anywhere in the line.
        assert "hunter2" not in line
        assert "tok_abc" not in line

    @pytest.mark.asyncio
    async def test_none_inputs_logged_as_empty_dict(self, tmp_path: Path) -> None:
        import json as _json

        from web_agent.config import AuditConfig
        from web_agent.models import SkillApplicationResult

        agent = _agent(tmp_path, audit=AuditConfig(enabled=True))

        async def _apply(_self: Any, url: str, name: str, inputs: dict[str, Any]) -> Any:
            return SkillApplicationResult(
                skill_name=name, domain="sec.gov", url=url, succeeded=True
            )

        agent._skills.apply = _apply  # type: ignore[assignment]

        await agent.apply_domain_skill("https://sec.gov", "filing_search", None)
        entry = _json.loads(agent._audit.path.read_text(encoding="utf-8").strip().splitlines()[0])
        # None -> {} (shape preserved, no crash).
        assert entry["args"]["inputs"] == {}


# ---------------------------------------------------------------------------
# AG-3 + TRACE-2: replay_trace handles the redaction sentinel
# ---------------------------------------------------------------------------
def _replay_agent(tmp_path: Path) -> tuple[Any, Path]:
    """Agent + its trace dir, with execute_sequence stubbed to echo actions.

    ``execute_sequence`` is replaced with an AsyncMock so we can inspect the
    exact Action list handed to it without launching a browser.
    """
    from web_agent.config import DiagnosticsConfig

    agent = _agent(
        tmp_path,
        diagnostics=DiagnosticsConfig(trace_enabled=True, trace_dir=str(tmp_path / "traces")),
    )
    agent._actions.execute_sequence = AsyncMock(name="execute_sequence")
    trace_dir = Path(agent._config.diagnostics.trace_dir)
    trace_dir.mkdir(parents=True, exist_ok=True)
    return agent, trace_dir


def _write_trace(trace_dir: Path, sid: str, entries: list[dict[str, Any]]) -> Path:
    import json as _json

    f = trace_dir / f"{sid}.jsonl"
    f.write_text("\n".join(_json.dumps(e) for e in entries), encoding="utf-8")
    return f


class TestAG3ReplayRedactedValue:
    @staticmethod
    def _login_trace_entries() -> list[dict[str, Any]]:
        from web_agent.trace_recorder import _REDACTED

        # A realistic recorded login: click, fill(redacted password), click.
        return [
            {
                "method": "action.fill",
                "args": {"selector": "#user", "value": "alice"},
                "url": "https://example.com/login",
                "status": "success",
                "elapsed_ms": 1,
            },
            {
                "method": "action.fill",
                "args": {"selector": "#pass", "value": _REDACTED},
                "status": "success",
                "elapsed_ms": 1,
            },
            {
                "method": "action.click",
                "args": {"selector": "#submit"},
                "status": "success",
                "elapsed_ms": 1,
            },
        ]

    @pytest.mark.asyncio
    async def test_redacted_value_skipped_not_typed(self, tmp_path: Path) -> None:
        """With no override, the redacted fill is SKIPPED -- the sentinel is
        never handed to execute_sequence."""
        agent, trace_dir = _replay_agent(tmp_path)
        f = _write_trace(trace_dir, "login", self._login_trace_entries())

        await agent.replay_trace(f)

        actions = agent._actions.execute_sequence.await_args.args[1]
        # 3 recorded -> 2 replayed (the redacted password fill dropped).
        kinds = [(a.action, getattr(a, "value", None)) for a in actions]
        assert ("fill", "alice") in kinds  # benign fill kept
        assert ("click", None) in kinds
        # No action carries the sentinel value.
        from web_agent.trace_recorder import _REDACTED

        assert all(getattr(a, "value", None) != _REDACTED for a in actions)
        assert len(actions) == 2

    @pytest.mark.asyncio
    async def test_warning_emitted_for_skipped_redacted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A logger.warning is emitted naming the skipped redacted action."""
        from web_agent import agent as agent_module

        agent, trace_dir = _replay_agent(tmp_path)
        f = _write_trace(trace_dir, "login", self._login_trace_entries())

        warnings: list[str] = []
        monkeypatch.setattr(
            agent_module.logger,
            "warning",
            lambda msg, *a, **k: warnings.append(msg),
        )
        await agent.replay_trace(f)
        assert any("skipping redacted" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_secrets_override_reinjects_real_value(self, tmp_path: Path) -> None:
        """Supplying ``secrets={index: value}`` re-injects the real value so
        the action replays faithfully (the sentinel is replaced)."""
        agent, trace_dir = _replay_agent(tmp_path)
        f = _write_trace(trace_dir, "login", self._login_trace_entries())

        # The redacted fill is at index 1 in the replayable action list.
        await agent.replay_trace(f, secrets={1: "hunter2"})

        actions = agent._actions.execute_sequence.await_args.args[1]
        assert len(actions) == 3  # nothing skipped
        pass_fill = next(a for a in actions if getattr(a, "selector", None) == "#pass")
        assert pass_fill.value == "hunter2"

    @pytest.mark.asyncio
    async def test_all_actions_redacted_no_override_raises(self, tmp_path: Path) -> None:
        """If every replayable action is redacted and none is overridden, a
        clear ValueError is raised rather than calling execute_sequence with
        an empty list."""
        from web_agent.trace_recorder import _REDACTED

        agent, trace_dir = _replay_agent(tmp_path)
        entries = [
            {
                "method": "action.fill",
                "args": {"selector": "#pass", "value": _REDACTED},
                "url": "https://example.com/login",
                "status": "success",
                "elapsed_ms": 1,
            }
        ]
        f = _write_trace(trace_dir, "allsecret", entries)
        with pytest.raises(ValueError, match="all were redacted"):
            await agent.replay_trace(f)
        agent._actions.execute_sequence.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_redacted_trace_unaffected(self, tmp_path: Path) -> None:
        """Regression: a trace with no redacted values replays every action
        (back-compat with the existing replay behaviour)."""
        agent, trace_dir = _replay_agent(tmp_path)
        entries = [
            {
                "method": "action.fill",
                "args": {"selector": "#q", "value": "search term"},
                "url": "https://example.com/",
                "status": "success",
                "elapsed_ms": 1,
            },
            {
                "method": "action.click",
                "args": {"selector": "#go"},
                "status": "success",
                "elapsed_ms": 1,
            },
        ]
        f = _write_trace(trace_dir, "plain", entries)
        await agent.replay_trace(f)
        actions = agent._actions.execute_sequence.await_args.args[1]
        assert len(actions) == 2
        assert actions[0].value == "search term"

    @pytest.mark.asyncio
    async def test_record_then_replay_end_to_end(self, tmp_path: Path) -> None:
        """Full loop: a real fill secret recorded via SessionTraceRecorder is
        persisted as the sentinel, and replay_trace skips it (does NOT type
        the sentinel) -- the exact AG-3/TRACE-2 footgun, now closed."""
        from web_agent.config import DiagnosticsConfig
        from web_agent.trace_recorder import _REDACTED, SessionTraceRecorder

        diag = DiagnosticsConfig(trace_enabled=True, trace_dir=str(tmp_path / "traces"))
        rec = SessionTraceRecorder(diag, base_dir=str(tmp_path))
        # Record a fill carrying a real password + a benign click.
        await rec.record(
            session_id="s1",
            method="action.fill",
            args={"selector": "#pass", "value": "s3cr3t-pw"},
            status="success",
            elapsed_ms=1,
            url="https://example.com/login",
        )
        await rec.record(
            session_id="s1",
            method="action.click",
            args={"selector": "#submit"},
            status="success",
            elapsed_ms=1,
        )
        f = rec.path_for("s1")
        # The persisted secret is the sentinel, never the plaintext.
        raw = f.read_text(encoding="utf-8")
        assert "s3cr3t-pw" not in raw
        assert _REDACTED in raw

        agent, _ = _replay_agent(tmp_path)
        # Point the agent's recorder at the same dir (already shared via cfg).
        await agent.replay_trace(f)
        actions = agent._actions.execute_sequence.await_args.args[1]
        # Redacted fill skipped; only the benign click replays.
        assert [a.action for a in actions] == ["click"]


# ---------------------------------------------------------------------------
# TRACE-1: action.evaluate expression is redacted in the trace
# ---------------------------------------------------------------------------
def _trace_recorder(tmp_path: Path) -> Any:
    from web_agent.config import DiagnosticsConfig
    from web_agent.trace_recorder import SessionTraceRecorder

    diag = DiagnosticsConfig(trace_enabled=True, trace_dir=str(tmp_path / "traces"))
    return SessionTraceRecorder(diag, base_dir=str(tmp_path))


class TestTRACE1EvaluateRedacted:
    @pytest.mark.asyncio
    async def test_evaluate_expression_redacted(self, tmp_path: Path) -> None:
        import json as _json

        from web_agent.trace_recorder import _REDACTED

        rec = _trace_recorder(tmp_path)
        secret_js = "localStorage.setItem('access_token', 'eyJhbGciOi.SECRET')"
        await rec.record(
            session_id="s1",
            method="action.evaluate",
            args={"expression": secret_js},
            status="success",
            elapsed_ms=1,
        )
        line = rec.path_for("s1").read_text(encoding="utf-8").strip()
        entry = _json.loads(line)
        assert entry["args"]["expression"] == _REDACTED
        # The token must not have been written verbatim.
        assert "access_token" not in line
        assert "SECRET" not in line

    @pytest.mark.asyncio
    async def test_redaction_helper_includes_evaluate(self) -> None:
        # Unit-level: the map gained action.evaluate (and kept fill/type).
        from web_agent.trace_recorder import _redact_args

        out = _redact_args("action.evaluate", {"expression": "fetch('/x')"})
        assert out["expression"] == "***REDACTED***"
        # fill/type unchanged behaviour.
        assert _redact_args("action.fill", {"value": "pw"})["value"] == "***REDACTED***"
        # A non-secret action is returned unchanged (same object).
        clk = {"selector": "#x"}
        assert _redact_args("action.click", clk) is clk

    @pytest.mark.asyncio
    async def test_record_does_not_mutate_caller_args(self, tmp_path: Path) -> None:
        """The live action dict the caller passes must NOT be mutated by the
        redaction (only the serialized copy is redacted)."""
        rec = _trace_recorder(tmp_path)
        live = {"expression": "real_js_with_token('abc')"}
        await rec.record(
            session_id="s1", method="action.evaluate", args=live, status="success", elapsed_ms=1
        )
        assert live["expression"] == "real_js_with_token('abc')"


# ---------------------------------------------------------------------------
# TRACE-3: per-session _counters map is FIFO-bounded
# ---------------------------------------------------------------------------
class TestTRACE3CountersBounded:
    @pytest.mark.asyncio
    async def test_counters_evict_past_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from web_agent import trace_recorder as tr_module

        monkeypatch.setattr(tr_module, "_COUNTERS_MAXSIZE", 3)
        rec = _trace_recorder(tmp_path)
        for i in range(10):
            await rec.record(
                session_id=f"sid{i}",
                method="action.click",
                args={},
                status="success",
                elapsed_ms=1,
            )
        # Never exceeds the cap; the oldest sessions were evicted FIFO.
        assert len(rec._counters) <= 3
        assert "sid9" in rec._counters  # most recent kept
        assert "sid0" not in rec._counters  # oldest evicted

    @pytest.mark.asyncio
    async def test_repeated_same_session_does_not_grow_map(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-recording an existing session increments its ordinal without
        adding map entries or triggering eviction of itself."""
        from web_agent import trace_recorder as tr_module

        monkeypatch.setattr(tr_module, "_COUNTERS_MAXSIZE", 3)
        rec = _trace_recorder(tmp_path)
        for _ in range(20):
            await rec.record(
                session_id="stable",
                method="action.click",
                args={},
                status="success",
                elapsed_ms=1,
            )
        assert len(rec._counters) == 1
        assert rec._counters["stable"] == 20


# ===========================================================================
# Hygiene & leaks cluster (CE-1, CACHE-1/CACHE-2, RL-1, SM-1, BR-2)
# ===========================================================================
#
# Finding -> test map:
#   CE-1   : oversized HTML / binary extraction content is truncated to the
#            ``safety.max_chars_per_call`` cap (the api_json E-6 cap is reused
#            across every branch); content_length stays honest.
#   CACHE-1: DiskCache get/set still round-trips correctly after the blocking
#            FS I/O was moved onto ``asyncio.to_thread`` (semantics unchanged).
#   CACHE-2: a set() produces a readable file and leaves no leftover *.tmp.
#   RL-1   : the per-host state dicts (_locks/_next_allowed/_429_counts) are
#            FIFO-bounded across many hosts; a held lock is never evicted.
#   SM-1   : if TabManager construction raises, the freshly-built context's
#            close() is called before the error propagates (no orphan).
#   BR-2   : a second concurrent sequence on the same session tab is refused
#            and does NOT clobber the first sequence's dialog state.


# ---------------------------------------------------------------------------
# CE-1: every extractor branch is capped, not just api_json
# ---------------------------------------------------------------------------
class TestCE1ExtractorContentCapped:
    def _extractor(self, cap: int):
        from web_agent.content_extractor import ContentExtractor

        cfg = AppConfig(safety=SafetyConfig(max_chars_per_call=cap))
        return ContentExtractor(cfg)

    def test_oversized_html_raw_content_truncated(self) -> None:
        # A huge HTML body that falls through to the raw extractor must be
        # truncated to the cap, with content_length reset to the cut length.
        cap = 1000
        ext = self._extractor(cap)
        huge_text = "A" * (cap * 5)
        fr = FetchResult(
            url="https://e/x",
            final_url="https://e/x",
            status=FetchStatus.SUCCESS,
            html=f"<html><body><p>{huge_text}</p></body></html>",
        )
        result = ext.extract(fr)
        assert result.content is not None
        assert len(result.content) == cap
        assert result.content_length == cap

    def test_oversized_binary_csv_content_truncated(self) -> None:
        # The CSV binary branch (no optional dep) must also be capped.
        cap = 500
        ext = self._extractor(cap)
        # Build a CSV blob whose extracted text far exceeds the cap.
        rows = "\n".join(f"col_{i},value_{i}" for i in range(cap))
        fr = FetchResult(
            url="https://e/data.csv",
            final_url="https://e/data.csv",
            status=FetchStatus.SUCCESS,
            binary=rows.encode("utf-8"),
            content_type="text/csv",
        )
        result = ext.extract(fr)
        assert result.extraction_method == "csv"
        assert result.content is not None
        assert len(result.content) == cap
        assert result.content_length == cap

    def test_under_cap_content_untouched(self) -> None:
        # Regression: content below the cap is returned verbatim.
        ext = self._extractor(1_000_000)
        body = "<html><body><article>hello world</article></body></html>"
        fr = FetchResult(
            url="https://e/x",
            final_url="https://e/x",
            status=FetchStatus.SUCCESS,
            html=body,
        )
        result = ext.extract(fr)
        assert result.content is not None
        assert "hello world" in result.content
        assert result.content_length == len(result.content)

    def test_cap_helper_truncates_markdown_too(self) -> None:
        # The markdown field (trafilatura can populate it) is capped as well.
        from web_agent.models import ExtractionResult

        ext = self._extractor(10)
        res = ExtractionResult(
            url="u",
            content="x" * 50,
            markdown="y" * 50,
            extraction_method="trafilatura",
            content_length=50,
        )
        capped = ext._cap_content(res)
        assert capped.content is not None and len(capped.content) == 10
        assert capped.markdown is not None and len(capped.markdown) == 10
        assert capped.content_length == 10


# ---------------------------------------------------------------------------
# CACHE-1 / CACHE-2: round-trip after to_thread offload; atomic, no temp litter
# ---------------------------------------------------------------------------
class TestCache1And2:
    @pytest.mark.asyncio
    async def test_set_get_round_trip(self, tmp_path: Path) -> None:
        from web_agent.cache import DiskCache

        cache = DiskCache(cache_dir=str(tmp_path / "c"), ttl_seconds=1e9)
        payload = {"k": "v", "n": 42, "nested": {"a": [1, 2, 3]}}
        await cache.set("https://example.com/page", payload)
        got = await cache.get("https://example.com/page")
        assert got == payload

    @pytest.mark.asyncio
    async def test_miss_returns_none(self, tmp_path: Path) -> None:
        from web_agent.cache import DiskCache

        cache = DiskCache(cache_dir=str(tmp_path / "c"))
        assert await cache.get("never-written") is None

    @pytest.mark.asyncio
    async def test_expired_entry_evicted_on_get(self, tmp_path: Path) -> None:
        from web_agent.cache import DiskCache

        cache = DiskCache(cache_dir=str(tmp_path / "c"), ttl_seconds=0.0)
        await cache.set("k", {"x": 1})
        # ttl=0 -> any positive age is stale; second read is a miss and the
        # stale file is best-effort deleted.
        assert await cache.get("k") is None

    @pytest.mark.asyncio
    async def test_set_writes_readable_file_no_temp_litter(self, tmp_path: Path) -> None:
        from web_agent.cache import DiskCache

        cdir = tmp_path / "c"
        cache = DiskCache(cache_dir=str(cdir))
        await cache.set("k1", {"a": 1})
        await cache.set("k2", {"b": 2})
        # Exactly the JSON entries exist; no leftover atomic-write temp files.
        json_files = list(cdir.glob("*.json"))
        tmp_files = list(cdir.glob("*.tmp"))
        assert len(json_files) == 2
        assert tmp_files == []
        # And the file is genuinely readable JSON.
        assert await cache.get("k1") == {"a": 1}

    @pytest.mark.asyncio
    async def test_clear_removes_all(self, tmp_path: Path) -> None:
        from web_agent.cache import DiskCache

        cdir = tmp_path / "c"
        cache = DiskCache(cache_dir=str(cdir))
        await cache.set("a", {"1": 1})
        await cache.set("b", {"2": 2})
        removed = await cache.clear()
        assert removed == 2
        assert list(cdir.glob("*.json")) == []

    @pytest.mark.asyncio
    async def test_eviction_keeps_cache_under_cap(self, tmp_path: Path) -> None:
        from web_agent.cache import DiskCache

        # Tiny cap forces eviction; verify it stays bounded and readable.
        cache = DiskCache(cache_dir=str(tmp_path / "c"), max_cache_mb=0)
        # max_cache_mb=0 -> _max_bytes=0 so every write triggers eviction of
        # everything-but-itself; the dir never grows unbounded.
        for i in range(8):
            await cache.set(f"key{i}", {"v": i})
        # At most a handful of files survive (best-effort LRU); never all 8
        # accumulate, and no temp litter remains.
        assert len(list((tmp_path / "c").glob("*.json"))) <= 1
        assert list((tmp_path / "c").glob("*.tmp")) == []

    @pytest.mark.asyncio
    async def test_corrupt_file_treated_as_miss(self, tmp_path: Path) -> None:
        from web_agent.cache import DiskCache, _hash_key

        cdir = tmp_path / "c"
        cdir.mkdir(parents=True)
        cache = DiskCache(cache_dir=str(cdir))
        # Simulate a half-written / corrupt entry: get() must swallow the
        # JSONDecodeError and report a miss (CACHE-2's failure-mode contract).
        (cdir / f"{_hash_key('bad')}.json").write_text("{not json", encoding="utf-8")
        assert await cache.get("bad") is None


# ---------------------------------------------------------------------------
# RL-1: per-host state dicts are FIFO-bounded
# ---------------------------------------------------------------------------
class TestRL1RateLimiterBounded:
    @pytest.mark.asyncio
    async def test_state_dicts_bounded_under_many_hosts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from web_agent import rate_limiter as rl_module
        from web_agent.rate_limiter import RateLimiter

        monkeypatch.setattr(rl_module, "_RATE_LIMITER_MAXSIZE", 4)
        # Large rps so acquire() never actually sleeps in the test.
        rl = RateLimiter(rps_per_host=1e9)
        for i in range(50):
            await rl.acquire(f"host{i}.example")
            rl.notify_429(f"host{i}.example", retry_after_seconds=1.0)

        assert len(rl._locks) <= 4
        assert len(rl._next_allowed) <= 4
        assert len(rl._429_counts) <= 4
        # Most-recent host kept; oldest evicted (FIFO).
        assert "host49.example" in rl._locks
        assert "host0.example" not in rl._locks

    @pytest.mark.asyncio
    async def test_held_lock_not_evicted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from web_agent import rate_limiter as rl_module
        from web_agent.rate_limiter import RateLimiter

        monkeypatch.setattr(rl_module, "_RATE_LIMITER_MAXSIZE", 2)
        rl = RateLimiter(rps_per_host=1e9)
        # Pre-create and HOLD the lock for "keep.example" -- an in-flight
        # acquire. It must never be chosen as an eviction victim.
        keep_lock = rl._locks.setdefault("keep.example", __import__("asyncio").Lock())
        await keep_lock.acquire()
        try:
            for i in range(20):
                await rl.acquire(f"other{i}.example")
            assert "keep.example" in rl._locks
        finally:
            keep_lock.release()

    @pytest.mark.asyncio
    async def test_repeated_same_host_does_not_grow(self) -> None:
        from web_agent.rate_limiter import RateLimiter

        rl = RateLimiter(rps_per_host=1e9)
        for _ in range(100):
            await rl.acquire("stable.example")
        assert len(rl._locks) == 1
        assert set(rl._locks) == {"stable.example"}

    @pytest.mark.asyncio
    async def test_disabled_limiter_is_noop(self) -> None:
        from web_agent.rate_limiter import RateLimiter

        rl = RateLimiter(rps_per_host=0)
        await rl.acquire("h.example")
        rl.notify_429("h.example", retry_after_seconds=5.0)
        # Disabled limiter never touches the maps.
        assert rl._locks == {}
        assert rl._next_allowed == {}


# ---------------------------------------------------------------------------
# SM-1: a context orphaned by a post-create failure is closed, not leaked
# ---------------------------------------------------------------------------
class TestSM1ContextClosedOnConstructionFailure:
    @pytest.mark.asyncio
    async def test_tabmanager_failure_closes_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from web_agent import session_manager as sm_module
        from web_agent.session_manager import SessionManager

        # A fake context that records whether close() was awaited.
        ctx = MagicMock()
        ctx.close = AsyncMock()

        bm = MagicMock()
        bm.create_persistent_context = AsyncMock(return_value=ctx)

        sm = SessionManager(bm, AppConfig())

        # Force TabManager construction to raise AFTER the context is built
        # but BEFORE it is registered -- the SM-1 orphan window.
        def _boom(*a: Any, **k: Any):
            raise RuntimeError("tab manager wiring failed")

        monkeypatch.setattr(sm_module, "TabManager", _boom)

        with pytest.raises(RuntimeError, match="tab manager wiring failed"):
            await sm.create()

        # The freshly-built context was closed (no orphan) and never
        # registered in the session dicts.
        ctx.close.assert_awaited_once()
        assert sm._sessions == {}
        assert sm._tabs == {}

    @pytest.mark.asyncio
    async def test_successful_create_does_not_close_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from web_agent import session_manager as sm_module
        from web_agent.session_manager import SessionManager

        ctx = MagicMock()
        ctx.close = AsyncMock()
        ctx.new_page = AsyncMock(return_value=MagicMock())
        bm = MagicMock()
        bm.create_persistent_context = AsyncMock(return_value=ctx)

        # A benign TabManager whose register_initial_page is awaitable.
        tab_mgr = MagicMock()
        tab_mgr.register_initial_page = AsyncMock()
        monkeypatch.setattr(sm_module, "TabManager", lambda *a, **k: tab_mgr)

        sm = SessionManager(bm, AppConfig())
        sid = await sm.create(name="ok")
        # Registered, and the happy path never closes the context.
        assert sid in sm._sessions
        ctx.close.assert_not_called()


# ---------------------------------------------------------------------------
# BR-2: concurrent execute_sequence on one session tab is refused, no clobber
# ---------------------------------------------------------------------------
class TestBR2ConcurrentSequenceGuard:
    def _actions_and_page(self, cfg: AppConfig):
        """Build a real BrowserActions over a faked session-persistent tab.

        ``get_or_current``-style lookups return the SAME page for every
        call, so two concurrent execute_sequence calls share one Page --
        exactly the BR-2 condition.
        """
        from web_agent.browser_actions import BrowserActions

        page = MagicMock()
        page.goto = AsyncMock()
        page.on = MagicMock()
        page.remove_listener = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        type(page).url = property(lambda _self: "https://good.example/app")

        tab_mgr = MagicMock()
        tab_mgr.current = MagicMock(return_value=page)
        sessions = MagicMock()
        sessions.get = MagicMock(return_value=MagicMock())
        sessions.touch = MagicMock()
        sessions.get_tab_manager = MagicMock(return_value=tab_mgr)

        ba = BrowserActions(MagicMock(), cfg, sessions=sessions)
        return ba, page

    @pytest.mark.asyncio
    async def test_second_sequence_refused_when_state_already_registered(self) -> None:
        from web_agent.browser_actions import _PAGE_DIALOG_STATES, _DialogState
        from web_agent.models import WaitInput, WaitTarget

        cfg = AppConfig()
        ba, page = self._actions_and_page(cfg)

        # Simulate a first sequence already in flight: its dialog state owns
        # the single _PAGE_DIALOG_STATES slot for this page.
        first_state = _DialogState()
        _PAGE_DIALOG_STATES[page] = first_state
        try:
            actions = [WaitInput(target=WaitTarget.LOAD_STATE, value="load")]
            res = await ba.execute_sequence("https://good.example/app", actions, session_id="sid")
            # The second sequence is refused (its only action aborted) and the
            # error names the concurrency conflict.
            assert res.actions_failed >= 1
            joined = " ".join((r.error_message or "") for r in res.results)
            assert "concurrent" in joined.lower()
            # Critically: the first sequence's state slot was NOT clobbered,
            # and its listener was NOT removed by the refused sequence.
            assert _PAGE_DIALOG_STATES.get(page) is first_state
            page.remove_listener.assert_not_called()
        finally:
            _PAGE_DIALOG_STATES.pop(page, None)

    @pytest.mark.asyncio
    async def test_truly_concurrent_sequences_one_refused(self) -> None:
        """Two overlapping execute_sequence calls on the same tab: the first
        holds the dialog slot while the second runs and is refused."""
        import asyncio

        from web_agent.browser_actions import _PAGE_DIALOG_STATES
        from web_agent.models import ActionResult, ActionStatus, ActionType, WaitInput, WaitTarget

        cfg = AppConfig()
        ba, _page = self._actions_and_page(cfg)

        gate = asyncio.Event()
        first_inside = asyncio.Event()

        async def _blocking_action(_page: Any, _action: Any) -> ActionResult:
            # First call parks here (keeping its dialog state registered)
            # until the second sequence has had its chance to run.
            first_inside.set()
            await gate.wait()
            return ActionResult(action=ActionType.WAIT, status=ActionStatus.SUCCESS)

        # Patch execute_action so the first sequence blocks deterministically.
        ba.execute_action = AsyncMock(side_effect=_blocking_action)  # type: ignore[method-assign]

        actions = [WaitInput(target=WaitTarget.LOAD_STATE, value="load")]
        try:
            first = asyncio.create_task(
                ba.execute_sequence("https://good.example/app", actions, session_id="sid")
            )
            await first_inside.wait()  # first is parked, slot is held

            # Now the second concurrent sequence runs against the same tab.
            second_res = await ba.execute_sequence(
                "https://good.example/app", actions, session_id="sid"
            )
            joined = " ".join((r.error_message or "") for r in second_res.results)
            assert "concurrent" in joined.lower()

            # Release the first; it completes normally.
            gate.set()
            first_res = await first
            assert first_res.actions_succeeded == 1
        finally:
            gate.set()
            _PAGE_DIALOG_STATES.pop(_page, None)

    @pytest.mark.asyncio
    async def test_sequential_reuse_not_refused_after_completion(self) -> None:
        """Regression (v1.6.16 BR-2): after a sequence COMPLETES on a
        session-persistent tab, its _PAGE_DIALOG_STATES slot must be cleared
        in finally so the NEXT sequential execute_sequence on the same tab is
        not falsely refused. The tab is never closed, so a WeakKeyDictionary
        keyed by the live Page would otherwise keep the slot forever and the
        guard would reject every call after the first."""
        from web_agent.browser_actions import _PAGE_DIALOG_STATES
        from web_agent.models import ActionResult, ActionStatus, ActionType, WaitInput, WaitTarget

        cfg = AppConfig()
        ba, page = self._actions_and_page(cfg)

        async def _ok_action(_page: Any, _action: Any) -> ActionResult:
            return ActionResult(action=ActionType.WAIT, status=ActionStatus.SUCCESS)

        ba.execute_action = AsyncMock(side_effect=_ok_action)  # type: ignore[method-assign]
        actions = [WaitInput(target=WaitTarget.LOAD_STATE, value="load")]
        try:
            r1 = await ba.execute_sequence("https://good.example/app", actions, session_id="sid")
            assert r1.actions_failed == 0
            # finally cleared the slot, so the live persistent tab isn't "stuck".
            assert page not in _PAGE_DIALOG_STATES

            r2 = await ba.execute_sequence("https://good.example/app", actions, session_id="sid")
            assert r2.actions_failed == 0
            joined = " ".join((rr.error_message or "") for rr in r2.results)
            assert "concurrent" not in joined.lower()
        finally:
            _PAGE_DIALOG_STATES.pop(page, None)
