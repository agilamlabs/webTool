"""v1.6.6 Feature 3: TabManager lifecycle.

These tests exercise the TabManager directly with mocked Playwright
Pages/Contexts -- the unit under test is the tab-tracking logic, not
the real browser. Integration smoke is covered by the existing
session_manager tests against a live browser.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.tab_manager import INITIAL_TAB_ID, TabManager


def _make_page(url: str = "about:blank", title: str = "") -> MagicMock:
    """Build a fake Page with the surface area TabManager touches."""
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    page.bring_to_front = AsyncMock()
    page.close = AsyncMock()
    page.title = AsyncMock(return_value=title)
    # url is a property on real Pages
    type(page).url = property(lambda _self: url)

    # page.on(event, callback) stores the close-handler so we can fire it
    page._on_close_handlers = []  # type: ignore[attr-defined]

    def _on(event: str, cb) -> None:
        if event == "close":
            page._on_close_handlers.append(cb)  # type: ignore[attr-defined]

    page.on = MagicMock(side_effect=_on)
    return page


def _make_ctx() -> MagicMock:
    """Build a fake BrowserContext that tracks the 'page' event listener."""
    ctx = MagicMock()
    ctx._page_event_handler = None  # populated by ctx.on("page", ...)

    def _on(event: str, cb) -> None:
        if event == "page":
            ctx._page_event_handler = cb

    ctx.on = MagicMock(side_effect=_on)

    # ctx.new_page returns a fresh page each call
    async def _new_page():
        return _make_page()

    ctx.new_page = AsyncMock(side_effect=_new_page)
    return ctx


# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_initial_page_sets_main_tab_active() -> None:
    """SessionManager.create() calls register_initial_page("main").
    After registration the session has exactly one tab, named "main",
    and it is the current tab."""
    ctx = _make_ctx()
    tm = TabManager(ctx)
    page = _make_page(url="https://example.com")

    tid = await tm.register_initial_page(page, INITIAL_TAB_ID)

    assert tid == "main"
    assert tm.current_tab_id() == "main"
    assert tm.current() is page

    tabs = await tm.list()
    assert len(tabs) == 1
    assert tabs[0].tab_id == "main"
    assert tabs[0].active is True


@pytest.mark.asyncio
async def test_new_tab_opens_and_becomes_current() -> None:
    ctx = _make_ctx()
    tm = TabManager(ctx)
    await tm.register_initial_page(_make_page(), INITIAL_TAB_ID)

    new_tid = await tm.new_tab()
    assert new_tid != "main"
    assert tm.current_tab_id() == new_tid

    tabs = await tm.list()
    assert len(tabs) == 2
    actives = [t.tab_id for t in tabs if t.active]
    assert actives == [new_tid]


@pytest.mark.asyncio
async def test_switch_tab_updates_current_pointer() -> None:
    ctx = _make_ctx()
    tm = TabManager(ctx)
    await tm.register_initial_page(_make_page(), INITIAL_TAB_ID)
    second = await tm.new_tab()
    assert tm.current_tab_id() == second

    await tm.switch_tab(INITIAL_TAB_ID)
    assert tm.current_tab_id() == INITIAL_TAB_ID


@pytest.mark.asyncio
async def test_switch_tab_unknown_id_raises_keyerror() -> None:
    ctx = _make_ctx()
    tm = TabManager(ctx)
    await tm.register_initial_page(_make_page(), INITIAL_TAB_ID)
    with pytest.raises(KeyError):
        await tm.switch_tab("does-not-exist")


@pytest.mark.asyncio
async def test_close_tab_removes_and_picks_new_current() -> None:
    ctx = _make_ctx()
    tm = TabManager(ctx)
    main_page = _make_page()
    await tm.register_initial_page(main_page, INITIAL_TAB_ID)
    second = await tm.new_tab()
    assert tm.current_tab_id() == second

    # Closing the current tab should fall back to the other.
    second_page = tm.get(second)
    await tm.close_tab(second)

    # Fire the close handler that real Playwright would fire
    for cb in second_page._on_close_handlers:  # type: ignore[attr-defined]
        cb(second_page)

    # Now only "main" remains, and it's current.
    assert tm.current_tab_id() == INITIAL_TAB_ID
    assert len(await tm.list()) == 1


@pytest.mark.asyncio
async def test_popup_event_auto_registers_but_does_not_become_current() -> None:
    """Popups (page-opened tabs) auto-register so they're visible to
    list_tabs, but they do NOT steal focus from the launcher tab."""
    ctx = _make_ctx()
    tm = TabManager(ctx)
    main_page = _make_page()
    await tm.register_initial_page(main_page, INITIAL_TAB_ID)

    # Simulate Chromium firing the "page" event for a popup
    popup = _make_page(url="https://popup.example.com")
    handler = ctx._page_event_handler
    assert handler is not None
    handler(popup)

    # Popup is registered but main is still current
    tabs = await tm.list()
    assert len(tabs) == 2
    assert tm.current_tab_id() == INITIAL_TAB_ID
    popup_tab = next(t for t in tabs if t.tab_id != INITIAL_TAB_ID)
    assert popup_tab.active is False


@pytest.mark.asyncio
async def test_get_or_current_falls_back_to_current() -> None:
    ctx = _make_ctx()
    tm = TabManager(ctx)
    page = _make_page()
    await tm.register_initial_page(page, INITIAL_TAB_ID)

    assert tm.get_or_current(None) is page
    assert tm.get_or_current(INITIAL_TAB_ID) is page
    assert tm.get_or_current("ghost") is None  # unknown tab_id => None


@pytest.mark.asyncio
async def test_initial_page_not_double_registered_by_listener() -> None:
    """register_initial_page runs AFTER ctx.on("page", ...) is attached
    in __init__, but the listener must not re-register the initial page
    (which would create two tab_ids for the same Page)."""
    ctx = _make_ctx()
    tm = TabManager(ctx)
    page = _make_page()
    await tm.register_initial_page(page, INITIAL_TAB_ID)

    # Simulate the listener firing on the SAME page object (shouldn't happen
    # in real Playwright but defends against the edge case).
    handler = ctx._page_event_handler
    assert handler is not None
    handler(page)

    tabs = await tm.list()
    assert len(tabs) == 1, f"Expected 1 tab after double-fire, got {len(tabs)}"


@pytest.mark.asyncio
async def test_popup_already_closed_on_registration_does_not_leak() -> None:
    """Regression for v1.6.6 code-review #4: a popup that fires close()
    BEFORE Playwright delivers the "page" event to our listener would
    leave a permanent dead entry in _tabs. The listener now checks
    page.is_closed() right after registering the handler and evicts
    immediately."""
    ctx = _make_ctx()
    tm = TabManager(ctx)
    await tm.register_initial_page(_make_page(), INITIAL_TAB_ID)

    # Simulate Chromium firing "page" for a popup that already closed
    closed_popup = _make_page(url="about:blank")
    closed_popup.is_closed = MagicMock(return_value=True)

    handler = ctx._page_event_handler
    assert handler is not None
    handler(closed_popup)

    # The already-closed popup must not appear in the live tab list
    tabs = await tm.list()
    assert len(tabs) == 1, f"Expected 1 tab (only main), got {len(tabs)}"
    assert tabs[0].tab_id == INITIAL_TAB_ID


@pytest.mark.asyncio
async def test_evict_on_close_is_idempotent() -> None:
    """Regression for v1.6.6 code-review #1: _evict_on_close must be
    idempotent so close_tab's except-branch fallback doesn't double-pick
    a different fallback current tab."""
    ctx = _make_ctx()
    tm = TabManager(ctx)
    await tm.register_initial_page(_make_page(), INITIAL_TAB_ID)
    second = await tm.new_tab()
    third = await tm.new_tab()
    assert tm.current_tab_id() == third

    # Fire eviction twice for `third` -- the second call must NOT pick a
    # different fallback than the first.
    tm._evict_on_close(third)
    chosen_after_first = tm.current_tab_id()
    tm._evict_on_close(third)
    chosen_after_second = tm.current_tab_id()

    assert chosen_after_first == chosen_after_second
    assert chosen_after_first in (INITIAL_TAB_ID, second)
