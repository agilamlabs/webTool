"""v1.6.8 Remote CDP backend tests.

Validates the config gate (loopback, ws://, no-isolation), the
``BrowserManager.start()`` dispatch path (uses ``connect_over_cdp``
instead of ``launch``), and the ``get_remote_cdp_url`` accessor.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from web_agent import AppConfig, BrowserConfig
from web_agent.browser_manager import BrowserManager
from web_agent.exceptions import BrowserError, ConfigError

# ---------------------------------------------------------------------------
# Validator gate
# ---------------------------------------------------------------------------


def test_backend_remote_cdp_requires_remote_cdp_url() -> None:
    with pytest.raises((ConfigError, ValidationError), match="remote_cdp_url"):
        BrowserConfig(backend="remote_cdp")


def test_backend_remote_cdp_rejects_non_loopback_url() -> None:
    with pytest.raises((ConfigError, ValidationError), match="not loopback"):
        BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://example.com:9222/devtools/browser/x",
        )


def test_backend_remote_cdp_rejects_non_ws_scheme() -> None:
    with pytest.raises((ConfigError, ValidationError), match="ws://"):
        BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="https://127.0.0.1:9222/devtools/browser/x",
        )


def test_backend_remote_cdp_incompatible_with_isolation_mode() -> None:
    with pytest.raises((ConfigError, ValidationError), match="isolation_mode"):
        BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
            isolation_mode=True,
        )


# v1.6.8 review C-3 regression: the old exact-string allowlist
# {"127.0.0.1", "localhost", "::1"} missed the rest of 127.0.0.0/8.
# 127.0.0.2 still routes locally on every OS, but for safety we want the
# ipaddress.is_loopback check to be the source of truth.


@pytest.mark.parametrize(
    "url",
    [
        "ws://127.0.0.2:9222/devtools/browser/x",
        "ws://127.255.255.254:9222/devtools/browser/x",
        "wss://127.10.20.30:9222/devtools/browser/x",
    ],
)
def test_backend_remote_cdp_accepts_127_8_range(url: str) -> None:
    # No raise == accepted
    bc = BrowserConfig(backend="remote_cdp", remote_cdp_url=url)
    assert bc.backend == "remote_cdp"


@pytest.mark.parametrize(
    "url",
    [
        # Non-loopback IPv4
        "ws://10.0.0.1:9222/devtools/browser/x",
        "ws://192.168.1.1:9222/devtools/browser/x",
        # Public IPv4
        "ws://8.8.8.8:9222/devtools/browser/x",
        # Non-loopback IPv6
        "ws://[fe80::1]:9222/devtools/browser/x",
        # Empty host (validators must reject)
        "ws:///devtools/browser/x",
    ],
)
def test_backend_remote_cdp_rejects_non_loopback_ip(url: str) -> None:
    with pytest.raises((ConfigError, ValidationError), match="not loopback"):
        BrowserConfig(backend="remote_cdp", remote_cdp_url=url)


def test_backend_remote_cdp_accepts_ipv6_loopback_bracket_form() -> None:
    # urlparse strips the brackets internally; verify ::1 passes.
    bc = BrowserConfig(
        backend="remote_cdp",
        remote_cdp_url="ws://[::1]:9222/devtools/browser/x",
    )
    assert bc.backend == "remote_cdp"


# ---------------------------------------------------------------------------
# BrowserManager.start() dispatches to connect_over_cdp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backend_remote_cdp_dispatch_calls_connect_over_cdp_not_launch() -> None:
    cfg = AppConfig(
        browser=BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
        )
    )
    bm = BrowserManager(cfg)

    # Mock the stealth-wrapped Playwright entry context.
    fake_browser = MagicMock(name="Browser")
    fake_browser.close = AsyncMock()
    fake_pw = MagicMock(name="Playwright")
    fake_pw.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    fake_pw.chromium.launch = AsyncMock(
        side_effect=AssertionError("launch must NOT be called for remote_cdp")
    )
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    with patch.object(bm._stealth, "use_async", return_value=fake_cm):
        await bm.start()

    fake_pw.chromium.connect_over_cdp.assert_awaited_once_with(
        "ws://127.0.0.1:9222/devtools/browser/x"
    )
    fake_pw.chromium.launch.assert_not_awaited()
    assert bm._is_remote_cdp is True


@pytest.mark.asyncio
async def test_backend_remote_cdp_stop_disconnects_without_killing_process() -> None:
    """Per Playwright docs: ``close()`` on a connect_over_cdp browser disconnects,
    not terminates. Our stop() path therefore just calls ``close()`` -- the
    important invariant is that ``stop()`` doesn't raise."""
    cfg = AppConfig(
        browser=BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
        )
    )
    bm = BrowserManager(cfg)
    fake_browser = MagicMock(name="Browser")
    fake_browser.close = AsyncMock()
    fake_pw = MagicMock(name="Playwright")
    fake_pw.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    with patch.object(bm._stealth, "use_async", return_value=fake_cm):
        await bm.start()
        await bm.stop()
    # close() called (disconnect path); _is_remote_cdp reset
    fake_browser.close.assert_awaited()
    assert bm._is_remote_cdp is False


@pytest.mark.asyncio
async def test_backend_remote_cdp_connection_failure_wraps_as_browser_error() -> None:
    cfg = AppConfig(
        browser=BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://127.0.0.1:9999/devtools/browser/x",
        )
    )
    bm = BrowserManager(cfg)
    fake_pw = MagicMock(name="Playwright")
    fake_pw.chromium.connect_over_cdp = AsyncMock(
        side_effect=Exception("connection refused")
    )
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_cm.__aexit__ = AsyncMock(return_value=None)

    with patch.object(bm._stealth, "use_async", return_value=fake_cm):
        with pytest.raises(BrowserError, match="remote CDP"):
            await bm.start()


# ---------------------------------------------------------------------------
# get_remote_cdp_url accessor
# ---------------------------------------------------------------------------


def test_get_remote_cdp_url_returns_none_for_default_backend() -> None:
    bm = BrowserManager(AppConfig())
    assert bm.get_remote_cdp_url() is None


@pytest.mark.asyncio
async def test_get_remote_cdp_url_returns_configured_url_after_start() -> None:
    url = "ws://127.0.0.1:9222/devtools/browser/x"
    cfg = AppConfig(browser=BrowserConfig(backend="remote_cdp", remote_cdp_url=url))
    bm = BrowserManager(cfg)
    fake_browser = MagicMock(name="Browser")
    fake_browser.close = AsyncMock()
    fake_pw = MagicMock(name="Playwright")
    fake_pw.chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    fake_cm = MagicMock()
    fake_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_cm.__aexit__ = AsyncMock(return_value=None)
    with patch.object(bm._stealth, "use_async", return_value=fake_cm):
        await bm.start()
        try:
            assert bm.get_remote_cdp_url() == url
        finally:
            await bm.stop()
