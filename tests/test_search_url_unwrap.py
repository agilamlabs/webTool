"""Tests for v1.6.1 search-engine SERP URL unwrapping (suggestion #5)."""

from __future__ import annotations

from web_agent.agent import _query_is_url, _unwrap_search_url


def test_query_is_url_for_plain_url():
    assert _query_is_url("https://example.com/page") is True


def test_query_is_url_false_for_natural_language():
    assert _query_is_url("what is python") is False
    assert _query_is_url("fetch https://example.com please") is False


def test_unwrap_google_search():
    assert _unwrap_search_url("https://www.google.com/search?q=tesla") == "tesla"


def test_unwrap_google_country_tld():
    assert _unwrap_search_url("https://www.google.co.uk/search?q=football") == "football"


def test_unwrap_bing():
    assert _unwrap_search_url("https://www.bing.com/search?q=hello+world") == "hello world"


def test_unwrap_duckduckgo_root():
    assert _unwrap_search_url("https://duckduckgo.com/?q=python") == "python"


def test_unwrap_duckduckgo_html():
    assert _unwrap_search_url("https://duckduckgo.com/html/?q=foo") == "foo"


def test_unwrap_brave():
    assert _unwrap_search_url("https://search.brave.com/search?q=bravo") == "bravo"


def test_unwrap_searxng():
    assert _unwrap_search_url("https://searx.tiekoetter.com/search?q=meta") == "meta"


def test_unwrap_returns_none_for_plain_url():
    assert _unwrap_search_url("https://example.com/page") is None


def test_unwrap_returns_none_for_serp_without_q():
    assert _unwrap_search_url("https://www.google.com/search") is None


def test_unwrap_decodes_plus_to_space():
    assert _unwrap_search_url("https://www.google.com/search?q=hello+world") == "hello world"


def test_unwrap_handles_query_alias():
    """Some SERPs use ?query= instead of ?q="""
    assert _unwrap_search_url("https://search.brave.com/search?query=brave") == "brave"
