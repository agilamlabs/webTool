"""v1.6.6 Feature 5: observe mode.

ObserveResult captures viewport / page / scroll / DPR via a single
page.evaluate round-trip, plus a screenshot saved into screenshot_dir.
The screenshot path goes through safe_join_path (v1.6.4 fix).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.browser_actions import BrowserActions
from web_agent.config import AppConfig, AutomationConfig, SafetyConfig


def _make_page(url: str = "https://example.com/page") -> MagicMock:
    """Fake Page that fakes the bits observe() touches."""
    page = MagicMock()
    type(page).url = property(lambda _self: url)
    page.goto = AsyncMock()
    page.title = AsyncMock(return_value="Example Page")
    page.screenshot = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    # page.evaluate returns dimensions or innerText depending on the arg
    page.evaluate = AsyncMock(
        side_effect=lambda _expr: (
            {"vw": 1280, "vh": 720, "pw": 1280, "ph": 2400, "sx": 0, "sy": 100, "dpr": 2.0}
            if "innerWidth" in _expr
            else "the visible page text"
        )
    )
    accessibility = MagicMock()
    accessibility.snapshot = AsyncMock(return_value={"role": "WebArea", "name": "Example"})
    page.accessibility = accessibility
    page.mouse = MagicMock()
    page.keyboard = MagicMock()
    return page


def _make_browser_manager(page: MagicMock) -> MagicMock:
    """Fake BrowserManager that yields a fake new_page ctx-manager."""

    class _PageCtx:
        async def __aenter__(self):
            return page

        async def __aexit__(self, *_a):
            return False

    bm = MagicMock()
    bm.new_page = MagicMock(return_value=_PageCtx())
    return bm


def _make_sessions(page: MagicMock) -> MagicMock:
    """Fake SessionManager whose current tab resolves to ``page``.

    Mirrors what observe()/scroll_until_text() touch on the session path:
    get_tab_manager -> tab_mgr; tab_mgr.get_or_current -> page;
    tab_mgr.current_tab_id() -> id; sessions.touch(...).
    """
    tab_mgr = MagicMock()
    tab_mgr.get_or_current = MagicMock(return_value=page)
    tab_mgr.current_tab_id = MagicMock(return_value="main")
    sessions = MagicMock()
    sessions.get_tab_manager = MagicMock(return_value=tab_mgr)
    sessions.touch = MagicMock()
    return sessions


# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_returns_dimensions_scroll_and_dpr(tmp_path: Path) -> None:
    config = AppConfig(
        base_dir=str(tmp_path),
        automation=AutomationConfig(screenshot_dir=str(tmp_path / "shots")),
    )
    page = _make_page()
    ba = BrowserActions(_make_browser_manager(page), config, sessions=None)

    obs = await ba.observe(url="https://example.com/page", include_text=False)

    assert obs.url == "https://example.com/page"
    assert obs.viewport_width == 1280
    assert obs.viewport_height == 720
    assert obs.page_width == 1280
    assert obs.page_height == 2400
    assert obs.scroll_x == 0
    assert obs.scroll_y == 100
    assert obs.device_pixel_ratio == 2.0
    page.goto.assert_awaited_once()


@pytest.mark.asyncio
async def test_observe_writes_screenshot_under_screenshot_dir(tmp_path: Path) -> None:
    shot_dir = tmp_path / "shots"
    config = AppConfig(
        base_dir=str(tmp_path), automation=AutomationConfig(screenshot_dir=str(shot_dir))
    )
    page = _make_page()
    ba = BrowserActions(_make_browser_manager(page), config, sessions=None)

    obs = await ba.observe(url="https://example.com/page", include_text=False)

    # The path goes through safe_join_path -- it must live UNDER screenshot_dir
    sp = Path(obs.screenshot_path)
    assert shot_dir.resolve() in sp.parents, f"{sp} not under {shot_dir}"
    page.screenshot.assert_awaited_once()
    # And the screenshot dir was created
    assert shot_dir.exists()


@pytest.mark.asyncio
async def test_observe_include_aria_false_returns_none(tmp_path: Path) -> None:
    config = AppConfig(
        base_dir=str(tmp_path),
        automation=AutomationConfig(screenshot_dir=str(tmp_path / "shots")),
    )
    page = _make_page()
    ba = BrowserActions(_make_browser_manager(page), config, sessions=None)

    obs = await ba.observe(url="https://example.com/page", include_aria=False)

    assert obs.aria_snapshot is None
    page.accessibility.snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_observe_include_aria_true_captures_snapshot(tmp_path: Path) -> None:
    config = AppConfig(
        base_dir=str(tmp_path),
        automation=AutomationConfig(screenshot_dir=str(tmp_path / "shots")),
    )
    page = _make_page()
    ba = BrowserActions(_make_browser_manager(page), config, sessions=None)

    obs = await ba.observe(url="https://example.com/page", include_aria=True)

    page.accessibility.snapshot.assert_awaited_once()
    assert obs.aria_snapshot == {"role": "WebArea", "name": "Example"}


@pytest.mark.asyncio
async def test_observe_text_truncated_to_safety_cap(tmp_path: Path) -> None:
    """visible_text is truncated to safety.max_chars_per_call."""
    big_text = "x" * 5000
    config = AppConfig(
        base_dir=str(tmp_path),
        safety=SafetyConfig(max_chars_per_call=100),
        automation=AutomationConfig(screenshot_dir=str(tmp_path / "shots")),
    )
    page = _make_page()
    # Override evaluate to return the big text
    page.evaluate = AsyncMock(
        side_effect=lambda _expr: (
            {"vw": 1, "vh": 1, "pw": 1, "ph": 1, "sx": 0, "sy": 0, "dpr": 1.0}
            if "innerWidth" in _expr
            else big_text
        )
    )
    ba = BrowserActions(_make_browser_manager(page), config, sessions=None)

    obs = await ba.observe(url="https://example.com/page", include_text=True)

    assert obs.visible_text is not None
    assert len(obs.visible_text) == 100


@pytest.mark.asyncio
async def test_observe_session_path_gates_input_url_before_goto(tmp_path: Path) -> None:
    """H2: observe(session_id=..., url=<denied>) resolves the page from the
    session tab, skipping the ephemeral branch's gate. The session path must
    itself gate the INPUT url BEFORE navigating, so a denied/private host
    never receives an outbound request (blind SSRF guard)."""
    config = AppConfig(
        base_dir=str(tmp_path),
        # block_private_ips defaults True -> the IMDS host is denied
        automation=AutomationConfig(screenshot_dir=str(tmp_path / "shots")),
    )
    page = _make_page()
    ba = BrowserActions(
        _make_browser_manager(page), config, sessions=_make_sessions(page)
    )

    with pytest.raises(ValueError, match="Domain not allowed"):
        await ba.observe(
            session_id="sid",
            url="http://169.254.169.254/latest/meta-data/",
        )

    # Critical: the outbound navigation must NEVER have fired.
    page.goto.assert_not_awaited()
    page.screenshot.assert_not_awaited()
