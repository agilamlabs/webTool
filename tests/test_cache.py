"""Tests for the disk-backed TTL cache."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from web_agent.cache import DiskCache, _hash_key


class TestHashKey:
    def test_deterministic(self) -> None:
        assert _hash_key("hello") == _hash_key("hello")

    def test_different_inputs_different_hashes(self) -> None:
        assert _hash_key("a") != _hash_key("b")

    def test_returns_filesystem_safe_string(self) -> None:
        h = _hash_key("https://example.com/path?q=1&r=2")
        # Hex digits only -- safe in any filename
        assert all(c in "0123456789abcdef" for c in h)
        assert len(h) == 32


class TestDiskCache:
    @pytest.mark.asyncio
    async def test_miss_returns_none(self, tmp_path: Path) -> None:
        cache = DiskCache(cache_dir=str(tmp_path / "cache"))
        assert await cache.get("not-cached") is None

    @pytest.mark.asyncio
    async def test_set_then_get_roundtrips(self, tmp_path: Path) -> None:
        cache = DiskCache(cache_dir=str(tmp_path / "cache"))
        payload = {"url": "https://x", "html": "<p>hello</p>", "status": "success"}
        await cache.set("key1", payload)
        got = await cache.get("key1")
        assert got == payload

    @pytest.mark.asyncio
    async def test_get_returns_none_for_expired(self, tmp_path: Path) -> None:
        cache = DiskCache(cache_dir=str(tmp_path / "cache"), ttl_seconds=0.1)
        await cache.set("k", {"x": 1})
        await asyncio.sleep(0.2)
        assert await cache.get("k") is None

    @pytest.mark.asyncio
    async def test_expired_entry_deleted_on_access(self, tmp_path: Path) -> None:
        cache = DiskCache(cache_dir=str(tmp_path / "cache"), ttl_seconds=0.1)
        await cache.set("k", {"x": 1})
        # Confirm a file was created
        files_before = list((tmp_path / "cache").glob("*.json"))
        assert len(files_before) == 1

        await asyncio.sleep(0.2)
        assert await cache.get("k") is None
        # Accessing the expired entry deleted it
        files_after = list((tmp_path / "cache").glob("*.json"))
        assert len(files_after) == 0

    @pytest.mark.asyncio
    async def test_clear_removes_all_entries(self, tmp_path: Path) -> None:
        cache = DiskCache(cache_dir=str(tmp_path / "cache"))
        for i in range(5):
            await cache.set(f"key{i}", {"i": i})

        assert len(list((tmp_path / "cache").glob("*.json"))) == 5
        count = await cache.clear()
        assert count == 5
        assert len(list((tmp_path / "cache").glob("*.json"))) == 0

    @pytest.mark.asyncio
    async def test_clear_on_empty_dir_returns_zero(self, tmp_path: Path) -> None:
        cache = DiskCache(cache_dir=str(tmp_path / "nonexistent"))
        assert await cache.clear() == 0

    @pytest.mark.asyncio
    async def test_keys_with_same_hash_dont_collide_for_distinct_inputs(
        self, tmp_path: Path
    ) -> None:
        # Different keys -> different files
        cache = DiskCache(cache_dir=str(tmp_path / "cache"))
        await cache.set("foo", {"v": 1})
        await cache.set("bar", {"v": 2})
        assert (await cache.get("foo")) == {"v": 1}
        assert (await cache.get("bar")) == {"v": 2}

    @pytest.mark.asyncio
    async def test_eviction_removes_oldest_when_over_cap(self, tmp_path: Path) -> None:
        # Set max_cache_mb very small -- a single ~5KB entry overflows.
        cache = DiskCache(
            cache_dir=str(tmp_path / "cache"),
            max_cache_mb=0,  # Effectively zero -- every write triggers eviction
        )
        await cache.set("a", {"data": "x" * 100})
        await asyncio.sleep(0.01)  # ensure mtime ordering
        await cache.set("b", {"data": "y" * 100})
        # With max=0 the oldest entry should have been evicted
        files = list((tmp_path / "cache").glob("*.json"))
        # At most 1 should remain (the most recent write)
        assert len(files) <= 1

    @pytest.mark.asyncio
    async def test_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        bad = cache_dir / f"{_hash_key('badkey')}.json"
        bad.write_text("not valid JSON {", encoding="utf-8")

        cache = DiskCache(cache_dir=str(cache_dir))
        assert await cache.get("badkey") is None

    @pytest.mark.asyncio
    async def test_lazy_directory_creation(self, tmp_path: Path) -> None:
        # Cache dir doesn't exist yet
        cache_dir = tmp_path / "deep" / "nested" / "cache"
        assert not cache_dir.exists()

        cache = DiskCache(cache_dir=str(cache_dir))
        # Reading a missing key shouldn't create the dir
        await cache.get("k")
        assert not cache_dir.exists()

        # Writing should create it
        await cache.set("k", {"v": 1})
        assert cache_dir.exists()


class TestCacheIntegrationViaAgent:
    """End-to-end: enable cache via AppConfig, ensure subsystem uses it."""

    @pytest.mark.asyncio
    async def test_cache_disabled_by_default(self) -> None:
        from web_agent import Agent, AppConfig

        agent = Agent(AppConfig())
        # Don't actually start the browser -- just check construction
        assert agent._cache is None

    @pytest.mark.asyncio
    async def test_cache_enabled_creates_diskcache(self, tmp_path: Path) -> None:
        from web_agent import Agent, AppConfig

        config = AppConfig(cache={"enabled": True, "cache_dir": str(tmp_path / "cache")})
        agent = Agent(config)
        assert agent._cache is not None
        assert isinstance(agent._cache, DiskCache)

    @pytest.mark.asyncio
    async def test_search_engine_receives_cache(self, tmp_path: Path) -> None:
        from web_agent import Agent, AppConfig

        config = AppConfig(cache={"enabled": True, "cache_dir": str(tmp_path / "cache")})
        agent = Agent(config)
        # Same Cache instance threaded through
        assert agent._search._cache is agent._cache
        assert agent._fetcher._cache is agent._cache

    @pytest.mark.asyncio
    async def test_search_caches_response_and_serves_from_cache(self, tmp_path: Path) -> None:
        """End-to-end: first call hits provider, second call hits cache."""

        from web_agent import Agent, AppConfig
        from web_agent.models import SearchResponse, SearchResultItem

        config = AppConfig(
            cache={"enabled": True, "cache_dir": str(tmp_path / "cache")},
            search={"providers": ["playwright"]},  # we'll stub this
        )
        agent = Agent(config)

        # Replace the provider chain with a single recording fake.
        call_count = 0

        class _Fake:
            name = "fake"
            is_available = True

            async def search(self, query: str, max_results: int) -> SearchResponse:
                nonlocal call_count
                call_count += 1
                return SearchResponse(
                    query=query,
                    total_results=1,
                    results=[
                        SearchResultItem(
                            position=1,
                            title="t",
                            url="https://example.com",
                            snippet="s",
                        )
                    ],
                )

        agent._search._providers = [_Fake()]

        first = await agent._search.search("test query", max_results=5)
        assert call_count == 1
        assert first.from_cache is False
        assert first.total_results == 1

        # Second identical call should hit the cache; provider not invoked again
        second = await agent._search.search("test query", max_results=5)
        assert call_count == 1  # unchanged -- served from cache
        assert second.from_cache is True
        assert second.total_results == 1

        # Different max_results = different cache key, provider runs again
        third = await agent._search.search("test query", max_results=10)
        assert call_count == 2
        assert third.from_cache is False

    @pytest.mark.asyncio
    async def test_fetch_caches_result_and_serves_from_cache(self, tmp_path: Path) -> None:
        """Counterpart of the search test: WebFetcher.fetch sets from_cache=True
        on a hit. We stub _do_fetch so no browser is started."""
        from unittest.mock import AsyncMock

        from web_agent import Agent, AppConfig
        from web_agent.models import FetchResult, FetchStatus

        config = AppConfig(
            cache={"enabled": True, "cache_dir": str(tmp_path / "cache")},
            # Disable both politeness checks so the test is hermetic.
            safety={
                "rate_limit_per_host_rps": 0,
                "respect_robots_txt": False,
            },
        )
        agent = Agent(config)

        # Stub the network layer so no browser launches.
        call_count = 0

        async def _fake_do_fetch(url: str, session_id=None) -> FetchResult:
            nonlocal call_count
            call_count += 1
            return FetchResult(
                url=url,
                final_url=url,
                status_code=200,
                status=FetchStatus.SUCCESS,
                html=f"<html><body>Hi from {url}</body></html>",
                response_time_ms=10.0,
            )

        agent._fetcher._do_fetch = AsyncMock(side_effect=_fake_do_fetch)

        first = await agent._fetcher.fetch("https://example.com/page")
        assert call_count == 1
        assert first.from_cache is False
        assert first.status == FetchStatus.SUCCESS

        # Second identical call -> cache hit, no network, from_cache=True
        second = await agent._fetcher.fetch("https://example.com/page")
        assert call_count == 1, "_do_fetch should NOT have been re-called"
        assert second.from_cache is True
        assert second.status == FetchStatus.SUCCESS
        assert second.html == first.html
