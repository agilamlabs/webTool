"""v1.6.7 Interaction Skill Library tests.

Exercises the 4 new Action types (UploadFileInput, IframeClickInput,
ShadowDomClickInput, DragAndDropInput) at the handler level, plus the
top-level scroll_until_text + print_page_as_pdf helpers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from web_agent.browser_actions import BrowserActions
from web_agent.config import AppConfig, DownloadConfig, SafetyConfig
from web_agent.models import (
    ActionStatus,
    ActionType,
    DragAndDropInput,
    IframeClickInput,
    ShadowDomClickInput,
    UploadFileInput,
)


def _make_page() -> MagicMock:
    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.wheel = AsyncMock()
    page.keyboard = MagicMock()
    page.locator = MagicMock()
    page.frame_locator = MagicMock()
    page.evaluate = AsyncMock(return_value="")
    page.wait_for_load_state = AsyncMock()
    return page


# ----------------------------------------------------------------------
# upload_file
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_file_under_download_dir_succeeds(tmp_path: Path) -> None:
    """Default safety: paths under download_dir are accepted."""
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    f = download_dir / "doc.pdf"
    f.write_bytes(b"%PDF-1.4")

    cfg = AppConfig(
        base_dir=str(tmp_path),
        download=DownloadConfig(download_dir=str(download_dir)),
    )

    ba = BrowserActions(MagicMock(), cfg, sessions=None)
    page = _make_page()
    fake_loc = MagicMock()
    fake_loc.set_input_files = AsyncMock()

    action = UploadFileInput(selector="input[type=file]", paths=[str(f)])
    with patch("web_agent.browser_actions._resolve_locator", return_value=fake_loc):
        result = await ba._do_upload_file(page, action)

    assert result.status == ActionStatus.SUCCESS
    fake_loc.set_input_files.assert_awaited_once()


@pytest.mark.asyncio
async def test_upload_file_outside_download_dir_blocked_by_default(tmp_path: Path) -> None:
    """Default safety: paths outside download_dir are rejected."""
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    sensitive = tmp_path / "secret.txt"
    sensitive.write_text("hello", encoding="utf-8")

    cfg = AppConfig(
        base_dir=str(tmp_path),
        download=DownloadConfig(download_dir=str(download_dir)),
        safety=SafetyConfig(allow_upload_outside_download_dir=False),
    )

    ba = BrowserActions(MagicMock(), cfg, sessions=None)
    page = _make_page()

    action = UploadFileInput(selector="#f", paths=[str(sensitive)])
    result = await ba._do_upload_file(page, action)

    assert result.status == ActionStatus.FAILED
    assert "outside download_dir" in (result.error_message or "")


@pytest.mark.asyncio
async def test_upload_file_outside_dir_allowed_when_flag_on(tmp_path: Path) -> None:
    """With safety.allow_upload_outside_download_dir=True, paths
    outside download_dir succeed."""
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    sensitive = tmp_path / "secret.txt"
    sensitive.write_text("hello", encoding="utf-8")

    cfg = AppConfig(
        base_dir=str(tmp_path),
        download=DownloadConfig(download_dir=str(download_dir)),
        safety=SafetyConfig(allow_upload_outside_download_dir=True),
    )

    ba = BrowserActions(MagicMock(), cfg, sessions=None)
    page = _make_page()
    fake_loc = MagicMock()
    fake_loc.set_input_files = AsyncMock()

    action = UploadFileInput(selector="#f", paths=[str(sensitive)])
    with patch("web_agent.browser_actions._resolve_locator", return_value=fake_loc):
        result = await ba._do_upload_file(page, action)

    assert result.status == ActionStatus.SUCCESS


@pytest.mark.asyncio
async def test_upload_file_nonexistent_path_blocked(tmp_path: Path) -> None:
    """Code-review M1: the path '/this/does/not/exist.txt' is absolute
    and outside download_dir. The containment check fires FIRST (no
    file-existence oracle), so the error names the containment failure,
    not the existence failure."""
    cfg = AppConfig(base_dir=str(tmp_path))
    ba = BrowserActions(MagicMock(), cfg, sessions=None)
    page = _make_page()

    action = UploadFileInput(selector="#f", paths=["/this/does/not/exist.txt"])
    result = await ba._do_upload_file(page, action)

    assert result.status == ActionStatus.FAILED
    assert "outside download_dir" in (result.error_message or "")


@pytest.mark.asyncio
async def test_upload_file_no_existence_oracle(tmp_path: Path) -> None:
    """Regression for code-review M1: both an existing and a
    non-existing absolute path outside download_dir must produce the
    SAME error class (no information leak about which paths exist)."""
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()
    # Real file outside download_dir
    real = tmp_path / "real.txt"
    real.write_text("hi", encoding="utf-8")
    fake = tmp_path / "does_not_exist.txt"

    cfg = AppConfig(
        base_dir=str(tmp_path),
        download=DownloadConfig(download_dir=str(download_dir)),
    )
    ba = BrowserActions(MagicMock(), cfg, sessions=None)
    page = _make_page()

    r_real = await ba._do_upload_file(
        page, UploadFileInput(selector="#f", paths=[str(real)])
    )
    r_fake = await ba._do_upload_file(
        page, UploadFileInput(selector="#f", paths=[str(fake)])
    )
    assert r_real.status == ActionStatus.FAILED
    assert r_fake.status == ActionStatus.FAILED
    # Same error reason regardless of existence -- no oracle
    assert "outside download_dir" in (r_real.error_message or "")
    assert "outside download_dir" in (r_fake.error_message or "")


# ----------------------------------------------------------------------
# iframe_click
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iframe_click_uses_frame_locator() -> None:
    cfg = AppConfig()
    ba = BrowserActions(MagicMock(), cfg, sessions=None)
    page = _make_page()
    fake_inner = MagicMock()
    fake_inner.click = AsyncMock()
    fake_frame = MagicMock()
    fake_frame.locator = MagicMock(return_value=fake_inner)
    page.frame_locator = MagicMock(return_value=fake_frame)

    action = IframeClickInput(
        iframe_selector="iframe#consent", inner_selector="button#agree"
    )
    result = await ba._do_iframe_click(page, action)

    page.frame_locator.assert_called_once_with("iframe#consent")
    fake_frame.locator.assert_called_once_with("button#agree")
    fake_inner.click.assert_awaited_once()
    assert result.status == ActionStatus.SUCCESS
    assert result.action == ActionType.IFRAME_CLICK


# ----------------------------------------------------------------------
# shadow_dom_click
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_dom_click_uses_pierce_combinator() -> None:
    cfg = AppConfig()
    ba = BrowserActions(MagicMock(), cfg, sessions=None)
    page = _make_page()
    fake_loc = MagicMock()
    fake_loc.click = AsyncMock()
    page.locator = MagicMock(return_value=fake_loc)

    action = ShadowDomClickInput(
        host_selector="cookie-banner", inner_selector="button.accept"
    )
    result = await ba._do_shadow_dom_click(page, action)

    page.locator.assert_called_once_with("cookie-banner >> button.accept")
    fake_loc.click.assert_awaited_once()
    assert result.status == ActionStatus.SUCCESS


# ----------------------------------------------------------------------
# drag_and_drop
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drag_and_drop_calls_locator_drag_to() -> None:
    cfg = AppConfig()
    ba = BrowserActions(MagicMock(), cfg, sessions=None)
    page = _make_page()
    src = MagicMock()
    src.drag_to = AsyncMock()
    tgt = MagicMock()
    resolved = [src, tgt]

    def _fake_resolve(_p, _s):
        return resolved.pop(0)

    action = DragAndDropInput(source="#a", target="#b")
    with patch(
        "web_agent.browser_actions._resolve_locator", side_effect=_fake_resolve
    ):
        result = await ba._do_drag_and_drop(page, action)

    src.drag_to.assert_awaited_once_with(tgt)
    assert result.status == ActionStatus.SUCCESS


# ----------------------------------------------------------------------
# scroll_until_text
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scroll_until_text_finds_text_in_initial_view() -> None:
    """If the text is already on-page, no scrolls are needed."""
    cfg = AppConfig()
    fake_tab_mgr = MagicMock()
    page = _make_page()
    page.evaluate = AsyncMock(return_value="The target string is here.")
    fake_tab_mgr.get_or_current = MagicMock(return_value=page)
    fake_sessions = MagicMock()
    fake_sessions.get_tab_manager = MagicMock(return_value=fake_tab_mgr)
    fake_sessions.touch = MagicMock()

    ba = BrowserActions(MagicMock(), cfg, sessions=fake_sessions)
    result = await ba.scroll_until_text(
        "target string", session_id="sid", max_scrolls=3
    )

    assert result.status == ActionStatus.SUCCESS
    assert result.data is not None
    assert result.data["scrolls_used"] == 0
    page.mouse.wheel.assert_not_awaited()


@pytest.mark.asyncio
async def test_scroll_until_text_scrolls_then_finds() -> None:
    """When text is not initially visible, scroll until it appears."""
    cfg = AppConfig()
    fake_tab_mgr = MagicMock()
    page = _make_page()
    # First call: empty. Second call (after 1 scroll): contains text.
    page.evaluate = AsyncMock(side_effect=["", "Found: target"])
    fake_tab_mgr.get_or_current = MagicMock(return_value=page)
    fake_sessions = MagicMock()
    fake_sessions.get_tab_manager = MagicMock(return_value=fake_tab_mgr)
    fake_sessions.touch = MagicMock()

    ba = BrowserActions(MagicMock(), cfg, sessions=fake_sessions)
    result = await ba.scroll_until_text("target", session_id="sid", max_scrolls=3)

    assert result.status == ActionStatus.SUCCESS
    assert result.data is not None
    assert result.data["scrolls_used"] == 1
    assert page.mouse.wheel.await_count == 1


@pytest.mark.asyncio
async def test_scroll_until_text_exhausted_returns_failed() -> None:
    cfg = AppConfig()
    fake_tab_mgr = MagicMock()
    page = _make_page()
    page.evaluate = AsyncMock(return_value="never present")
    fake_tab_mgr.get_or_current = MagicMock(return_value=page)
    fake_sessions = MagicMock()
    fake_sessions.get_tab_manager = MagicMock(return_value=fake_tab_mgr)
    fake_sessions.touch = MagicMock()

    ba = BrowserActions(MagicMock(), cfg, sessions=fake_sessions)
    result = await ba.scroll_until_text("missing", session_id="sid", max_scrolls=2)

    assert result.status == ActionStatus.FAILED
    assert result.data is not None
    assert result.data["found"] is False
    assert result.data["scrolls_used"] == 2


# ----------------------------------------------------------------------
# print_page_as_pdf
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_print_page_as_pdf_writes_under_screenshot_dir(tmp_path: Path) -> None:
    cfg = AppConfig(base_dir=str(tmp_path))
    fake_tab_mgr = MagicMock()
    page = _make_page()
    page.pdf = AsyncMock()
    type(page).url = property(lambda _self: "https://example.com/")
    fake_tab_mgr.get_or_current = MagicMock(return_value=page)
    fake_sessions = MagicMock()
    fake_sessions.get_tab_manager = MagicMock(return_value=fake_tab_mgr)
    fake_sessions.touch = MagicMock()

    ba = BrowserActions(MagicMock(), cfg, sessions=fake_sessions)
    result = await ba.print_page_as_pdf(session_id="sid")

    page.pdf.assert_awaited_once()
    out = Path(result.path)
    assert out.suffix == ".pdf"
    # Should be under <base>/screenshots/
    assert Path(cfg.automation.screenshot_dir).resolve() in out.parents
