"""v1.6.9 BrowserConfig.locale / timezone_id / user_agent_mode tests.

Previously hardcoded in BrowserManager._build_context. v1.6.9 makes them
configurable for reproducible agents while preserving v1.6.8 defaults.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from web_agent import BrowserConfig
from web_agent.browser_manager import BrowserManager, _resolve_user_agent
from web_agent.config import AppConfig
from web_agent.exceptions import ConfigError

# ---------------------------------------------------------------------------
# Config defaults preserve v1.6.8 behaviour
# ---------------------------------------------------------------------------


def test_locale_defaults_to_en_us() -> None:
    assert BrowserConfig().locale == "en-US"


def test_timezone_id_defaults_to_america_new_york() -> None:
    assert BrowserConfig().timezone_id == "America/New_York"


def test_user_agent_mode_defaults_to_random() -> None:
    assert BrowserConfig().user_agent_mode == "random"


def test_user_agent_defaults_to_none() -> None:
    assert BrowserConfig().user_agent is None


# ---------------------------------------------------------------------------
# Validator: explicit mode requires user_agent
# ---------------------------------------------------------------------------


def test_explicit_mode_requires_user_agent() -> None:
    with pytest.raises((ConfigError, ValidationError), match="user_agent"):
        BrowserConfig(user_agent_mode="explicit")


def test_explicit_mode_accepts_user_agent() -> None:
    bc = BrowserConfig(user_agent_mode="explicit", user_agent="MyBot/1.0")
    assert bc.user_agent == "MyBot/1.0"


# ---------------------------------------------------------------------------
# _resolve_user_agent helper
# ---------------------------------------------------------------------------


def test_resolve_ua_explicit_returns_pinned_string() -> None:
    bc = BrowserConfig(user_agent_mode="explicit", user_agent="MyBot/1.0")
    assert _resolve_user_agent(bc) == "MyBot/1.0"


def test_resolve_ua_playwright_default_returns_none() -> None:
    bc = BrowserConfig(user_agent_mode="playwright_default")
    assert _resolve_user_agent(bc) is None


def test_resolve_ua_random_returns_a_string() -> None:
    bc = BrowserConfig(user_agent_mode="random")
    ua = _resolve_user_agent(bc)
    assert isinstance(ua, str)
    assert len(ua) > 0


# ---------------------------------------------------------------------------
# Integration with _build_context (mocked Playwright)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_context_passes_configured_locale_and_tz() -> None:
    cfg = AppConfig(
        browser=BrowserConfig(
            locale="de-DE",
            timezone_id="Europe/Berlin",
            user_agent_mode="explicit",
            user_agent="MyBot/1.0",
        )
    )
    bm = BrowserManager(cfg)

    fake_ctx = MagicMock()
    fake_ctx.set_default_timeout = MagicMock()
    fake_ctx.set_default_navigation_timeout = MagicMock()
    fake_ctx.route = AsyncMock()
    bm._browser = MagicMock()
    bm._browser.new_context = AsyncMock(return_value=fake_ctx)

    await bm._build_context()
    _args, kwargs = bm._browser.new_context.call_args
    assert kwargs["locale"] == "de-DE"
    assert kwargs["timezone_id"] == "Europe/Berlin"
    assert kwargs["user_agent"] == "MyBot/1.0"


@pytest.mark.asyncio
async def test_build_context_explicit_user_agent_arg_wins_over_config() -> None:
    """Callers that pass a user_agent kwarg (e.g. search-engine rotation)
    keep priority over the config-derived default."""
    cfg = AppConfig(browser=BrowserConfig(user_agent_mode="explicit", user_agent="ConfigBot/1.0"))
    bm = BrowserManager(cfg)

    fake_ctx = MagicMock()
    fake_ctx.set_default_timeout = MagicMock()
    fake_ctx.set_default_navigation_timeout = MagicMock()
    fake_ctx.route = AsyncMock()
    bm._browser = MagicMock()
    bm._browser.new_context = AsyncMock(return_value=fake_ctx)

    await bm._build_context(user_agent="CallerBot/2.0")
    _args, kwargs = bm._browser.new_context.call_args
    assert kwargs["user_agent"] == "CallerBot/2.0"
