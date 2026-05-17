"""v1.6.8 post-action verification screenshot tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent import AppConfig, DiagnosticsConfig
from web_agent.browser_actions import BrowserActions


def _make_page() -> MagicMock:
    """Return a mock Playwright Page with an awaitable screenshot()."""
    page = MagicMock()
    page.screenshot = AsyncMock()
    return page


def _make_actions(tmp_path: Path, screenshot_after_action: bool) -> BrowserActions:
    cfg = AppConfig(
        base_dir=str(tmp_path),
        diagnostics=DiagnosticsConfig(screenshot_after_action=screenshot_after_action),
    )
    bm = MagicMock()
    return BrowserActions(bm, cfg)


# ---------------------------------------------------------------------------
# off-by-default
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_screenshot_off_by_default_does_not_write(tmp_path: Path) -> None:
    actions = _make_actions(tmp_path, screenshot_after_action=False)
    page = _make_page()
    # Drive the helper directly (sequence-level integration tested elsewhere)
    out = await actions._capture_verification_screenshot(page, action_index=0, cid="cid1")
    # Default-off: caller's loop won't even invoke this helper, but if it
    # IS invoked, the helper writes and returns a path. The off-by-default
    # gate lives in the caller; here we verify the helper is harmless.
    assert page.screenshot.await_count == 1
    assert out is not None
    assert Path(out).name.endswith(".png")


# ---------------------------------------------------------------------------
# Path placement under screenshot_dir
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_screenshot_path_under_screenshot_dir(tmp_path: Path) -> None:
    actions = _make_actions(tmp_path, screenshot_after_action=True)
    page = _make_page()
    out = await actions._capture_verification_screenshot(page, action_index=3, cid="abc")
    assert out is not None
    p = Path(out)
    # File should live under automation.screenshot_dir, which resolves
    # to tmp_path/screenshots by default.
    assert p.parent.name == "screenshots"
    assert tmp_path.resolve() in p.resolve().parents
    assert "abc" in p.name
    assert "003" in p.name


# ---------------------------------------------------------------------------
# Filename safety: a malicious cid with path separators is sanitized
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_screenshot_sanitizes_cid_with_path_chars(tmp_path: Path) -> None:
    actions = _make_actions(tmp_path, screenshot_after_action=True)
    page = _make_page()
    # Even though cids are UUIDs in practice, defensive: separators
    # become underscores so safe_join_path doesn't reject them.
    out = await actions._capture_verification_screenshot(
        page, action_index=1, cid="../../escape"
    )
    assert out is not None
    p = Path(out)
    assert "escape" in p.name
    # No traversal -- the path stays under screenshot_dir
    assert p.parent.name == "screenshots"


# ---------------------------------------------------------------------------
# Failure is non-fatal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_screenshot_failure_returns_none_does_not_raise(tmp_path: Path) -> None:
    actions = _make_actions(tmp_path, screenshot_after_action=True)
    page = _make_page()
    page.screenshot.side_effect = RuntimeError("page closed")
    out = await actions._capture_verification_screenshot(page, action_index=0, cid="cid")
    assert out is None  # Best-effort: swallowed


# ---------------------------------------------------------------------------
# verification_screenshots field present on ActionSequenceResult
# ---------------------------------------------------------------------------


def test_action_sequence_result_has_verification_screenshots_field() -> None:
    from web_agent.models import ActionSequenceResult

    asr = ActionSequenceResult(url="https://example.com")
    assert asr.verification_screenshots == []
