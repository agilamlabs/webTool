"""v1.6.6 Feature 1: Browser Isolation Profile Launcher tests.

Verifies:
- Off by default: launch args contain no --user-data-dir
- Ephemeral mode creates a temp profile under base_dir/.webtool/browser-profiles/
- Ephemeral cleanup_on_exit=True removes the profile on stop()
- Named mode persists the profile across stop() / restart
- Named mode without profile_dir raises ConfigError
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from web_agent.browser_manager import BrowserManager
from web_agent.config import AppConfig, BrowserConfig
from web_agent.exceptions import ConfigError


def _make_config(tmp_path: Path, **browser_overrides) -> AppConfig:
    """Build an AppConfig rooted at tmp_path with browser overrides."""
    return AppConfig(base_dir=str(tmp_path), browser=BrowserConfig(**browser_overrides))


# ----------------------------------------------------------------------
# Off by default
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolation_off_no_user_data_dir_in_launch_args(tmp_path: Path) -> None:
    """When isolation_mode is False, chromium.launch is called with the four
    legacy flags and no --user-data-dir. v1.7.0: isolation defaults ON, so
    this non-isolated path must be opted into explicitly."""
    config = _make_config(tmp_path, isolation_mode=False)
    assert config.browser.isolation_mode is False  # sanity

    bm = BrowserManager(config)
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()

    fake_chromium = MagicMock()
    fake_chromium.launch = AsyncMock(return_value=fake_browser)
    fake_pw = MagicMock(chromium=fake_chromium)

    # Patch the stealth-wrapped async_playwright context-manager protocol.
    fake_pw_cm = MagicMock()
    fake_pw_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_pw_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_pw_cm):
        await bm.start()

    fake_chromium.launch.assert_called_once()
    args = fake_chromium.launch.call_args.kwargs["args"]
    # v1.6.9: --no-sandbox is now CI/container-conditional (opt-in
    # locally). The hardening check is in
    # tests/test_v169_no_sandbox_autodetect.py.
    assert all(not a.startswith("--user-data-dir") for a in args), args
    assert bm._effective_profile_dir is None

    await bm.stop()


# ----------------------------------------------------------------------
# Ephemeral: tempdir creation
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolation_ephemeral_creates_temp_profile_under_base_dir(
    tmp_path: Path,
) -> None:
    """Ephemeral mode creates a fresh tempdir under
    base_dir/.webtool/browser-profiles/run-<token>/, and passes it to
    chromium via --user-data-dir."""
    config = _make_config(tmp_path, isolation_mode=True, profile_mode="ephemeral")

    bm = BrowserManager(config)
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_root_ctx = MagicMock(name="EphemeralRootContext")
    fake_root_ctx.browser = fake_browser
    fake_root_ctx.close = AsyncMock()
    fake_chromium = MagicMock()
    fake_chromium.launch_persistent_context = AsyncMock(return_value=fake_root_ctx)
    fake_chromium.launch = AsyncMock(
        side_effect=AssertionError("ephemeral isolation must use launch_persistent_context")
    )
    fake_pw = MagicMock(chromium=fake_chromium)
    fake_pw_cm = MagicMock()
    fake_pw_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_pw_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_pw_cm):
        await bm.start()

    profile_root = tmp_path / ".webtool" / "browser-profiles"
    assert profile_root.exists()
    # Effective dir is a child of the root
    assert bm._effective_profile_dir is not None
    assert bm._effective_profile_dir.parent == profile_root.resolve()
    assert bm._effective_profile_dir.exists()
    assert bm._owned_profile_dir is True

    # v1.7.0: the user-data-dir is passed as the launch_persistent_context
    # kwarg, NOT a --user-data-dir CLI arg (Playwright >= 1.5x rejects the
    # CLI flag on chromium.launch -- the bug this refactor fixes).
    kwargs = fake_chromium.launch_persistent_context.call_args.kwargs
    assert kwargs["user_data_dir"] == str(bm._effective_profile_dir)
    args = kwargs["args"]
    assert all(not a.startswith("--user-data-dir") for a in args), args

    await bm.stop()


# ----------------------------------------------------------------------
# Ephemeral: cleanup
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolation_ephemeral_cleanup_on_exit_removes_dir(tmp_path: Path) -> None:
    """cleanup_on_exit=True (the default) removes the ephemeral profile
    dir on stop(). The profile must not survive Agent shutdown."""
    config = _make_config(
        tmp_path,
        isolation_mode=True,
        profile_mode="ephemeral",
        cleanup_on_exit=True,
    )

    bm = BrowserManager(config)
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_root_ctx = MagicMock(name="EphemeralRootContext")
    fake_root_ctx.browser = fake_browser
    fake_root_ctx.close = AsyncMock()
    fake_chromium = MagicMock()
    fake_chromium.launch_persistent_context = AsyncMock(return_value=fake_root_ctx)
    fake_pw = MagicMock(chromium=fake_chromium)
    fake_pw_cm = MagicMock()
    fake_pw_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_pw_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_pw_cm):
        await bm.start()

    profile_dir = bm._effective_profile_dir
    assert profile_dir is not None and profile_dir.exists()

    await bm.stop()

    assert not profile_dir.exists(), "ephemeral profile should be removed on stop()"


# ----------------------------------------------------------------------
# Named: persistence
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isolation_named_persists_after_stop(tmp_path: Path) -> None:
    """Named mode points at a stable directory that persists across
    runs. stop() must NOT remove it -- the user owns it.

    v1.6.9: named profiles now use chromium.launch_persistent_context
    (returns BrowserContext, not Browser) so the test mocks that path."""
    profile_path = "my-named-profile"
    config = _make_config(
        tmp_path,
        isolation_mode=True,
        profile_mode="named",
        profile_dir=profile_path,
        cleanup_on_exit=True,  # should be ignored for named profiles
    )

    bm = BrowserManager(config)
    # v1.7.0: stealth is applied per-context (not via the launch hook);
    # neutralize it so the MagicMock context isn't touched by real Stealth.
    bm._apply_stealth = AsyncMock()
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_ctx = MagicMock(name="PersistentContext")
    fake_ctx.browser = fake_browser
    fake_ctx.close = AsyncMock()
    fake_ctx.route = AsyncMock()
    fake_ctx.set_default_timeout = MagicMock()
    fake_ctx.set_default_navigation_timeout = MagicMock()
    fake_chromium = MagicMock()
    fake_chromium.launch_persistent_context = AsyncMock(return_value=fake_ctx)
    fake_chromium.launch = AsyncMock(
        side_effect=AssertionError("launch must NOT be called for named profile")
    )
    fake_pw = MagicMock(chromium=fake_chromium)
    fake_pw_cm = MagicMock()
    fake_pw_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_pw_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_pw_cm):
        await bm.start()

    resolved = bm._effective_profile_dir
    assert resolved is not None and resolved.exists()
    assert bm._owned_profile_dir is False  # we don't own named profiles
    # Drop a marker file into the profile to prove persistence
    (resolved / "marker.txt").write_text("hello", encoding="utf-8")

    await bm.stop()

    assert resolved.exists(), "named profile must survive stop()"
    assert (resolved / "marker.txt").read_text(encoding="utf-8") == "hello"


# ----------------------------------------------------------------------
# Named without profile_dir: rejected at config validation
# ----------------------------------------------------------------------


def test_isolation_named_without_profile_dir_raises_config_error() -> None:
    """profile_mode='named' without profile_dir set is a clear misconfig
    -- raise at config-load time, not at browser-launch time."""
    with pytest.raises(ConfigError, match="profile_dir"):
        BrowserConfig(isolation_mode=True, profile_mode="named", profile_dir=None)
