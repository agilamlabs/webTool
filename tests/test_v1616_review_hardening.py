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

import pytest
from web_agent import robots as robots_module
from web_agent import utils
from web_agent.config import AppConfig, DownloadConfig, SafetyConfig
from web_agent.exceptions import DomainNotAllowedError, NavigationError
from web_agent.models import FetchStatus
from web_agent.robots import RobotsChecker
from web_agent.utils import check_domain_allowed, httpx_peer_ip, is_private_address


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
