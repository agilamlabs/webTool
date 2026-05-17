"""v1.6.8 DiagnosticsConfig + BrowserConfig.remote_cdp_url tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from web_agent import AppConfig, BrowserConfig, DiagnosticsConfig
from web_agent.exceptions import ConfigError

# ---------------------------------------------------------------------------
# DiagnosticsConfig defaults
# ---------------------------------------------------------------------------


def test_diagnostics_config_defaults_all_false() -> None:
    diag = DiagnosticsConfig()
    assert diag.capture_network is False
    assert diag.capture_download_intents is False
    assert diag.screenshot_after_action is False
    assert diag.trace_enabled is False
    # Numeric / list defaults present:
    assert diag.max_network_events == 500
    assert "xhr" in diag.network_resource_types
    assert "fetch" in diag.network_resource_types


def test_diagnostics_config_trace_dir_resolved_against_base_dir(tmp_path: Path) -> None:
    cfg = AppConfig(base_dir=str(tmp_path))
    # _resolve_paths runs in @model_validator(mode="after")
    assert Path(cfg.diagnostics.trace_dir).is_absolute()
    assert str(tmp_path.resolve()) in cfg.diagnostics.trace_dir


def test_diagnostics_max_network_events_bounds_low() -> None:
    with pytest.raises(ValidationError):
        DiagnosticsConfig(max_network_events=0)


def test_diagnostics_max_network_events_bounds_high() -> None:
    with pytest.raises(ValidationError):
        DiagnosticsConfig(max_network_events=99999)


def test_diagnostics_capture_network_independent_of_download_intents() -> None:
    diag = DiagnosticsConfig(capture_network=True)
    assert diag.capture_network is True
    assert diag.capture_download_intents is False
    diag2 = DiagnosticsConfig(capture_download_intents=True)
    assert diag2.capture_network is False
    assert diag2.capture_download_intents is True


def test_diagnostics_screenshot_after_action_default_false() -> None:
    assert DiagnosticsConfig().screenshot_after_action is False


def test_app_config_exposes_diagnostics_sub_block() -> None:
    cfg = AppConfig()
    assert isinstance(cfg.diagnostics, DiagnosticsConfig)
    assert cfg.diagnostics.capture_network is False


def test_diagnostics_env_var_override_via_double_underscore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_AGENT_DIAGNOSTICS__CAPTURE_NETWORK", "true")
    cfg = AppConfig()
    assert cfg.diagnostics.capture_network is True


# ---------------------------------------------------------------------------
# BrowserConfig.remote_cdp_url + backend extension
# ---------------------------------------------------------------------------


def test_remote_cdp_url_field_exposed_under_browser_config() -> None:
    bc = BrowserConfig()
    assert hasattr(bc, "remote_cdp_url")
    assert bc.remote_cdp_url is None


def test_backend_literal_widened_to_remote_cdp() -> None:
    # accepted by Pydantic Literal -- no validation error
    bc = BrowserConfig(
        backend="remote_cdp", remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x"
    )
    assert bc.backend == "remote_cdp"


def test_remote_cdp_without_url_rejected() -> None:
    with pytest.raises((ConfigError, ValidationError)):
        BrowserConfig(backend="remote_cdp")


def test_remote_cdp_non_loopback_rejected() -> None:
    with pytest.raises((ConfigError, ValidationError), match="not loopback"):
        BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://evil.example.com/devtools/browser/x",
        )


def test_remote_cdp_non_ws_scheme_rejected() -> None:
    with pytest.raises((ConfigError, ValidationError), match="ws://"):
        BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="http://127.0.0.1:9222/devtools/browser/x",
        )


def test_remote_cdp_incompatible_with_isolation_mode() -> None:
    with pytest.raises((ConfigError, ValidationError), match="isolation_mode"):
        BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
            isolation_mode=True,
        )


def test_remote_cdp_incompatible_with_cdp_enabled() -> None:
    with pytest.raises((ConfigError, ValidationError), match="cdp_enabled"):
        BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
            cdp_enabled=True,
        )


def test_remote_cdp_url_set_without_remote_cdp_backend_rejected() -> None:
    """Setting remote_cdp_url with backend='playwright' is a foot-gun -- surface it."""
    with pytest.raises((ConfigError, ValidationError), match="remote_cdp_url"):
        BrowserConfig(remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x")
