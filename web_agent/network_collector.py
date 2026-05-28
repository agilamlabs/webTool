"""v1.6.8: Per-Page network event collector.

Single ``NetworkCollector`` instance lives per ``Agent``. Every page-creation
site (``BrowserManager.new_page``, ``SessionManager.create``,
``TabManager.new_tab``, ``BrowserActions.execute_sequence``, ``WebFetcher``)
calls :meth:`NetworkCollector.attach` immediately after the Page is born.
``TabManager._on_new_page`` also calls ``attach`` so popups/target=_blank
pages inherit capture automatically.

Storage uses ``WeakKeyDictionary[Page, deque]`` so closed pages auto-evict
(mirroring the v1.6.6 pattern in ``tab_manager._reverse`` and
``browser_actions._PAGE_DIALOG_STATES``). The deque carries a ``maxlen``
matching ``DiagnosticsConfig.max_network_events`` so retention is bounded
mid-stream rather than at read time.

Everything is **off by default** -- when both ``capture_network`` and
``capture_download_intents`` are False, :meth:`attach` is a no-op so this
module contributes zero overhead to existing call paths.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import time
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary, WeakSet

from loguru import logger

from .correlation import get_correlation_id
from .models import NetworkEvent

if TYPE_CHECKING:  # pragma: no cover -- runtime types come from Playwright
    from .config import DiagnosticsConfig


class NetworkCollector:
    """Per-Page request/response/requestfailed + download-intent collector.

    Args:
        diag: Live ``DiagnosticsConfig``. The collector reads
            ``capture_network``, ``capture_download_intents``,
            ``max_network_events``, ``network_resource_types``,
            ``include_request_headers``, and ``include_response_headers``.
            Changes to the config after construction are honoured on the
            next :meth:`attach` call (existing attachments retain whatever
            settings were live when they were registered).
    """

    def __init__(self, diag: DiagnosticsConfig) -> None:
        self._diag = diag
        # WeakKeyDictionary: closed Pages drop out automatically, so we
        # don't need a separate detach path -- Playwright's Page.close()
        # is the only signal we need.
        self._events: WeakKeyDictionary[Any, collections.deque[NetworkEvent]] = WeakKeyDictionary()
        self._download_intents: WeakKeyDictionary[Any, list[str]] = WeakKeyDictionary()
        # Tracks which Pages we've already wired hooks for; without it,
        # repeated attach() calls (from e.g. TabManager and SessionManager
        # racing the same initial page) would register duplicate listeners.
        self._attached: WeakSet[Any] = WeakSet()
        # request.url -> monotonic start time for timing_ms calculation.
        # WeakKeyDictionary outer + plain dict inner because Request objects
        # are cheap and short-lived; we wipe inner dict on Page close via
        # the WeakKeyDictionary parent.
        self._req_start: WeakKeyDictionary[Any, dict[str, float]] = WeakKeyDictionary()
        # v1.6.8 (review C-2 fix): strong references for fire-and-forget
        # tmpfile-cleanup tasks. asyncio.create_task returns a Task whose
        # only reference here would be the inner local in _on_download --
        # CPython's GC may collect it before .delete() completes, defeating
        # the cleanup. We keep the Task alive until it finishes, then
        # discard it via add_done_callback. See:
        # https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
        self._pending_deletes: set[asyncio.Task[None]] = set()
        # v1.6.12: same strong-reference pattern for async body-capture
        # tasks scheduled from the (sync) ``_on_response`` handler.
        # Callers needing bodies must drain via
        # :meth:`wait_for_pending_bodies` before snapshotting events.
        self._pending_body_captures: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when at least one capture switch is on."""
        return bool(self._diag.capture_network or self._diag.capture_download_intents)

    def attach(self, page: Any) -> None:
        """Register network + download listeners on *page*.

        Idempotent: calling twice for the same Page is a no-op. Safe to
        call from multiple page-creation sites; the first call wins.

        When both ``capture_network`` and ``capture_download_intents`` are
        False, this method returns immediately without touching the Page
        -- the only cost is the membership check.
        """
        if not self.enabled:
            return
        if page in self._attached:
            return
        self._attached.add(page)

        if self._diag.capture_network:
            # collections.deque(maxlen=N) gives O(1) eviction when the
            # cap is exceeded -- preferable to ``list[:max]`` which only
            # bounds at read time and lets memory creep.
            self._events[page] = collections.deque(maxlen=self._diag.max_network_events)
            self._req_start[page] = {}

            # Capture references explicitly so the lambdas don't close
            # over a loop variable.
            def _on_request(req: Any, _p: Any = page) -> None:
                self._on_request(_p, req)

            def _on_response(resp: Any, _p: Any = page) -> None:
                self._on_response(_p, resp)

            def _on_failed(req: Any, _p: Any = page) -> None:
                self._on_failed(_p, req)

            page.on("request", _on_request)
            page.on("response", _on_response)
            page.on("requestfailed", _on_failed)

        if self._diag.capture_download_intents:
            self._download_intents[page] = []

            def _on_download(dl: Any, _p: Any = page) -> None:
                self._on_download(_p, dl)

            page.on("download", _on_download)

    def events_for(self, page: Any) -> list[NetworkEvent]:
        """Snapshot of captured events for *page*.

        Always returns a fresh list -- safe to mutate; the underlying
        deque is unaffected. Empty list when capture is off or the Page
        has been garbage-collected.
        """
        return list(self._events.get(page) or ())

    def api_candidates_for(self, page: Any) -> list[str]:
        """Filter ``events_for(page)`` for XHR/fetch JSON responses.

        De-duplicated, order-preserving. Matches "json" anywhere in the
        Content-Type (covers ``application/json``,
        ``application/ld+json``, ``application/vnd.api+json``, etc.).
        """
        out: list[str] = []
        for evt in self._events.get(page) or ():
            if (
                evt.event_type == "response"
                and evt.resource_type in {"xhr", "fetch"}
                and evt.content_type
                and "json" in evt.content_type.lower()
            ):
                out.append(evt.url)
        # De-duplicate while preserving first-seen order.
        seen: set[str] = set()
        deduped: list[str] = []
        for u in out:
            if u not in seen:
                seen.add(u)
                deduped.append(u)
        return deduped

    def download_intents_for(self, page: Any) -> list[str]:
        """URLs of downloads triggered by *page* (captured via ``page.on('download')``)."""
        return list(self._download_intents.get(page) or ())

    def clear(self, page: Any) -> None:
        """Discard all collected state for *page*.

        Mostly useful between iterations of a long-running session
        where the caller wants fresh per-iteration diagnostics. The
        weak-ref-based eviction makes this optional in normal flows.
        """
        with contextlib.suppress(KeyError):
            self._events.pop(page)
        with contextlib.suppress(KeyError):
            self._download_intents.pop(page)
        with contextlib.suppress(KeyError):
            self._req_start.pop(page)
        # WeakSet has no `pop(key)`; discard is the idiomatic remove-if-present.
        self._attached.discard(page)

    # ------------------------------------------------------------------
    # event handlers (internal)
    # ------------------------------------------------------------------

    def _on_request(self, page: Any, req: Any) -> None:
        try:
            rtype = req.resource_type
            if self._diag.network_resource_types and rtype not in self._diag.network_resource_types:
                return
            self._req_start.setdefault(page, {})[req.url] = time.monotonic()
            headers = dict(req.headers) if self._diag.include_request_headers else {}
            evt = NetworkEvent(
                event_type="request",
                url=req.url,
                method=req.method,
                resource_type=rtype,
                request_headers=headers,
                correlation_id=get_correlation_id(),
            )
            buf = self._events.get(page)
            if buf is not None:
                buf.append(evt)
        except Exception as exc:  # pragma: no cover -- defensive
            logger.debug("NetworkCollector._on_request swallowed: {e}", e=exc)

    def _on_response(self, page: Any, resp: Any) -> None:
        try:
            req = resp.request
            rtype = req.resource_type
            if self._diag.network_resource_types and rtype not in self._diag.network_resource_types:
                return
            start = self._req_start.get(page, {}).pop(req.url, None)
            timing_ms = (time.monotonic() - start) * 1000.0 if start else 0.0
            headers: dict[str, str] = {}
            ctype: str | None = None
            try:
                if self._diag.include_response_headers:
                    headers = dict(resp.headers)
                # content-type is cheap to extract regardless of the
                # include_response_headers flag -- the api_candidates
                # filter relies on it.
                ctype = resp.headers.get("content-type")
            except Exception:
                pass
            # v1.6.12: capture TTFB + body size for granular telemetry.
            # Both are best-effort -- ``request.timing`` may be missing
            # for cross-origin requests (Timing-Allow-Origin) and
            # ``Content-Length`` is dropped on chunked responses.
            ttfb_ms: float | None = None
            body_size: int | None = None
            try:
                timing = req.timing
                # Playwright's ``RequestTiming`` is a dict with
                # ``responseStart`` in ms relative to ``startTime``.
                # Playwright uses ``-1`` to signal "unavailable" -- a
                # value of ``0`` is technically valid (cache hit with
                # no measurable delay), so we filter on ``!= -1`` /
                # ``>= 0`` rather than ``> 0`` (the earlier draft).
                if timing is not None:
                    rs = timing.get("responseStart", -1)
                    if rs is not None and rs >= 0:
                        ttfb_ms = round(float(rs), 2)
            except Exception:
                pass
            try:
                cl = resp.headers.get("content-length")
                if cl is not None:
                    body_size = int(cl)
            except (ValueError, TypeError, AttributeError):
                pass
            evt = NetworkEvent(
                event_type="response",
                url=req.url,
                method=req.method,
                resource_type=rtype,
                status_code=resp.status,
                content_type=ctype,
                request_headers=(dict(req.headers) if self._diag.include_request_headers else {}),
                response_headers=headers,
                timing_ms=round(timing_ms, 2),
                ttfb_ms=ttfb_ms,
                body_size=body_size,
                correlation_id=get_correlation_id(),
            )
            buf = self._events.get(page)
            if buf is not None:
                buf.append(evt)
            # v1.6.12: optionally schedule async body capture. We can't
            # ``await`` here (the listener is registered sync), so we
            # fire-and-forget a task that reads the body and mutates the
            # NetworkEvent in-place. Callers must drain via
            # ``wait_for_pending_bodies`` before snapshotting if they
            # need the body text.
            if self._diag.capture_response_bodies and ctype and self._should_capture_body(ctype):
                with contextlib.suppress(RuntimeError):
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(self._capture_response_body(evt, resp))
                    self._pending_body_captures.add(task)
                    task.add_done_callback(self._pending_body_captures.discard)
        except Exception as exc:  # pragma: no cover -- defensive
            logger.debug("NetworkCollector._on_response swallowed: {e}", e=exc)

    def _on_failed(self, page: Any, req: Any) -> None:
        try:
            rtype = req.resource_type
            if self._diag.network_resource_types and rtype not in self._diag.network_resource_types:
                return
            failure = ""
            try:
                f = req.failure
                if f is not None:
                    # In Playwright, failure can be a dict or string-like.
                    failure = f.get("errorText", "") if isinstance(f, dict) else str(f)
            except Exception:
                pass
            self._req_start.get(page, {}).pop(req.url, None)
            evt = NetworkEvent(
                event_type="requestfailed",
                url=req.url,
                method=req.method,
                resource_type=rtype,
                failure_text=failure or None,
                request_headers=(dict(req.headers) if self._diag.include_request_headers else {}),
                correlation_id=get_correlation_id(),
            )
            buf = self._events.get(page)
            if buf is not None:
                buf.append(evt)
        except Exception as exc:  # pragma: no cover -- defensive
            logger.debug("NetworkCollector._on_failed swallowed: {e}", e=exc)

    def _on_download(self, page: Any, download: Any) -> None:
        """Record the URL the page tried to download.

        We don't save the file here -- the downloader's explicit
        ``expect_download`` path owns that. This is a notification only.

        Foot-gun: when ``capture_download_intents=True`` but no
        ``expect_download`` consumer is active, Playwright/Chromium holds
        a tmpfile open until something acks it. We schedule
        ``download.delete()`` so the listener doesn't cause tmpfile pileup
        on long-running sessions.
        """
        try:
            url = download.url
            intents = self._download_intents.setdefault(page, [])
            intents.append(url)
        except Exception as exc:  # pragma: no cover -- defensive
            logger.debug("NetworkCollector._on_download swallowed: {e}", e=exc)
            return
        # Best-effort tmpfile cleanup. If no event loop is running (unlikely
        # in async Playwright), just skip -- Chromium will clean up on exit.
        # v1.6.8 (review C-2 fix): keep a strong reference to the Task in
        # ``self._pending_deletes`` until it finishes, otherwise CPython
        # may GC the Task before ``download.delete()`` completes and the
        # cleanup the listener was added for never actually runs.
        with contextlib.suppress(RuntimeError):
            task = asyncio.get_running_loop().create_task(self._safe_delete(download))
            self._pending_deletes.add(task)
            task.add_done_callback(self._pending_deletes.discard)

    @staticmethod
    async def _safe_delete(download: Any) -> None:
        with contextlib.suppress(Exception):
            await download.delete()

    # ------------------------------------------------------------------
    # v1.6.12: response body capture
    # ------------------------------------------------------------------

    def _should_capture_body(self, content_type: str) -> bool:
        """v1.6.12: True iff ``content_type`` matches a configured prefix."""
        ct = content_type.lower().split(";", 1)[0].strip()
        if not ct:
            return False
        for prefix in self._diag.body_capture_content_types:
            if ct.startswith(prefix.lower()):
                return True
        return False

    async def _capture_response_body(self, evt: NetworkEvent, resp: Any) -> None:
        """v1.6.12: read response body, apply cap, mutate evt in-place.

        Best-effort -- any failure (closed page, decode error, network
        failure) is swallowed and the event's body_text stays None. The
        cap from ``DiagnosticsConfig.max_response_body_bytes`` is
        enforced byte-wise BEFORE decoding so we never load more than
        configured into memory.
        """
        try:
            body_bytes = await resp.body()
        except Exception as exc:  # pragma: no cover -- defensive
            logger.debug("body capture failed for {url}: {e}", url=evt.url, e=exc)
            return
        if not isinstance(body_bytes, (bytes, bytearray)):
            return
        max_bytes = self._diag.max_response_body_bytes
        truncated = len(body_bytes) > max_bytes
        if truncated:
            body_bytes = bytes(body_bytes[:max_bytes])
        try:
            text = bytes(body_bytes).decode("utf-8", errors="replace")
        except Exception:  # pragma: no cover -- defensive
            return
        # Pydantic models are mutable by default; safe to set after the
        # event was already appended to the deque.
        evt.body_text = text
        evt.body_truncated = truncated

    async def wait_for_pending_bodies(self, *, timeout: float = 5.0) -> None:
        """v1.6.12: drain in-flight body-capture tasks before snapshotting.

        v1.6.14 C-7: any tasks that do NOT complete inside ``timeout`` are
        explicitly cancelled so they cannot keep running orphaned against
        a Page that the caller is about to close. The previous
        ``asyncio.wait_for(asyncio.gather(..., return_exceptions=True))``
        construction cancelled the gather wrapper future but
        ``gather(return_exceptions=True)`` does NOT cascade cancellation
        to its children -- they continued running, racing the now-closed
        Page and surfacing "Task exception was never retrieved" /
        Playwright "Target closed" noise. Cancellation is also drained
        with a second ``gather`` so we don't leave "Task was destroyed
        but it is pending!" warnings in test output. The
        ``add_done_callback`` registered in ``_on_response`` keeps
        ``_pending_body_captures`` consistent automatically.

        Args:
            timeout: Maximum seconds to wait for in-flight body reads to
                finish. Default 5.0. Tasks still pending at the deadline
                are cancelled (and drained) before this method returns.
        """
        if not self._pending_body_captures:
            return
        pending_tasks = list(self._pending_body_captures)
        # v1.6.14 C-7: ``asyncio.wait`` returns (done, pending) but we
        # don't introspect ``done`` -- the per-task done callback in
        # ``_on_response`` already removes finished tasks from
        # ``_pending_body_captures``. ``_done`` is named-with-underscore
        # so ruff/RUF059 doesn't flag it as an unused unpack target.
        _done, pending = await asyncio.wait(pending_tasks, timeout=timeout)
        if pending:
            # v1.6.14 C-7: cancel orphans so they can't keep running
            # against a (possibly closed) Page.
            for task in pending:
                task.cancel()
            logger.debug(
                "wait_for_pending_bodies cancelling {n} tasks still pending after {t}s",
                n=len(pending),
                t=timeout,
            )
            # Drain the cancellations -- otherwise CPython logs
            # "Task was destroyed but it is pending!" once the GC
            # eventually collects them.
            await asyncio.gather(*pending, return_exceptions=True)


__all__ = ["NetworkCollector"]
