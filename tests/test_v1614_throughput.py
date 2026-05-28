"""v1.6.14 hardening: DoS / throughput cluster.

Tests for the three throughput-cluster fixes:

* **C-1** -- :meth:`web_agent.rate_limiter.RateLimiter.notify_429` caps
  the per-event delay at ``MAX_RETRY_AFTER_SECONDS`` so a hostile or
  misconfigured ``Retry-After`` cannot wedge the agent for days.
* **C-4** -- :meth:`web_agent.web_fetcher.WebFetcher.fetch_many` gates
  concurrent page creation with
  ``BrowserConfig.max_pages_per_session_fetch`` when a ``session_id``
  is supplied, preventing Chromium-renderer crashes under high
  parallelism within a single BrowserContext.
* **C-7** -- :meth:`web_agent.network_collector.NetworkCollector.wait_for_pending_bodies`
  explicitly cancels body-capture tasks that don't finish inside the
  timeout, so they can't keep running orphaned against a closed Page.

Pattern follows ``tests/test_agent.py::TestV1613Integration`` --
AsyncMock-driven, no real Playwright / network I/O.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

# ----------------------------------------------------------------------
# C-1: RateLimiter.notify_429 caps extreme Retry-After
# ----------------------------------------------------------------------


class TestNotify429Cap:
    """v1.6.14 C-1: ``MAX_RETRY_AFTER_SECONDS`` ceiling on delays."""

    @pytest.mark.asyncio
    async def test_notify_429_caps_extreme_retry_after(self) -> None:
        """``Retry-After: 99999999`` (~1157 days) gets clamped to the cap.

        Without the v1.6.14 cap, a server returning a wild ``Retry-After``
        would block the next ``acquire(host)`` call for days. The clamp
        keeps the worst-case sleep at ``MAX_RETRY_AFTER_SECONDS`` (5 min).
        """
        from web_agent.rate_limiter import RateLimiter

        rl = RateLimiter(rps_per_host=10.0)
        # Snapshot the cap (it's a class constant we read for the bound).
        cap = RateLimiter.MAX_RETRY_AFTER_SECONDS

        before = time.monotonic()
        rl.notify_429("example.com", retry_after_seconds=99999999.0)
        next_allowed = rl._next_allowed["example.com"]
        elapsed = next_allowed - before

        assert elapsed > 0, "next_allowed should be in the future"
        # Tolerance covers execution-time variance between the
        # ``before = time.monotonic()`` snapshot and the
        # ``time.monotonic()`` call inside ``notify_429``. 1s is
        # comfortably above any realistic scheduling jitter.
        assert elapsed <= cap + 1.0, (
            f"delay should be <= {cap}s (MAX_RETRY_AFTER_SECONDS) + 1s tolerance, "
            f"got {elapsed:.3f}s"
        )

    @pytest.mark.asyncio
    async def test_notify_429_caps_extreme_retry_after_none(self) -> None:
        """``retry_after_seconds=None`` (no header): fallback path is also capped.

        With ``rps_per_host`` low enough that ``interval * fallback_factor``
        could exceed the cap, the clamp still fires. We use a very
        small rps so the fallback (``interval = 1/rps``) is huge.
        """
        from web_agent.rate_limiter import RateLimiter

        # interval = 1/0.001 = 1000s, * fallback_factor=2.0 => 2000s
        # which is well above the 300s cap.
        rl = RateLimiter(rps_per_host=0.001)
        cap = RateLimiter.MAX_RETRY_AFTER_SECONDS

        before = time.monotonic()
        rl.notify_429("example.com", retry_after_seconds=None)
        next_allowed = rl._next_allowed["example.com"]
        elapsed = next_allowed - before

        assert elapsed > 0
        assert elapsed <= cap + 1.0, (
            f"fallback delay should also be capped at {cap}s, got {elapsed:.3f}s"
        )

    @pytest.mark.asyncio
    async def test_notify_429_normal_retry_after_unaffected(self) -> None:
        """A normal 30s Retry-After must pass through unchanged.

        Defends against an over-eager cap that throttles legitimate
        backoff signals; the clamp should only fire on outliers.
        """
        from web_agent.rate_limiter import RateLimiter

        rl = RateLimiter(rps_per_host=10.0)

        before = time.monotonic()
        rl.notify_429("example.com", retry_after_seconds=30.0)
        next_allowed = rl._next_allowed["example.com"]
        elapsed = next_allowed - before

        # Approximately 30s -- tolerance covers monotonic-clock jitter
        # between our snapshot and notify_429's internal snapshot.
        assert 29.0 <= elapsed <= 31.0, (
            f"30s Retry-After should round-trip unchanged; got {elapsed:.3f}s"
        )


# ----------------------------------------------------------------------
# C-4: WebFetcher.fetch_many bounds session-path parallelism
# ----------------------------------------------------------------------


class TestFetchManySessionSemaphore:
    """v1.6.14 C-4: ``fetch_many`` with ``session_id`` gates concurrency."""

    @pytest.mark.asyncio
    async def test_fetch_many_session_bounded_by_semaphore(self) -> None:
        """With ``max_pages_per_session_fetch=3`` and 10 URLs, observed
        max parallel ``fetch`` invocations must be <= 3.

        Without the C-4 gate, ``fetch_many`` would issue
        ``len(urls)``-way concurrent ``self.fetch()`` calls and the
        observed parallelism would equal the URL count.
        """
        from web_agent.config import AppConfig
        from web_agent.web_fetcher import WebFetcher

        config = AppConfig()
        # Set the cap to 3 so we can see the gate in action.
        config.browser.max_pages_per_session_fetch = 3
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)

        in_flight = 0
        max_in_flight = 0
        lock = asyncio.Lock()

        async def _fake_fetch(url: str, session_id: str | None = None) -> MagicMock:
            nonlocal in_flight, max_in_flight
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            # Hold the slot long enough for additional fetches to attempt
            # entry; 50ms is small but reliably enough for the scheduler
            # to attempt to launch all 10 tasks before any finishes.
            try:
                await asyncio.sleep(0.05)
            finally:
                async with lock:
                    in_flight -= 1
            return MagicMock()  # placeholder FetchResult

        # Patch the bound method on the instance so the gating logic in
        # ``fetch_many`` is exercised but ``self.fetch`` is a no-op.
        fetcher.fetch = AsyncMock(side_effect=_fake_fetch)  # type: ignore[method-assign]

        urls = [f"https://example.com/page{i}" for i in range(10)]
        results = await fetcher.fetch_many(urls, session_id="sid-1")

        assert len(results) == 10, "all URLs should produce a result"
        assert max_in_flight <= 3, (
            f"max_pages_per_session_fetch=3 should cap concurrency at 3, "
            f"observed max_in_flight={max_in_flight}"
        )
        # And the gate should actually have fired at least once -- with
        # 10 URLs and a 50ms hold time the cap is reached every time.
        assert max_in_flight >= 2, (
            f"expected the semaphore to be exercised (max_in_flight >= 2), "
            f"got {max_in_flight}; the gating logic may not be on the "
            f"hot path"
        )

    @pytest.mark.asyncio
    async def test_fetch_many_ephemeral_path_does_not_apply_session_gate(self) -> None:
        """When ``session_id=None``, ``fetch_many`` must NOT impose the
        ``max_pages_per_session_fetch`` cap -- the ephemeral path is
        already gated by ``BrowserManager._semaphore`` (``max_contexts``)
        and adding another layer would silently halve effective
        concurrency.
        """
        from web_agent.config import AppConfig
        from web_agent.web_fetcher import WebFetcher

        config = AppConfig()
        # Set the session cap LOW so its absence is visible if the
        # ephemeral path mistakenly applies it.
        config.browser.max_pages_per_session_fetch = 1
        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)

        in_flight = 0
        max_in_flight = 0
        lock = asyncio.Lock()

        async def _fake_fetch(url: str, session_id: str | None = None) -> MagicMock:
            nonlocal in_flight, max_in_flight
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.05)
            finally:
                async with lock:
                    in_flight -= 1
            return MagicMock()

        fetcher.fetch = AsyncMock(side_effect=_fake_fetch)  # type: ignore[method-assign]

        urls = [f"https://example.com/p{i}" for i in range(5)]
        results = await fetcher.fetch_many(urls, session_id=None)

        assert len(results) == 5
        # With session_id=None and a 1-wide session cap, if the cap
        # WERE applied we'd see max_in_flight == 1. We expect strictly
        # higher -- the ephemeral path issues all 5 fetches in parallel
        # at this layer (the real cap lives in BrowserManager which is
        # a MagicMock here).
        assert max_in_flight > 1, (
            f"ephemeral path should not be gated by "
            f"max_pages_per_session_fetch; observed max_in_flight="
            f"{max_in_flight}"
        )


# ----------------------------------------------------------------------
# C-7: wait_for_pending_bodies cancels orphans on timeout
# ----------------------------------------------------------------------


class TestWaitForPendingBodiesCancel:
    """v1.6.14 C-7: orphan-task cancellation on timeout."""

    @pytest.mark.asyncio
    async def test_wait_for_pending_bodies_cancels_orphaned_tasks(self) -> None:
        """A body-capture task that doesn't finish inside the timeout
        is explicitly cancelled instead of being left to run orphaned.

        Without the C-7 fix, ``asyncio.wait_for`` cancelled the
        ``gather`` wrapper but ``gather(return_exceptions=True)``
        swallowed the cancellation -- the child tasks kept running.
        """
        from web_agent.config import DiagnosticsConfig
        from web_agent.network_collector import NetworkCollector

        diag = DiagnosticsConfig(
            capture_network=True,
            capture_response_bodies=True,
        )
        collector = NetworkCollector(diag)

        # Long-running stand-in for a stuck body read. 10s is comfortably
        # longer than any test scheduler delay; it's effectively
        # "infinite" relative to the 0.1s wait timeout below.
        async def _long_running() -> None:
            await asyncio.sleep(10.0)

        task = asyncio.create_task(_long_running())
        collector._pending_body_captures.add(task)
        # Register the same done callback the real code path uses so
        # the set stays consistent after cancellation lands.
        task.add_done_callback(collector._pending_body_captures.discard)

        await collector.wait_for_pending_bodies(timeout=0.1)

        # The task must be done (cancelled), not still running. We use
        # ``task.done()`` rather than asserting equality with
        # ``task.cancelled()`` because, post-cancel-drain, the task is
        # in "cancelled" state -- but the contract that matters for the
        # fix is "no longer running".
        assert task.done(), (
            "orphaned body-capture task must be done after wait_for_pending_bodies returns"
        )
        assert task.cancelled(), (
            "orphaned task should be cancelled, not still running; this is the C-7 contract"
        )

    @pytest.mark.asyncio
    async def test_wait_for_pending_bodies_no_op_when_empty(self) -> None:
        """Empty ``_pending_body_captures`` -> early return with no work.

        Defends the fast path -- the new code does extra work
        (``asyncio.wait`` + cancel-drain) which we don't want to fire
        when there's nothing to drain.
        """
        from web_agent.config import DiagnosticsConfig
        from web_agent.network_collector import NetworkCollector

        diag = DiagnosticsConfig(capture_response_bodies=True)
        collector = NetworkCollector(diag)

        # Empty set: should return immediately with no exceptions.
        assert not collector._pending_body_captures
        start = time.monotonic()
        await collector.wait_for_pending_bodies(timeout=5.0)
        elapsed = time.monotonic() - start
        # The no-op path is just a truthiness check + return; well under
        # 50ms even on a slow CI.
        assert elapsed < 0.05, f"empty-set path should be near-instant, got {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_wait_for_pending_bodies_drains_completed_tasks(self) -> None:
        """Tasks that complete inside the timeout are awaited normally;
        no cancellation is triggered. Sanity check that the C-7 fix
        didn't break the happy path."""
        from web_agent.config import DiagnosticsConfig
        from web_agent.network_collector import NetworkCollector

        diag = DiagnosticsConfig(capture_response_bodies=True)
        collector = NetworkCollector(diag)

        async def _quick() -> None:
            await asyncio.sleep(0.01)

        task = asyncio.create_task(_quick())
        collector._pending_body_captures.add(task)
        task.add_done_callback(collector._pending_body_captures.discard)

        await collector.wait_for_pending_bodies(timeout=1.0)

        assert task.done()
        # Crucially: NOT cancelled -- it finished on its own.
        assert not task.cancelled(), "tasks that finish inside the timeout must not be cancelled"
