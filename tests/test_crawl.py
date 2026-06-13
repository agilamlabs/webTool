"""Offline unit tests for web_agent.crawl (fake fetcher/extractor, no browser)."""

from __future__ import annotations

from typing import Optional

import pytest
from web_agent.config import AppConfig
from web_agent.crawl import SiteCrawler, extract_links
from web_agent.models import ExtractionResult, FetchResult, FetchStatus

# All test hosts are PUBLIC-looking (example.com / *.example.com). localhost /
# 127.0.0.1 would be rejected by SafetyConfig.block_private_ips (default True).


def _html_with_links(*hrefs: str) -> str:
    anchors = "".join(f'<a href="{h}">link</a>' for h in hrefs)
    return f"<html><body>{anchors}</body></html>"


class _FakeFetcher:
    """Returns canned FetchResults keyed by URL.

    Records every fetched URL in ``calls``. An unknown URL yields a
    NETWORK_ERROR FetchResult (a page that doesn't exist), so the crawler's
    failure path is exercised rather than a KeyError.
    """

    def __init__(self, results: dict[str, FetchResult]) -> None:
        self._results = results
        self.calls: list[str] = []

    async def fetch(self, url: str, session_id: Optional[str] = None) -> FetchResult:
        self.calls.append(url)
        if url in self._results:
            return self._results[url]
        return FetchResult(
            url=url,
            final_url=url,
            status=FetchStatus.NETWORK_ERROR,
            error_message="not found in fake fetcher",
        )


class _FakeExtractor:
    """Returns canned ExtractionResults keyed by the FetchResult URL.

    Falls back to a tiny generic extraction when a URL has no canned result,
    so a crawl over pages without explicit extraction still gets content.
    """

    def __init__(self, results: Optional[dict[str, ExtractionResult]] = None) -> None:
        self._results = results or {}

    async def extract_async(
        self,
        fetch_result: FetchResult,
        *,
        strict: bool = False,
        prefer_api: bool = False,
        max_chars: Optional[int] = None,
        offset: int = 0,
    ) -> ExtractionResult:
        url = fetch_result.url
        if url in self._results:
            return self._results[url]
        content = f"content for {url}"
        return ExtractionResult(
            url=url,
            title=f"Title {url}",
            content=content,
            content_length=len(content),
            extraction_method="bs4",
        )


def _ok(url: str, html: str, *, final_url: Optional[str] = None) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=final_url or url,
        status=FetchStatus.SUCCESS,
        html=html,
    )


def _crawler(
    fetch_map: dict[str, FetchResult],
    extract_map: Optional[dict[str, ExtractionResult]] = None,
) -> tuple[SiteCrawler, _FakeFetcher]:
    fetcher = _FakeFetcher(fetch_map)
    extractor = _FakeExtractor(extract_map)
    config = AppConfig()
    crawler = SiteCrawler(fetcher, extractor, config)  # type: ignore[arg-type]
    return crawler, fetcher


# =====================================================================
# extract_links (pure function)
# =====================================================================


