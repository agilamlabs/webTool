"""v1.6.9 --no-sandbox auto-detect tests.

Verifies the new ``browser.disable_chromium_sandbox`` config field and
the CI/container detection helper. Local-dev default is now to keep
Chromium's sandbox enabled (deliberate hardening from v1.6.8).
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from web_agent import BrowserConfig
from web_agent.browser_manager import _should_disable_chromium_sandbox

# ---------------------------------------------------------------------------
# Explicit cfg overrides
# ---------------------------------------------------------------------------


def test_explicit_true_returns_true() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert _should_disable_chromium_sandbox(True) is True


def test_explicit_false_returns_false_even_in_ci() -> None:
    with patch.dict(os.environ, {"CI": "true", "GITHUB_ACTIONS": "true"}, clear=True):
        assert _should_disable_chromium_sandbox(False) is False


# ---------------------------------------------------------------------------
# Auto-detect (cfg=None)
# ---------------------------------------------------------------------------


def test_autodetect_local_returns_false() -> None:
    with patch.dict(os.environ, {}, clear=True):
        # /.dockerenv check is via Path.exists() -- patch that explicitly
        with patch.object(Path, "exists", return_value=False):
            assert _should_disable_chromium_sandbox(None) is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE", "True"])
def test_autodetect_ci_env_var_true_disables_sandbox(value: str) -> None:
    with patch.dict(os.environ, {"CI": value}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            assert _should_disable_chromium_sandbox(None) is True


@pytest.mark.parametrize("value", ["false", "0", "", "no"])
def test_autodetect_ci_env_var_false_keeps_sandbox(value: str) -> None:
    with patch.dict(os.environ, {"CI": value}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            assert _should_disable_chromium_sandbox(None) is False


def test_autodetect_github_actions_disables_sandbox() -> None:
    with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
        with patch.object(Path, "exists", return_value=False):
            assert _should_disable_chromium_sandbox(None) is True


def test_autodetect_dockerenv_marker_disables_sandbox() -> None:
    with patch.dict(os.environ, {}, clear=True):
        with patch.object(Path, "exists", return_value=True):
            assert _should_disable_chromium_sandbox(None) is True


# ---------------------------------------------------------------------------
# Config field defaults
# ---------------------------------------------------------------------------


def test_browser_config_disable_chromium_sandbox_defaults_none() -> None:
    bc = BrowserConfig()
    assert bc.disable_chromium_sandbox is None


def test_browser_config_disable_chromium_sandbox_accepts_explicit() -> None:
    bc = BrowserConfig(disable_chromium_sandbox=True)
    assert bc.disable_chromium_sandbox is True
    bc = BrowserConfig(disable_chromium_sandbox=False)
    assert bc.disable_chromium_sandbox is False
