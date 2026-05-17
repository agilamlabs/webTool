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
        self._events: WeakKeyDictionary[Any, collections.deque[NetworkEvent]] = (
            WeakKeyDictionary()
        )
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
            if (
                self._diag.network_resource_types
                and rtype not in self._diag.network_resource_types
            ):
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
            if (
                self._diag.network_resource_types
                and rtype not in self._diag.network_resource_types
            ):
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
            evt = NetworkEvent(
                event_type="response",
                url=req.url,
                method=req.method,
                resource_type=rtype,
                status_code=resp.status,
                content_type=ctype,
                request_headers=(
                    dict(req.headers) if self._diag.include_request_headers else {}
                ),
                response_headers=headers,
                timing_ms=round(timing_ms, 2),
                correlation_id=get_correlation_id(),
            )
            buf = self._events.get(page)
            if buf is not None:
                buf.append(evt)
        except Exception as exc:  # pragma: no cover -- defensive
            logger.debug("NetworkCollector._on_response swallowed: {e}", e=exc)

    def _on_failed(self, page: Any, req: Any) -> None:
        try:
            rtype = req.resource_type
            if (
                self._diag.network_resource_types
                and rtype not in self._diag.network_resource_types
            ):
                return
            failure = ""
            try:
                f = req.failure
                if f is not None:
                    # In Playwright, failure can be a dict or string-like.
                    failure = (
                        f.get("errorText", "") if isinstance(f, dict) else str(f)
                    )
            except Exception:
                pass
            self._req_start.get(page, {}).pop(req.url, None)
            evt = NetworkEvent(
                event_type="requestfailed",
                url=req.url,
                method=req.method,
                resource_type=rtype,
                failure_text=failure or None,
                request_headers=(
                    dict(req.headers) if self._diag.include_request_headers else {}
                ),
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
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self._safe_delete(download))

    @staticmethod
    async def _safe_delete(download: Any) -> None:
        with contextlib.suppress(Exception):
            await download.delete()


__all__ = ["NetworkCollector"]
