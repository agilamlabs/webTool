"""v1.6.6 Feature 2: CDP attach to webTool-launched browser.

Verifies:
- Off by default: launch args contain no --remote-debugging-port
- cdp_enabled=True (with isolation) appends --remote-debugging-port and
  resolves the endpoint via DevToolsActivePort
- attach_existing_browser=True is rejected at config-validation time
- get_cdp_endpoint() returns None when disabled
- cdp_enabled=True without isolation_mode is rejected
- Non-loopback cdp_host is rejected
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from web_agent.browser_manager import BrowserManager
from web_agent.config import AppConfig, BrowserConfig
from web_agent.exceptions import ConfigError


def _make_config(tmp_path: Path, **browser_overrides) -> AppConfig:
    return AppConfig(base_dir=str(tmp_path), browser=BrowserConfig(**browser_overrides))


# ----------------------------------------------------------------------
# Off by default
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdp_off_no_remote_debugging_port_in_args(tmp_path: Path) -> None:
    # v1.7.0: cdp + isolation default ON. This test exercises the cdp-OFF
    # raw-launch path, so opt out explicitly (isolation_mode=False also
    # auto-disables cdp via the validator's reconciliation).
    config = _make_config(tmp_path, isolation_mode=False)
    assert config.browser.cdp_enabled is False

    bm = BrowserManager(config)
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_chromium = MagicMock()
    fake_chromium.launch = AsyncMock(return_value=fake_browser)
    fake_pw = MagicMock(chromium=fake_chromium)
    fake_pw_cm = MagicMock()
    fake_pw_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_pw_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_pw_cm):
        await bm.start()

    args = fake_chromium.launch.call_args.kwargs["args"]
    assert all(not a.startswith("--remote-debugging-port") for a in args), args
    assert bm.get_cdp_endpoint() is None

    await bm.stop()


# ----------------------------------------------------------------------
# On: appends arg and resolves endpoint from DevToolsActivePort
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdp_on_appends_arg_and_resolves_endpoint(tmp_path: Path) -> None:
    """CDP requires isolation. Simulate Chromium writing
    DevToolsActivePort into the profile dir post-launch, then verify
    get_cdp_endpoint returns the ws:// URL."""
    config = _make_config(
        tmp_path,
        isolation_mode=True,
        profile_mode="ephemeral",
        cdp_enabled=True,
        cdp_port=0,
    )

    bm = BrowserManager(config)
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_root_ctx = MagicMock(name="EphemeralRootContext")
    fake_root_ctx.browser = fake_browser
    fake_root_ctx.close = AsyncMock()
    fake_chromium = MagicMock()

    async def _launch_and_write_port_file(**_kwargs):
        # v1.7.0: ephemeral isolation dispatches to
        # launch_persistent_context (Playwright rejects --user-data-dir
        # on chromium.launch). Simulate Chromium writing
        # DevToolsActivePort into the user-data-dir as soon as it starts.
        assert bm._effective_profile_dir is not None
        (bm._effective_profile_dir / "DevToolsActivePort").write_text(
            "54321\n/devtools/browser/abc-123-uuid\n",
            encoding="utf-8",
        )
        return fake_root_ctx

    fake_chromium.launch_persistent_context = AsyncMock(side_effect=_launch_and_write_port_file)
    fake_chromium.launch = AsyncMock(
        side_effect=AssertionError("ephemeral isolation must use launch_persistent_context")
    )
    fake_pw = MagicMock(chromium=fake_chromium)
    fake_pw_cm = MagicMock()
    fake_pw_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_pw_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_pw_cm):
        await bm.start()

    args = fake_chromium.launch_persistent_context.call_args.kwargs["args"]
    assert "--remote-debugging-port=0" in args
    assert "--remote-debugging-address=127.0.0.1" in args

    endpoint = bm.get_cdp_endpoint()
    assert endpoint == "ws://127.0.0.1:54321/devtools/browser/abc-123-uuid"
    assert bm._cdp_port_resolved == 54321

    await bm.stop()
    # endpoint cleared after stop
    assert bm.get_cdp_endpoint() is None


# ----------------------------------------------------------------------
# attach_existing_browser is explicitly rejected
# ----------------------------------------------------------------------


def test_attach_existing_browser_true_raises_config_error() -> None:
    """The spec's safety rule: never attach to a user's real Chrome.
    Setting this flag at config-load time must error out with a clear
    message, not silently get ignored."""
    with pytest.raises(ConfigError, match="attach_existing_browser"):
        BrowserConfig(attach_existing_browser=True)


# ----------------------------------------------------------------------
# CDP requires isolation
# ----------------------------------------------------------------------


