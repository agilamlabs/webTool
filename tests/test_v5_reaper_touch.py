"""R1 (concurrency) regression: the idle reaper must not reap a session that
is actively mid-scroll.

``SessionManager._idle_expired`` selects sessions purely on
``now - last_used > session_idle_ttl_s`` with NO busy/in-use check, and
``_reap_idle`` (driven from any ``create()`` / ``list()``) then ``close()``s
them. ``scroll_to_bottom`` / ``scroll_until_text`` run a loop of up to 1000
rounds, each waiting ``settle_ms``; pre-fix they touched the session only ONCE
at entry, so the idle clock was frozen for the whole walk. If the scroll ran
longer than ``session_idle_ttl_s`` (easy when an ops team lowers it) AND a
concurrent create()/list() landed, the live tab was closed out from under the
active scroll -> mid-walk crash.

The fix touches the SessionManager ONCE PER LOOP ROUND, keeping the idle clock
warm. These offline tests assert:

  * ``scroll_to_bottom`` calls ``sessions.touch`` once per round (entry touch
    + one per round == ``rounds + 1``).
  * ``scroll_until_text`` does the same.
  * Against a REAL ``SessionManager`` with a fake monotonic clock, an
    actively-scrolling session's ``last_used`` is advanced every round (so it
    never enters ``_idle_expired`` while the scroll is working).

No Playwright launch, no network -- the page / tab manager are mocks and (for
the real-clock case) the TabManager is injected into the SessionManager
registry directly.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.browser_actions import BrowserActions
from web_agent.config import AppConfig
from web_agent.models import ActionStatus, SessionInfo
from web_agent.session_manager import SessionManager

# ----------------------------------------------------------------------
# Shared offline builders (mirror tests/test_collection.py)
# ----------------------------------------------------------------------


def _make_scroll_page(heights: list[int], *, url: str = "https://example.com/feed") -> MagicMock:
    """Fake Page whose ``evaluate`` returns a scrollHeight sequence.

    ``scroll_to_bottom`` calls page.evaluate(scrollHeight) once up-front, then
    per round calls scrollTo (-> None) and scrollHeight again. Height reads are
    served from ``heights`` in order; once exhausted the last value repeats.
    """
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    type(page).url = property(lambda _self: url)

    seq = list(heights)

    async def _evaluate(expr: str, *args: object, **kwargs: object) -> object:
        if "scrollTo" in expr:
            return None
        if seq:
            return seq.pop(0)
        return heights[-1] if heights else 0

    page.evaluate = AsyncMock(side_effect=_evaluate)
    return page


def _make_text_page(*, url: str = "https://example.com/feed") -> MagicMock:
    """Fake Page for ``scroll_until_text`` whose body NEVER contains the target,
    so the loop always runs the full ``max_scrolls`` rounds."""
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    type(page).url = property(lambda _self: url)
    page.evaluate = AsyncMock(return_value="")  # target text never present
    page.wait_for_load_state = AsyncMock()
    page.mouse = MagicMock()
    page.mouse.wheel = AsyncMock()
    return page


def _actions_with_page(
    page: MagicMock, cfg: AppConfig | None = None
) -> tuple[BrowserActions, MagicMock]:
    """BrowserActions whose session tab resolves to ``page``; returns the
    ``sessions`` mock so callers can assert on ``sessions.touch``."""
    cfg = cfg or AppConfig()
    tab_mgr = MagicMock()
    tab_mgr.get_or_current = MagicMock(return_value=page)
    sessions = MagicMock()
    sessions.get_tab_manager = MagicMock(return_value=tab_mgr)
    sessions.touch = MagicMock()
    return BrowserActions(MagicMock(), cfg, sessions=sessions), sessions


# ----------------------------------------------------------------------
# scroll_to_bottom: touch once per round
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scroll_to_bottom_touches_each_round() -> None:
    """Strictly-growing height never stabilizes, so the loop runs the full
    ``max_scrolls`` rounds. touch() fires once at entry plus once per round."""
    rounds = 7
    # up-front read + one strictly-larger read per round so it never stabilizes.
    heights = [100] + [200 * i for i in range(1, rounds + 5)]
    page = _make_scroll_page(heights)
    actions, sessions = _actions_with_page(page)

    res = await actions.scroll_to_bottom(
        session_id="s1", settle_ms=0, stable_rounds=2, max_scrolls=rounds
    )

    assert res.status == ActionStatus.SUCCESS
    assert res.data is not None
    assert res.data["scrolls_used"] == rounds
    assert res.data["reached_bottom"] is False
    # At least once per executed round, and exactly entry + per-round here.
    assert sessions.touch.call_count >= rounds
    assert sessions.touch.call_count == rounds + 1
    # Every touch targeted this session id.
    assert sessions.touch.call_args_list == [(("s1",), {})] * (rounds + 1)


@pytest.mark.asyncio
async def test_scroll_to_bottom_touches_each_round_until_stable() -> None:
    """Even when the walk ends early on a stable bottom, every executed round
    still touches: entry + scrolls_used rounds."""
    # up-front 500; r1=900 (grew), r2=900 (stable=1), r3=900 (stable=2 -> stop).
    page = _make_scroll_page([500, 900, 900, 900])
    actions, sessions = _actions_with_page(page)

    res = await actions.scroll_to_bottom(
        session_id="s1", settle_ms=0, stable_rounds=2, max_scrolls=50
    )

    assert res.data is not None
    assert res.data["reached_bottom"] is True
    rounds = res.data["scrolls_used"]
    assert rounds == 3
    assert sessions.touch.call_count >= rounds
    assert sessions.touch.call_count == rounds + 1


# ----------------------------------------------------------------------
# scroll_until_text: touch once per round
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scroll_until_text_touches_each_round() -> None:
    """Target text never present -> loop runs all ``max_scrolls`` rounds.
    touch() fires once at entry plus once per round."""
    rounds = 6
    page = _make_text_page()
    actions, sessions = _actions_with_page(page)

    res = await actions.scroll_until_text("never-present", session_id="s1", max_scrolls=rounds)

    assert res.status == ActionStatus.FAILED
    assert res.data is not None
    assert res.data["scrolls_used"] == rounds
    assert page.mouse.wheel.await_count == rounds
    assert sessions.touch.call_count >= rounds
    assert sessions.touch.call_count == rounds + 1
    assert sessions.touch.call_args_list == [(("s1",), {})] * (rounds + 1)


@pytest.mark.asyncio
async def test_scroll_until_text_touches_each_round_until_found() -> None:
    """When the text appears after a few rounds, every executed round still
    touched: entry + scrolls_used rounds."""
    page = _make_text_page()
    # Initial body read (pre-loop) empty; then round bodies. Found on round 3.
    page.evaluate = AsyncMock(side_effect=["", "", "", "hit TARGET here", "x"])
    actions, sessions = _actions_with_page(page)

    res = await actions.scroll_until_text("TARGET", session_id="s1", max_scrolls=10)

    assert res.status == ActionStatus.SUCCESS
    assert res.data is not None
    rounds = res.data["scrolls_used"]
    assert rounds == 3
    assert sessions.touch.call_count >= rounds
    assert sessions.touch.call_count == rounds + 1


# ----------------------------------------------------------------------
# Real SessionManager + fake clock: last_used advances each round
# ----------------------------------------------------------------------


def _real_sm_with_tab(page: MagicMock, clock: Callable[[], float]) -> tuple[SessionManager, str]:
    """A real SessionManager wired with a fake monotonic clock and a single
    session whose TabManager resolves to ``page`` -- no browser launched.

    Only ``_tabs`` (+ ``_info``, required so the real ``touch`` advances the
    idle clock rather than no-opping on an unknown id) is populated -- NOT
    ``_sessions`` -- so ``get_tab_manager`` skips the liveness/dead-check (it is
    gated on ``_sessions.get(sid)``) and returns the injected TabManager.
    """
    sid = "live-scroll"
    sm = SessionManager(MagicMock(), AppConfig())
    sm._clock = clock  # type: ignore[assignment]
    tab_mgr = MagicMock()
    tab_mgr.get_or_current = MagicMock(return_value=page)
    sm._tabs[sid] = tab_mgr  # type: ignore[assignment]
    # touch() reads _info[sid] and returns early if absent; seed it so the
    # per-round touches actually stamp _last_used.
    sm._info[sid] = SessionInfo(session_id=sid)
    return sm, sid


@pytest.mark.asyncio
async def test_active_scroll_advances_last_used_each_round() -> None:
    """An actively-scrolling session must have its monotonic ``last_used``
    advanced every round so ``_idle_expired`` never selects it mid-walk.

    The fake clock strictly increases on every read. We capture the stamp taken
    at entry (get_tab_manager + entry touch) and the final stamp after the walk;
    per-round touches mean the final stamp is at least ``rounds`` ticks beyond
    the entry stamp. Pre-fix the value would have been frozen at the entry
    stamp for the whole walk.
    """
    counter = itertools.count(start=1000)
    clock = lambda: float(next(counter))  # noqa: E731 -- tiny test clock

    rounds = 5
    heights = [100] + [200 * i for i in range(1, rounds + 5)]
    page = _make_scroll_page(heights)
    sm, sid = _real_sm_with_tab(page, clock)

    # Seed a stale last_used: far in the "past" relative to the fake clock.
    sm._last_used[sid] = 0.0

    # Snapshot the stamp the moment the FIRST round begins. The page's first
    # scrollHeight read (the up-front read, before round 1) lets us capture the
    # entry-era stamp: by then get_tab_manager + the entry touch have both run.
    entry_stamp: dict[str, float] = {}
    orig_evaluate = page.evaluate.side_effect

    async def _capture_then_eval(expr: str, *args: object, **kwargs: object) -> object:
        if "scrollTo" not in expr and "entry" not in entry_stamp:
            entry_stamp["entry"] = sm._last_used[sid]
        return await orig_evaluate(expr, *args, **kwargs)

    page.evaluate = AsyncMock(side_effect=_capture_then_eval)

    actions = BrowserActions(MagicMock(), AppConfig(), sessions=sm)
    res = await actions.scroll_to_bottom(
        session_id=sid, settle_ms=0, stable_rounds=2, max_scrolls=rounds
    )

    assert res.data is not None
    assert res.data["scrolls_used"] == rounds
    final = sm._last_used[sid]
    # The session was kept warm: it advanced off the 0.0 stale seed entirely...
    assert entry_stamp["entry"] >= 1000.0
    # ...and the per-round touches pushed it at least ``rounds`` ticks further.
    assert final >= entry_stamp["entry"] + rounds

    # Proof the refresh matters for the reaper: pick a TTL that sits BETWEEN the
    # frozen entry stamp's age and the refreshed stamp's age, measured against a
    # fixed "now". With per-round touches the live session is NOT expired; a
    # frozen-at-entry stamp WOULD have been reaped at the same instant.
    now = clock()  # one tick past ``final``
    ttl = (now - entry_stamp["entry"]) - 1  # frozen entry stamp is older than this
    sm._config.browser.session_idle_ttl_s = int(ttl)
    sm._clock = lambda: now  # type: ignore[assignment] -- freeze "now" for the check
    assert sid not in sm._idle_expired()  # refreshed stamp survives
    # Counterfactual: had last_used stayed frozen at entry, it WOULD be reaped.
    assert (now - entry_stamp["entry"]) > sm._config.browser.session_idle_ttl_s
