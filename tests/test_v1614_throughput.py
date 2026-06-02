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


# ----------------------------------------------------------------------
# H6: WebFetcher.fetch_many isolates per-url failures
# ----------------------------------------------------------------------


class TestFetchManyExceptionIsolation:
    """H6: a single ``self.fetch`` raising must not abort the whole batch.

    Pre-fix, ``fetch_many`` ended with a bare
    ``asyncio.gather(*tasks)`` (no ``return_exceptions=True``), so one
    raising fetch propagated out of gather -- losing every sibling
    result and orphaning their in-flight pages. The fix mirrors
    ``Recipes.web_research``: gather with ``return_exceptions=True``,
    re-raise ``CancelledError``, and degrade any other exception to an
    error ``FetchResult`` for that url.
    """

    @pytest.mark.asyncio
    async def test_fetch_many_isolates_single_url_failure(self) -> None:
        """One url's ``fetch`` raises; the other two return normal
        results. ``fetch_many`` must still return 3 results IN ORDER,
        the raising url yielding an error ``FetchResult`` (not an
        exception), with the siblings intact.
        """
        from web_agent.config import AppConfig
        from web_agent.correlation import correlation_scope
        from web_agent.models import FetchResult, FetchStatus
        from web_agent.web_fetcher import WebFetcher

        fetcher = WebFetcher(browser_manager=MagicMock(), config=AppConfig())

        u1 = "https://example.com/ok-1"
        u2 = "https://example.com/boom"
        u3 = "https://example.com/ok-3"

        async def _fake_fetch(url: str, session_id: str | None = None) -> FetchResult:
            if url == u2:
                raise RuntimeError("simulated unexpected playwright failure")
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.SUCCESS,
                html="<html/>",
            )

        fetcher.fetch = AsyncMock(side_effect=_fake_fetch)  # type: ignore[method-assign]

        # Ephemeral path (session_id=None) -- the simplest of the two
        # branches; index alignment is identical on the session path.
        # Run inside a correlation scope so we can assert the error result
        # threads the active correlation id (via get_correlation_id()),
        # matching how fetch()'s own error returns are tagged.
        with correlation_scope("test-cid-h6"):
            results = await fetcher.fetch_many([u1, u2, u3])

        # The batch survived: 3 results, same order as input.
        assert len(results) == 3
        assert all(isinstance(r, FetchResult) for r in results), (
            "every entry must be a FetchResult -- exceptions must be "
            "converted, never returned raw or allowed to abort gather"
        )
        assert [r.url for r in results] == [u1, u2, u3], "order must match input urls"

        # The two healthy urls are untouched successes.
        assert results[0].status == FetchStatus.SUCCESS
        assert results[0].html == "<html/>"
        assert results[2].status == FetchStatus.SUCCESS
        assert results[2].html == "<html/>"

        # The raising url degraded to an error result (matching fetch()'s
        # established error shape) -- NOT a success, NOT an exception.
        failed = results[1]
        assert failed.url == u2
        assert failed.final_url == u2
        assert failed.status == FetchStatus.NETWORK_ERROR
        assert failed.html is None
        assert "simulated unexpected playwright failure" in (failed.error_message or "")
        # correlation_id is wired from get_correlation_id() -- here the
        # active scope id -- exactly like fetch()'s own error returns.
        assert failed.correlation_id == "test-cid-h6"

    @pytest.mark.asyncio
    async def test_fetch_many_session_path_isolates_single_failure(self) -> None:
        """Same isolation guarantee on the SESSION path (the branch that
        also wraps each fetch in the per-call semaphore). Confirms the
        zip-back alignment holds when the gated wrapper is in play.
        """
        from web_agent.config import AppConfig
        from web_agent.models import FetchResult, FetchStatus
        from web_agent.web_fetcher import WebFetcher

        fetcher = WebFetcher(browser_manager=MagicMock(), config=AppConfig())

        urls = [f"https://example.com/s{i}" for i in range(4)]
        bad = urls[1]

        async def _fake_fetch(url: str, session_id: str | None = None) -> FetchResult:
            assert session_id == "sid-X"
            if url == bad:
                raise ValueError("boom-session")
            return FetchResult(
                url=url, final_url=url, status=FetchStatus.SUCCESS, html="<html/>"
            )

        fetcher.fetch = AsyncMock(side_effect=_fake_fetch)  # type: ignore[method-assign]

        results = await fetcher.fetch_many(urls, session_id="sid-X")

        assert [r.url for r in results] == urls, "order preserved on session path"
        assert results[1].status == FetchStatus.NETWORK_ERROR
        assert "boom-session" in (results[1].error_message or "")
        # All others succeeded.
        for i in (0, 2, 3):
            assert results[i].status == FetchStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_fetch_many_reraises_cancelled_error(self) -> None:
        """When ``self.fetch`` raises ``asyncio.CancelledError``,
        ``fetch_many`` must RE-RAISE it (cancellation propagates) and
        must NOT swallow it into an error FetchResult -- exactly like
        ``Recipes.web_research``.
        """
        from web_agent.config import AppConfig
        from web_agent.models import FetchResult, FetchStatus
        from web_agent.web_fetcher import WebFetcher

        fetcher = WebFetcher(browser_manager=MagicMock(), config=AppConfig())

        async def _fake_fetch(url: str, session_id: str | None = None) -> FetchResult:
            if url.endswith("cancel"):
                raise asyncio.CancelledError()
            return FetchResult(
                url=url, final_url=url, status=FetchStatus.SUCCESS, html="<html/>"
            )

        fetcher.fetch = AsyncMock(side_effect=_fake_fetch)  # type: ignore[method-assign]

        urls = [
            "https://example.com/ok",
            "https://example.com/please-cancel",
            "https://example.com/ok-2",
        ]

        with pytest.raises(asyncio.CancelledError):
            await fetcher.fetch_many(urls)


