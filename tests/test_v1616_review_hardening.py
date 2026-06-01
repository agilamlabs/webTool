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
