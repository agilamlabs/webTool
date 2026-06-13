"""Wave 2E search-resilience tests (offline, no network, no Playwright).

Covers the v1.7.0 SearchEngine hardening:

- Provider health memory / circuit breaker: a provider that blocks/raises
  is skipped on the next call within the cooldown (driven by a fake clock,
  no sleeps), then re-probed after the cooldown elapses.
- Fallback chain: provider A blocked -> engine falls through to provider B
  and returns B's results.
- Blocked-vs-empty: all providers blocked surfaces a distinguishable
  ``blocked=True`` signal; providers-ok-zero-hits surfaces ``blocked=False``.
- Links-only: ``engine.search`` only calls providers -- it never touches a
  fetcher / extractor.

All providers are fakes; the chain is injected by replacing
``engine._providers`` (the idiom used in ``tests/test_search_providers.py``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from web_agent.config import AppConfig
from web_agent.models import SearchResponse, SearchResultItem
from web_agent.search_engine import SearchEngine, _ProviderCircuitBreaker
from web_agent.search_providers import (
    DDGSProvider,
    ProviderBlockedError,
    SearchProvider,
    _is_ddgs_ratelimit,
)

# ---------------------------------------------------------------------------
# Fakes + helpers
# ---------------------------------------------------------------------------


class _FakeClock:
    """Monotonic-clock stand-in. ``advance`` moves time without sleeping."""

    def __init__(self, start: float = 1000.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _make_item(idx: int, url: str | None = None) -> SearchResultItem:
    return SearchResultItem(
        position=idx,
        title=f"T{idx}",
        url=url or f"https://example.com/{idx}",
        displayed_url="example.com",
        snippet="snippet",
    )


class _ScriptedProvider(SearchProvider):
    """Fake provider with a per-call scripted outcome.

    Each call pops the next action from ``script``:
    - ``"ok"``       -> return one result item.
    - ``"empty"``    -> return a genuine zero-results response.
    - ``"block"``    -> raise :class:`ProviderBlockedError`.
    - ``"error"``    -> raise a generic ``RuntimeError``.
    When the script is exhausted, the ``default`` action repeats.
    """

    def __init__(
        self,
        name: str,
        script: list[str] | None = None,
        default: str = "ok",
        available: bool = True,
    ) -> None:
        self._name = name
        self._script = list(script or [])
        self._default = default
        self._available = available
        self.call_count = 0

    # SearchProvider.name is a ClassVar str; per-instance name keeps the
    # fakes distinguishable in the breaker's keying.
    @property
    def name(self) -> str:  # type: ignore[override]
        return self._name

    @property
    def is_available(self) -> bool:
        return self._available

    async def search(self, query: str, max_results: int) -> SearchResponse:
        self.call_count += 1
        action = self._script.pop(0) if self._script else self._default
        if action == "block":
            raise ProviderBlockedError(self._name, "ratelimit")
        if action == "error":
            raise RuntimeError("provider boom")
        if action == "empty":
            return SearchResponse(query=query, total_results=0, results=[])
        item = _make_item(1)
        return SearchResponse(query=query, total_results=1, results=[item])


def _engine(clock: _FakeClock | None = None, cooldown: float = 60.0) -> SearchEngine:
    """Build an engine with no real browser and an injectable clock."""
    return SearchEngine(
        browser_manager=MagicMock(),
        config=AppConfig(),
        circuit_cooldown_s=cooldown,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# Circuit breaker unit (clock-driven, no engine)
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_trip_opens_then_cooldown_reprobes(self) -> None:
        clock = _FakeClock()
        cb = _ProviderCircuitBreaker(cooldown=60.0, clock=clock)
        p = _ScriptedProvider("ddgs")

        assert not cb.is_open(p)
        cb.trip(p, "ratelimit")
        assert cb.is_open(p)  # immediately open

        clock.advance(59.0)
        assert cb.is_open(p)  # still within cooldown

        clock.advance(1.0)  # now exactly 60s -> half-open / re-probe allowed
        assert not cb.is_open(p)

    def test_record_success_clears_trip(self) -> None:
        clock = _FakeClock()
        cb = _ProviderCircuitBreaker(cooldown=60.0, clock=clock)
        p = _ScriptedProvider("ddgs")
        cb.trip(p)
        assert cb.is_open(p)
        cb.record_success(p)
        assert not cb.is_open(p)

    def test_distinct_instances_have_independent_circuits(self) -> None:
        # Keyed by instance identity, not name: two providers that share a
        # name keep independent circuits (regression guard for the engine's
        # same-name-fake test idiom).
        cb = _ProviderCircuitBreaker(cooldown=60.0)
        a = _ScriptedProvider("dup")
        b = _ScriptedProvider("dup")
        cb.trip(a)
        assert cb.is_open(a)
        assert not cb.is_open(b)

    def test_zero_cooldown_disables_breaker(self) -> None:
        cb = _ProviderCircuitBreaker(cooldown=0.0)
        p = _ScriptedProvider("ddgs")
        cb.trip(p)
        assert not cb.is_open(p)


# ---------------------------------------------------------------------------
# Circuit breaker via the engine (the Wave 2E acceptance behaviour)
# ---------------------------------------------------------------------------


class TestEngineCircuitBreaker:
    @pytest.mark.asyncio
    async def test_blocked_provider_skipped_within_cooldown(self) -> None:
        clock = _FakeClock()
        engine = _engine(clock=clock, cooldown=60.0)
        # Provider blocks on its FIRST call; if it were re-invoked it would
        # return results -- but the breaker must keep it skipped.
        provider = _ScriptedProvider("ddgs", script=["block"], default="ok")
        engine._providers = [provider]

        out1 = await engine.search_with_outcome("q", max_results=5)
        assert provider.call_count == 1
        assert out1.blocked is True
        assert out1.response.total_results == 0

        # Second call within cooldown: provider is skipped (NOT re-invoked).
        out2 = await engine.search_with_outcome("q", max_results=5)
        assert provider.call_count == 1  # unchanged -> skipped silently
        assert out2.blocked is True
        assert "ddgs" in out2.providers_skipped_cooldown

    @pytest.mark.asyncio
    async def test_provider_reprobed_after_cooldown(self) -> None:
        clock = _FakeClock()
        engine = _engine(clock=clock, cooldown=60.0)
        provider = _ScriptedProvider("ddgs", script=["block"], default="ok")
        engine._providers = [provider]

        await engine.search_with_outcome("q", max_results=5)
        assert provider.call_count == 1

        clock.advance(61.0)  # cooldown elapsed -> half-open, re-probe allowed
        out = await engine.search_with_outcome("q", max_results=5)
        assert provider.call_count == 2  # re-invoked
        assert out.response.total_results == 1  # now healthy
        assert out.blocked is False
        # A healthy probe clears the trip entirely.
        assert not engine._breaker.is_open(provider)

    @pytest.mark.asyncio
    async def test_generic_error_also_trips_breaker(self) -> None:
        clock = _FakeClock()
        engine = _engine(clock=clock, cooldown=60.0)
        provider = _ScriptedProvider("ddgs", script=["error"], default="ok")
        engine._providers = [provider]

        await engine.search_with_outcome("q", max_results=5)
        assert provider.call_count == 1
        # Within cooldown the failed provider is skipped.
        await engine.search_with_outcome("q", max_results=5)
        assert provider.call_count == 1


# ---------------------------------------------------------------------------
# Fallback chain
# ---------------------------------------------------------------------------


class TestFallbackChain:
    @pytest.mark.asyncio
    async def test_blocked_a_falls_through_to_b(self) -> None:
        engine = _engine()
        a = _ScriptedProvider("a", script=["block"])
        b = _ScriptedProvider("b", script=["ok"])
        engine._providers = [a, b]

        out = await engine.search_with_outcome("q", max_results=5)
        assert a.call_count == 1
        assert b.call_count == 1
        assert out.response.total_results == 1
        assert out.blocked is False  # B succeeded -> overall not blocked
        assert out.providers_tried == ["a", "b"]
        assert out.provider_errors.get("a") == "ratelimit"

    @pytest.mark.asyncio
    async def test_skipped_cooldown_a_falls_through_to_b(self) -> None:
        clock = _FakeClock()
        engine = _engine(clock=clock, cooldown=60.0)
        a = _ScriptedProvider("a", script=["block", "ok"])
        b = _ScriptedProvider("b", default="ok")
        engine._providers = [a, b]

        # First call trips A, B serves results.
        await engine.search_with_outcome("q", max_results=5)
        assert a.call_count == 1
        assert b.call_count == 1

        # Second call within cooldown: A is skipped, B serves again.
        out = await engine.search_with_outcome("q", max_results=5)
        assert a.call_count == 1  # A skipped
        assert b.call_count == 2  # B used again
        assert out.response.total_results == 1
        assert "a" in out.providers_skipped_cooldown


# ---------------------------------------------------------------------------
# Blocked-vs-empty distinction
# ---------------------------------------------------------------------------


class TestBlockedVsEmpty:
    @pytest.mark.asyncio
    async def test_all_blocked_is_blocked_true(self) -> None:
        engine = _engine()
        a = _ScriptedProvider("a", script=["block"])
        b = _ScriptedProvider("b", script=["block"])
        engine._providers = [a, b]

        out = await engine.search_with_outcome("q", max_results=5)
        assert out.response.total_results == 0
        assert out.blocked is True
        assert out.provider_errors == {"a": "ratelimit", "b": "ratelimit"}
        assert out.providers_tried == ["a", "b"]

    @pytest.mark.asyncio
    async def test_all_zero_hits_is_blocked_false(self) -> None:
        engine = _engine()
        a = _ScriptedProvider("a", script=["empty"])
        b = _ScriptedProvider("b", script=["empty"])
        engine._providers = [a, b]

        out = await engine.search_with_outcome("q", max_results=5)
        assert out.response.total_results == 0
        assert out.blocked is False  # reachable, genuinely zero hits
        assert out.provider_errors == {}
        assert out.providers_tried == ["a", "b"]

    @pytest.mark.asyncio
    async def test_mixed_block_then_empty_is_blocked_true(self) -> None:
        # One provider blocked, the other genuinely empty -> still "blocked"
        # because at least one provider was actively refused.
        engine = _engine()
        a = _ScriptedProvider("a", script=["block"])
        b = _ScriptedProvider("b", script=["empty"])
        engine._providers = [a, b]

        out = await engine.search_with_outcome("q", max_results=5)
        assert out.response.total_results == 0
        assert out.blocked is True

    @pytest.mark.asyncio
    async def test_generic_error_is_degraded_not_blocked(self) -> None:
        # A lone transient provider error: not an active block, but the empty
        # answer is untrustworthy -> degraded True, blocked False.
        engine = _engine()
        engine._providers = [_ScriptedProvider("a", script=["error"])]

        out = await engine.search_with_outcome("q", max_results=5)
        assert out.response.total_results == 0
        assert out.blocked is False
        assert out.degraded is True
        assert "a" in out.provider_errors

    @pytest.mark.asyncio
    async def test_zero_hits_is_not_degraded(self) -> None:
        engine = _engine()
        engine._providers = [_ScriptedProvider("a", script=["empty"])]

        out = await engine.search_with_outcome("q", max_results=5)
        assert out.degraded is False  # genuinely nothing out there

    @pytest.mark.asyncio
    async def test_success_is_never_degraded_or_blocked(self) -> None:
        engine = _engine()
        engine._providers = [_ScriptedProvider("a", script=["block"]), _ScriptedProvider("b")]
        out = await engine.search_with_outcome("q", max_results=5)
        assert out.response.total_results == 1
        assert out.blocked is False
        assert out.degraded is False  # B succeeded; the earlier block is moot

    @pytest.mark.asyncio
    async def test_strict_raises_with_blocked_cause(self) -> None:
        from web_agent.exceptions import SearchError

        engine = _engine()
        engine._providers = [_ScriptedProvider("a", script=["block"])]

        with pytest.raises(SearchError, match="CAPTCHA / rate-limit"):
            await engine.search_with_outcome("q", max_results=5, strict=True)

    @pytest.mark.asyncio
    async def test_strict_raises_with_zero_hits_cause(self) -> None:
        from web_agent.exceptions import SearchError

        engine = _engine()
        engine._providers = [_ScriptedProvider("a", script=["empty"])]

        with pytest.raises(SearchError, match="zero results"):
            await engine.search_with_outcome("q", max_results=5, strict=True)


# ---------------------------------------------------------------------------
# Links-only guarantee
# ---------------------------------------------------------------------------


class TestLinksOnly:
    @pytest.mark.asyncio
    async def test_search_only_calls_providers_no_fetch(self) -> None:
        """engine.search must not fetch/extract -- only providers are touched.

        The engine has no fetcher attribute; the browser_manager it receives
        is only handed to PlaywrightProvider at construction. We assert the
        injected fake providers are the ONLY thing invoked, and the
        browser_manager mock records no page activity in the links-only path.
        """
        bm = MagicMock()
        engine = SearchEngine(browser_manager=bm, config=AppConfig())
        provider = _ScriptedProvider("a", script=["ok"])
        engine._providers = [provider]

        out = await engine.search_with_outcome("q", max_results=5)
        assert out.response.total_results == 1
        assert provider.call_count == 1
        # No fetch path: the engine never opened a page / context itself.
        # (PlaywrightProvider would, but it isn't in the injected chain here.)
        assert not bm.new_page.called
        assert not bm.new_context.called

    @pytest.mark.asyncio
    async def test_search_returns_searchresponse_not_outcome(self) -> None:
        # Back-compat: the legacy search() still returns a bare SearchResponse.
        engine = _engine()
        engine._providers = [_ScriptedProvider("a", script=["ok"])]
        resp = await engine.search("q", max_results=5)
        assert isinstance(resp, SearchResponse)
        assert resp.total_results == 1


# ---------------------------------------------------------------------------
# Provider-level blocked detection (feeds the engine's breaker accurate signals)
# ---------------------------------------------------------------------------


class TestDDGSBlockedDetection:
    def test_is_ddgs_ratelimit_by_class_name(self) -> None:
        # Name deliberately mirrors ddgs.exceptions.RatelimitException so the
        # by-name match is exercised without importing ddgs.exceptions.
        class RatelimitException(Exception):  # noqa: N818
            pass

        assert _is_ddgs_ratelimit(RatelimitException("202 Ratelimit"))

    def test_is_ddgs_ratelimit_by_message(self) -> None:
        assert _is_ddgs_ratelimit(RuntimeError("https 202 Ratelimit"))
        assert _is_ddgs_ratelimit(RuntimeError("rate limit exceeded"))

    def test_is_ddgs_ratelimit_false_for_generic(self) -> None:
        assert not _is_ddgs_ratelimit(RuntimeError("connection reset"))
        assert not _is_ddgs_ratelimit(ValueError("bad input"))

    @pytest.mark.asyncio
    async def test_ddgs_ratelimit_raises_provider_blocked(self) -> None:
        p = DDGSProvider()

        class RatelimitException(Exception):  # noqa: N818 - mirrors ddgs's name
            pass

        class _Boom:
            def __enter__(self) -> _Boom:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def text(self, *args: Any, **kwargs: Any) -> list[dict]:
                raise RatelimitException("202 Ratelimit")

        with patch("ddgs.DDGS", _Boom):
            with pytest.raises(ProviderBlockedError) as ei:
                await p.search("python", max_results=5)
        assert ei.value.provider == "ddgs"
        assert ei.value.reason == "ratelimit"

    @pytest.mark.asyncio
    async def test_ddgs_generic_error_still_empty(self) -> None:
        # A non-ratelimit failure stays a soft empty (chain falls through),
        # NOT a ProviderBlockedError.
        p = DDGSProvider()

        class _Boom:
            def __enter__(self) -> _Boom:
                return self

            def __exit__(self, *args: Any) -> None:
                return None

            def text(self, *args: Any, **kwargs: Any) -> list[dict]:
                raise RuntimeError("DDG changed their HTML")

        with patch("ddgs.DDGS", _Boom):
            resp = await p.search("python", max_results=5)
        assert resp.total_results == 0


class TestProviderBlockedError:
    def test_message_and_attrs(self) -> None:
        err = ProviderBlockedError("playwright", "captcha")
        assert err.provider == "playwright"
        assert err.reason == "captcha"
        assert "playwright" in str(err)
        assert "captcha" in str(err)