# ----------------------------------------------------------------------
# L1: classify_url logs (and stays safe) when the redirect hook blocks
# ----------------------------------------------------------------------


class TestClassifyUrlBlockedRedirectObservability:
    """L1: a HEAD-probe redirect blocked by ``_check_redirect`` (an SSRF
    attempt) must be logged at WARNING -- not swallowed silently at
    DEBUG -- while still returning the safe ``'unknown'`` answer.

    Scaffolding is light and honest: we inject an ``httpx.MockTransport``
    (preserving classify_url's real ``event_hooks``) so the REAL
    ``_check_redirect`` hook fires on a simulated 302 to a denied host
    and raises the REAL ``NavigationError`` that the new ``except``
    clause handles. No browser, no live network.
    """

    @pytest.mark.asyncio
    async def test_classify_url_warns_on_blocked_redirect(self) -> None:
        import functools

        import httpx
        import web_agent.web_fetcher as wf_module
        from loguru import logger
        from web_agent.config import AppConfig
        from web_agent.web_fetcher import WebFetcher

        config = AppConfig()
        # Defaults already enable probe_binary_urls + block_private_ips,
        # but assert the preconditions so the test fails loudly if a
        # default ever changes underneath it.
        assert config.safety.probe_binary_urls is True
        assert config.safety.block_private_ips is True

        fetcher = WebFetcher(browser_manager=MagicMock(), config=config)

        # MockTransport returns a 302 whose Location points at a denied
        # (link-local / private) host. follow_redirects=True makes httpx
        # fire the response event hook on this 302 before following it;
        # classify_url's _check_redirect hook then raises NavigationError.
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "http://169.254.169.254/admin"})

        transport = httpx.MockTransport(_handler)

        # Inject the transport while preserving every kwarg classify_url
        # passes to AsyncClient (follow_redirects / event_hooks / headers
        # / cookies / timeout). functools.partial keeps the real hook
        # wiring intact -- we only swap the underlying transport.
        patched_client = functools.partial(httpx.AsyncClient, transport=transport)

        # Capture loguru WARNING records (this suite has no caplog<->loguru
        # bridge, so add a scoped sink and tear it down afterward).
        records: list[str] = []
        sink_id = logger.add(
            lambda msg: records.append(msg),
            level="WARNING",
            format="{level}|{message}",
        )
        original_client = wf_module.httpx.AsyncClient
        try:
            wf_module.httpx.AsyncClient = patched_client  # type: ignore[misc]
            result = await fetcher.classify_url("https://example.com/extensionless-doc")
        finally:
            wf_module.httpx.AsyncClient = original_client  # type: ignore[misc]
            logger.remove(sink_id)

        # Safe behavior preserved: a blocked redirect classifies as unknown.
        assert result == "unknown"

        # Observability improved: exactly one WARNING mentioning the url
        # was emitted for the blocked redirect (not a silent DEBUG).
        warning_msgs = [r for r in records if r.startswith("WARNING|")]
        assert warning_msgs, "blocked redirect must log at WARNING, not be swallowed at DEBUG"
        assert any("example.com/extensionless-doc" in r for r in warning_msgs), (
            f"WARNING should name the probed url; got {warning_msgs!r}"
        )
