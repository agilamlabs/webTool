"""Offline unit tests for web_agent.sitemap (pure regex parsing, no network)."""

from __future__ import annotations

from web_agent.sitemap import SITEMAP_PATHS, SitemapParse, parse_sitemap


class TestParseUrlset:
    def test_extracts_loc_urls(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/a</loc></url>
          <url><loc>https://example.com/b</loc></url>
          <url><loc>https://example.com/c</loc></url>
        </urlset>"""
        result = parse_sitemap(xml)
        assert result.is_index is False
        assert result.urls == [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        ]

    def test_unescapes_entities(self) -> None:
        # &amp; in the XML must come back as a literal & in the URL.
        xml = (
            "<urlset><url><loc>https://example.com/search?q=a&amp;b=c</loc>"
            "</url></urlset>"
        )
        result = parse_sitemap(xml)
        assert result.urls == ["https://example.com/search?q=a&b=c"]

    def test_strips_whitespace_and_drops_empties(self) -> None:
        xml = (
            "<urlset>"
            "<url><loc>  https://example.com/x  </loc></url>"
            "<url><loc>   </loc></url>"  # whitespace-only -> dropped
            "<url><loc></loc></url>"  # empty -> dropped
            "<url><loc>https://example.com/y</loc></url>"
            "</urlset>"
        )
        result = parse_sitemap(xml)
        assert result.urls == ["https://example.com/x", "https://example.com/y"]

    def test_case_insensitive_tags(self) -> None:
        xml = "<URLSET><URL><LOC>https://example.com/up</LOC></URL></URLSET>"
        result = parse_sitemap(xml)
        assert result.is_index is False
        assert result.urls == ["https://example.com/up"]

    def test_loc_with_attributes_on_tag(self) -> None:
        xml = '<urlset><url><loc xml:lang="en">https://example.com/attr</loc></url></urlset>'
        result = parse_sitemap(xml)
        assert result.urls == ["https://example.com/attr"]


class TestParseIndex:
    def test_detects_sitemapindex(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <sitemap><loc>https://example.com/sitemap-1.xml</loc></sitemap>
          <sitemap><loc>https://example.com/sitemap-2.xml</loc></sitemap>
        </sitemapindex>"""
        result = parse_sitemap(xml)
        assert result.is_index is True
        assert result.urls == [
            "https://example.com/sitemap-1.xml",
            "https://example.com/sitemap-2.xml",
        ]

    def test_index_detection_case_insensitive(self) -> None:
        xml = "<SitemapIndex><sitemap><loc>https://e.com/s.xml</loc></sitemap></SitemapIndex>"
        result = parse_sitemap(xml)
        assert result.is_index is True


class TestMaxUrls:
    def test_caps_and_stops_early(self) -> None:
        locs = "".join(f"<url><loc>https://example.com/p{i}</loc></url>" for i in range(50))
        xml = f"<urlset>{locs}</urlset>"
        result = parse_sitemap(xml, max_urls=5)
        assert len(result.urls) == 5
        assert result.urls[0] == "https://example.com/p0"
        assert result.urls[-1] == "https://example.com/p4"

    def test_zero_max_urls_yields_empty_but_flags_index(self) -> None:
        xml = "<sitemapindex><sitemap><loc>https://e.com/s.xml</loc></sitemap></sitemapindex>"
        result = parse_sitemap(xml, max_urls=0)
        assert result.urls == []
        assert result.is_index is True


class TestRobustness:
    def test_malformed_xml_does_not_raise(self) -> None:
        # Unclosed tags, stray angle brackets, truncated document.
        xml = "<urlset><url><loc>https://example.com/ok</loc></url><url><loc>https"
        result = parse_sitemap(xml)
        # The one well-formed <loc> is recovered; the truncated one is ignored.
        assert result.urls == ["https://example.com/ok"]

    def test_truncated_mid_loc(self) -> None:
        xml = "<urlset><url><loc>https://example.com/a</loc></url><url><loc>https://exa"
        result = parse_sitemap(xml)
        assert result.urls == ["https://example.com/a"]

    def test_empty_input(self) -> None:
        result = parse_sitemap("")
        assert result.urls == []
        assert result.is_index is False

    def test_whitespace_only_input(self) -> None:
        result = parse_sitemap("   \n  ")
        assert result.urls == []
        assert result.is_index is False

    def test_no_loc_elements(self) -> None:
        xml = "<urlset><url><lastmod>2024-01-01</lastmod></url></urlset>"
        result = parse_sitemap(xml)
        assert result.urls == []
        assert result.is_index is False

    def test_garbage_input(self) -> None:
        result = parse_sitemap("this is not xml at all <<< >>>")
        assert result.urls == []
        assert result.is_index is False


class TestModuleSurface:
    def test_sitemap_paths_constant(self) -> None:
        assert isinstance(SITEMAP_PATHS, tuple)
        assert "/sitemap.xml" in SITEMAP_PATHS

    def test_sitemapparse_is_frozen(self) -> None:
        parsed = SitemapParse(urls=["https://e.com/a"], is_index=False)
        # Frozen dataclass: attribute assignment must raise.
        try:
            parsed.is_index = True  # type: ignore[misc]
        except Exception as exc:
            assert "frozen" in str(exc).lower() or isinstance(exc, AttributeError)
        else:  # pragma: no cover - the assignment should never succeed
            raise AssertionError("SitemapParse should be immutable (frozen)")