class TestExtractLinks:
    def test_resolves_relative_urls(self) -> None:
        html = _html_with_links("/about", "products/item")
        links = extract_links(
            html,
            "https://example.com/home",
            scope_host="example.com",
            same_registrable_domain=False,
            cap=10,
        )
        assert "https://example.com/about" in links
        assert "https://example.com/products/item" in links

    def test_drops_non_http_schemes_and_fragments(self) -> None:
        html = _html_with_links(
            "mailto:a@example.com",
            "javascript:void(0)",
            "tel:+15551234",
            "data:text/plain,hi",
            "#section",
            "/real",
        )
        links = extract_links(
            html,
            "https://example.com/p",
            scope_host="example.com",
            same_registrable_domain=False,
            cap=10,
        )
        assert links == ["https://example.com/real"]

    def test_fragment_stripped_from_otherwise_valid_url(self) -> None:
        html = _html_with_links("/page#anchor")
        links = extract_links(
            html,
            "https://example.com/",
            scope_host="example.com",
            same_registrable_domain=False,
            cap=10,
        )
        assert links == ["https://example.com/page"]

    def test_same_host_scope_drops_offsite(self) -> None:
        html = _html_with_links(
            "https://example.com/keep",
            "https://other.com/drop",
            "https://sub.example.com/also-drop-when-exact-host",
        )
        links = extract_links(
            html,
            "https://example.com/",
            scope_host="example.com",
            same_registrable_domain=False,
            cap=10,
        )
        assert links == ["https://example.com/keep"]

    def test_registrable_domain_keeps_subdomains(self) -> None:
        html = _html_with_links(
            "https://example.com/a",
            "https://shop.example.com/b",
            "https://www.example.com/c",
            "https://evil.com/d",
        )
        links = extract_links(
            html,
            "https://www.example.com/",
            scope_host="www.example.com",
            same_registrable_domain=True,
            cap=10,
        )
        assert "https://example.com/a" in links
        assert "https://shop.example.com/b" in links
        assert "https://www.example.com/c" in links
        assert "https://evil.com/d" not in links

    def test_include_filter(self) -> None:
        html = _html_with_links(
            "https://example.com/blog/post-1",
            "https://example.com/about",
            "https://example.com/blog/post-2",
        )
        links = extract_links(
            html,
            "https://example.com/",
            scope_host="example.com",
            same_registrable_domain=False,
            cap=10,
            include=[r"/blog/"],
        )
        assert links == [
            "https://example.com/blog/post-1",
            "https://example.com/blog/post-2",
        ]

    def test_exclude_filter(self) -> None:
        html = _html_with_links(
            "https://example.com/page",
            "https://example.com/admin/secret",
            "https://example.com/other",
        )
        links = extract_links(
            html,
            "https://example.com/",
            scope_host="example.com",
            same_registrable_domain=False,
            cap=10,
            exclude=[r"/admin/"],
        )
        assert "https://example.com/admin/secret" not in links
        assert "https://example.com/page" in links
        assert "https://example.com/other" in links

    def test_dedupe_preserves_first_seen_order(self) -> None:
        html = _html_with_links(
            "/a",
            "/b",
            "/a",  # duplicate
            "/c",
            "/b",  # duplicate
        )
        links = extract_links(
            html,
            "https://example.com/",
            scope_host="example.com",
            same_registrable_domain=False,
            cap=10,
        )
        assert links == [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        ]

    def test_cap(self) -> None:
        html = _html_with_links("/a", "/b", "/c", "/d", "/e")
        links = extract_links(
            html,
            "https://example.com/",
            scope_host="example.com",
            same_registrable_domain=False,
            cap=3,
        )
        assert len(links) == 3
        assert links == [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        ]

    def test_zero_cap_returns_empty(self) -> None:
        html = _html_with_links("/a")
        links = extract_links(
            html,
            "https://example.com/",
            scope_host="example.com",
            same_registrable_domain=False,
            cap=0,
        )
        assert links == []

    def test_empty_html_returns_empty(self) -> None:
        links = extract_links(
            "",
            "https://example.com/",
            scope_host="example.com",
            same_registrable_domain=False,
            cap=10,
        )
        assert links == []


# =====================================================================
# SiteCrawler.crawl
# =====================================================================


