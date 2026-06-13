"""v1.7.0 (Wave 4A): metrics registry + instrumentation tests.

All offline -- no network, no Playwright launch. Three layers:

1. :class:`MetricsRegistry` unit behaviour: counters accumulate, observe
   tracks count/sum/min/max, labels distinguish series, snapshot shape,
   reset, the label-cardinality cap folding overflow into ``_other``, and the
   disabled registry being a no-op.
2. Instrumentation of the owned hot paths driven with fakes / mocks:
   WebFetcher fetch outcomes, SearchEngine provider-outcome counters (reusing
   the ``tests/test_search_resilience.py`` fake-provider idiom), and
   BrowserManager crash / relaunch counters (the ``tests/test_lifecycle.py``
   AsyncMock pattern).
3. Schema: :class:`MetricsSnapshot` round-trip and :class:`MetricsConfig`
   env-var loading.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.browser_manager import BrowserManager
from web_agent.config import AppConfig, BrowserConfig, MetricsConfig
from web_agent.exceptions import BrowserError
from web_agent.metrics import MetricsRegistry, default_registry, get_metrics, noop_registry
from web_agent.models import (
    FetchResult,
    FetchStatus,
    MetricsSnapshot,
    SearchResponse,
    SearchResultItem,
)
from web_agent.search_engine import SearchEngine
from web_agent.search_providers import ProviderBlockedError, SearchProvider
from web_agent.web_fetcher import WebFetcher

# ===========================================================================
# 1. MetricsRegistry unit behaviour
# ===========================================================================


class TestMetricsRegistryCounters:
    def test_incr_accumulates(self) -> None:
        reg = MetricsRegistry()
        reg.incr("fetch_total")
        reg.incr("fetch_total")
        reg.incr("fetch_total", value=3)
        assert reg.snapshot()["counters"]["fetch_total"] == 5

    def test_labels_distinguish_series(self) -> None:
        reg = MetricsRegistry()
        reg.incr("fetch_outcome", status="success")
        reg.incr("fetch_outcome", status="success")
        reg.incr("fetch_outcome", status="blocked")
        counters = reg.snapshot()["counters"]
        assert counters["fetch_outcome{status=success}"] == 2
        assert counters["fetch_outcome{status=blocked}"] == 1

    def test_label_order_is_canonical(self) -> None:
        # Same labels in different kwarg order must hit the SAME series.
        reg = MetricsRegistry()
        reg.incr("x", a="1", b="2")
        reg.incr("x", b="2", a="1")
        counters = reg.snapshot()["counters"]
        assert counters == {"x{a=1,b=2}": 2}

    def test_label_free_metric_has_no_braces(self) -> None:
        reg = MetricsRegistry()
        reg.incr("search_total")
        assert "search_total" in reg.snapshot()["counters"]


class TestMetricsRegistryDistributions:
    def test_observe_tracks_count_sum_min_max(self) -> None:
        reg = MetricsRegistry()
        for v in (10.0, 30.0, 20.0):
            reg.observe("bytes_downloaded", v)
        dist = reg.snapshot()["distributions"]["bytes_downloaded"]
        assert dist["count"] == 3.0
        assert dist["sum"] == 60.0
        assert dist["min"] == 10.0
        assert dist["max"] == 30.0
        assert dist["avg"] == 20.0

    def test_observe_single_value(self) -> None:
        reg = MetricsRegistry()
        reg.observe("ttfb_ms", 42.5)
        dist = reg.snapshot()["distributions"]["ttfb_ms"]
        assert dist["count"] == 1.0
        assert dist["min"] == dist["max"] == 42.5

    def test_observe_labels_distinguish_series(self) -> None:
        reg = MetricsRegistry()
        reg.observe("lat", 1.0, host="a")
        reg.observe("lat", 9.0, host="b")
        dists = reg.snapshot()["distributions"]
        assert dists["lat{host=a}"]["sum"] == 1.0
        assert dists["lat{host=b}"]["sum"] == 9.0


class TestSnapshotShape:
    def test_snapshot_keys_and_uptime(self) -> None:
        reg = MetricsRegistry()
        reg.incr("c")
        reg.observe("d", 1.0)
        snap = reg.snapshot()
        assert set(snap.keys()) == {"enabled", "uptime_s", "counters", "distributions"}
        assert snap["enabled"] is True
        assert isinstance(snap["uptime_s"], float)
        assert snap["uptime_s"] >= 0.0
        # Distribution inner dict has the five-number summary.
        assert set(snap["distributions"]["d"].keys()) == {"count", "sum", "min", "max", "avg"}


class TestReset:
    def test_reset_clears_everything(self) -> None:
        reg = MetricsRegistry()
        reg.incr("c", value=5)
        reg.observe("d", 3.0)
        reg.reset()
        snap = reg.snapshot()
        assert snap["counters"] == {}
        assert snap["distributions"] == {}

    def test_reset_allows_reuse(self) -> None:
        reg = MetricsRegistry()
        reg.incr("c")
        reg.reset()
        reg.incr("c")
        assert reg.snapshot()["counters"]["c"] == 1


class TestCardinalityCap:
    def test_overflow_folds_into_other_bucket(self) -> None:
        reg = MetricsRegistry(max_label_cardinality=3)
        # 10 distinct hosts but cap is 3 -> 3 real series + 1 overflow bucket.
        for i in range(10):
            reg.incr("by_host", host=f"h{i}")
        counters = reg.snapshot()["counters"]
        # The overflow bucket exists and absorbed the excess.
        overflow = [k for k in counters if "_other" in k]
        assert len(overflow) == 1
        # Total distinct series is bounded: at most cap + 1.
        assert len(counters) <= 4
        # Every increment is still accounted for (3 singletons + 7 in overflow).
        assert sum(counters.values()) == 10

    def test_cap_is_per_metric_name(self) -> None:
        reg = MetricsRegistry(max_label_cardinality=2)
        reg.incr("a", k="1")
        reg.incr("a", k="2")
        reg.incr("a", k="3")  # overflow for 'a'
        reg.incr("b", k="1")  # 'b' has its own budget
        counters = reg.snapshot()["counters"]
        assert any("_other" in k and k.startswith("a") for k in counters)
        assert "b{k=1}" in counters

    def test_distributions_also_capped(self) -> None:
        reg = MetricsRegistry(max_label_cardinality=2)
        for i in range(6):
            reg.observe("d", float(i), tag=f"t{i}")
        dists = reg.snapshot()["distributions"]
        assert len([k for k in dists if "_other" in k]) == 1
        assert len(dists) <= 3

    def test_cap_floored_at_one(self) -> None:
        # A non-positive cap is clamped to 1 (never 0 -> would fold everything
        # immediately and is a nonsensical config).
        reg = MetricsRegistry(max_label_cardinality=0)
        reg.incr("a", k="1")
        reg.incr("a", k="2")
        counters = reg.snapshot()["counters"]
        # One real series + overflow.
        assert "a{k=1}" in counters
        assert any("_other" in k for k in counters)

    def test_known_series_keeps_recording_after_cap_hit(self) -> None:
        # A series admitted before the cap was hit must keep accumulating,
        # not get redirected to overflow.
        reg = MetricsRegistry(max_label_cardinality=1)
        reg.incr("a", k="first")
        reg.incr("a", k="second")  # overflow
        reg.incr("a", k="first")  # known series -- still counts directly
        counters = reg.snapshot()["counters"]
        assert counters["a{k=first}"] == 2


class TestDisabledRegistry:
    def test_disabled_incr_and_observe_are_noops(self) -> None:
        reg = MetricsRegistry(enabled=False)
        reg.incr("c", value=99)
        reg.observe("d", 1234.0)
        snap = reg.snapshot()
        assert snap["enabled"] is False
        assert snap["counters"] == {}
        assert snap["distributions"] == {}

    def test_enabled_property(self) -> None:
        assert MetricsRegistry().enabled is True
        assert MetricsRegistry(enabled=False).enabled is False


class TestModuleHelpers:
    def test_default_registry_is_singleton(self) -> None:
        assert default_registry() is default_registry()

    def test_noop_registry_is_disabled_singleton(self) -> None:
        n = noop_registry()
        assert n.enabled is False
        assert noop_registry() is n

    def test_get_metrics_normalizes_none_to_noop(self) -> None:
        assert get_metrics(None) is noop_registry()
        real = MetricsRegistry()
        assert get_metrics(real) is real


# ===========================================================================
# 2. Instrumentation: WebFetcher fetch outcomes
# ===========================================================================


def _fetcher_with_metrics(reg: MetricsRegistry, config: AppConfig | None = None) -> WebFetcher:
    """WebFetcher wired with a real registry and a mocked browser manager.

    No robots / rate-limiter / cache so the early gates don't interfere; the
    BrowserManager is a MagicMock since the tests that exercise the navigation
    path stub ``_do_fetch`` directly.
    """
    return WebFetcher(
        browser_manager=MagicMock(),
        config=config or AppConfig(),
        metrics=reg,
    )


class TestWebFetcherInstrumentation:
    @pytest.mark.asyncio
    async def test_blocked_domain_increments_outcome(self) -> None:
        reg = MetricsRegistry()
        cfg = AppConfig(safety={"denied_domains": ["evil.com"]})
        fetcher = _fetcher_with_metrics(reg, cfg)

        result = await fetcher.fetch("https://evil.com/page")

        assert result.status == FetchStatus.BLOCKED
        counters = reg.snapshot()["counters"]
        assert counters["fetch_total"] == 1
        assert counters["fetch_outcome{status=blocked}"] == 1

    @pytest.mark.asyncio
    async def test_download_url_increments_network_error(self) -> None:
        reg = MetricsRegistry()
        fetcher = _fetcher_with_metrics(reg)

        result = await fetcher.fetch("https://example.com/report.pdf")

        assert result.status == FetchStatus.NETWORK_ERROR
        counters = reg.snapshot()["counters"]
        assert counters["fetch_total"] == 1
        assert counters["fetch_outcome{status=network_error}"] == 1

    @pytest.mark.asyncio
    async def test_success_increments_outcome_and_observes_bytes(self) -> None:
        reg = MetricsRegistry()
        fetcher = _fetcher_with_metrics(reg)

        async def _fake_do_fetch(url: str, session_id: Any = None) -> FetchResult:
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.SUCCESS,
                html="<html><body>hello</body></html>",
                ttfb_ms=12.5,
            )

        fetcher._do_fetch = _fake_do_fetch  # type: ignore[assignment]

        result = await fetcher.fetch("https://example.com/")

        assert result.status == FetchStatus.SUCCESS
        snap = reg.snapshot()
        assert snap["counters"]["fetch_total"] == 1
        assert snap["counters"]["fetch_outcome{status=success}"] == 1
        # bytes_downloaded falls back to len(html) when no captured weight.
        assert snap["distributions"]["bytes_downloaded"]["sum"] == float(len(result.html or ""))
        assert snap["distributions"]["ttfb_ms"]["sum"] == 12.5

    @pytest.mark.asyncio
    async def test_blocked_challenge_increments_vendor(self) -> None:
        from web_agent.models import ChallengeInfo

        reg = MetricsRegistry()
        fetcher = _fetcher_with_metrics(reg)

        async def _fake_do_fetch(url: str, session_id: Any = None) -> FetchResult:
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.BLOCKED,
                challenge=ChallengeInfo(
                    vendor="cloudflare",
                    kind="js_challenge",
                    confidence=0.95,
                    evidence=["cf-marker"],
                ),
            )

        fetcher._do_fetch = _fake_do_fetch  # type: ignore[assignment]

        await fetcher.fetch("https://example.com/")

        counters = reg.snapshot()["counters"]
        assert counters["fetch_outcome{status=blocked}"] == 1
        assert counters["challenge_detected{vendor=cloudflare}"] == 1

    @pytest.mark.asyncio
    async def test_disabled_registry_records_nothing(self) -> None:
        reg = MetricsRegistry(enabled=False)
        cfg = AppConfig(safety={"denied_domains": ["evil.com"]})
        fetcher = _fetcher_with_metrics(reg, cfg)

        await fetcher.fetch("https://evil.com/page")

        assert reg.snapshot()["counters"] == {}

    @pytest.mark.asyncio
    async def test_binary_blocked_domain_increments(self) -> None:
        reg = MetricsRegistry()
        cfg = AppConfig(safety={"denied_domains": ["evil.com"]})
        fetcher = _fetcher_with_metrics(reg, cfg)

        result = await fetcher.fetch_binary("https://evil.com/file.pdf")

        assert result.status == FetchStatus.BLOCKED
        counters = reg.snapshot()["counters"]
        assert counters["fetch_total"] == 1
        assert counters["fetch_outcome{status=blocked}"] == 1


# ===========================================================================
# 2. Instrumentation: SearchEngine provider outcomes
# ===========================================================================


def _item(idx: int = 1) -> SearchResultItem:
    return SearchResultItem(position=idx, title=f"T{idx}", url=f"https://example.com/{idx}")


class _ScriptedProvider(SearchProvider):
    """Fake provider whose single call yields a fixed outcome."""

    def __init__(self, name: str, action: str = "ok") -> None:
        self._name = name
        self._action = action

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._name

    @property
    def is_available(self) -> bool:
        return True

    async def search(self, query: str, max_results: int) -> SearchResponse:
        if self._action == "block":
            raise ProviderBlockedError(self._name, "ratelimit")
        if self._action == "error":
            raise RuntimeError("boom")
        if self._action == "empty":
            return SearchResponse(query=query, total_results=0, results=[])
        return SearchResponse(query=query, total_results=1, results=[_item()])


def _engine(reg: MetricsRegistry) -> SearchEngine:
    return SearchEngine(browser_manager=MagicMock(), config=AppConfig(), metrics=reg)


class TestSearchEngineInstrumentation:
    @pytest.mark.asyncio
    async def test_ok_outcome_and_search_total(self) -> None:
        reg = MetricsRegistry()
        engine = _engine(reg)
        engine._providers = [_ScriptedProvider("p1", "ok")]

        await engine.search("q")

        counters = reg.snapshot()["counters"]
        assert counters["search_total"] == 1
        assert counters["search_provider_outcome{outcome=ok,provider=p1}"] == 1

    @pytest.mark.asyncio
    async def test_blocked_falls_through_and_counts_both(self) -> None:
        reg = MetricsRegistry()
        engine = _engine(reg)
        # p1 blocks (counts blocked + circuit_trip), p2 answers ok.
        engine._providers = [_ScriptedProvider("p1", "block"), _ScriptedProvider("p2", "ok")]

        outcome = await engine.search_with_outcome("q")

        assert outcome.response.results  # p2 answered
        counters = reg.snapshot()["counters"]
        assert counters["search_provider_outcome{outcome=blocked,provider=p1}"] == 1
        assert counters["search_circuit_trip{provider=p1}"] == 1
        assert counters["search_provider_outcome{outcome=ok,provider=p2}"] == 1

    @pytest.mark.asyncio
    async def test_error_outcome_and_circuit_trip(self) -> None:
        reg = MetricsRegistry()
        engine = _engine(reg)
        engine._providers = [_ScriptedProvider("p1", "error")]

        await engine.search("q")

        counters = reg.snapshot()["counters"]
        assert counters["search_provider_outcome{outcome=error,provider=p1}"] == 1
        assert counters["search_circuit_trip{provider=p1}"] == 1

    @pytest.mark.asyncio
    async def test_cooldown_outcome_on_open_circuit(self) -> None:
        reg = MetricsRegistry()
        engine = _engine(reg)
        p1 = _ScriptedProvider("p1", "block")
        engine._providers = [p1]

        # First call trips the breaker; second call (circuit open) records a
        # 'cooldown' outcome for the skipped provider.
        await engine.search("q1")
        await engine.search("q2")

        counters = reg.snapshot()["counters"]
        assert counters["search_provider_outcome{outcome=cooldown,provider=p1}"] == 1
        assert counters["search_total"] == 2


# ===========================================================================
# 2. Instrumentation: BrowserManager crash / relaunch / launch
# ===========================================================================


def _bm(reg: MetricsRegistry, **browser_overrides: Any) -> BrowserManager:
    return BrowserManager(
        AppConfig(browser=BrowserConfig(**browser_overrides)),
        metrics=reg,
    )


class TestBrowserManagerInstrumentation:
    def test_crash_increments_browser_crash(self) -> None:
        reg = MetricsRegistry()
        bm = _bm(reg)
        bm._started = True  # crash handler only fires on a started browser
        bm._on_browser_disconnected(None)
        assert reg.snapshot()["counters"]["browser_crash"] == 1

    def test_crash_noop_when_stopping(self) -> None:
        reg = MetricsRegistry()
        bm = _bm(reg)
        bm._started = True
        bm._stopping = True
        bm._on_browser_disconnected(None)
        assert reg.snapshot()["counters"] == {}

    @pytest.mark.asyncio
    async def test_relaunch_ok_counter(self) -> None:
        reg = MetricsRegistry()
        bm = _bm(reg, auto_relaunch=True, relaunch_max_attempts=3)
        bm._started = True
        bm._crashed = True

        # stop() + start() are stubbed; start() flips the browser "alive".
        async def _fake_stop() -> None:
            return None

        async def _fake_start() -> None:
            bm._crashed = False
            bm._started = True
            bm._browser = MagicMock()
            bm._browser.is_connected = MagicMock(return_value=True)

        bm.stop = AsyncMock(side_effect=_fake_stop)  # type: ignore[method-assign]
        bm.start = AsyncMock(side_effect=_fake_start)  # type: ignore[method-assign]

        await bm.ensure_running()

        assert reg.snapshot()["counters"]["browser_relaunch{result=ok}"] == 1

    @pytest.mark.asyncio
    async def test_relaunch_failed_counter(self) -> None:
        reg = MetricsRegistry()
        bm = _bm(reg, auto_relaunch=True, relaunch_max_attempts=2, relaunch_backoff_base_s=0.1)
        bm._started = True
        bm._crashed = True

        bm.stop = AsyncMock()  # type: ignore[method-assign]
        bm.start = AsyncMock(side_effect=BrowserError("launch failed"))  # type: ignore[method-assign]

        with pytest.raises(BrowserError):
            await bm.ensure_running()

        assert reg.snapshot()["counters"]["browser_relaunch{result=failed}"] == 1


# ===========================================================================
# 3. Schema: MetricsSnapshot + MetricsConfig
# ===========================================================================


class TestMetricsSnapshotSchema:
    def test_defaults(self) -> None:
        snap = MetricsSnapshot()
        assert snap.enabled is True
        assert snap.counters == {}
        assert snap.distributions == {}
        assert snap.uptime_s == 0.0
        assert snap.correlation_id is None
        assert snap.snapshot_at is not None

    def test_round_trip(self) -> None:
        snap = MetricsSnapshot(
            enabled=True,
            counters={"fetch_total": 7},
            distributions={"bytes_downloaded": {"count": 1.0, "sum": 10.0, "min": 10.0, "max": 10.0, "avg": 10.0}},
            uptime_s=3.5,
            correlation_id="cid-123",
        )
        dumped = snap.model_dump_json()
        restored = MetricsSnapshot.model_validate_json(dumped)
        assert restored.counters["fetch_total"] == 7
        assert restored.distributions["bytes_downloaded"]["sum"] == 10.0
        assert restored.uptime_s == 3.5
        assert restored.correlation_id == "cid-123"

    def test_built_from_registry_snapshot(self) -> None:
        reg = MetricsRegistry()
        reg.incr("fetch_total", value=2)
        reg.observe("bytes_downloaded", 100.0)
        raw = reg.snapshot()
        snap = MetricsSnapshot(
            enabled=bool(raw["enabled"]),
            counters=dict(raw["counters"]),  # type: ignore[arg-type]
            distributions=dict(raw["distributions"]),  # type: ignore[arg-type]
            uptime_s=float(raw["uptime_s"]),  # type: ignore[arg-type]
        )
        assert snap.counters["fetch_total"] == 2
        assert snap.distributions["bytes_downloaded"]["count"] == 1.0


class TestMetricsConfig:
    def test_defaults(self) -> None:
        cfg = MetricsConfig()
        assert cfg.enabled is True
        assert cfg.max_label_cardinality == 200

    def test_wired_into_appconfig(self) -> None:
        assert isinstance(AppConfig().metrics, MetricsConfig)

    def test_env_var_loading(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEB_AGENT_METRICS__ENABLED", "false")
        monkeypatch.setenv("WEB_AGENT_METRICS__MAX_LABEL_CARDINALITY", "500")
        cfg = MetricsConfig()
        assert cfg.enabled is False
        assert cfg.max_label_cardinality == 500

    def test_nested_env_via_appconfig(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WEB_AGENT_METRICS__ENABLED", "false")
        assert AppConfig().metrics.enabled is False

    def test_cardinality_bounds_enforced(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MetricsConfig(max_label_cardinality=0)
        with pytest.raises(ValidationError):
            MetricsConfig(max_label_cardinality=20000)
