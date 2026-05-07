"""Tests for v1.6.1 model additions: warnings, download_candidates, diagnostics, etc."""

from __future__ import annotations

from web_agent import (
    AgentResult,
    FetchDiagnostic,
    FetchResult,
    FetchStatus,
    FormFilterSpec,
    LocatorSpec,
    ResearchResult,
    SearchResponse,
    SearchResultItem,
    __version__,
)


def test_version_bump():
    # v1.6.x family; bumped to 1.6.2 in the follow-up release.
    assert __version__.startswith("1.6.")


def test_agent_result_new_fields_default_empty():
    r = AgentResult(query="x", search=SearchResponse(query="x"))
    assert r.errors == []
    assert r.warnings == []
    assert r.download_candidates == []
    assert r.diagnostics == []


def test_research_result_new_fields_default_empty():
    r = ResearchResult(query="x")
    assert r.errors == []
    assert r.warnings == []
    assert r.download_candidates == []
    assert r.diagnostics == []


def test_search_result_item_provider_default():
    s = SearchResultItem(position=1, title="t", url="https://x.com")
    assert s.provider == "unknown"


def test_search_result_item_provider_explicit():
    s = SearchResultItem(position=1, title="t", url="https://x.com", provider="searxng")
    assert s.provider == "searxng"


def test_fetch_result_binary_defaults_to_none():
    fr = FetchResult(url="u", final_url="u", status=FetchStatus.SUCCESS)
    assert fr.binary is None
    assert fr.content_type is None


def test_fetch_result_binary_can_be_set():
    fr = FetchResult(
        url="u",
        final_url="u",
        status=FetchStatus.SUCCESS,
        binary=b"%PDF-1.4...",
        content_type="application/pdf",
    )
    assert fr.binary == b"%PDF-1.4..."
    assert fr.content_type == "application/pdf"


def test_fetch_diagnostic_minimal_construction():
    d = FetchDiagnostic(url="https://x.com", status=FetchStatus.SUCCESS)
    assert d.url == "https://x.com"
    assert d.provider == "unknown"
    assert d.block_reason is None
    assert d.content_length == 0


def test_fetch_diagnostic_full_construction():
    d = FetchDiagnostic(
        url="https://blocked.example",
        status=FetchStatus.BLOCKED,
        provider="searxng",
        block_reason="domain_blocked",
        content_length=0,
        response_time_ms=0.0,
    )
    assert d.block_reason == "domain_blocked"
    assert d.provider == "searxng"


def test_form_filter_spec_minimal():
    spec = FormFilterSpec()
    assert spec.query_selector is None
    assert spec.query_value is None
    assert spec.filters == []
    assert spec.submit_selector is None
    assert spec.wait_timeout_ms == 15000


def test_form_filter_spec_full():
    spec = FormFilterSpec(
        query_selector="input#q",
        query_value="hello",
        filters=[("select#year", "2024")],
        submit_selector=LocatorSpec(role="button", role_name="Search"),
        wait_for=".results",
        wait_timeout_ms=20000,
    )
    assert spec.query_value == "hello"
    assert len(spec.filters) == 1
    assert spec.wait_timeout_ms == 20000


def test_agent_result_round_trips_through_json():
    """v1.6.0 JSON dumps must still parse against the v1.6.1 model."""
    legacy_payload = {
        "query": "x",
        "search": {"query": "x"},
        "pages": [],
        "errors": ["legacy error"],
        "total_time_ms": 100.0,
    }
    r = AgentResult.model_validate(legacy_payload)
    assert r.errors == ["legacy error"]
    assert r.warnings == []
    assert r.download_candidates == []
    assert r.diagnostics == []
