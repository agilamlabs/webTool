"""Per-session tab lifecycle management for v1.6.6.

A session today (v1.6.5) holds exactly one :class:`BrowserContext` and
``execute_sequence`` creates a fresh :class:`Page` per call. v1.6.6 keeps
the BrowserContext model but treats pages as named tabs: each session
owns a :class:`TabManager` that maps ``tab_id -> Page`` and tracks a
sticky ``_current_tab_id`` pointer.

Popups (target=_blank, ``window.open``, OAuth flows, document previews)
are auto-registered via ``ctx.on("page", ...)`` but do NOT become the
session's current tab until an explicit ``switch_tab`` call -- preserving
the launcher tab's role in observe-act loops.

The reverse lookup ``page -> tab_id`` uses :class:`WeakKeyDictionary`,
matching the pattern from ``browser_actions._PAGE_DIALOG_STATES`` (v1.6.5
L14). Closing a page automatically evicts the entry.
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional
from weakref import WeakKeyDictionary

from loguru import logger
from playwright.async_api import BrowserContext, Page

from .models import TabInfo

if TYPE_CHECKING:  # pragma: no cover -- avoid runtime import cycle
    from .network_collector import NetworkCollector

# Reserved tab_id for the initial page created at session-create time.
INITIAL_TAB_ID = "main"


class TabManager:
    """Manages the tabs (Pages) for a single session's BrowserContext."""

    def __init__(
        self,
        ctx: BrowserContext,
        network_collector: Optional[NetworkCollector] = None,
    ) -> None:
        self._ctx = ctx
        self._tabs: dict[str, Page] = {}
        self._reverse: WeakKeyDictionary[Page, str] = WeakKeyDictionary()
        self._opened_at: dict[str, datetime] = {}
        self._current_tab_id: Optional[str] = None
        # v1.6.8: optional NetworkCollector that auto-attaches to every
        # Page this manager sees -- the registered popup hook below also
        # routes through here so popups inherit network capture.
        self._network_collector = network_collector
        # Serialize ops that mutate _tabs / _current_tab_id so concurrent
        # callers (e.g. parallel switch + close) don't race.
        self._lock = asyncio.Lock()
        # Auto-register page-opened popups. The handler is sync because
        # Playwright fires "page" with a synchronous callback signature;
        # the heavy work (URL/title introspection) happens on demand in
        # ``list()`` instead.
        ctx.on("page", self._on_new_page)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _generate_tab_id(self) -> str:
        return secrets.token_urlsafe(6)

    def _on_new_page(self, page: Page) -> None:
        """Listener fired by Playwright when a new Page appears in the
        context (popup, target=_blank, window.open, download-in-new-tab).

        Auto-registers the page with a generated tab_id but does NOT
        change ``_current_tab_id``. The launcher tab keeps focus until
        explicit ``switch_tab``.
        """
        # Skip pages already registered (the initial page is registered
        # synchronously by register_initial_page before this listener
        # could fire on it). The reverse map is the source of truth.
        if page in self._reverse:
            return
        tid = self._generate_tab_id()
        self._tabs[tid] = page
        self._reverse[page] = tid
        self._opened_at[tid] = datetime.now(timezone.utc)
        # Auto-evict on close
        page.on("close", lambda _p: self._evict_on_close(tid))
        # Race guard: if the popup closed BEFORE Playwright delivered this
        # "page" event to us, the page.on("close", ...) handler above will
        # never fire and the entry would leak forever. Sweep it now.
        try:
            already_closed = page.is_closed()
        except Exception:
            already_closed = True
        if already_closed:
            self._evict_on_close(tid)
            return
        # v1.6.8: attach network capture to popups too. Off-by-default
        # gating happens inside NetworkCollector.attach().
        if self._network_collector is not None:
            self._network_collector.attach(page)
        logger.debug("Tab auto-registered from popup: {tid}", tid=tid)

    def _evict_on_close(self, tab_id: str) -> None:
        """Cleanup when a Page fires its 'close' event.

        Idempotent: ``close_tab``'s except branch may call this a second
        time after Playwright's "close" event already fired it once. The
        early-out guard prevents that second call from re-picking a
        DIFFERENT fallback current tab.
        """
        if tab_id not in self._tabs:
            return  # already evicted (close event + explicit close_tab race)
        self._tabs.pop(tab_id, None)
        self._opened_at.pop(tab_id, None)
        if self._current_tab_id == tab_id:
            # Pick any remaining tab as the new current, or None if empty.
            self._current_tab_id = next(iter(self._tabs), None)

    # ------------------------------------------------------------------
    # registration (called by SessionManager)
    # ------------------------------------------------------------------

    async def register_initial_page(
        self, page: Page, tab_id: str = INITIAL_TAB_ID
    ) -> str:
        """Register the first page of a freshly-created session.

        Called by ``SessionManager.create()`` after ``ctx.new_page()``.
        The initial page becomes the current tab; it is NOT auto-registered
        by ``_on_new_page`` because that listener fires only for pages
        added AFTER the listener is attached, and ``__init__`` attaches
        before this method runs.
        """
        async with self._lock:
            self._tabs[tab_id] = page
            self._reverse[page] = tab_id
            self._opened_at[tab_id] = datetime.now(timezone.utc)
            self._current_tab_id = tab_id
            page.on("close", lambda _p: self._evict_on_close(tab_id))
        # v1.6.8: attach network capture (no-op when both switches off).
        if self._network_collector is not None:
            self._network_collector.attach(page)
        return tab_id

    # ------------------------------------------------------------------
    # public API (used by Agent + BrowserActions)
    # ------------------------------------------------------------------

    async def new_tab(self, url: Optional[str] = None) -> str:
        """Open a fresh tab. If ``url`` is given, navigate to it.

        The new tab becomes the session's current tab.
        """
        async with self._lock:
            tid = self._generate_tab_id()
            page = await self._ctx.new_page()
            self._tabs[tid] = page
            self._reverse[page] = tid
            self._opened_at[tid] = datetime.now(timezone.utc)
            self._current_tab_id = tid
            page.on("close", lambda _p: self._evict_on_close(tid))
        # v1.6.8: attach network capture immediately after the page is
        # created so we don't miss the first navigation's requests.
        if self._network_collector is not None:
            self._network_collector.attach(page)
        if url:
            try:
                await page.goto(url)
            except Exception as exc:
                logger.warning("new_tab goto failed for {u}: {e}", u=url, e=exc)
        return tid

    async def switch_tab(self, tab_id: str) -> None:
        """Make ``tab_id`` the current tab. Brings it to front if possible."""
        async with self._lock:
            if tab_id not in self._tabs:
                raise KeyError(f"Unknown tab_id: {tab_id!r}")
            self._current_tab_id = tab_id
            page = self._tabs[tab_id]
        try:
            await page.bring_to_front()
        except Exception as exc:
            # bring_to_front fails on some headless setups; non-fatal
            logger.debug("bring_to_front failed for {tid}: {e}", tid=tab_id, e=exc)

    async def close_tab(self, tab_id: str) -> None:
        """Close a tab and drop its state.

        Closing the current tab updates ``_current_tab_id`` to any
        remaining tab (or None if this was the last one).
        """
        async with self._lock:
            page = self._tabs.get(tab_id)
            if page is None:
                raise KeyError(f"Unknown tab_id: {tab_id!r}")
            # _evict_on_close will fire via the page.on("close") listener
            # we attached at registration. Closing the page synchronously
            # drops it from _tabs/_opened_at and updates _current_tab_id.
        try:
            await page.close()
        except Exception as exc:
            logger.warning("Error closing tab {tid}: {e}", tid=tab_id, e=exc)
            # Force the eviction even if Playwright's close raised, so the
            # state stays consistent.
            self._evict_on_close(tab_id)

    def current(self) -> Optional[Page]:
        """Return the current tab's Page, or None if no tabs are live."""
        if self._current_tab_id is None:
            return None
        return self._tabs.get(self._current_tab_id)

    def current_tab_id(self) -> Optional[str]:
        return self._current_tab_id

    def get(self, tab_id: str) -> Page:
        """Return the Page for ``tab_id`` or raise KeyError."""
        page = self._tabs.get(tab_id)
        if page is None:
            raise KeyError(f"Unknown tab_id: {tab_id!r}")
        return page

    def get_or_current(self, tab_id: Optional[str]) -> Optional[Page]:
        """Look up ``tab_id`` or fall back to the current tab.

        Returns None if no tab is available. Used by execute_sequence to
        resolve where an action targets.
        """
        if tab_id is not None:
            return self._tabs.get(tab_id)
        return self.current()

    async def list(self) -> list[TabInfo]:
        """Return a snapshot of all live tabs with URL + title.

        URL/title introspection requires a Playwright round-trip per tab,
        so this is async even though it doesn't mutate state.
        """
        out: list[TabInfo] = []
        # Snapshot keys under the lock so concurrent closes don't break
        # iteration. URL/title fetches happen outside the lock.
        async with self._lock:
            items = list(self._tabs.items())
            current = self._current_tab_id
            opened = dict(self._opened_at)
        for tid, page in items:
            try:
                url = page.url
            except Exception:
                url = ""
            title: Optional[str]
            try:
                title = await page.title()
            except Exception:
                title = None
            out.append(
                TabInfo(
                    tab_id=tid,
                    url=url,
                    title=title,
                    active=(tid == current),
                    opened_at=opened.get(tid, datetime.now(timezone.utc)),
                )
            )
        return out

    def __len__(self) -> int:
        return len(self._tabs)
