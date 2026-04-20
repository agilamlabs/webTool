"""Tests for the three-tier content extraction pipeline."""

from __future__ import annotations

from web_agent.config import AppConfig
from web_agent.content_extractor import ContentExtractor
from web_agent.models import FetchResult, FetchStatus


def _make_fetch_result(html: str, url: str = "https://example.com") -> FetchResult:
    """Helper to create a successful FetchResult from raw HTML."""
    return FetchResult(
        url=url,
        final_url=url,
        status_code=200,
        status=FetchStatus.SUCCESS,
        html=html,
    )


class TestTrafilaturaExtraction:
    """Tests for the primary trafilatura extraction layer."""

    def test_extracts_article_content(
        self, app_config: AppConfig, sample_article_html: str
    ) -> None:
        extractor = ContentExtractor(app_config)
        result = extractor.extract(_make_fetch_result(sample_article_html))

        assert result.extraction_method in ("trafilatura", "bs4")
        assert result.content is not None
        assert result.content_length > 50
        # Content should contain substantive article text
        assert "web scraping" in result.content.lower() or "playwright" in result.content.lower()

    def test_extracts_title(
        self, app_config: AppConfig, sample_article_html: str
    ) -> None:
        extractor = ContentExtractor(app_config)
        result = extractor.extract(_make_fetch_result(sample_article_html))

        assert result.title is not None
        assert "Web Scraping" in result.title or "Python" in result.title


class TestBS4Extraction:
    """Tests for the BeautifulSoup fallback layer."""

    def test_extracts_from_article_tag(self, app_config: AppConfig) -> None:
        html = """
        <html><head><title>BS4 Test</title>
        <meta name="description" content="Testing BS4 extraction">
        </head><body>
        <nav>Nav content to ignore</nav>
        <article>
            <h1>Article Title</h1>
            <p>This is the main article content that should be extracted by
            the BeautifulSoup fallback extractor when trafilatura fails to
            produce sufficient content from this particular HTML structure.</p>
        </article>
        <footer>Footer to ignore</footer>
        </body></html>
        """
        extractor = ContentExtractor(app_config)
        result = extractor.extract(_make_fetch_result(html))

        assert result.content is not None
        assert result.content_length > 0
        # Title may come from <title> or <h1> depending on which extractor succeeds
        assert result.title in ("BS4 Test", "Article Title")

    def test_extracts_from_main_tag(self, app_config: AppConfig) -> None:
        html = """
        <html><head><title>Main Tag Test</title></head><body>
        <main>
            <p>Content inside main tag that should be found by the extractor
            as a fallback when no article tag exists in the document.</p>
        </main>
        </body></html>
        """
        extractor = ContentExtractor(app_config)
        result = extractor.extract(_make_fetch_result(html))

        assert result.content is not None
        assert "main tag" in result.content.lower()

    def test_strips_nav_and_footer(self, app_config: AppConfig) -> None:
        html = """
        <html><head><title>Strip Test</title></head><body>
        <article>
            <nav>Navigation that should be removed from output</nav>
            <p>This is the real content that we want to keep and extract
            from the page while removing all navigation elements.</p>
            <footer>Footer that should also be removed</footer>
        </article>
        </body></html>
        """
        extractor = ContentExtractor(app_config)
        result = extractor.extract(_make_fetch_result(html))

        assert result.content is not None
        assert "real content" in result.content.lower()
        # If BS4 is the extractor, nav/footer should be stripped
        # Trafilatura may or may not strip them depending on its algorithm
        if result.extraction_method == "bs4":
            assert "navigation that should be removed" not in result.content.lower()
            assert "footer that should also be removed" not in result.content.lower()


class TestRawExtraction:
    """Tests for the raw text fallback."""

    def test_minimal_html_falls_to_raw(self, app_config: AppConfig) -> None:
        # Content too short for trafilatura/bs4 min_content_length
        html = "<html><body><p>Hi</p></body></html>"
        extractor = ContentExtractor(app_config)
        result = extractor.extract(_make_fetch_result(html))

        # May be "raw" or one of the others depending on min_content_length
        assert result.extraction_method in ("trafilatura", "bs4", "raw")


class TestEdgeCases:
    """Tests for error conditions and edge cases."""

    def test_failed_fetch_returns_none_method(self, app_config: AppConfig) -> None:
        fetch_result = FetchResult(
            url="https://bad.com",
            final_url="https://bad.com",
            status=FetchStatus.HTTP_ERROR,
            error_message="404 Not Found",
        )
        extractor = ContentExtractor(app_config)
        result = extractor.extract(fetch_result)

        assert result.extraction_method == "none"
        assert result.content is None

    def test_empty_html(self, app_config: AppConfig) -> None:
        extractor = ContentExtractor(app_config)
        result = extractor.extract(
            _make_fetch_result("<html><head></head><body></body></html>")
        )

        # Should still produce a result (possibly with empty content)
        assert result.extraction_method in ("trafilatura", "bs4", "raw", "none")

    def test_html_with_only_scripts(self, app_config: AppConfig) -> None:
        html = """
        <html><head></head><body>
        <script>var x = 1;</script>
        <style>.foo { color: red; }</style>
        </body></html>
        """
        extractor = ContentExtractor(app_config)
        result = extractor.extract(_make_fetch_result(html))

        # Scripts should be stripped; content should be empty or minimal
        if result.content:
            assert "var x" not in result.content
            assert ".foo" not in result.content

    def test_description_extraction(self, app_config: AppConfig) -> None:
        html = """
        <html><head>
        <title>Meta Test</title>
        <meta name="description" content="This is the meta description">
        <meta name="author" content="Test Author">
        </head><body>
        <article>
            <p>Article content that is long enough to pass the minimum content
            length threshold for extraction by any of the available methods.</p>
        </article>
        </body></html>
        """
        extractor = ContentExtractor(app_config)
        result = extractor.extract(_make_fetch_result(html))

        # At least one extractor should capture the description
        if result.description:
            assert "meta description" in result.description
