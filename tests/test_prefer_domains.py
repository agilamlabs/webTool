"""Tests for v1.6.1 caller-supplied prefer_domains ranking bonus (suggestion #4)."""

from __future__ import annotations

from web_agent.models import SearchResultItem
from web_agent.recipes import Recipes


def _item(url: str, title: str = "t", snippet: str = "", position: int = 1) -> SearchResultItem:
    return SearchResultItem(position=position, title=title, url=url, snippet=snippet)


def test_prefer_domain_exact_host_bonus():
    base = Recipes._rank("foo", _item("https://example.com/x"))
    prefd = Recipes._rank("foo", _item("https://example.com/x"), prefer_domains=("example.com",))
    assert prefd > base
    # +0.40 bonus
    assert abs((prefd - base) - 0.40) < 0.001


def test_prefer_domain_subdomain_match():
    base = Recipes._rank("foo", _item("https://docs.example.com/x"))
    prefd = Recipes._rank(
        "foo", _item("https://docs.example.com/x"), prefer_domains=("example.com",)
    )
    assert prefd > base


def test_prefer_domain_no_match():
    base = Recipes._rank("foo", _item("https://other.com/x"))
    prefd = Recipes._rank("foo", _item("https://other.com/x"), prefer_domains=("example.com",))
    assert prefd == base


def test_prefer_domain_dominates_well_known_bonus():
    """Caller hint (+0.40) should outrank well-known bonus (+0.20)."""
    # github.com is well-known (+0.20); example.com is not, but is preferred (+0.40)
    github = Recipes._rank("foo", _item("https://github.com/x"))
    preferred = Recipes._rank(
        "foo", _item("https://example.com/x"), prefer_domains=("example.com",)
    )
    assert preferred > github


def test_prefer_domain_empty_tuple_no_op():
    base = Recipes._rank("foo", _item("https://example.com/x"))
    same = Recipes._rank("foo", _item("https://example.com/x"), prefer_domains=())
    assert base == same


def test_prefer_domain_strip_leading_dot():
    """Hint '.example.com' should match 'example.com'."""
    prefd = Recipes._rank(
        "foo", _item("https://example.com/x"), prefer_domains=(".example.com",)
    )
    base = Recipes._rank("foo", _item("https://example.com/x"))
    assert prefd > base
