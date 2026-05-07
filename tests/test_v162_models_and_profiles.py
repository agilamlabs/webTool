"""Tests for v1.6.2 ranking profiles + structured ToolError/ToolWarning (issues #8, #12)."""

from __future__ import annotations

from web_agent import (
    AgentResult,
    ResearchResult,
    ToolError,
    ToolMessage,
    ToolSeverity,
    ToolWarning,
    __version__,
)
from web_agent.agent import _classify_message, _to_structured
from web_agent.models import SearchResponse
from web_agent.recipes import RANKING_PROFILES, Recipes, _resolve_domain_hints

# ----------------------------------------------------------------------
# Version
# ----------------------------------------------------------------------


def test_version_bump():
    assert __version__ == "1.6.2"


# ----------------------------------------------------------------------
# Ranking profiles (#8)
# ----------------------------------------------------------------------


def test_known_profiles_present():
    assert set(RANKING_PROFILES.keys()) == {
        "official_sources",
        "docs",
        "research",
        "news",
        "files",
    }


def test_each_profile_has_at_least_three_hosts():
    for name, hints in RANKING_PROFILES.items():
        assert len(hints) >= 3, f"profile {name!r} has too few hosts"


def test_resolve_combines_profile_and_user_hints():
    hints = _resolve_domain_hints(["my.example.com"], "official_sources")
    assert "my.example.com" in hints
    assert "sec.gov" in hints
    assert "ec.europa.eu" in hints


def test_resolve_unknown_profile_silently_ignored():
    hints = _resolve_domain_hints(["my.example.com"], "no_such_profile")
    assert hints == ("my.example.com",)


def test_resolve_no_profile_no_hints():
    assert _resolve_domain_hints(None, None) == ()
    assert _resolve_domain_hints([], None) == ()


def test_profile_hint_boosts_ranking():
    """A result on a profile-listed domain should outrank a generic one."""
    from web_agent.models import SearchResultItem

    sec = SearchResultItem(position=1, title="t", url="https://www.sec.gov/x", snippet="")
    other = SearchResultItem(position=1, title="t", url="https://www.other.com/x", snippet="")

    sec_score = Recipes._rank(
        "tesla 10k",
        sec,
        prefer_domains=RANKING_PROFILES["official_sources"],
    )
    other_score = Recipes._rank("tesla 10k", other, prefer_domains=())
    assert sec_score > other_score


# ----------------------------------------------------------------------
# Structured ToolError / ToolWarning (#12)
# ----------------------------------------------------------------------


def test_tool_message_round_trip():
    m = ToolMessage(
        code="domain_blocked",
        message="Domain blocked: https://x.com",
        url="https://x.com",
        severity=ToolSeverity.WARNING,
    )
    js = m.model_dump_json()
    back = ToolMessage.model_validate_json(js)
    assert back == m


def test_tool_warning_and_error_are_aliases():
    """ToolWarning and ToolError both point to ToolMessage for clarity at call sites."""
    assert ToolWarning is ToolMessage
    assert ToolError is ToolMessage


def test_classify_message_domain_blocked():
    code, url = _classify_message("Domain blocked: https://bad.example.com/path")
    assert code == "domain_blocked"
    assert url == "https://bad.example.com/path"


def test_classify_message_fetch_failed():
    code, url = _classify_message("Failed to fetch https://x.com/y: timeout")
    assert code == "fetch_failed"
    assert url == "https://x.com/y"


def test_classify_message_download_skipped():
    code, _ = _classify_message("3 downloadable file URLs skipped; see download_candidates")
    assert code == "download_skipped"


def test_classify_message_no_search_results():
    code, _ = _classify_message("No search results found")
    assert code == "no_search_results"


def test_classify_message_unknown_falls_through():
    code, url = _classify_message("Something completely unexpected happened")
    assert code == "unknown"
    assert url is None


def test_to_structured_preserves_severity():
    msgs = ["Domain blocked: https://x.com", "Failed to fetch https://y.com/z"]
    structured = _to_structured(msgs, ToolSeverity.WARNING)
    assert all(s.severity == ToolSeverity.WARNING for s in structured)


def test_agent_result_structured_default_empty():
    r = AgentResult(query="x", search=SearchResponse(query="x"))
    assert r.structured_warnings == []
    assert r.structured_errors == []


def test_research_result_structured_default_empty():
    r = ResearchResult(query="x")
    assert r.structured_warnings == []
    assert r.structured_errors == []


def test_legacy_payload_still_parses():
    """v1.6.0 / v1.6.1 JSON dumps must parse against the v1.6.2 model."""
    legacy = {
        "query": "x",
        "search": {"query": "x"},
        "pages": [],
        "errors": ["one fatal"],
        "warnings": ["one warn"],
        "total_time_ms": 100.0,
    }
    r = AgentResult.model_validate(legacy)
    assert r.errors == ["one fatal"]
    assert r.warnings == ["one warn"]
    # New fields default to []
    assert r.structured_warnings == []
    assert r.structured_errors == []
