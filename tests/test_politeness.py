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
