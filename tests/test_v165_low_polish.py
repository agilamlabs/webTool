"""v1.6.5 low-severity / polish fixes.

- L11: take_screenshot uses FetchConfig.wait_until (not hardcoded networkidle).
- L12: shared _OFFICE_AND_ARCHIVE_EXTENSIONS keeps fetcher/downloader in sync.
- L13: MCP server honors WEB_AGENT_CONFIG env var for YAML config.
- L14: dialog state stored in WeakKeyDictionary, not on the Page object.
- L16: SafetyConfig auto-normalizes URL-shaped deny/allow patterns to hostnames.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ----------------------------------------------------------------------
# L11: screenshot wait_until follows fetch config
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_take_screenshot_uses_configured_wait_until(tmp_path):
    """The screenshot helper must use FetchConfig.wait_until (default
    'domcontentloaded'), not the hardcoded 'networkidle' that v1.6.4
    used."""
    from unittest.mock import AsyncMock, MagicMock

    from web_agent.browser_actions import BrowserActions
    from web_agent.config import AppConfig, AutomationConfig, FetchConfig

    config = AppConfig(
        fetch=FetchConfig(wait_until="load"),
        automation=AutomationConfig(screenshot_dir=str(tmp_path)),
    )

    # Mock browser_manager so new_page returns a fake context manager.
    fake_page = MagicMock()
    fake_page.goto = AsyncMock()
    fake_page.screenshot = AsyncMock()
    fake_page.close = AsyncMock()
    type(fake_page).url = property(lambda _self: "https://example.com/")

    class FakePageCtx:
        async def __aenter__(self):
            return fake_page

        async def __aexit__(self, *_a):
            return False

    fake_bm = MagicMock()
    fake_bm.new_page = MagicMock(return_value=FakePageCtx())

    actions = BrowserActions(fake_bm, config)

    # File needs to exist for the post-screenshot stat() call.
    # Use a relative filename so safe_join_path resolves it under
    # the configured screenshot_dir (which IS tmp_path).
    out = tmp_path / "shot.png"
    out.write_bytes(b"\x89PNG\r\n\x1a\n")

    await actions.take_screenshot("https://example.com/", path="shot.png")

    fake_page.goto.assert_called_once()
    _, kwargs = fake_page.goto.call_args
    assert kwargs.get("wait_until") == "load"


# ----------------------------------------------------------------------
# L12: shared extensions constant
# ----------------------------------------------------------------------


def test_office_and_archive_extensions_shared():
    """Both modules consult the same source of truth for office/archive types."""
    from web_agent.downloader import _BINARY_EXTENSIONS
    from web_agent.web_fetcher import _DOWNLOAD_EXTENSIONS, _OFFICE_AND_ARCHIVE_EXTENSIONS

    # Both supersets contain the shared subset
    assert _OFFICE_AND_ARCHIVE_EXTENSIONS.issubset(_DOWNLOAD_EXTENSIONS)
    assert _OFFICE_AND_ARCHIVE_EXTENSIONS.issubset(_BINARY_EXTENSIONS)
    # Sanity: the shared set has the obvious entries
    for ext in (".pdf", ".docx", ".xlsx", ".csv", ".zip"):
        assert ext in _OFFICE_AND_ARCHIVE_EXTENSIONS


def test_download_extensions_includes_installers_not_in_binary_extensions():
    """OS installers are downloaded by the fetcher path but not the binary
    extension set (we route around them but don't extract them)."""
    from web_agent.downloader import _BINARY_EXTENSIONS
    from web_agent.web_fetcher import _DOWNLOAD_EXTENSIONS

    for ext in (".iso", ".deb", ".rpm"):
        assert ext in _DOWNLOAD_EXTENSIONS
        assert ext not in _BINARY_EXTENSIONS


# ----------------------------------------------------------------------
# L13: MCP server config loader
# ----------------------------------------------------------------------


def test_load_mcp_config_no_env_var_returns_default(monkeypatch):
    monkeypatch.delenv("WEB_AGENT_CONFIG", raising=False)
    from web_agent.mcp_server import _load_mcp_config

    config = _load_mcp_config()
    # Defaults: safe_mode False
    assert config.safety.safe_mode is False


def test_load_mcp_config_loads_yaml(monkeypatch, tmp_path: Path):
    """A real YAML pointed-at by WEB_AGENT_CONFIG configures the server."""
    yaml_content = """
log_level: DEBUG
safety:
  safe_mode: true
  denied_domains:
    - evil.example.com
"""
    yaml_path = tmp_path / "mcp.yaml"
    yaml_path.write_text(yaml_content, encoding="utf-8")
    monkeypatch.setenv("WEB_AGENT_CONFIG", str(yaml_path))

    from web_agent.mcp_server import _load_mcp_config

    config = _load_mcp_config()
    assert config.log_level == "DEBUG"
    assert config.safety.safe_mode is True
    # safe_mode forces all allow_* to False (covered by SafetyConfig validator)
    assert config.safety.allow_js_evaluation is False


def test_load_mcp_config_missing_file_falls_back_to_default(monkeypatch, tmp_path):
    """Pointing at a nonexistent file logs a warning but doesn't crash."""
    monkeypatch.setenv("WEB_AGENT_CONFIG", str(tmp_path / "does-not-exist.yaml"))
    from web_agent.mcp_server import _load_mcp_config

    config = _load_mcp_config()
    assert config.safety.safe_mode is False


# ----------------------------------------------------------------------
# L14: WeakKeyDictionary-based dialog state
# ----------------------------------------------------------------------


def test_dialog_state_uses_weak_key_dictionary():
    """The internal store is a WeakKeyDictionary so closing a Page
    automatically reclaims the entry."""
    from weakref import WeakKeyDictionary

    from web_agent.browser_actions import _PAGE_DIALOG_STATES

    assert isinstance(_PAGE_DIALOG_STATES, WeakKeyDictionary)


def test_page_dialog_state_no_attribute_pollution():
    """v1.6.5 must NOT shove ``_web_agent_dialog_state`` onto Page objects.

    Verified by reading the source of execute_sequence -- the literal
    attribute name should not appear there anymore.
    """
    import inspect

    from web_agent.browser_actions import BrowserActions

    src = inspect.getsource(BrowserActions.execute_sequence)
    assert "_web_agent_dialog_state" not in src


# ----------------------------------------------------------------------
# L16: domain pattern normalization
# ----------------------------------------------------------------------


def test_safety_config_normalizes_full_url_pattern():
    from web_agent.config import SafetyConfig

    sc = SafetyConfig(denied_domains=["https://Evil.com/"])
    assert sc.denied_domains == ["evil.com"]


def test_safety_config_normalizes_leading_dot():
    from web_agent.config import SafetyConfig

    sc = SafetyConfig(allowed_domains=[".Example.com"])
    assert sc.allowed_domains == ["example.com"]


def test_safety_config_strips_path_query_fragment():
    from web_agent.config import SafetyConfig

    sc = SafetyConfig(denied_domains=["evil.com/some/path?q=1#frag"])
    assert sc.denied_domains == ["evil.com"]


def test_safety_config_drops_empty_after_normalization():
    """A pattern that normalizes to empty (e.g. just ``"/"``) is dropped."""
    from web_agent.config import SafetyConfig

    sc = SafetyConfig(denied_domains=["/", "", ".", "real.com"])
    assert sc.denied_domains == ["real.com"]


def test_safety_config_normalized_pattern_actually_blocks():
    """End-to-end: normalized URL pattern blocks the matching host."""
    from web_agent.config import SafetyConfig
    from web_agent.utils import check_domain_allowed

    sc = SafetyConfig(denied_domains=["https://evil.com/path"])
    assert not check_domain_allowed("https://evil.com/anything", sc)
    assert not check_domain_allowed("https://api.evil.com/x", sc)
    assert check_domain_allowed("https://other.com/", sc)


def test_safety_config_preserves_well_formed_patterns():
    from web_agent.config import SafetyConfig

    sc = SafetyConfig(denied_domains=["evil.com", "x.com"])
    assert sc.denied_domains == ["evil.com", "x.com"]
