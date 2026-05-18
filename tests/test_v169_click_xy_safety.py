"""v1.6.9 click_xy safety tests.

Covers the elementFromPoint-based destructive-control heuristic and the
new ``safety.allow_coordinate_clicks`` toggle. Tests are unit-level: they
mock the Playwright Page so no browser launch is required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.browser_actions import (
    _DESTRUCTIVE_TEXT_PATTERN,
    BrowserActions,
)
from web_agent.config import AppConfig, SafetyConfig
from web_agent.models import ActionStatus, ActionType, ClickXYInput, MouseButton

# ---------------------------------------------------------------------------
# Config-level: safe_mode forces allow_coordinate_clicks=False
# ---------------------------------------------------------------------------


def test_allow_coordinate_clicks_defaults_true() -> None:
    cfg = SafetyConfig()
    assert cfg.allow_coordinate_clicks is True


def test_safe_mode_forces_allow_coordinate_clicks_false() -> None:
    cfg = SafetyConfig(safe_mode=True)
    assert cfg.allow_coordinate_clicks is False


def test_safe_mode_overrides_explicit_true() -> None:
    cfg = SafetyConfig(safe_mode=True, allow_coordinate_clicks=True)
    # safe_mode wins
    assert cfg.allow_coordinate_clicks is False


def test_explicit_false_without_safe_mode() -> None:
    cfg = SafetyConfig(safe_mode=False, allow_coordinate_clicks=False)
    assert cfg.allow_coordinate_clicks is False


# ---------------------------------------------------------------------------
# Destructive-text pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Submit",
        "submit form",
        "Login",
        "log in",
        "Sign in",
        "Sign Up",
        "Register",
        "Continue",
        "Delete account",
        "Buy now",
        "Pay",
        "Checkout",
        "Accept",
        "I Agree",
    ],
)
def test_destructive_pattern_matches_common_verbs(text: str) -> None:
    assert _DESTRUCTIVE_TEXT_PATTERN.search(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "Read more",
        "Hello world",
        "About",
        "Documentation",
        "Pricing",
    ],
)
def test_destructive_pattern_misses_innocuous_text(text: str) -> None:
    assert _DESTRUCTIVE_TEXT_PATTERN.search(text) is None


# ---------------------------------------------------------------------------
# _looks_like_destructive_at_point classification
# ---------------------------------------------------------------------------


def _ba(safety: SafetyConfig | None = None) -> BrowserActions:
    """Build a BrowserActions with a default AppConfig and mocked deps."""
    cfg = AppConfig(safety=safety or SafetyConfig())
    return BrowserActions(
        config=cfg,
        browser_manager=MagicMock(),
        sessions=MagicMock(),
    )


def test_destructive_empty_list_returns_false() -> None:
    assert _ba()._looks_like_destructive_at_point([]) is False


def test_destructive_button_type_submit() -> None:
    elements: list[dict[str, Any]] = [
        {
            "tag": "button",
            "type": "submit",
            "role": None,
            "text": "OK",
            "aria": None,
            "in_form": True,
        },
    ]
    assert _ba()._looks_like_destructive_at_point(elements) is True


def test_destructive_input_type_submit() -> None:
    elements: list[dict[str, Any]] = [
        {"tag": "input", "type": "submit", "role": None, "text": "", "aria": None, "in_form": True},
    ]
    assert _ba()._looks_like_destructive_at_point(elements) is True


def test_destructive_role_button_with_destructive_text() -> None:
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "type": None,
            "role": "button",
            "text": "Delete account",
            "aria": None,
            "in_form": False,
        },
    ]
    assert _ba()._looks_like_destructive_at_point(elements) is True


def test_destructive_anchor_with_login_text() -> None:
    elements: list[dict[str, Any]] = [
        {"tag": "a", "type": None, "role": None, "text": "Sign in", "aria": None, "in_form": False},
    ]
    assert _ba()._looks_like_destructive_at_point(elements) is True


def test_destructive_input_type_button_with_value_text() -> None:
    """v1.6.9 review I-1: <input type='button' value='Delete account'>
    must be classified destructive. The elementFromPoint JS captures
    n.value into the text field for input/textarea; the classifier
    extends the tag check to include 'input'."""
    elements: list[dict[str, Any]] = [
        {
            "tag": "input",
            "type": "button",
            "role": None,
            "text": "Delete account",  # came from value=
            "aria": None,
            "in_form": False,
        },
    ]
    assert _ba()._looks_like_destructive_at_point(elements) is True


def test_destructive_button_with_innocuous_text_is_false() -> None:
    elements: list[dict[str, Any]] = [
        {
            "tag": "button",
            "type": "button",
            "role": None,
            "text": "Read more",
            "aria": None,
            "in_form": False,
        },
    ]
    assert _ba()._looks_like_destructive_at_point(elements) is False


def test_destructive_walks_ancestors() -> None:
    # Inner div is innocuous but its parent is a submit button -> destructive
    elements: list[dict[str, Any]] = [
        {"tag": "span", "type": None, "role": None, "text": "OK", "aria": None, "in_form": True},
        {
            "tag": "button",
            "type": "submit",
            "role": None,
            "text": "OK",
            "aria": None,
            "in_form": True,
        },
    ]
    assert _ba()._looks_like_destructive_at_point(elements) is True


# ---------------------------------------------------------------------------
# _do_click_xy gating
# ---------------------------------------------------------------------------


def _click_input() -> ClickXYInput:
    return ClickXYInput(action="click_xy", x=100.0, y=100.0, button=MouseButton.LEFT, clicks=1)


@pytest.mark.asyncio
async def test_click_xy_rejected_when_allow_coordinate_clicks_false() -> None:
    ba = _ba(safety=SafetyConfig(allow_coordinate_clicks=False))
    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.click = AsyncMock()

    result = await ba._do_click_xy(page, _click_input())

    assert result.status == ActionStatus.FAILED
    assert result.action == ActionType.CLICK_XY
    assert "allow_coordinate_clicks" in (result.error_message or "")
    page.mouse.click.assert_not_called()


@pytest.mark.asyncio
async def test_click_xy_rejected_in_safe_mode() -> None:
    ba = _ba(safety=SafetyConfig(safe_mode=True))
    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.click = AsyncMock()

    result = await ba._do_click_xy(page, _click_input())

    assert result.status == ActionStatus.FAILED
    assert "safe_mode" in (result.error_message or "")
    page.mouse.click.assert_not_called()


@pytest.mark.asyncio
async def test_click_xy_runs_when_allowed_and_no_destructive_inspection_needed() -> None:
    # allow_form_submit=True -> no inspection, just click
    ba = _ba(safety=SafetyConfig(allow_form_submit=True, allow_coordinate_clicks=True))
    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.click = AsyncMock()
    page.evaluate = AsyncMock(return_value=[])  # should not be called

    result = await ba._do_click_xy(page, _click_input())

    assert result.status == ActionStatus.SUCCESS
    page.mouse.click.assert_awaited_once()
    page.evaluate.assert_not_called()


@pytest.mark.asyncio
async def test_click_xy_inspects_when_form_submit_disabled_and_blocks_submit() -> None:
    ba = _ba(safety=SafetyConfig(allow_form_submit=False, allow_coordinate_clicks=True))
    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.click = AsyncMock()
    page.evaluate = AsyncMock(
        return_value=[
            {
                "tag": "button",
                "type": "submit",
                "role": None,
                "text": "Send",
                "aria": None,
                "in_form": True,
            }
        ]
    )

    result = await ba._do_click_xy(page, _click_input())

    assert result.status == ActionStatus.FAILED
    assert "submit" in (result.error_message or "").lower()
    page.evaluate.assert_awaited_once()
    page.mouse.click.assert_not_called()


@pytest.mark.asyncio
async def test_click_xy_inspects_when_form_submit_disabled_and_allows_non_destructive() -> None:
    ba = _ba(safety=SafetyConfig(allow_form_submit=False, allow_coordinate_clicks=True))
    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.click = AsyncMock()
    page.evaluate = AsyncMock(
        return_value=[
            {
                "tag": "a",
                "type": None,
                "role": None,
                "text": "Read more",
                "aria": None,
                "in_form": False,
            }
        ]
    )

    result = await ba._do_click_xy(page, _click_input())

    assert result.status == ActionStatus.SUCCESS
    page.mouse.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_click_xy_inspection_failure_allows_click() -> None:
    """If elementFromPoint throws, treat as 'cannot tell' -> allow click.

    Matches the selector-path behavior where _looks_like_submit returning
    False also allows the click. We only block on positive evidence of
    a destructive control.
    """
    ba = _ba(safety=SafetyConfig(allow_form_submit=False, allow_coordinate_clicks=True))
    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.click = AsyncMock()
    page.evaluate = AsyncMock(side_effect=RuntimeError("eval boom"))

    result = await ba._do_click_xy(page, _click_input())

    assert result.status == ActionStatus.SUCCESS
    page.mouse.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_click_xy_inspection_empty_list_allows_click() -> None:
    ba = _ba(safety=SafetyConfig(allow_form_submit=False, allow_coordinate_clicks=True))
    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.click = AsyncMock()
    page.evaluate = AsyncMock(return_value=[])

    result = await ba._do_click_xy(page, _click_input())

    assert result.status == ActionStatus.SUCCESS
    page.mouse.click.assert_awaited_once()
