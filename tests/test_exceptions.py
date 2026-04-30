"""Tests verifying exception classes are actually raised at expected sites."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from web_agent.config import AppConfig, SafetyConfig
from web_agent.exceptions import (
    ActionError,
    ConfigError,
    DomainNotAllowedError,
    SelectorNotFoundError,
)
from web_agent.models import LocatorSpec, SelectInput
from web_agent.utils import check_domain_allowed


class TestConfigError:
    def test_invalid_yaml_raises_config_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("not: valid: yaml: nested: invalid:")
        with pytest.raises(ConfigError, match="Failed to parse YAML"):
            AppConfig.from_yaml(bad)

    def test_invalid_field_value_raises_config_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        # max_pages_per_call must be an int
        bad.write_text("safety:\n  max_pages_per_call: not_a_number\n")
        with pytest.raises(ConfigError, match="Config validation failed"):
            AppConfig.from_yaml(bad)

    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            AppConfig.from_yaml(tmp_path / "does_not_exist.yaml")


class TestDomainNotAllowedErrorStrict:
    def test_strict_raises_with_url_and_host_attrs(self) -> None:
        sc = SafetyConfig(denied_domains=["bad.com"])
        with pytest.raises(DomainNotAllowedError) as exc_info:
            check_domain_allowed("https://api.bad.com/x", sc, strict=True)
        assert exc_info.value.url == "https://api.bad.com/x"
        assert exc_info.value.host == "api.bad.com"

    def test_non_strict_returns_false_no_raise(self) -> None:
        sc = SafetyConfig(denied_domains=["bad.com"])
        # Default strict=False
        assert check_domain_allowed("https://api.bad.com/x", sc) is False


class TestSelectorNotFoundError:
    def test_resolve_locator_raises_on_empty_spec(self) -> None:
        from web_agent.browser_actions import _resolve_locator

        # Pass a fake page; the empty LocatorSpec should raise before page is touched
        empty_spec = LocatorSpec()
        # Sanity: confirm it's actually empty
        assert empty_spec.is_empty()
        with pytest.raises(SelectorNotFoundError):
            _resolve_locator(None, empty_spec)  # type: ignore[arg-type]


class TestActionErrorOnInvalidSelectAction:
    @pytest.mark.asyncio
    async def test_select_without_value_label_or_index_raises(self) -> None:
        from web_agent.browser_actions import BrowserActions
        from web_agent.config import AppConfig

        cfg = AppConfig()
        # We don't need a real browser/page -- _do_select raises before
        # touching the page when value/label/index are all None.
        actions = BrowserActions(browser_manager=None, config=cfg)  # type: ignore[arg-type]
        bad = SelectInput(selector="select#country")
        with pytest.raises(ActionError, match="value, label, or index"):
            await actions._do_select(None, bad)  # type: ignore[arg-type]


class TestExceptionsAreInPublicAPI:
    """Ensure all the exception classes still export from the public package."""

    def test_all_exception_classes_importable(self) -> None:
        from web_agent import (
            ActionError,
            ActionTimeoutError,
            BrowserError,
            BudgetExceededError,
            ConfigError,
            DomainNotAllowedError,
            DownloadError,
            ExtractionError,
            NavigationError,
            SafeModeBlockedError,
            SearchError,
            SelectorNotFoundError,
            WebAgentError,
        )

        # All inherit from WebAgentError
        for cls in (
            ActionError,
            ActionTimeoutError,
            BrowserError,
            BudgetExceededError,
            ConfigError,
            DomainNotAllowedError,
            DownloadError,
            ExtractionError,
            NavigationError,
            SafeModeBlockedError,
            SearchError,
            SelectorNotFoundError,
        ):
            assert issubclass(cls, WebAgentError)
