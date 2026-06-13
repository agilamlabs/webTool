"""v1.7.0 Wave 8: recipe + Agent wiring for snapshot/diff/crawl (offline).

The snapshot/diff/crawl CORES are tested in test_monitoring / test_crawl /
test_sitemap. This file covers the GLUE the integrator wrote: the
``Recipes.snapshot_page`` / ``diff_page`` / ``crawl_site`` recipes (fetch +
extract + normalize + persist; baseline roll-forward; clamp to config ceilings)
and the thin ``Agent`` delegations. All offline -- the fetcher/extractor are
AsyncMocks; no browser, no network.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
import web_agent.recipes as recipes_mod
from web_agent.agent import Agent
from web_agent.config import AppConfig
from web_agent.crawl import SiteCrawler
from web_agent.models import (
    CrawlResult,
    ExtractionResult,
    FetchResult,
    FetchStatus,
    PageSnapshot,
    SnapshotDiff,
)
from web_agent.recipes import Recipes


def _fr(url: str = "https://example.com/p", html: str = "<html>body</html>") -> FetchResult:
    return FetchResult(url=url, final_url=url, status=FetchStatus.SUCCESS, html=html)


def _ext(markdown: str, *, title: str = "T", method: str = "trafilatura") -> ExtractionResult:
    return ExtractionResult(
        url="https://example.com/p", title=title, markdown=markdown, extraction_method=method
    )


def _recipes(tmp_path, *, fetch=None, extract=None) -> Recipes:
    cfg = AppConfig(base_dir=str(tmp_path))
    fetcher = MagicMock()
    fetcher.fetch_smart = AsyncMock(return_value=fetch if fetch is not None else _fr())
    extractor = MagicMock()
    if isinstance(extract, list):
        extractor.extract_async = AsyncMock(side_effect=extract)
    else:
        extractor.extract_async = AsyncMock(
            return_value=extract if extract is not None else _ext("line one\nline two")
        )
    return Recipes(MagicMock(), fetcher, extractor, MagicMock(), cfg)


# ----------------------------------------------------------------------
# snapshot_page recipe
# ----------------------------------------------------------------------


class TestSnapshotPageRecipe:
    @pytest.mark.asyncio
    async def test_captures_and_persists(self, tmp_path) -> None:
        r = _recipes(tmp_path, extract=_ext("alpha\nbeta\ngamma"))
        snap = await r.snapshot_page("https://example.com/p", label="watch1")
        assert isinstance(snap, PageSnapshot)
        assert snap.fetch_status is None  # success
        assert snap.content == "alpha\nbeta\ngamma"
        assert snap.content_length == len(snap.content)
        assert snap.content_hash
        assert snap.title == "T"
        # persisted under the label
        assert r._snapshot_store().exists("watch1")

    @pytest.mark.asyncio
    async def test_failed_fetch_not_persisted(self, tmp_path) -> None:
        blocked = FetchResult(
            url="https://example.com/p",
            final_url="https://example.com/p",
            status=FetchStatus.BLOCKED,
            error_message="bot wall",
        )
        r = _recipes(tmp_path, fetch=blocked)
        snap = await r.snapshot_page("https://example.com/p", label="watch2")
        assert snap.fetch_status == "blocked"
        assert snap.content == ""
        assert not r._snapshot_store().exists("watch2")

    @pytest.mark.asyncio
    async def test_persist_false_skips_store(self, tmp_path) -> None:
        r = _recipes(tmp_path)
        snap = await r.snapshot_page("https://example.com/p", label="watch3", persist=False)
        assert snap.fetch_status is None
        assert not r._snapshot_store().exists("watch3")

    @pytest.mark.asyncio
    async def test_default_label_is_url_hash_stable(self, tmp_path) -> None:
        r = _recipes(tmp_path)
        snap = await r.snapshot_page("https://example.com/p")
        assert snap.label == r._default_label("https://example.com/p")
        assert r._snapshot_store().exists(snap.label)


# ----------------------------------------------------------------------
# diff_page recipe
# ----------------------------------------------------------------------


class TestDiffPageRecipe:
    @pytest.mark.asyncio
    async def test_first_then_unchanged_then_changed(self, tmp_path) -> None:
        # 1st diff: no baseline -> first snapshot. 2nd: same content -> no change.
        # 3rd: different content -> changed, with added/removed lines.
        r = _recipes(
            tmp_path,
            extract=[
                _ext("alpha\nbeta"),
                _ext("alpha\nbeta"),
                _ext("alpha\nGAMMA"),
            ],
        )
        first = await r.diff_page("https://example.com/p", label="m")
        assert isinstance(first, SnapshotDiff)
        assert first.is_first_snapshot is True
        assert first.changed is True

        second = await r.diff_page("https://example.com/p", label="m")
        assert second.is_first_snapshot is False
        assert second.changed is False
        assert second.similarity == 1.0

        third = await r.diff_page("https://example.com/p", label="m")
        assert third.changed is True
        assert "GAMMA" in third.added_lines
        assert "beta" in third.removed_lines

    @pytest.mark.asyncio
    async def test_failed_capture_reports_no_false_change(self, tmp_path) -> None:
        # Review fix #3: a transient fetch failure must NOT report changed=True
        # (which would fire a false "page changed" alert), nor clobber the baseline.
        cfg = AppConfig(base_dir=str(tmp_path))
        blocked = FetchResult(
            url="https://example.com/p",
            final_url="https://example.com/p",
            status=FetchStatus.BLOCKED,
            error_message="bot wall",
        )
        fetcher = MagicMock()
        fetcher.fetch_smart = AsyncMock(side_effect=[_fr(), blocked])
        extractor = MagicMock()
        extractor.extract_async = AsyncMock(return_value=_ext("alpha\nbeta"))
        r = Recipes(MagicMock(), fetcher, extractor, MagicMock(), cfg)

        await r.snapshot_page("https://example.com/p", label="m")  # good baseline
        d = await r.diff_page("https://example.com/p", label="m")
        assert d.changed is False
        assert d.removed_lines == []
        assert "capture failed" in d.summary
        # baseline preserved (not overwritten by the empty failed snapshot)
        kept = r._snapshot_store().load("m")
        assert kept is not None and kept.content == "alpha\nbeta"

    @pytest.mark.asyncio
    async def test_update_false_keeps_fixed_baseline(self, tmp_path) -> None:
        r = _recipes(
            tmp_path,
            extract=[_ext("base"), _ext("changed once"), _ext("changed twice")],
        )
        await r.snapshot_page("https://example.com/p", label="fix")  # baseline "base"
        # extract side_effect index now at 1
        d1 = await r.diff_page("https://example.com/p", label="fix", update=False)
        d2 = await r.diff_page("https://example.com/p", label="fix", update=False)
        # Both diff against the SAME fixed baseline ("base"), never rolled forward.
        assert d1.changed is True and d2.changed is True
        assert d1.old_hash == d2.old_hash


# ----------------------------------------------------------------------
# crawl_site recipe -- clamps to config ceilings + delegates
# ----------------------------------------------------------------------


class TestCrawlSiteRecipe:
    @pytest.mark.asyncio
    async def test_clamps_to_config_ceilings(self, tmp_path, monkeypatch) -> None:
        captured: dict = {}

        class _FakeCrawler:
            def __init__(self, fetcher, extractor, config) -> None:
                pass

            async def crawl(self, start_url, **kwargs):
                captured.update(kwargs)
                captured["start_url"] = start_url
                return CrawlResult(start_url=start_url, stopped_reason="frontier_empty")

        monkeypatch.setattr(recipes_mod, "SiteCrawler", _FakeCrawler)
        cfg = AppConfig(base_dir=str(tmp_path))  # crawl.max_pages=20, max_depth=3 defaults
        r = Recipes(MagicMock(), MagicMock(), MagicMock(), MagicMock(), cfg)

        out = await r.crawl_site("https://example.com", max_pages=999, max_depth=50)
        assert isinstance(out, CrawlResult)
        assert captured["max_pages"] == 20  # clamped to ceiling
        assert captured["max_depth"] == 3  # clamped to ceiling
        assert captured["sitemap_max_urls"] == cfg.crawl.sitemap_max_urls
        assert captured["per_page_link_cap"] == cfg.crawl.per_page_link_cap
        assert captured["time_budget_s"] == cfg.safety.max_time_per_call_seconds

    @pytest.mark.asyncio
    async def test_passes_caller_values_when_within_bounds(self, tmp_path, monkeypatch) -> None:
        captured: dict = {}

        class _FakeCrawler:
            def __init__(self, *a, **k) -> None:
                pass

            async def crawl(self, start_url, **kwargs):
                captured.update(kwargs)
                return CrawlResult(start_url=start_url)

        monkeypatch.setattr(recipes_mod, "SiteCrawler", _FakeCrawler)
        r = Recipes(MagicMock(), MagicMock(), MagicMock(), MagicMock(), AppConfig(base_dir=str(tmp_path)))
        await r.crawl_site("https://example.com", max_pages=5, max_depth=1, same_registrable_domain=True)
        assert captured["max_pages"] == 5
        assert captured["max_depth"] == 1
        assert captured["same_registrable_domain"] is True


# ----------------------------------------------------------------------
# SiteCrawler review fixes (#1 off-scope child sitemap, #4 max_depth reason)
# ----------------------------------------------------------------------


class _DictFetcher:
    """Fake WebFetcher: returns canned FetchResults keyed by URL, records fetches."""

    def __init__(self, pages: dict) -> None:
        self._pages = pages
        self.fetched: list[str] = []

    async def fetch(self, url, session_id=None):
        self.fetched.append(url)
        return self._pages.get(url) or FetchResult(
            url=url, final_url=url, status=FetchStatus.NETWORK_ERROR
        )


class _PassExtractor:
    async def extract_async(self, fr):
        return ExtractionResult(url=fr.url, markdown=fr.html or "", extraction_method="raw")


def _ok(url: str, html: str = "<html></html>") -> FetchResult:
    return FetchResult(url=url, final_url=url, status=FetchStatus.SUCCESS, html=html)


class TestCrawlReviewFixes:
    @pytest.mark.asyncio
    async def test_offscope_child_sitemap_not_fetched(self, tmp_path) -> None:
        # Review fix #1: a sitemap INDEX pointing at an off-scope child sitemap
        # host must NOT cause the crawler to fetch that off-scope document.
        index = (
            "<sitemapindex>"
            "<sitemap><loc>https://evil.com/sitemap.xml</loc></sitemap>"
            "<sitemap><loc>https://example.com/sitemap2.xml</loc></sitemap>"
            "</sitemapindex>"
        )
        child = "<urlset><url><loc>https://example.com/page1</loc></url></urlset>"
        pages = {
            "https://example.com": _ok("https://example.com"),
            "https://example.com/sitemap.xml": _ok("https://example.com/sitemap.xml", index),
            "https://example.com/sitemap2.xml": _ok("https://example.com/sitemap2.xml", child),
            "https://evil.com/sitemap.xml": _ok(
                "https://evil.com/sitemap.xml",
                "<urlset><url><loc>https://evil.com/x</loc></url></urlset>",
            ),
            "https://example.com/page1": _ok("https://example.com/page1"),
        }
        fetcher = _DictFetcher(pages)
        crawler = SiteCrawler(fetcher, _PassExtractor(), AppConfig(base_dir=str(tmp_path)))
        result = await crawler.crawl(
            "https://example.com",
            max_pages=10,
            max_depth=2,
            same_registrable_domain=False,
            use_sitemap=True,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert "https://evil.com/sitemap.xml" not in fetcher.fetched  # off-scope, skipped
        assert result.sitemap_used is True
        # The in-scope child WAS fetched + its page crawled.
        assert "https://example.com/sitemap2.xml" in fetcher.fetched
        assert any(p.url == "https://example.com/page1" for p in result.pages)

    @pytest.mark.asyncio
    async def test_max_depth_stop_reason(self, tmp_path) -> None:
        # Review fix #4: max_depth=0 + a sitemap seed at depth 1 drains the
        # frontier via the over-depth skip -> stopped_reason 'max_depth'.
        sitemap = "<urlset><url><loc>https://example.com/deep</loc></url></urlset>"
        pages = {
            "https://example.com": _ok("https://example.com"),
            "https://example.com/sitemap.xml": _ok("https://example.com/sitemap.xml", sitemap),
            "https://example.com/deep": _ok("https://example.com/deep"),
        }
        fetcher = _DictFetcher(pages)
        crawler = SiteCrawler(fetcher, _PassExtractor(), AppConfig(base_dir=str(tmp_path)))
        result = await crawler.crawl(
            "https://example.com",
            max_pages=10,
            max_depth=0,
            same_registrable_domain=False,
            use_sitemap=True,
            sitemap_max_urls=100,
            per_page_link_cap=50,
        )
        assert result.stopped_reason == "max_depth"
        # The depth-1 seed was never fetched (over the depth-0 ceiling).
        assert "https://example.com/deep" not in fetcher.fetched


# ----------------------------------------------------------------------
# Agent delegations
# ----------------------------------------------------------------------


def _bare_agent() -> Agent:
    agent = Agent.__new__(Agent)
    agent._config = AppConfig()

    @asynccontextmanager
    async def _noop_scope(_method, _args=None):  # type: ignore[no-untyped-def]
        yield None

    agent._call_scope = _noop_scope  # type: ignore[method-assign]
    agent._debug = MagicMock()
    agent._recipes = MagicMock()
    return agent


class TestAgentDelegations:
    @pytest.mark.asyncio
    async def test_snapshot_page_delegates(self) -> None:
        agent = _bare_agent()
        expected = PageSnapshot(url="https://x")
        agent._recipes.snapshot_page = AsyncMock(return_value=expected)
        out = await agent.snapshot_page("https://x", label="L")
        assert out is expected
        agent._recipes.snapshot_page.assert_awaited_once_with(
            "https://x", label="L", session_id=None, persist=True
        )

    @pytest.mark.asyncio
    async def test_diff_page_delegates(self) -> None:
        agent = _bare_agent()
        expected = SnapshotDiff(url="https://x", changed=False)
        agent._recipes.diff_page = AsyncMock(return_value=expected)
        out = await agent.diff_page("https://x", label="L", update=False)
        assert out is expected
        agent._recipes.diff_page.assert_awaited_once_with(
            "https://x", label="L", session_id=None, update=False
        )

    @pytest.mark.asyncio
    async def test_crawl_site_delegates(self) -> None:
        agent = _bare_agent()
        expected = CrawlResult(start_url="https://x")
        agent._recipes.crawl_site = AsyncMock(return_value=expected)
        out = await agent.crawl_site("https://x", max_pages=7, use_sitemap=False)
        assert out is expected
        agent._recipes.crawl_site.assert_awaited_once_with(
            "https://x",
            max_pages=7,
            max_depth=None,
            same_registrable_domain=None,
            use_sitemap=False,
            include=None,
            exclude=None,
            session_id=None,
        )
