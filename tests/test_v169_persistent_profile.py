"""v1.6.9 named-profile -> launch_persistent_context dispatch tests.

Unit-level: mocks the Playwright entry point so we can assert which
chromium method was called without launching a real browser. A real
integration test that verifies cookie / localStorage persistence is in
``tests/test_agent.py`` under the integration marker.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from web_agent import AppConfig, BrowserConfig
from web_agent.browser_manager import BrowserManager, _NoCloseContextProxy


def _persistent_pw_mocks() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build the standard mock trio: persistent context, chromium, playwright.

    The persistent context exposes ``.browser`` returning a Browser mock,
    plus all the methods _build_context configures (route /
    set_default_timeout / set_default_navigation_timeout).
    """
    fake_browser = MagicMock(name="Browser")
    fake_browser.close = AsyncMock()
    fake_ctx = MagicMock(
        name="PersistentContext",
        spec=[
            "browser",
            "close",
            "route",
            "set_default_timeout",
            "set_default_navigation_timeout",
            "new_page",
            "cookies",
            # v1.7.0: crash-recovery wiring registers a "close" listener
            # on the persistent context via ctx.on(...).
            "on",
        ],
    )
    fake_ctx.browser = fake_browser
    fake_ctx.close = AsyncMock()
    fake_ctx.route = AsyncMock()
    fake_chromium = MagicMock(name="chromium")
    fake_chromium.launch_persistent_context = AsyncMock(return_value=fake_ctx)
    fake_chromium.launch = AsyncMock(
        side_effect=AssertionError("launch must NOT be called for named profile")
    )
    fake_pw = MagicMock(name="Playwright")
    fake_pw.chromium = fake_chromium
    return fake_ctx, fake_chromium, fake_pw


@pytest.mark.asyncio
async def test_named_profile_dispatches_to_launch_persistent_context(
    tmp_path: Path,
) -> None:
    expected_resolved = (tmp_path / "named-profile").resolve()
    cfg = AppConfig(
        base_dir=str(tmp_path),
        browser=BrowserConfig(
            isolation_mode=True,
            profile_mode="named",
            profile_dir="named-profile",
            cleanup_on_exit=False,
            headless=True,
        ),
    )
    bm = BrowserManager(cfg)
    bm._apply_stealth = AsyncMock()  # v1.7.0: stealth applied per-context; keep off the mock

    fake_ctx, fake_chromium, fake_pw = _persistent_pw_mocks()
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_cm):
        await bm.start()

    fake_chromium.launch_persistent_context.assert_awaited_once()
    # Verify user_data_dir kwarg matches the resolved profile dir
    _args, kwargs = fake_chromium.launch_persistent_context.call_args
    assert kwargs["user_data_dir"] == str(expected_resolved)
    assert kwargs["headless"] is True
    # And launch (the non-persistent path) was not called
    fake_chromium.launch.assert_not_awaited()
    # The persistent context is stored on the manager
    assert bm._persistent_context is fake_ctx


@pytest.mark.asyncio
async def test_ephemeral_profile_uses_launch_persistent_context(
    tmp_path: Path,
) -> None:
    """v1.7.0: ephemeral isolation now ALSO dispatches to
    launch_persistent_context. Playwright >= 1.5x rejects a
    --user-data-dir CLI arg on chromium.launch (the bug this refactor
    fixes), so both isolation flavors pass user_data_dir as a kwarg. The
    ephemeral root context is held as _ephemeral_root_context (NOT the
    named _persistent_context slot); per-session contexts are still fresh
    incognito contexts spun from root.browser, preserving isolation."""
    cfg = AppConfig(
        browser=BrowserConfig(
            isolation_mode=True,
            profile_mode="ephemeral",
            cleanup_on_exit=True,
            headless=True,
        )
    )
    bm = BrowserManager(cfg)

    fake_browser = MagicMock(name="Browser")
    fake_browser.close = AsyncMock()
    fake_root_ctx = MagicMock(name="EphemeralRootContext")
    fake_root_ctx.browser = fake_browser
    fake_root_ctx.close = AsyncMock()
    fake_chromium = MagicMock(name="chromium")
    fake_chromium.launch_persistent_context = AsyncMock(return_value=fake_root_ctx)
    fake_chromium.launch = AsyncMock(
        side_effect=AssertionError("ephemeral isolation must use launch_persistent_context")
    )
    fake_pw = MagicMock(name="Playwright")
    fake_pw.chromium = fake_chromium
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_cm):
        await bm.start()

    fake_chromium.launch_persistent_context.assert_awaited_once()
    fake_chromium.launch.assert_not_awaited()
    assert bm._persistent_context is None
    assert bm._ephemeral_root_context is fake_root_ctx


