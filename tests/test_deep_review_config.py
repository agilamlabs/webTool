"""Deep-review (post-v1.6.16) config-validation regression tests.

Covers the config.py findings surfaced by the deep full-codebase review:

  * ``FetchConfig.max_retries`` must be >= 1 (async_retry's contract) so the
    natural ``max_retries=0`` "disable" sentinel is rejected at config time
    instead of making every fetch raise a raw ValueError. retry delays bounded.
  * ``SearchConfig.providers`` rejects an unknown provider name (Literal)
    instead of silently dropping it from the chain.
  * Enumerated-string fields (``wait_until`` / ``screenshot_format`` /
    ``log_level``) reject typo'd values at config time instead of failing
    per-operation at runtime.
  * ``DownloadConfig.allowed_extensions`` entries are normalized (lowercase +
    leading dot) so a wrong-case / dotless entry can't fail-closed-block every
    matching download.
  * ``AppConfig.from_yaml`` raises the documented ConfigError (not a raw
    TypeError) when the YAML root is not a mapping.
"""

from __future__ import annotations

import pathlib

import pydantic
import pytest
from web_agent.config import (
    AppConfig,
    AutomationConfig,
    BrowserConfig,
    DownloadConfig,
    FetchConfig,
    SearchConfig,
)
from web_agent.exceptions import ConfigError
from web_agent.ownership import OwnershipToken


class TestV170IsolationCdpDefaults:
    """v1.7.0: isolation_mode + cdp_enabled default ON, with graceful
    reconciliation so toggling one off (or using remote_cdp) doesn't error on
    a value the caller never explicitly chose."""

    def test_both_on_by_default(self) -> None:
        b = BrowserConfig()
        assert b.isolation_mode is True
        assert b.cdp_enabled is True
        assert AppConfig().browser.cdp_enabled is True

    def test_isolation_off_auto_disables_cdp(self) -> None:
        # Turning isolation off must NOT error on the on-by-default cdp; cdp
        # follows isolation off (it needs the owned user-data-dir).
        b = BrowserConfig(isolation_mode=False)
        assert b.isolation_mode is False
        assert b.cdp_enabled is False

    def test_explicit_cdp_with_isolation_off_still_errors(self) -> None:
        # An EXPLICIT cdp_enabled=True with isolation off is a real misconfig.
        with pytest.raises(ConfigError, match="isolation_mode"):
            BrowserConfig(isolation_mode=False, cdp_enabled=True)

    def test_cdp_off_keeps_isolation_on(self) -> None:
        # Turning only cdp off leaves isolation on (no reverse coupling).
        b = BrowserConfig(cdp_enabled=False)
        assert b.cdp_enabled is False
        assert b.isolation_mode is True

    def test_remote_cdp_reconciles_default_isolation_and_cdp(self, tmp_path) -> None:
        # remote_cdp connects to an existing browser; the on-by-default
        # isolation/cdp must be auto-cleared rather than erroring.
        token = OwnershipToken.issue(tmp_path)
        b = BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
            remote_cdp_ownership_token=token,
            remote_cdp_profile_dir=str(tmp_path),
        )
        assert b.isolation_mode is False
        assert b.cdp_enabled is False

    def test_remote_cdp_with_explicit_isolation_still_errors(self, tmp_path) -> None:
        token = OwnershipToken.issue(tmp_path)
        with pytest.raises(ConfigError, match="isolation_mode"):
            BrowserConfig(
                backend="remote_cdp",
                isolation_mode=True,
                remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
                remote_cdp_ownership_token=token,
                remote_cdp_profile_dir=str(tmp_path),
            )


class TestFetchRetryBounds:
    def test_max_retries_zero_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            FetchConfig(max_retries=0)

    def test_max_retries_negative_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            FetchConfig(max_retries=-1)

    def test_retry_max_delay_zero_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            FetchConfig(retry_max_delay=0)

    def test_negative_base_delay_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            FetchConfig(retry_base_delay=-1.0)

    def test_min_valid_retry_count_accepted(self) -> None:
        assert FetchConfig(max_retries=1).max_retries == 1

    def test_named_policy_layering_still_works(self) -> None:
        # Regression: the after-validator still applies the named policy's
        # values (all >= 1), so paranoid yields 5 retries.
        assert FetchConfig(retry_policy="paranoid").max_retries == 5
        assert FetchConfig(retry_policy="fast").max_retries == 1


class TestEnumeratedStringFields:
    def test_wait_until_typo_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            FetchConfig(wait_until="networkIdle")

    def test_wait_until_valid_values_accepted(self) -> None:
        for v in ("commit", "domcontentloaded", "load", "networkidle"):
            assert FetchConfig(wait_until=v).wait_until == v

    def test_screenshot_format_typo_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            AutomationConfig(screenshot_format="gif")

    def test_screenshot_format_valid_accepted(self) -> None:
        assert AutomationConfig(screenshot_format="jpeg").screenshot_format == "jpeg"

    def test_log_level_typo_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            AppConfig(log_level="verbose")

    def test_log_level_default_unchanged(self) -> None:
        assert AppConfig().log_level == "INFO"


class TestSearchProvidersLiteral:
    def test_unknown_provider_rejected(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            SearchConfig(providers=["ddgs", "duckduckgo"])

    def test_valid_subset_accepted(self) -> None:
        assert SearchConfig(providers=["playwright"]).providers == ["playwright"]


class TestAllowedExtensionsNormalization:
    def test_case_and_dot_normalized(self) -> None:
        cfg = DownloadConfig(allowed_extensions=["PDF", "xls", ".CSV"])
        assert cfg.allowed_extensions == [".pdf", ".xls", ".csv"]

    def test_blank_entries_dropped(self) -> None:
        cfg = DownloadConfig(allowed_extensions=[" ", ".pdf", ""])
        assert cfg.allowed_extensions == [".pdf"]

    def test_already_normalized_unchanged(self) -> None:
        cfg = DownloadConfig(allowed_extensions=[".pdf", ".csv"])
        assert cfg.allowed_extensions == [".pdf", ".csv"]


class TestFromYamlNonMappingRoot:
    def test_list_root_raises_config_error(self, tmp_path: pathlib.Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("- browser:\n    headless: false\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            AppConfig.from_yaml(p)

    def test_scalar_root_raises_config_error(self, tmp_path: pathlib.Path) -> None:
        p = tmp_path / "scalar.yaml"
        p.write_text("just-a-string\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            AppConfig.from_yaml(p)

    def test_empty_file_still_uses_defaults(self, tmp_path: pathlib.Path) -> None:
        # Regression: an empty file (``or {}``) is still valid -> defaults.
        p = tmp_path / "empty.yaml"
        p.write_text("", encoding="utf-8")
        cfg = AppConfig.from_yaml(p)
        assert cfg.log_level == "INFO"

    def test_mapping_root_still_works(self, tmp_path: pathlib.Path) -> None:
        p = tmp_path / "ok.yaml"
        p.write_text("log_level: DEBUG\n", encoding="utf-8")
        cfg = AppConfig.from_yaml(p)
        assert cfg.log_level == "DEBUG"
