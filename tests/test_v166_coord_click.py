"""v1.6.6 Feature 4: coordinate-level fallback actions.

ClickXYInput / TypeTextInput / PressKeyInput slot into the Action union
and have dedicated handlers that bypass selector resolution. The top-level
Agent methods require session_id since coord clicks are only meaningful
against an observed live page.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.browser_actions import BrowserActions
from web_agent.config import AppConfig, SafetyConfig
from web_agent.models import (
    ActionStatus,
    ActionType,
    ClickXYInput,
    MouseButton,
    PressKeyInput,
    TypeTextInput,
)


def _make_page() -> MagicMock:
    """Fake Page with mouse + keyboard surface."""
    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.click = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.type = AsyncMock()
    page.keyboard.press = AsyncMock()
    return page


# ----------------------------------------------------------------------
# _do_click_xy: calls page.mouse.click with the right args
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_do_click_xy_calls_mouse_click_with_css_pixels() -> None:
    config = AppConfig()
    ba = BrowserActions(MagicMock(), config, sessions=None)
    page = _make_page()

    action = ClickXYInput(x=120.5, y=240.0, button=MouseButton.LEFT, clicks=2, delay=10)
    result = await ba._do_click_xy(page, action)

    page.mouse.click.assert_awaited_once_with(120.5, 240.0, button="left", click_count=2, delay=10)
    assert result.status == ActionStatus.SUCCESS
    assert result.action == ActionType.CLICK_XY
    assert result.data == {"x": 120.5, "y": 240.0, "button": "left"}


@pytest.mark.asyncio
async def test_do_type_text_calls_keyboard_type() -> None:
    config = AppConfig()
    ba = BrowserActions(MagicMock(), config, sessions=None)
    page = _make_page()

    action = TypeTextInput(text="hello world", delay=5)
    result = await ba._do_type_text(page, action)

    page.keyboard.type.assert_awaited_once_with("hello world", delay=5)
    assert result.status == ActionStatus.SUCCESS
    assert result.action == ActionType.TYPE_TEXT
    assert result.data == {"length": 11}


@pytest.mark.asyncio
async def test_do_press_key_with_modifiers_builds_combo() -> None:
    config = AppConfig()
    ba = BrowserActions(MagicMock(), config, sessions=None)
    page = _make_page()

    # Bare key
    await ba._do_press_key(page, PressKeyInput(key="Enter"))
    page.keyboard.press.assert_awaited_with("Enter")

    # With modifiers
    await ba._do_press_key(
        page, PressKeyInput(key="a", modifiers=["Control", "Shift"])
    )
    page.keyboard.press.assert_awaited_with("Control+Shift+a")


@pytest.mark.asyncio
async def test_click_xy_safe_mode_logs_warning_but_does_not_block(caplog) -> None:
    """In safe_mode, coord click cannot inspect a selector for the submit
    heuristic. We log a WARNING but the click still runs -- safe_mode
    was a config-level opt-in, not a per-coord-click block."""
    config = AppConfig(safety=SafetyConfig(safe_mode=True))
    ba = BrowserActions(MagicMock(), config, sessions=None)
    page = _make_page()

    # Loguru doesn't integrate with caplog by default; instead assert that
    # the call succeeded and the mouse click was issued.
    result = await ba._do_click_xy(page, ClickXYInput(x=10, y=20))

    page.mouse.click.assert_awaited_once()
    assert result.status == ActionStatus.SUCCESS


@pytest.mark.asyncio
async def test_execute_single_on_session_routes_through_current_tab() -> None:
    """Top-level agent.click_xy delegates here. It must look up the
    session's current tab and call execute_action against it."""
    config = AppConfig()
    page = _make_page()

    # Mock the session manager surface
    fake_tab_mgr = MagicMock()
    fake_tab_mgr.get_or_current = MagicMock(return_value=page)

    fake_sessions = MagicMock()
    fake_sessions.get_tab_manager = MagicMock(return_value=fake_tab_mgr)
    fake_sessions.touch = MagicMock()

    ba = BrowserActions(MagicMock(), config, sessions=fake_sessions)

    action = ClickXYInput(x=50, y=75)
    result = await ba.execute_single_on_session(action, session_id="sid-abc")

    fake_sessions.get_tab_manager.assert_called_once_with("sid-abc")
    fake_tab_mgr.get_or_current.assert_called_once_with(None)
    fake_sessions.touch.assert_called_once_with("sid-abc")
    page.mouse.click.assert_awaited_once()
    assert result.status == ActionStatus.SUCCESS