class TestCrawlBasics:
    @pytest.mark.asyncio
    async def test_bfs_visits_start_then_links(self) -> None:
        start = "https://example.com/"
        fetch_map = {
            start: _ok(start, _html_with_links("/a", "/b")),
            "https://example.com/a": _ok("https://example.com/a", "<html>a</html>"),
            "https://example.com/b": _ok("https://example.com/b", "<html>b</html>"),
        }
        crawler, _ = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert result.pages_crawled == 3
        crawled_urls = {p.url for p in result.pages}
        assert crawled_urls == {
            start,
            "https://example.com/a",
            "https://example.com/b",
        }
        # Start visited first.
        assert result.pages[0].url == start
        assert result.pages[0].depth == 0
        # Links from start are depth 1.
        depths = {p.url: p.depth for p in result.pages}
        assert depths["https://example.com/a"] == 1
        assert depths["https://example.com/b"] == 1
        assert result.stopped_reason == "frontier_empty"
        assert result.urls_discovered == 3

    @pytest.mark.asyncio
    async def test_links_found_count(self) -> None:
        start = "https://example.com/"
        fetch_map = {
            start: _ok(start, _html_with_links("/a", "/b")),
            "https://example.com/a": _ok("https://example.com/a", "<html>a</html>"),
            "https://example.com/b": _ok("https://example.com/b", "<html>b</html>"),
        }
        crawler, _ = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        start_page = next(p for p in result.pages if p.url == start)
        assert start_page.links_found == 2

    @pytest.mark.asyncio
    async def test_content_and_extraction_populated(self) -> None:
        start = "https://example.com/"
        fetch_map = {start: _ok(start, "<html>hi</html>")}
        extract_map = {
            start: ExtractionResult(
                url=start,
                title="Home",
                content="hello world",
                content_length=len("hello world"),
                extraction_method="trafilatura",
            )
        }
        crawler, _ = _crawler(fetch_map, extract_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=0,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        page = result.pages[0]
        assert page.title == "Home"
        assert page.content == "hello world"
        assert page.content_length == len("hello world")
        assert page.extraction_method == "trafilatura"
        assert page.status == "success"
        assert result.total_content_length == len("hello world")


class TestCrawlBounds:
    @pytest.mark.asyncio
    async def test_max_pages_respected(self) -> None:
        start = "https://example.com/"
        # Start links to 5 children; cap pages at 3.
        fetch_map = {
            start: _ok(start, _html_with_links("/a", "/b", "/c", "/d", "/e")),
        }
        for c in ("a", "b", "c", "d", "e"):
            u = f"https://example.com/{c}"
            fetch_map[u] = _ok(u, "<html>leaf</html>")
        crawler, _ = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=3,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert result.pages_crawled == 3
        assert result.stopped_reason == "max_pages"

    @pytest.mark.asyncio
    async def test_max_depth_bound(self) -> None:
        # start(0) -> a(1) -> b(2): with max_depth=1, b is never fetched.
        start = "https://example.com/"
        a = "https://example.com/a"
        b = "https://example.com/b"
        fetch_map = {
            start: _ok(start, _html_with_links("/a")),
            a: _ok(a, _html_with_links("/b")),
            b: _ok(b, "<html>b</html>"),
        }
        crawler, fetcher = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=1,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        crawled = {p.url for p in result.pages}
        assert start in crawled
        assert a in crawled
        # Depth-2 link 'b' was never fetched (depth < max_depth gate on 'a').
        assert b not in crawled
        assert b not in fetcher.calls
        assert result.max_depth_reached == 1

    @pytest.mark.asyncio
    async def test_depth_zero_fetches_only_start(self) -> None:
        start = "https://example.com/"
        a = "https://example.com/a"
        fetch_map = {
            start: _ok(start, _html_with_links("/a")),
            a: _ok(a, "<html>a</html>"),
        }
        crawler, fetcher = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=0,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert result.pages_crawled == 1
        assert fetcher.calls == [start]
        assert a not in fetcher.calls


class TestCrawlCyclesAndDedup:
    @pytest.mark.asyncio
    async def test_cycle_fetches_each_once(self) -> None:
        # A -> B -> A : each fetched exactly once.
        a = "https://example.com/a"
        b = "https://example.com/b"
        fetch_map = {
            a: _ok(a, _html_with_links("/b")),
            b: _ok(b, _html_with_links("/a")),
        }
        crawler, fetcher = _crawler(fetch_map)
        result = await crawler.crawl(
            a,
            max_pages=10,
            max_depth=5,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert fetcher.calls.count(a) == 1
        assert fetcher.calls.count(b) == 1
        assert result.pages_crawled == 2

    @pytest.mark.asyncio
    async def test_self_loop_fetched_once(self) -> None:
        a = "https://example.com/a"
        fetch_map = {a: _ok(a, _html_with_links("/a", "/a"))}
        crawler, fetcher = _crawler(fetch_map)
        result = await crawler.crawl(
            a,
            max_pages=10,
            max_depth=5,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert fetcher.calls.count(a) == 1
        assert result.pages_crawled == 1


class TestCrawlBlockedAndOffsite:
    @pytest.mark.asyncio
    async def test_blocked_page_increments_skipped_disallowed(self) -> None:
        start = "https://example.com/"
        blocked = "https://example.com/blocked"
        fetch_map = {
            start: _ok(start, _html_with_links("/blocked")),
            blocked: FetchResult(
                url=blocked,
                final_url=blocked,
                status=FetchStatus.BLOCKED,
                error_message="robots disallowed",
            ),
        }
        crawler, _ = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert result.skipped_disallowed == 1
        # The blocked page is still recorded (with its status) but contributes
        # no links and no content.
        blocked_page = next(p for p in result.pages if p.url == blocked)
        assert blocked_page.status == "blocked"
        assert blocked_page.links_found == 0
        assert blocked_page.content == ""

    @pytest.mark.asyncio
    async def test_blocked_page_contributes_no_links(self) -> None:
        # A blocked page whose (unreachable) HTML has links must not enqueue
        # them -- we never extract links from a non-success fetch.
        start = "https://example.com/"
        blocked = "https://example.com/blocked"
        leaf = "https://example.com/leaf"
        fetch_map = {
            start: _ok(start, _html_with_links("/blocked")),
            blocked: FetchResult(
                url=blocked,
                final_url=blocked,
                status=FetchStatus.BLOCKED,
                html=_html_with_links("/leaf"),
                error_message="blocked",
            ),
            leaf: _ok(leaf, "<html>leaf</html>"),
        }
        crawler, fetcher = _crawler(fetch_map)
        await crawler.crawl(
            start,
            max_pages=10,
            max_depth=3,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert leaf not in fetcher.calls

    @pytest.mark.asyncio
    async def test_offsite_links_excluded_and_counted(self) -> None:
        start = "https://example.com/"
        fetch_map = {
            start: _ok(
                start,
                _html_with_links(
                    "/onsite",
                    "https://external.com/x",
                    "https://another-external.com/y",
                ),
            ),
            "https://example.com/onsite": _ok(
                "https://example.com/onsite", "<html>onsite</html>"
            ),
        }
        crawler, fetcher = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        # Offsite URLs never fetched.
        assert "https://external.com/x" not in fetcher.calls
        assert "https://another-external.com/y" not in fetcher.calls
        # Both offsite links were seen-but-dropped.
        assert result.skipped_offsite == 2

    @pytest.mark.asyncio
    async def test_non_success_non_blocked_records_error(self) -> None:
        start = "https://example.com/"
        broken = "https://example.com/broken"
        fetch_map = {
            start: _ok(start, _html_with_links("/broken")),
            broken: FetchResult(
                url=broken,
                final_url=broken,
                status=FetchStatus.TIMEOUT,
                error_message="timed out",
            ),
        }
        crawler, _ = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        # Timeout is not BLOCKED, so it lands in errors, not skipped_disallowed.
        assert result.skipped_disallowed == 0
        assert any("broken" in e for e in result.errors)
        broken_page = next(p for p in result.pages if p.url == broken)
        assert broken_page.status == "timeout"
        assert broken_page.error_message == "timed out"


class TestCrawlGuards:
    @pytest.mark.asyncio
    async def test_blocked_start_url_no_host(self) -> None:
        crawler, fetcher = _crawler({})
        result = await crawler.crawl(
            "not-a-url",
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert result.stopped_reason == "blocked"
        assert result.pages_crawled == 0
        assert result.errors
        # Nothing was fetched.
        assert fetcher.calls == []

    @pytest.mark.asyncio
    async def test_blocked_start_url_denied_domain(self) -> None:
        config = AppConfig()
        config.safety.denied_domains = ["blocked.example"]
        fetcher = _FakeFetcher({})
        extractor = _FakeExtractor()
        crawler = SiteCrawler(fetcher, extractor, config)  # type: ignore[arg-type]
        result = await crawler.crawl(
            "https://blocked.example/page",
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert result.stopped_reason == "blocked"
        assert result.pages_crawled == 0
        assert fetcher.calls == []

    @pytest.mark.asyncio
    async def test_fetch_exception_does_not_kill_crawl(self) -> None:
        start = "https://example.com/"
        boom = "https://example.com/boom"
        good = "https://example.com/good"

        class _RaisingFetcher(_FakeFetcher):
            async def fetch(
                self, url: str, session_id: Optional[str] = None
            ) -> FetchResult:
                self.calls.append(url)
                if url == boom:
                    raise RuntimeError("kaboom")
                return await super().fetch(url, session_id=session_id)

        fetch_map = {
            start: _ok(start, _html_with_links("/boom", "/good")),
            good: _ok(good, "<html>good</html>"),
        }
        fetcher = _RaisingFetcher(fetch_map)
        crawler = SiteCrawler(fetcher, _FakeExtractor(), AppConfig())  # type: ignore[arg-type]
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        # The crawl survived the raising page and still fetched the good one.
        assert good in fetcher.calls
        assert any(p.url == good for p in result.pages)
        assert any("boom" in w for w in result.warnings)
        # The raising page is NOT recorded as a crawled page.
        assert not any(p.url == boom for p in result.pages)


class TestSitemapSeeding:
    @pytest.mark.asyncio
    async def test_sitemap_seeds_get_crawled(self) -> None:
        start = "https://example.com/"
        sm_url = "https://example.com/sitemap.xml"
        seed_a = "https://example.com/seed-a"
        seed_b = "https://example.com/seed-b"
        sitemap_xml = (
            "<urlset>"
            f"<url><loc>{seed_a}</loc></url>"
            f"<url><loc>{seed_b}</loc></url>"
            "</urlset>"
        )
        fetch_map = {
            start: _ok(start, "<html>home, no links</html>"),
            sm_url: _ok(sm_url, sitemap_xml),
            seed_a: _ok(seed_a, "<html>a</html>"),
            seed_b: _ok(seed_b, "<html>b</html>"),
        }
        crawler, fetcher = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=True,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert result.sitemap_used is True
        assert result.sitemap_urls_seeded == 2
        assert seed_a in fetcher.calls
        assert seed_b in fetcher.calls
        crawled = {p.url for p in result.pages}
        assert seed_a in crawled
        assert seed_b in crawled

    @pytest.mark.asyncio
    async def test_sitemap_index_fans_out(self) -> None:
        start = "https://example.com/"
        sm_url = "https://example.com/sitemap.xml"
        child_url = "https://example.com/sitemap-child.xml"
        seed = "https://example.com/from-child"
        index_xml = f"<sitemapindex><sitemap><loc>{child_url}</loc></sitemap></sitemapindex>"
        child_xml = f"<urlset><url><loc>{seed}</loc></url></urlset>"
        fetch_map = {
            start: _ok(start, "<html>home</html>"),
            sm_url: _ok(sm_url, index_xml),
            child_url: _ok(child_url, child_xml),
            seed: _ok(seed, "<html>seed</html>"),
        }
        crawler, fetcher = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=True,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert result.sitemap_used is True
        assert result.sitemap_urls_seeded == 1
        assert seed in fetcher.calls

    @pytest.mark.asyncio
    async def test_sitemap_offsite_seeds_filtered(self) -> None:
        start = "https://example.com/"
        sm_url = "https://example.com/sitemap.xml"
        onsite = "https://example.com/onsite"
        offsite = "https://evil.com/offsite"
        sitemap_xml = (
            "<urlset>"
            f"<url><loc>{onsite}</loc></url>"
            f"<url><loc>{offsite}</loc></url>"
            "</urlset>"
        )
        fetch_map = {
            start: _ok(start, "<html>home</html>"),
            sm_url: _ok(sm_url, sitemap_xml),
            onsite: _ok(onsite, "<html>onsite</html>"),
        }
        crawler, fetcher = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=True,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert result.sitemap_urls_seeded == 1
        assert onsite in fetcher.calls
        assert offsite not in fetcher.calls

    @pytest.mark.asyncio
    async def test_missing_sitemap_is_silently_skipped(self) -> None:
        # No sitemap.xml in the fetch map -> NETWORK_ERROR -> seeding skipped,
        # crawl proceeds normally without it.
        start = "https://example.com/"
        fetch_map = {start: _ok(start, "<html>home</html>")}
        crawler, _ = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=True,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert result.sitemap_used is False
        assert result.sitemap_urls_seeded == 0
        assert result.pages_crawled == 1
        assert result.stopped_reason == "frontier_empty"


class TestPerPageLinkCap:
    @pytest.mark.asyncio
    async def test_per_page_link_cap_limits_new_links(self) -> None:
        start = "https://example.com/"
        fetch_map = {
            start: _ok(start, _html_with_links("/a", "/b", "/c", "/d")),
        }
        for c in ("a", "b", "c", "d"):
            u = f"https://example.com/{c}"
            fetch_map[u] = _ok(u, "<html>leaf</html>")
        crawler, _ = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=2,
        )
        start_page = next(p for p in result.pages if p.url == start)
        # Only 2 links harvested from the start page due to the cap.
        assert start_page.links_found == 2
        # Exactly start + 2 children fetched.
        assert result.pages_crawled == 3


class TestInjectionRollup:
    @pytest.mark.asyncio
    async def test_injection_rollup_reports_max_and_count(self) -> None:
        from web_agent.models import InjectionReport

        start = "https://example.com/"
        a = "https://example.com/a"
        fetch_map = {
            start: _ok(start, _html_with_links("/a")),
            a: _ok(a, "<html>a</html>"),
        }
        extract_map = {
            start: ExtractionResult(
                url=start,
                content="c1",
                content_length=2,
                extraction_method="bs4",
                injection=InjectionReport(risk="low", score=1.0),
            ),
            a: ExtractionResult(
                url=a,
                content="c2",
                content_length=2,
                extraction_method="bs4",
                injection=InjectionReport(risk="high", score=9.0),
            ),
        }
        crawler, _ = _crawler(fetch_map, extract_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert result.max_injection_risk == "high"
        assert result.pages_with_injection == 2

    @pytest.mark.asyncio
    async def test_injection_none_when_no_reports(self) -> None:
        start = "https://example.com/"
        fetch_map = {start: _ok(start, "<html>home</html>")}
        crawler, _ = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=0,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        # The fallback extractor sets no injection report -> None rollup.
        assert result.max_injection_risk is None
        assert result.pages_with_injection == 0


class TestDiagnostics:
    @pytest.mark.asyncio
    async def test_diagnostic_per_attempted_url(self) -> None:
        start = "https://example.com/"
        a = "https://example.com/a"
        fetch_map = {
            start: _ok(start, _html_with_links("/a")),
            a: _ok(a, "<html>a</html>"),
        }
        crawler, _ = _crawler(fetch_map)
        result = await crawler.crawl(
            start,
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=False,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        diag_urls = {d.url for d in result.diagnostics}
        assert start in diag_urls
        assert a in diag_urls
        # One diagnostic per attempted URL, and timing is populated.
        assert len(result.diagnostics) == result.pages_crawled
        assert result.total_time_ms >= 0.0
