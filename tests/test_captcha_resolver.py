"""v1.7.0 Wave 7: CAPTCHA / bot-challenge resolver hook (offline).

Covers the pluggable resolver contract end to end without a browser:

* ``normalize_resolution`` coercion of the lenient return contract;
* ``WebFetcher._attempt_captcha_resolution`` -- the bounded attempt loop
  with AUTHORITATIVE re-detection (the resolver's own verdict is advisory;
  only a fresh ``detect_challenge`` that comes back clean clears the wall),
  exception / timeout isolation, early concede, and metric outcomes;
* Agent constructor + property wiring threading the hook to the fetcher;
* the new ``FetchConfig`` knobs + ``ChallengeInfo`` resolution fields.

The page is a tiny stand-in (only ``.url`` is touched); ``detect_challenge``
and ``safe_page_content`` are monkeypatched so the loop's control flow is
exercised deterministically.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import web_agent.web_fetcher as wf
from pydantic import ValidationError
from web_agent.agent import Agent
from web_agent.captcha import CaptchaContext, CaptchaResolution, normalize_resolution
from web_agent.config import AppConfig
from web_agent.metrics import MetricsRegistry
from web_agent.models import ChallengeInfo
from web_agent.web_fetcher import WebFetcher

# ----------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------


def _fetcher(resolver=None, *, metrics=None, **fetch_overrides) -> WebFetcher:
    cfg = AppConfig(fetch=fetch_overrides) if fetch_overrides else AppConfig()
    return WebFetcher(MagicMock(), cfg, metrics=metrics, captcha_resolver=resolver)


def _wall(**over) -> ChallengeInfo:
    base: dict = {
        "vendor": "cloudflare",
        "kind": "captcha",
        "confidence": 0.95,
        "evidence": ["cf-turnstile"],
        "auto_settle_likely": False,
    }
    base.update(over)
    return ChallengeInfo(**base)


def _page(url: str = "https://blocked.example/path") -> SimpleNamespace:
    return SimpleNamespace(url=url)


def _patch(monkeypatch, *, detect, content=("<html>real</html>", "content")) -> tuple:
    """Patch the fetcher-module detect_challenge + safe_page_content.

    ``detect`` may be a single return value, a list (one per call), or a
    callable. Returns ``(detect_mock, content_mock)`` for assertions.
    """
    if isinstance(detect, list):
        detect_mock = MagicMock(side_effect=list(detect))
    elif callable(detect) and not isinstance(detect, (ChallengeInfo, type(None))):
        detect_mock = MagicMock(side_effect=detect)
    else:
        detect_mock = MagicMock(return_value=detect)
    content_mock = AsyncMock(return_value=content)
    monkeypatch.setattr(wf, "detect_challenge", detect_mock)
    monkeypatch.setattr(wf, "safe_page_content", content_mock)
    return detect_mock, content_mock


def _counters(registry: MetricsRegistry) -> dict:
    snap = registry.snapshot()
    return dict(snap["counters"])  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# normalize_resolution
# ----------------------------------------------------------------------


class TestNormalizeResolution:
    def test_none_is_not_resolved(self) -> None:
        assert normalize_resolution(None) == CaptchaResolution(resolved=False)

    def test_bools(self) -> None:
        assert normalize_resolution(True).resolved is True
        assert normalize_resolution(False).resolved is False

    def test_passthrough_identity(self) -> None:
        obj = CaptchaResolution(resolved=True, detail="d", method="m")
        assert normalize_resolution(obj) is obj

    def test_duck_typed_object(self) -> None:
        duck = SimpleNamespace(resolved=True, detail="solved", method="2captcha")
        out = normalize_resolution(duck)
        assert out.resolved is True
        assert out.detail == "solved"
        assert out.method == "2captcha"

    def test_duck_typed_non_string_detail_dropped(self) -> None:
        duck = SimpleNamespace(resolved=False, detail=123, method=object())
        out = normalize_resolution(duck)
        assert out.resolved is False
        assert out.detail is None
        assert out.method is None

    def test_arbitrary_truthy_falls_back_to_bool(self) -> None:
        assert normalize_resolution("yes").resolved is True
        assert normalize_resolution("").resolved is False
        assert normalize_resolution(0).resolved is False


# ----------------------------------------------------------------------
# _attempt_captcha_resolution -- the core loop
# ----------------------------------------------------------------------


class TestAttemptResolution:
    @pytest.mark.asyncio
    async def test_no_resolver_is_noop(self, monkeypatch) -> None:
        detect_mock, content_mock = _patch(monkeypatch, detect=None)
        f = _fetcher(resolver=None)
        info = _wall()
        out = await f._attempt_captcha_resolution(_page(), "u", info, "h", "content")
        assert out == (info, "h", "content")
        assert out[0] is info  # untouched, not even copied
        detect_mock.assert_not_called()
        content_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_flag_is_noop(self, monkeypatch) -> None:
        detect_mock, _ = _patch(monkeypatch, detect=None)
        f = _fetcher(resolver=AsyncMock(return_value=True), captcha_resolution_enabled=False)
        info = _wall()
        out = await f._attempt_captcha_resolution(_page(), "u", info, "h", "src")
        assert out[0] is info
        detect_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_budget_is_noop(self, monkeypatch) -> None:
        detect_mock, _ = _patch(monkeypatch, detect=None)
        f = _fetcher(resolver=AsyncMock(return_value=True), captcha_max_attempts=0)
        info = _wall()
        out = await f._attempt_captcha_resolution(_page(), "u", info, "h", "src")
        assert out[0] is info
        detect_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_resolver_clears(self, monkeypatch) -> None:
        # Re-detection comes back clean -> wall cleared.
        _patch(monkeypatch, detect=None, content=("real", "content"))
        metrics = MetricsRegistry(enabled=True)
        resolver = AsyncMock(return_value=True)
        f = _fetcher(resolver=resolver, metrics=metrics)
        standing, html, _src = await f._attempt_captcha_resolution(
            _page(), "u", _wall(), "wall-html", "content"
        )
        assert standing is None
        assert html == "real"
        resolver.assert_awaited_once()
        c = _counters(metrics)
        assert c["captcha_resolution_attempt{vendor=cloudflare}"] == 1
        assert c["captcha_resolution_outcome{result=resolved}"] == 1

    @pytest.mark.asyncio
    async def test_sync_resolver_returning_resolution_clears(self, monkeypatch) -> None:
        _patch(monkeypatch, detect=None)
        calls = []

        def resolver(ctx: CaptchaContext) -> CaptchaResolution:
            calls.append(ctx)
            return CaptchaResolution(resolved=True, detail="solved", method="manual")

        f = _fetcher(resolver=resolver)
        standing, _html, _src = await f._attempt_captcha_resolution(
            _page(), "u", _wall(), "h", "content"
        )
        assert standing is None
        assert len(calls) == 1
        assert isinstance(calls[0], CaptchaContext)
        assert calls[0].attempt == 1
        assert calls[0].max_attempts == 2

    @pytest.mark.asyncio
    async def test_sub_threshold_redetection_counts_as_cleared(self, monkeypatch) -> None:
        # A residual weak marker scoring below the action threshold == cleared.
        weak = _wall(confidence=0.5)
        _patch(monkeypatch, detect=weak)
        f = _fetcher(resolver=AsyncMock(return_value=True))
        standing, _h, _s = await f._attempt_captcha_resolution(
            _page(), "u", _wall(), "h", "content"
        )
        assert standing is None

    @pytest.mark.asyncio
    async def test_resolver_concedes_blocks_after_one_attempt(self, monkeypatch) -> None:
        detect_mock, _content_mock = _patch(monkeypatch, detect=_wall())
        metrics = MetricsRegistry(enabled=True)
        resolver = AsyncMock(return_value=False)  # "I cannot solve this"
        f = _fetcher(resolver=resolver, metrics=metrics)
        standing, _h, _s = await f._attempt_captcha_resolution(
            _page(), "u", _wall(), "h", "content"
        )
        assert standing is not None
        assert standing.resolution_attempted is True
        assert standing.resolution_succeeded is False
        resolver.assert_awaited_once()  # conceded -> stop early
        assert detect_mock.call_count == 1
        c = _counters(metrics)
        assert c["captcha_resolution_outcome{result=failed}"] == 1

    @pytest.mark.asyncio
    async def test_resolver_claims_success_but_wall_stands_is_authoritative(
        self, monkeypatch
    ) -> None:
        # THE honesty test: resolver always says "resolved", but detection
        # keeps finding the wall -> still BLOCKED, looped to the budget.
        detect_mock, _ = _patch(monkeypatch, detect=_wall())
        metrics = MetricsRegistry(enabled=True)
        resolver = AsyncMock(return_value=True)
        f = _fetcher(resolver=resolver, metrics=metrics, captcha_max_attempts=2)
        standing, _h, _s = await f._attempt_captcha_resolution(
            _page(), "u", _wall(), "h", "content"
        )
        assert standing is not None
        assert standing.resolution_attempted is True
        assert standing.resolution_succeeded is False
        assert resolver.await_count == 2  # bounded by captcha_max_attempts
        assert detect_mock.call_count == 2
        c = _counters(metrics)
        assert c["captcha_resolution_attempt{vendor=cloudflare}"] == 2
        assert c["captcha_resolution_outcome{result=failed}"] == 1

    @pytest.mark.asyncio
    async def test_max_attempts_respected(self, monkeypatch) -> None:
        _patch(monkeypatch, detect=_wall())
        resolver = AsyncMock(return_value=True)
        f = _fetcher(resolver=resolver, captcha_max_attempts=4)
        await f._attempt_captcha_resolution(_page(), "u", _wall(), "h", "content")
        assert resolver.await_count == 4

    @pytest.mark.asyncio
    async def test_clears_on_second_attempt(self, monkeypatch) -> None:
        # Wall on attempt 1's re-detect, clear on attempt 2's.
        detect_mock, _ = _patch(monkeypatch, detect=[_wall(), None])
        resolver = AsyncMock(return_value=True)
        f = _fetcher(resolver=resolver, captcha_max_attempts=3)
        standing, _h, _s = await f._attempt_captcha_resolution(
            _page(), "u", _wall(), "h", "content"
        )
        assert standing is None
        assert resolver.await_count == 2
        assert detect_mock.call_count == 2

    @pytest.mark.asyncio
    async def test_resolver_exception_is_isolated(self, monkeypatch) -> None:
        detect_mock, content_mock = _patch(monkeypatch, detect=None)
        metrics = MetricsRegistry(enabled=True)
        resolver = AsyncMock(side_effect=RuntimeError("boom"))
        f = _fetcher(resolver=resolver, metrics=metrics)
        standing, _h, _s = await f._attempt_captcha_resolution(
            _page(), "u", _wall(), "h", "content"
        )
        assert standing is not None
        assert standing.resolution_attempted is True
        assert standing.resolution_succeeded is False
        detect_mock.assert_not_called()  # broke before re-detection
        content_mock.assert_not_called()
        c = _counters(metrics)
        assert c["captcha_resolution_outcome{result=error}"] == 1

    @pytest.mark.asyncio
    async def test_async_resolver_timeout_is_isolated(self, monkeypatch) -> None:
        detect_mock, _ = _patch(monkeypatch, detect=None)
        metrics = MetricsRegistry(enabled=True)
        calls: list[int] = []

        async def slow(ctx: CaptchaContext) -> bool:
            calls.append(1)
            await asyncio.sleep(10)  # cancelled by wait_for ~immediately
            return True

        f = _fetcher(resolver=slow, metrics=metrics, captcha_attempt_timeout_s=0.01)
        standing, _h, _s = await f._attempt_captcha_resolution(
            _page(), "u", _wall(), "h", "content"
        )
        assert standing is not None
        assert standing.resolution_succeeded is False
        assert len(calls) == 1
        detect_mock.assert_not_called()
        c = _counters(metrics)
        assert c["captcha_resolution_outcome{result=timeout}"] == 1

    @pytest.mark.asyncio
    async def test_builtin_timeout_error_classified_as_timeout(self, monkeypatch) -> None:
        # A resolver that self-raises the BUILTIN TimeoutError (distinct from
        # asyncio.TimeoutError on Python 3.10) is classified as timeout, not error.
        detect_mock, _ = _patch(monkeypatch, detect=None)
        metrics = MetricsRegistry(enabled=True)
        resolver = AsyncMock(side_effect=TimeoutError("solver too slow"))
        f = _fetcher(resolver=resolver, metrics=metrics)
        standing, _h, _s = await f._attempt_captcha_resolution(
            _page(), "u", _wall(), "h", "content"
        )
        assert standing is not None
        assert standing.resolution_succeeded is False
        detect_mock.assert_not_called()
        c = _counters(metrics)
        assert c["captcha_resolution_outcome{result=timeout}"] == 1

    @pytest.mark.asyncio
    async def test_slow_sync_resolver_warns(self, monkeypatch) -> None:
        # A synchronous hook that overruns the budget can't be interrupted,
        # but it is measured and a warning is emitted after the fact.
        import time as _time

        _patch(monkeypatch, detect=None)
        warn = MagicMock()
        monkeypatch.setattr(wf.logger, "warning", warn)

        def slow(ctx: CaptchaContext) -> bool:
            _time.sleep(0.02)
            return True

        f = _fetcher(resolver=slow, captcha_attempt_timeout_s=0.01)
        standing, _h, _s = await f._attempt_captcha_resolution(
            _page(), "u", _wall(), "h", "content"
        )
        assert standing is None  # detect=None -> cleared
        assert warn.called
        assert "blocked the event loop" in warn.call_args[0][0]

    @pytest.mark.asyncio
    async def test_fast_sync_resolver_does_not_warn(self, monkeypatch) -> None:
        _patch(monkeypatch, detect=None)
        warn = MagicMock()
        monkeypatch.setattr(wf.logger, "warning", warn)
        f = _fetcher(resolver=lambda ctx: True, captcha_attempt_timeout_s=60.0)
        await f._attempt_captcha_resolution(_page(), "u", _wall(), "h", "content")
        warn.assert_not_called()

    @pytest.mark.asyncio
    async def test_recapture_failure_is_isolated(self, monkeypatch) -> None:
        detect_mock = MagicMock(return_value=None)
        content_mock = AsyncMock(side_effect=RuntimeError("page gone"))
        monkeypatch.setattr(wf, "detect_challenge", detect_mock)
        monkeypatch.setattr(wf, "safe_page_content", content_mock)
        metrics = MetricsRegistry(enabled=True)
        f = _fetcher(resolver=AsyncMock(return_value=True), metrics=metrics)
        standing, _h, _s = await f._attempt_captcha_resolution(
            _page(), "u", _wall(), "h", "content"
        )
        assert standing is not None
        assert standing.resolution_succeeded is False
        detect_mock.assert_not_called()  # never reached re-detection
        c = _counters(metrics)
        assert c["captcha_resolution_outcome{result=error}"] == 1

    @pytest.mark.asyncio
    async def test_context_carries_challenge_and_urls(self, monkeypatch) -> None:
        _patch(monkeypatch, detect=None)
        seen: list[CaptchaContext] = []

        async def resolver(ctx: CaptchaContext) -> bool:
            seen.append(ctx)
            return True

        f = _fetcher(resolver=resolver)
        page = _page("https://chl.example/cdn-cgi/challenge")
        await f._attempt_captcha_resolution(page, "https://orig.example", _wall(), "h", "content")
        assert seen[0].page is page
        assert seen[0].url == "https://orig.example"
        assert seen[0].final_url == "https://chl.example/cdn-cgi/challenge"
        assert seen[0].challenge.vendor == "cloudflare"


# ----------------------------------------------------------------------
# _mark_resolution
# ----------------------------------------------------------------------


class TestMarkResolution:
    def test_stamps_copy_not_original(self) -> None:
        info = _wall()
        out = WebFetcher._mark_resolution(info, succeeded=True)
        assert out.resolution_attempted is True
        assert out.resolution_succeeded is True
        # original untouched
        assert info.resolution_attempted is False
        assert info.resolution_succeeded is False

    def test_failed_stamp(self) -> None:
        out = WebFetcher._mark_resolution(_wall(), succeeded=False)
        assert out.resolution_attempted is True
        assert out.resolution_succeeded is False


# ----------------------------------------------------------------------
# Agent wiring
# ----------------------------------------------------------------------


class TestAgentWiring:
    def test_constructor_threads_to_fetcher(self) -> None:
        def hook(ctx):
            return True

        agent = Agent(captcha_resolver=hook)
        assert agent.captcha_resolver is hook
        assert agent._fetcher.captcha_resolver is hook

    def test_default_is_none(self) -> None:
        agent = Agent()
        assert agent.captcha_resolver is None
        assert agent._fetcher.captcha_resolver is None

    def test_setter_updates_fetcher(self) -> None:
        agent = Agent()

        def hook(ctx):
            return True

        agent.captcha_resolver = hook
        assert agent.captcha_resolver is hook
        assert agent._fetcher.captcha_resolver is hook
        agent.captcha_resolver = None
        assert agent._fetcher.captcha_resolver is None


# ----------------------------------------------------------------------
# Config + model surface
# ----------------------------------------------------------------------


class TestConfigAndModel:
    def test_fetch_defaults(self) -> None:
        fc = AppConfig().fetch
        assert fc.captcha_resolution_enabled is True
        assert fc.captcha_max_attempts == 2
        assert fc.captcha_attempt_timeout_s == 60.0

    def test_max_attempts_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            AppConfig(fetch={"captcha_max_attempts": 6})

    def test_max_attempts_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            AppConfig(fetch={"captcha_max_attempts": -1})

    def test_timeout_bounds(self) -> None:
        with pytest.raises(ValidationError):
            AppConfig(fetch={"captcha_attempt_timeout_s": -1.0})
        with pytest.raises(ValidationError):
            AppConfig(fetch={"captcha_attempt_timeout_s": 601.0})

    def test_challenge_info_resolution_fields_default_false(self) -> None:
        ci = _wall()
        assert ci.resolution_attempted is False
        assert ci.resolution_succeeded is False
        dumped = ci.model_dump()
        assert dumped["resolution_attempted"] is False
        assert dumped["resolution_succeeded"] is False
