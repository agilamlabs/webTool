"""Unit tests for the Recipes ranking logic and helper methods.

Live integration tests for end-to-end recipe execution are deferred to
the CLI smoke tests / browser-required suite.
"""

from __future__ import annotations

from web_agent.models import SearchResultItem
from web_agent.recipes import Recipes


def _item(position: int, title: str, url: str, snippet: str = "") -> SearchResultItem:
    return SearchResultItem(
        position=position, title=title, url=url, snippet=snippet
    )


class TestRanking:
    def test_default_promotes_https(self) -> None:
        http_item = _item(1, "Python tutorial", "http://example.com/python")
        https_item = _item(1, "Python tutorial", "https://example.com/python")
        s_http = Recipes._rank("python tutorial", http_item)
        s_https = Recipes._rank("python tutorial", https_item)
        assert s_https > s_http

    def test_default_promotes_well_known_domain(self) -> None:
        unknown = _item(1, "Python tutorial", "https://random.io/python")
        known = _item(1, "Python tutorial", "https://docs.python.org/python")
        s_unknown = Recipes._rank("python tutorial", unknown)
        s_known = Recipes._rank("python tutorial", known)
        assert s_known > s_unknown

    def test_default_token_overlap(self) -> None:
        relevant = _item(
            1, "Python web scraping tutorial", "https://example.com/x"
        )
        irrelevant = _item(
            1, "Cooking with mushrooms", "https://example.com/x"
        )
        s_rel = Recipes._rank("python web scraping", relevant)
        s_irr = Recipes._rank("python web scraping", irrelevant)
        assert s_rel > s_irr

    def test_position_scheme_ignores_content(self) -> None:
        first = _item(1, "Random", "https://x.com")
        last = _item(10, "Random", "https://x.com")
        assert Recipes._rank("anything", first, "position") > Recipes._rank(
            "anything", last, "position"
        )

    def test_overlap_scheme_pure(self) -> None:
        match = _item(
            1, "Python tutorial guide", "https://x.com", snippet="learn python"
        )
        nonmatch = _item(1, "Cooking", "https://x.com", snippet="recipes")
        s_match = Recipes._rank("python tutorial", match, "overlap")
        s_nonmatch = Recipes._rank("python tutorial", nonmatch, "overlap")
        assert s_match > s_nonmatch
        assert s_nonmatch == 0.0


class TestUrlExtension:
    def test_pdf(self) -> None:
        assert Recipes._url_extension("https://x.com/report.pdf") == ".pdf"

    def test_with_query(self) -> None:
        assert Recipes._url_extension("https://x.com/report.pdf?token=abc") == ".pdf"

    def test_no_extension(self) -> None:
        assert Recipes._url_extension("https://x.com/page") == ""

    def test_path_only(self) -> None:
        assert Recipes._url_extension("https://x.com/files/data.xlsx") == ".xlsx"


class TestTokenize:
    def test_lowercases_and_filters_short(self) -> None:
        toks = Recipes._tokenize("Python WEB-Scraping a tool")
        assert "python" in toks
        assert "web" in toks
        assert "scraping" in toks
        # Length-1 word filtered
        assert "a" not in toks
