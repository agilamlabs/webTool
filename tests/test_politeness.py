"""Tests for the politeness layer: rate limiter + robots.txt checker."""

from __future__ import annotations

import asyncio
import time

import pytest
from web_agent.rate_limiter import RateLimiter
from web_agent.robots import RobotsChecker


class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_disabled_when_rps_zero(self) -> None:
        rl = RateLimiter(rps_per_host=0)
        assert not rl.enabled
        # Should be effectively instant -- no sleeping.
        start = time.monotonic()
        for _ in range(10):
            await rl.acquire("example.com")
        assert (time.monotonic() - start) < 0.05

    @pytest.mark.asyncio
    async def test_disabled_for_negative_rps(self) -> None:
        rl = RateLimiter(rps_per_host=-1.0)
        assert not rl.enabled

    @pytest.mark.asyncio
    async def test_no_sleep_for_first_request(self) -> None:
        rl = RateLimiter(rps_per_host=1.0)
        start = time.monotonic()
        await rl.acquire("example.com")
        assert (time.monotonic() - start) < 0.05

    @pytest.mark.asyncio
    async def test_serializes_same_host(self) -> None:
        # 10 rps means subsequent acquires must wait at least ~0.1s
        rl = RateLimiter(rps_per_host=10.0)
        start = time.monotonic()
        await rl.acquire("example.com")
        await rl.acquire("example.com")
        elapsed = time.monotonic() - start
        # Allow some slack but the second acquire must have slept ~0.1s
        assert elapsed >= 0.08, f"expected >=0.08s, got {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_different_hosts_run_concurrently(self) -> None:
        rl = RateLimiter(rps_per_host=1.0)
        start = time.monotonic()
        # Two different hosts should both proceed without contention.
        await asyncio.gather(rl.acquire("foo.com"), rl.acquire("bar.com"))
        # Each first-request is instant, so total < 0.05s.
        assert (time.monotonic() - start) < 0.1

    @pytest.mark.asyncio
    async def test_empty_host_is_noop(self) -> None:
        rl = RateLimiter(rps_per_host=1.0)
        # Hammering an empty host shouldn't sleep.
        start = time.monotonic()
        for _ in range(20):
            await rl.acquire("")
        assert (time.monotonic() - start) < 0.05

    @pytest.mark.asyncio
    async def test_host_normalized_to_lowercase(self) -> None:
        # Acquire on UPPERCASE should still gate the lowercase form.
        rl = RateLimiter(rps_per_host=10.0)
        start = time.monotonic()
        await rl.acquire("Example.COM")
        await rl.acquire("example.com")
        elapsed = time.monotonic() - start
        assert elapsed >= 0.08, f"hosts should be unified; elapsed={elapsed:.3f}s"