def test_cdp_without_isolation_raises_config_error() -> None:
    """CDP discovery reads DevToolsActivePort from the user-data-dir;
    without isolation_mode there is no user-data-dir to read from."""
    with pytest.raises(ConfigError, match="isolation_mode"):
        BrowserConfig(cdp_enabled=True, isolation_mode=False)


# ----------------------------------------------------------------------
# Non-loopback cdp_host rejected
# ----------------------------------------------------------------------


def test_cdp_non_loopback_host_raises_config_error() -> None:
    """Silent foot-gun: binding CDP to 0.0.0.0 exposes browser control
    to the network. Reject at validation time."""
    with pytest.raises(ConfigError, match="loopback"):
        BrowserConfig(cdp_enabled=True, isolation_mode=True, cdp_host="0.0.0.0")


# ----------------------------------------------------------------------
# backend=cdp_owned without cdp_enabled rejected
# ----------------------------------------------------------------------


def test_backend_cdp_owned_without_cdp_enabled_raises() -> None:
    """backend='cdp_owned' explicitly implies CDP. Mismatch is a misconfig."""
    with pytest.raises(ConfigError, match="cdp_enabled=True"):
        BrowserConfig(backend="cdp_owned", cdp_enabled=False)


# ----------------------------------------------------------------------
# Fixed port (cdp_port != 0): _discover_cdp_endpoint still works
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cdp_fixed_port_still_discovers_endpoint(tmp_path: Path) -> None:
    """v1.6.6 code-review #11: regression for the cdp_port != 0 path.
    The launch args carry the explicit port; Chromium still writes
    DevToolsActivePort and we still parse it the same way."""
    config = _make_config(
        tmp_path,
        isolation_mode=True,
        profile_mode="ephemeral",
        cdp_enabled=True,
        cdp_port=9222,
    )

    bm = BrowserManager(config)
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_root_ctx = MagicMock(name="EphemeralRootContext")
    fake_root_ctx.browser = fake_browser
    fake_root_ctx.close = AsyncMock()
    fake_chromium = MagicMock()

    async def _launch_and_write_port_file(**_kwargs):
        assert bm._effective_profile_dir is not None
        # Chromium writes the SAME port it was told to use
        (bm._effective_profile_dir / "DevToolsActivePort").write_text(
            "9222\n/devtools/browser/fixed-port-uuid\n",
            encoding="utf-8",
        )
        return fake_root_ctx

    fake_chromium.launch_persistent_context = AsyncMock(side_effect=_launch_and_write_port_file)
    fake_chromium.launch = AsyncMock(
        side_effect=AssertionError("ephemeral isolation must use launch_persistent_context")
    )
    fake_pw = MagicMock(chromium=fake_chromium)
    fake_pw_cm = MagicMock()
    fake_pw_cm.__aenter__ = AsyncMock(return_value=fake_pw)
    fake_pw_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("web_agent.browser_manager.async_playwright", return_value=fake_pw_cm):
        await bm.start()

    args = fake_chromium.launch_persistent_context.call_args.kwargs["args"]
    assert "--remote-debugging-port=9222" in args
    endpoint = bm.get_cdp_endpoint()
    assert endpoint == "ws://127.0.0.1:9222/devtools/browser/fixed-port-uuid"

    await bm.stop()


# ----------------------------------------------------------------------
# SSRF gate in Agent.new_tab (code-review #6)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_tab_blocks_disallowed_domain(tmp_path: Path) -> None:
    """v1.6.6 code-review #6: Agent.new_tab(url=...) used to bypass
    check_domain_allowed entirely, opening an SSRF back door for callers
    with a session_id. Now it raises DomainNotAllowedError on a denied
    or private-IP target."""
    from contextlib import asynccontextmanager

    from web_agent.agent import Agent
    from web_agent.config import AppConfig, SafetyConfig
    from web_agent.exceptions import DomainNotAllowedError

    cfg = AppConfig(
        base_dir=str(tmp_path),
        safety=SafetyConfig(block_private_ips=True),
    )
    # Bypass Agent.__aenter__ -- we only need _config and _sessions on the
    # instance to exercise new_tab's gate.
    agent = Agent.__new__(Agent)
    agent._config = cfg
    agent._sessions = MagicMock()

    @asynccontextmanager
    async def _noop_scope(*_a, **_kw):
        yield

    agent._call_scope = _noop_scope  # type: ignore[method-assign]

    # AWS IMDS link-local -- the canonical SSRF target.
    with pytest.raises(DomainNotAllowedError):
        await agent.new_tab(
            url="http://169.254.169.254/latest/meta-data/",
            session_id="any-sid",
        )
