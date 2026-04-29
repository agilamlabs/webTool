"""Tests for retry policy profiles and FetchConfig integration."""

from __future__ import annotations

import pytest

from web_agent.config import AppConfig, FetchConfig
from web_agent.utils import RetryPolicy, get_retry_policy


class TestGetRetryPolicy:
    def test_fast_policy(self) -> None:
        kw = get_retry_policy("fast")
        assert kw["max_retries"] == 1
        assert kw["base_delay"] == 0.5
        assert kw["max_delay"] == 5.0

    def test_balanced_policy(self) -> None:
        kw = get_retry_policy("balanced")
        assert kw["max_retries"] == 3
        assert kw["base_delay"] == 1.0
        assert kw["max_delay"] == 30.0

    def test_paranoid_policy(self) -> None:
        kw = get_retry_policy("paranoid")
        assert kw["max_retries"] == 5
        assert kw["base_delay"] == 2.0
        assert kw["max_delay"] == 60.0

    def test_accepts_enum(self) -> None:
        kw = get_retry_policy(RetryPolicy.FAST)
        assert kw["max_retries"] == 1

    def test_unknown_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            get_retry_policy("aggressive")


class TestFetchConfigPolicyApplication:
    def test_default_balanced(self) -> None:
        cfg = FetchConfig()
        assert cfg.retry_policy == "balanced"
        assert cfg.max_retries == 3
        assert cfg.retry_base_delay == 1.0
        assert cfg.retry_max_delay == 30.0

    def test_fast_policy_applies_when_numeric_unset(self) -> None:
        cfg = FetchConfig(retry_policy="fast")
        assert cfg.max_retries == 1
        assert cfg.retry_base_delay == 0.5
        assert cfg.retry_max_delay == 5.0

    def test_paranoid_policy_applies_when_numeric_unset(self) -> None:
        cfg = FetchConfig(retry_policy="paranoid")
        assert cfg.max_retries == 5
        assert cfg.retry_base_delay == 2.0

    def test_explicit_max_retries_overrides_policy(self) -> None:
        cfg = FetchConfig(retry_policy="paranoid", max_retries=99)
        # User explicitly set max_retries -> policy NOT applied
        assert cfg.max_retries == 99

    def test_explicit_base_delay_overrides_policy(self) -> None:
        cfg = FetchConfig(retry_policy="fast", retry_base_delay=10.0)
        assert cfg.retry_base_delay == 10.0


class TestAppConfigPolicyIntegration:
    def test_app_config_with_fast_policy(self) -> None:
        app = AppConfig(fetch={"retry_policy": "fast"})
        assert app.fetch.max_retries == 1

    def test_app_config_with_paranoid_policy(self) -> None:
        app = AppConfig(fetch={"retry_policy": "paranoid"})
        assert app.fetch.max_retries == 5