class TestRobotsChecker:
    """Robots checker behavior. We don't make real HTTP -- monkeypatching
    _fetch_and_parse covers the cache + decision logic."""

    @pytest.mark.asyncio
    async def test_allows_when_robots_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rc = RobotsChecker(user_agent="test-bot")

        async def _stub(scheme: str, host: str) -> None:
            return None  # 404 / unreachable

        monkeypatch.setattr(rc, "_fetch_and_parse", _stub)
        assert await rc.is_allowed("https://example.com/page")

    @pytest.mark.asyncio
    async def test_disallows_blocked_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from urllib.robotparser import RobotFileParser

        rc = RobotsChecker(user_agent="test-bot")
        rp = RobotFileParser()
        rp.parse(["User-agent: *", "Disallow: /private/"])

        async def _stub(scheme: str, host: str) -> RobotFileParser:
            return rp

        monkeypatch.setattr(rc, "_fetch_and_parse", _stub)
        assert not await rc.is_allowed("https://example.com/private/secret")
        assert await rc.is_allowed("https://example.com/public/page")

    @pytest.mark.asyncio
    async def test_user_agent_specific_rules(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from urllib.robotparser import RobotFileParser

        rc = RobotsChecker(user_agent="my-bot")
        rp = RobotFileParser()
        rp.parse(
            [
                "User-agent: my-bot",
                "Disallow: /no-bots/",
                "User-agent: *",
                "Allow: /",
            ]
        )

        async def _stub(scheme: str, host: str) -> RobotFileParser:
            return rp

        monkeypatch.setattr(rc, "_fetch_and_parse", _stub)
        assert not await rc.is_allowed("https://example.com/no-bots/x")
        assert await rc.is_allowed("https://example.com/anything-else")

    @pytest.mark.asyncio
    async def test_caches_per_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rc = RobotsChecker(user_agent="bot", ttl_seconds=3600)
        call_count = 0

        async def _counting_stub(scheme: str, host: str) -> None:
            nonlocal call_count
            call_count += 1
            return None

        monkeypatch.setattr(rc, "_fetch_and_parse", _counting_stub)

        # Same host fetched 3 times -> 1 underlying call.
        await rc.is_allowed("https://example.com/a")
        await rc.is_allowed("https://example.com/b")
        await rc.is_allowed("https://example.com/c")
        assert call_count == 1

        # Different host triggers another fetch.
        await rc.is_allowed("https://other.example/x")
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_url_without_host_is_allowed(self) -> None:
        rc = RobotsChecker()
        # file:// URL has no hostname -- robots check skipped.
        assert await rc.is_allowed("file:///tmp/local")

    @pytest.mark.asyncio
    async def test_user_agent_property_exposed(self) -> None:
        rc = RobotsChecker(user_agent="custom-ua/1.0")
        assert rc.user_agent == "custom-ua/1.0"


class _FakeStream:
    """Minimal stand-in for httpcore's network_stream extension."""

    def __init__(self, server_addr: tuple[str, int]) -> None:
        self._server_addr = server_addr

    def get_extra_info(self, name: str):
        if name == "server_addr":
            return self._server_addr
        return None


def _resp_with_peer(peer_ip: str, status_code: int = 200) -> object:
    import httpx

    return httpx.Response(
        status_code=status_code,
        text="User-agent: *\nDisallow: /secret/\n",
        extensions={"network_stream": _FakeStream((peer_ip, 443))},
    )


class _ClientReturning:
    """Async-context-manager httpx.AsyncClient replacement whose STREAMING GET
    yields a pre-built response (used to inject a peer IP).

    robots.txt now probes via ``client.stream('GET')`` (ROBOTS-2 peer-IP fix):
    httpcore only populates the ``network_stream`` extension on a streaming
    response, so a buffered ``client.get()`` left the peer check a no-op."""

    def __init__(self, response, **_kwargs) -> None:
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def stream(self, method, url, headers=None):
        resp = self._response

        class _Ctx:
            async def __aenter__(_s):  # noqa: N805
                return resp

            async def __aexit__(_s, *a):  # noqa: N805
                return False

        return _Ctx()


class TestRobotsPostConnectPeerIPCheck:
    """M3: a DNS rebind inside the cache window can connect _fetch_and_parse's
    own httpx client to a private host even though the pre-connect host guard
    passed (e.g. host was a literal public name / unresolvable to the checker).
    After the GET, the peer IP is re-checked; a private peer -> allow-all
    (return None) when block_private_ips=True."""

    @pytest.mark.asyncio
    async def test_private_peer_ip_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        rc = RobotsChecker(user_agent="test-bot", block_private_ips=True)
        resp = _resp_with_peer("127.0.0.1")  # loopback peer

        def _fake_client(*args, **kwargs):
            return _ClientReturning(resp)

        monkeypatch.setattr(httpx, "AsyncClient", _fake_client)
        # Use a host that the pre-connect guard treats as public (does not
        # resolve to a private address here) so we exercise the POST-connect
        # path specifically.
        result = await rc._fetch_and_parse("https", "rebind.example")
        assert result is None  # allow-all

    @pytest.mark.asyncio
    async def test_public_peer_ip_parses_normally(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        rc = RobotsChecker(user_agent="test-bot", block_private_ips=True)
        resp = _resp_with_peer("93.184.216.34")  # public peer

        def _fake_client(*args, **kwargs):
            return _ClientReturning(resp)

        monkeypatch.setattr(httpx, "AsyncClient", _fake_client)
        result = await rc._fetch_and_parse("https", "rebind.example")
        # A normal parser is returned; the disallow rule is honored.
        assert result is not None
        assert not result.can_fetch("test-bot", "https://rebind.example/secret/x")
        assert result.can_fetch("test-bot", "https://rebind.example/ok")

    @pytest.mark.asyncio
    async def test_private_peer_allowed_when_blocking_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        # When block_private_ips=False the post-connect check is skipped and
        # a private peer's robots.txt is parsed normally.
        rc = RobotsChecker(user_agent="test-bot", block_private_ips=False)
        resp = _resp_with_peer("127.0.0.1")

        def _fake_client(*args, **kwargs):
            return _ClientReturning(resp)

        monkeypatch.setattr(httpx, "AsyncClient", _fake_client)
        result = await rc._fetch_and_parse("https", "rebind.example")
        assert result is not None