@pytest.mark.asyncio
async def test_build_context_returns_no_close_proxy_for_named_profile(
    tmp_path: Path,
) -> None:
    cfg = AppConfig(
        base_dir=str(tmp_path),
        browser=BrowserConfig(
            isolation_mode=True,
            profile_mode="named",
            profile_dir="p",
            cleanup_on_exit=False,
            headless=True,
        ),
    )
    bm = BrowserManager(cfg)
    bm._apply_stealth = AsyncMock()  # v1.7.0: stealth applied per-context; keep off the mock

    fake_ctx, _, fake_pw = _persistent_pw_mocks()
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_cm):
        await bm.start()

    ctx = await bm._build_context()
    assert isinstance(ctx, _NoCloseContextProxy)
    # Underlying context is the one we mocked
    assert ctx._ctx is fake_ctx


@pytest.mark.asyncio
async def test_no_close_proxy_close_is_noop(tmp_path: Path) -> None:
    cfg = AppConfig(
        base_dir=str(tmp_path),
        browser=BrowserConfig(
            isolation_mode=True,
            profile_mode="named",
            profile_dir="p",
            cleanup_on_exit=False,
            headless=True,
        ),
    )
    bm = BrowserManager(cfg)
    bm._apply_stealth = AsyncMock()  # v1.7.0: stealth applied per-context; keep off the mock

    fake_ctx, _, fake_pw = _persistent_pw_mocks()
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_cm):
        await bm.start()

    proxy = await bm._build_context()
    await proxy.close()
    # The underlying persistent context must NOT be closed by the proxy.
    fake_ctx.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_closes_persistent_context_first(tmp_path: Path) -> None:
    cfg = AppConfig(
        base_dir=str(tmp_path),
        browser=BrowserConfig(
            isolation_mode=True,
            profile_mode="named",
            profile_dir="p",
            cleanup_on_exit=False,
            headless=True,
        ),
    )
    bm = BrowserManager(cfg)
    bm._apply_stealth = AsyncMock()  # v1.7.0: stealth applied per-context; keep off the mock

    fake_ctx, _, fake_pw = _persistent_pw_mocks()
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_cm):
        await bm.start()
        await bm.stop()

    fake_ctx.close.assert_awaited_once()
    assert bm._persistent_context is None


@pytest.mark.asyncio
async def test_proxy_supports_async_context_manager_protocol(tmp_path: Path) -> None:
    """v1.6.9 review C-2: __aenter__ / __aexit__ must be defined on the
    proxy because Python looks up dunders on the class, not via
    __getattr__. ``async with proxy:`` must work without crashing.
    """
    cfg = AppConfig(
        base_dir=str(tmp_path),
        browser=BrowserConfig(
            isolation_mode=True,
            profile_mode="named",
            profile_dir="p",
            cleanup_on_exit=False,
            headless=True,
        ),
    )
    bm = BrowserManager(cfg)
    bm._apply_stealth = AsyncMock()  # v1.7.0: stealth applied per-context; keep off the mock

    fake_ctx, _, fake_pw = _persistent_pw_mocks()
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_cm):
        await bm.start()

    proxy = await bm._build_context()
    async with proxy:
        pass  # __aenter__ + __aexit__ must not raise
    # Underlying context still alive (no close call from __aexit__)
    fake_ctx.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_proxy_forwards_attribute_access(tmp_path: Path) -> None:
    cfg = AppConfig(
        base_dir=str(tmp_path),
        browser=BrowserConfig(
            isolation_mode=True,
            profile_mode="named",
            profile_dir="p",
            cleanup_on_exit=False,
            headless=True,
        ),
    )
    bm = BrowserManager(cfg)
    bm._apply_stealth = AsyncMock()  # v1.7.0: stealth applied per-context; keep off the mock

    fake_ctx, _, fake_pw = _persistent_pw_mocks()
    fake_ctx.new_page = AsyncMock(return_value=MagicMock())
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_cm):
        await bm.start()

    proxy = await bm._build_context()
    # __getattr__ should forward to underlying ctx
    await proxy.new_page()
    fake_ctx.new_page.assert_awaited_once()
