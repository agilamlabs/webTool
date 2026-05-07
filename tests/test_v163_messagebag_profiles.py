"""v1.6.3 _MessageBag + user-extensible ranking profiles (issues #4, #8)."""

from __future__ import annotations

from web_agent import Agent, AppConfig, ToolMessage, ToolSeverity, __version__
from web_agent.agent import _MessageBag
from web_agent.recipes import RANKING_PROFILES

# ----------------------------------------------------------------------
# Version
# ----------------------------------------------------------------------


def test_version_bump():
    assert __version__ == "1.6.3"


# ----------------------------------------------------------------------
# _MessageBag (#4)
# ----------------------------------------------------------------------


def test_message_bag_warn_creates_both_string_and_structured():
    bag = _MessageBag()
    bag.warn("domain_blocked", "Domain blocked: https://x.com", url="https://x.com")
    assert bag.warnings == ["Domain blocked: https://x.com"]
    assert len(bag.structured_warnings) == 1
    s: ToolMessage = bag.structured_warnings[0]
    assert s.code == "domain_blocked"
    assert s.url == "https://x.com"
    assert s.severity == ToolSeverity.WARNING
    # errors list stays empty
    assert bag.errors == []
    assert bag.structured_errors == []


def test_message_bag_err_creates_both_string_and_structured():
    bag = _MessageBag()
    bag.err("budget_exceeded", "too many pages")
    assert bag.errors == ["too many pages"]
    assert len(bag.structured_errors) == 1
    e: ToolMessage = bag.structured_errors[0]
    assert e.code == "budget_exceeded"
    assert e.severity == ToolSeverity.ERROR
    assert e.url is None


def test_message_bag_severity_override():
    bag = _MessageBag()
    bag.warn("info_msg", "informational note", severity=ToolSeverity.INFO)
    assert bag.structured_warnings[0].severity == ToolSeverity.INFO


def test_message_bag_keeps_lists_in_sync_under_many_appends():
    bag = _MessageBag()
    for i in range(5):
        bag.warn("foo", f"warn-{i}")
        bag.err("bar", f"err-{i}", url=f"https://x.com/{i}")
    assert len(bag.warnings) == len(bag.structured_warnings) == 5
    assert len(bag.errors) == len(bag.structured_errors) == 5
    assert bag.structured_errors[2].url == "https://x.com/2"


# ----------------------------------------------------------------------
# Codes are populated at the source -- regression for "unknown" leakage
# ----------------------------------------------------------------------


from unittest.mock import AsyncMock  # noqa: E402

import pytest  # noqa: E402
from web_agent.models import (  # noqa: E402
    FetchResult,
    FetchStatus,
    SearchResponse,
    SearchResultItem,
)


@pytest.mark.asyncio
async def test_search_and_extract_no_unknown_codes_for_normal_path():
    """When the pipeline runs through normal failure/skip paths, every
    structured message has a real code -- no 'unknown' leakage from the
    deprecated prefix classifier."""
    agent = Agent(AppConfig())
    agent._search.search = AsyncMock(
        return_value=SearchResponse(
            query="x",
            total_results=2,
            results=[
                SearchResultItem(
                    position=1,
                    title="A",
                    url="https://blocked.example.com/x",
                    provider="searxng",
                ),
                SearchResultItem(
                    position=2,
                    title="B",
                    url="https://x.com/page.html",
                    provider="searxng",
                ),
            ],
        )
    )
    agent._fetcher.classify_url = AsyncMock(return_value="html")
    agent._fetcher.fetch_many = AsyncMock(
        return_value=[
            FetchResult(
                url="https://x.com/page.html",
                final_url="https://x.com/page.html",
                status=FetchStatus.HTTP_ERROR,
                status_code=404,
                error_message="HTTP 404",
            )
        ]
    )

    # Block the first URL via SafetyConfig
    config = AppConfig(safety={"denied_domains": ["blocked.example.com"]})
    agent = Agent(config)
    agent._search.search = AsyncMock(
        return_value=SearchResponse(
            query="x",
            total_results=2,
            results=[
                SearchResultItem(
                    position=1,
                    title="A",
                    url="https://blocked.example.com/x",
                    provider="searxng",
                ),
                SearchResultItem(
                    position=2,
                    title="B",
                    url="https://x.com/page.html",
                    provider="searxng",
                ),
            ],
        )
    )
    agent._fetcher.classify_url = AsyncMock(return_value="html")
    agent._fetcher.fetch_many = AsyncMock(
        return_value=[
            FetchResult(
                url="https://x.com/page.html",
                final_url="https://x.com/page.html",
                status=FetchStatus.HTTP_ERROR,
                status_code=404,
                error_message="HTTP 404",
            )
        ]
    )

    result = await agent.search_and_extract("x")
    # All structured messages should have a real code
    for msg in result.structured_warnings + result.structured_errors:
        assert msg.code != "unknown", f"got unknown code for {msg.message!r}"


# ----------------------------------------------------------------------
# User-extensible ranking profiles (#8)
# ----------------------------------------------------------------------


def test_user_profile_merges_with_builtin():
    config = AppConfig(ranking_profiles={"acme": ["acme.io", "acme.com"]})
    agent = Agent(config)
    profiles = agent._recipes._profiles
    # User profile is present
    assert profiles["acme"] == ("acme.io", "acme.com")
    # Built-in profiles are still present
    assert "official_sources" in profiles
    assert "research" in profiles


def test_user_profile_overrides_builtin_on_collision():
    """If user defines 'docs', their list replaces the built-in 'docs' profile."""
    config = AppConfig(ranking_profiles={"docs": ["my-internal-docs.com"]})
    agent = Agent(config)
    assert agent._recipes._profiles["docs"] == ("my-internal-docs.com",)
    # Other built-ins still untouched
    assert agent._recipes._profiles["news"] == RANKING_PROFILES["news"]


def test_resolve_hints_uses_user_profile():
    config = AppConfig(ranking_profiles={"acme": ["acme.io"]})
    agent = Agent(config)
    hints = agent._recipes._resolve_hints(["my.org"], "acme")
    assert "acme.io" in hints
    assert "my.org" in hints


def test_resolve_hints_unknown_profile_silently_ignored():
    config = AppConfig(ranking_profiles={"acme": ["acme.io"]})
    agent = Agent(config)
    # 'no_such' isn't in built-ins or user profiles -- silently ignored
    hints = agent._recipes._resolve_hints(["my.org"], "no_such")
    assert hints == ("my.org",)


def test_resolve_hints_no_profile_no_hints_yields_empty():
    config = AppConfig()
    agent = Agent(config)
    assert agent._recipes._resolve_hints(None, None) == ()
    assert agent._recipes._resolve_hints([], None) == ()
