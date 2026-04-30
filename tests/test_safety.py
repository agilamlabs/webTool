"""Tests for SafetyConfig, domain checks, and BudgetTracker."""

from __future__ import annotations

import pytest
from web_agent.config import SafetyConfig
from web_agent.exceptions import BudgetExceededError
from web_agent.utils import (
    BudgetTracker,
    _matches_domain,
    _normalize_host,
    check_domain_allowed,
)


class TestNormalizeHost:
    def test_basic(self) -> None:
        assert _normalize_host("https://example.com/path") == "example.com"

    def test_lowercase(self) -> None:
        assert _normalize_host("HTTPS://EXAMPLE.COM/X") == "example.com"

    def test_with_port_strips_port(self) -> None:
        # urlparse hostname strips port automatically
        assert _normalize_host("https://example.com:8080/x") == "example.com"

    def test_invalid_url(self) -> None:
        assert _normalize_host("not a url") == ""


class TestMatchesDomain:
    def test_exact_match(self) -> None:
        assert _matches_domain("example.com", "example.com")

    def test_subdomain_match(self) -> None:
        assert _matches_domain("api.example.com", "example.com")
        assert _matches_domain("www.example.com", "example.com")
        assert _matches_domain("a.b.example.com", "example.com")

    def test_non_match_substring(self) -> None:
        assert not _matches_domain("notexample.com", "example.com")
        assert not _matches_domain("malicious-example.com", "example.com")

    def test_pattern_with_leading_dot(self) -> None:
        assert _matches_domain("api.example.com", ".example.com")


class TestCheckDomainAllowed:
    def test_empty_allow_list_permits_all(self) -> None:
        sc = SafetyConfig()
        assert check_domain_allowed("https://anywhere.com/x", sc)

    def test_allow_list_blocks_others(self) -> None:
        sc = SafetyConfig(allowed_domains=["example.com"])
        assert check_domain_allowed("https://www.example.com", sc)
        assert not check_domain_allowed("https://evil.com", sc)

    def test_deny_list_blocks(self) -> None:
        sc = SafetyConfig(denied_domains=["evil.com"])
        assert not check_domain_allowed("https://evil.com/x", sc)
        assert check_domain_allowed("https://safe.com", sc)

    def test_deny_overrides_allow(self) -> None:
        sc = SafetyConfig(
            allowed_domains=["example.com"],
            denied_domains=["bad.example.com"],
        )
        assert check_domain_allowed("https://www.example.com", sc)
        assert not check_domain_allowed("https://bad.example.com", sc)

    def test_invalid_url_blocked(self) -> None:
        sc = SafetyConfig()
        assert not check_domain_allowed("not a url", sc)


class TestBudgetTracker:
    def test_pages_within_limit(self) -> None:
        sc = SafetyConfig(max_pages_per_call=3)
        bt = BudgetTracker(sc)
        bt.add_page()
        bt.add_page()
        bt.add_page()
        assert bt.pages_used == 3

    def test_pages_limit_exceeded(self) -> None:
        sc = SafetyConfig(max_pages_per_call=2)
        bt = BudgetTracker(sc)
        bt.add_page()
        bt.add_page()
        with pytest.raises(BudgetExceededError) as exc_info:
            bt.add_page()
        assert exc_info.value.budget_type == "pages"

    def test_chars_limit_exceeded(self) -> None:
        sc = SafetyConfig(max_chars_per_call=100)
        bt = BudgetTracker(sc)
        bt.add_chars(50)
        bt.add_chars(40)
        with pytest.raises(BudgetExceededError) as exc_info:
            bt.add_chars(20)
        assert exc_info.value.budget_type == "chars"

    def test_remaining_dict(self) -> None:
        sc = SafetyConfig(
            max_pages_per_call=10,
            max_chars_per_call=1000,
            max_time_per_call_seconds=60.0,
        )
        bt = BudgetTracker(sc)
        bt.add_page()
        bt.add_chars(100)
        rem = bt.remaining
        assert rem["pages"] == 9.0
        assert rem["chars"] == 900.0
        assert 0.0 <= rem["seconds"] <= 60.0

    def test_negative_chars_ignored(self) -> None:
        sc = SafetyConfig(max_chars_per_call=100)
        bt = BudgetTracker(sc)
        bt.add_chars(-5)  # treated as 0
        assert bt.chars_used == 0

    def test_time_limit_passes_when_under(self) -> None:
        sc = SafetyConfig(max_time_per_call_seconds=60.0)
        bt = BudgetTracker(sc)
        bt.check_time()  # no raise


class TestGranularSafetyFlags:
    """Tests for the new allow_js_evaluation, allow_downloads, allow_form_submit flags."""

    def test_allow_js_evaluation_default_false(self) -> None:
        # Secure-by-default for an LLM-facing tool
        assert SafetyConfig().allow_js_evaluation is False

    def test_allow_downloads_default_true(self) -> None:
        assert SafetyConfig().allow_downloads is True

    def test_allow_form_submit_default_true(self) -> None:
        assert SafetyConfig().allow_form_submit is True

    def test_block_private_ips_default_true(self) -> None:
        # SSRF protection is on by default
        assert SafetyConfig().block_private_ips is True

    def test_safe_mode_overrides_all_allow_flags(self) -> None:
        # Even if user explicitly enables them, safe_mode wins
        sc = SafetyConfig(
            safe_mode=True,
            allow_js_evaluation=True,
            allow_downloads=True,
            allow_form_submit=True,
        )
        assert sc.allow_js_evaluation is False
        assert sc.allow_downloads is False
        assert sc.allow_form_submit is False

    def test_safe_mode_does_not_touch_block_private_ips(self) -> None:
        # block_private_ips is independent of safe_mode
        sc = SafetyConfig(safe_mode=True, block_private_ips=False)
        assert sc.block_private_ips is False


class TestLooksLikeSubmitExtension:
    """The Phase D5 fix: _looks_like_submit should now check text/label/placeholder."""

    def test_submit_via_text_locator(self) -> None:
        from web_agent.browser_actions import _looks_like_submit
        from web_agent.models import LocatorSpec

        assert _looks_like_submit(LocatorSpec(text="Sign in"))

    def test_submit_via_label_locator(self) -> None:
        from web_agent.browser_actions import _looks_like_submit
        from web_agent.models import LocatorSpec

        assert _looks_like_submit(LocatorSpec(label="Submit"))

    def test_submit_via_placeholder_locator(self) -> None:
        from web_agent.browser_actions import _looks_like_submit
        from web_agent.models import LocatorSpec

        assert _looks_like_submit(LocatorSpec(placeholder="Save changes"))

    def test_non_submit_text_not_flagged(self) -> None:
        from web_agent.browser_actions import _looks_like_submit
        from web_agent.models import LocatorSpec

        assert not _looks_like_submit(LocatorSpec(text="Read more"))

    def test_role_name_still_works(self) -> None:
        from web_agent.browser_actions import _looks_like_submit
        from web_agent.models import LocatorSpec

        assert _looks_like_submit(LocatorSpec(role="button", role_name="Sign In"))

    def test_css_selector_still_works(self) -> None:
        from web_agent.browser_actions import _looks_like_submit

        assert _looks_like_submit("button[type=submit]")
        assert not _looks_like_submit("button.cancel")
