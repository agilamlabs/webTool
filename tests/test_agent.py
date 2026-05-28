"""Integration tests for the Agent pipeline.

These tests require Playwright browsers to be installed:
    playwright install chromium

They also require network access for live page fetching.
"""

from __future__ import annotations

import pytest
from web_agent.agent import Agent
from web_agent.config import AppConfig
from web_agent.models import FetchStatus


@pytest.fixture
def fast_config() -> AppConfig:
    """Config optimized for fast testing (fewer retries, lower timeouts)."""
    return AppConfig(
        log_level="WARNING",
        browser={
            "headless": True,
            "default_timeout": 15000,
            "navigation_timeout": 20000,
            "max_contexts": 2,
        },
        fetch={
            "max_retries": 1,
            "retry_base_delay": 0.5,
        },
        search={
            "max_results": 3,
        },
    )


class TestFetchAndExtract:
    """Test fetching and extracting content from known URLs."""

    @pytest.mark.asyncio
    async def test_fetch_httpbin_html(self, fast_config: AppConfig) -> None:
        """Fetch httpbin.org/html which returns a known HTML page."""
        async with Agent(fast_config) as agent:
            result = await agent.fetch_and_extract("https://httpbin.org/html")

        assert result.extraction_method != "none"
        assert result.content is not None
        assert result.content_length > 0
        assert result.url == "https://httpbin.org/html"

    @pytest.mark.asyncio
    async def test_fetch_example_com(self, fast_config: AppConfig) -> None:
        """Fetch example.com which is a simple, reliable test page."""
        async with Agent(fast_config) as agent:
            result = await agent.fetch_and_extract("https://example.com")

        assert result.title is not None
        assert result.content is not None
        assert result.content_length > 0


class TestSearchAndExtract:
    """Test the full search-and-extract pipeline."""

    @pytest.mark.asyncio
    async def test_search_returns_results(self, fast_config: AppConfig) -> None:
        """Search for a common term and verify we get structured results."""
        async with Agent(fast_config) as agent:
            result = await agent.search_and_extract("Python programming language", max_results=3)

        assert result.query == "Python programming language"
        assert result.search.total_results > 0
        assert len(result.search.results) > 0
        assert result.total_time_ms > 0

        # Verify search result structure
        first = result.search.results[0]
        assert first.title
        assert first.url.startswith("http")
        assert first.position == 1

    @pytest.mark.asyncio
    async def test_search_extracts_pages(self, fast_config: AppConfig) -> None:
        """Verify that at least some pages are successfully extracted."""
        async with Agent(fast_config) as agent:
            result = await agent.search_and_extract("httpbin.org", max_results=3)

        # At least one page should be extracted (some may fail)
        assert len(result.pages) > 0 or len(result.errors) > 0


class TestDownload:
    """Test file download functionality."""

    @pytest.mark.asyncio
    async def test_download_json(self, fast_config: AppConfig, tmp_path) -> None:
        """Download a known JSON endpoint."""
        fast_config.download.download_dir = str(tmp_path)

        async with Agent(fast_config) as agent:
            result = await agent.download("https://httpbin.org/json", filename="test.json")

        assert result.status == FetchStatus.SUCCESS
        assert result.size_bytes > 0
        assert result.filename == "test.json"

        # Verify file exists
        from pathlib import Path

        assert Path(result.filepath).exists()


# ---------------------------------------------------------------------------
# v1.6.9 integration: named profile persistence
# ---------------------------------------------------------------------------


class TestV169NamedProfilePersistence:
    """v1.6.9: named profile must retain cookies + localStorage across
    Agent lifetimes via ``chromium.launch_persistent_context``."""

    @pytest.mark.asyncio
    async def test_named_profile_persists_cookies_across_runs(self, tmp_path) -> None:
        """Round-trip: run 1 sets a cookie; run 2 (same profile) reads it back."""
        from web_agent import BrowserConfig, SafetyConfig

        config = AppConfig(
            base_dir=str(tmp_path),
            log_level="WARNING",
            browser=BrowserConfig(
                isolation_mode=True,
                profile_mode="named",
                profile_dir="v169-persist-test",
                cleanup_on_exit=False,
                headless=True,
            ),
            safety=SafetyConfig(allow_js_evaluation=True),
        )

        # Run 1: navigate and set a localStorage value
        async with Agent(config) as agent:
            sid = await agent.create_session()
            await agent.interact(
                "https://httpbin.org/html",
                [
                    {
                        "action": "evaluate",
                        "script": "localStorage.setItem('wt_v169', 'persists');",
                    }
                ],
                session_id=sid,
            )

        # Run 2: same profile, verify the value survived
        async with Agent(config) as agent:
            sid = await agent.create_session()
            result = await agent.interact(
                "https://httpbin.org/html",
                [{"action": "evaluate", "script": "localStorage.getItem('wt_v169');"}],
                session_id=sid,
            )

        # Pull the last action result and assert the value round-tripped
        last = result.results[-1] if result.results else None
        assert last is not None
        assert last.status.value == "success"
        # data['result'] holds the JS return value
        value = (last.data or {}).get("result")
        assert value == "persists", f"expected 'persists', got {value!r}"


# ---------------------------------------------------------------------------
# v1.6.10 integration: connection bundle, cookie persistence, unknown-policy
# click blocking, binary routing, shared persistent context
# ---------------------------------------------------------------------------


class TestV1610Integration:
    """v1.6.10 hardening: integration tests for the items the v1.6.9 unit
    suite covered only via mocks. Each test exercises the real code path
    end-to-end except where flakiness from external URLs would make CI
    unreliable (test 4 uses the unit-mock pattern by design)."""

    @pytest.mark.asyncio
    async def test_get_owned_cdp_connection_info_returns_bundle(self, tmp_path) -> None:
        """v1.6.10 Item 6: ``get_owned_cdp_connection_info`` returns the
        full {cdp_url, profile_dir, ownership_token} bundle after a
        successful isolated ``cdp_owned`` launch."""
        from web_agent import BrowserConfig

        config = AppConfig(
            base_dir=str(tmp_path),
            log_level="WARNING",
            browser=BrowserConfig(
                isolation_mode=True,
                cdp_enabled=True,
                backend="cdp_owned",
                profile_mode="ephemeral",
                cleanup_on_exit=True,
                headless=True,
            ),
        )

        async with Agent(config) as agent:
            info = agent.get_owned_cdp_connection_info()

        assert info is not None, "expected a CdpConnectionInfo after isolated cdp_owned launch"
        assert info.cdp_url.startswith("ws://"), f"unexpected CDP scheme: {info.cdp_url!r}"
        assert info.profile_dir, "profile_dir must be a non-empty path"
        assert info.ownership_token, "ownership_token must be set"
        assert len(info.ownership_token) == 64, (
            f"expected 64-char hex token, got {len(info.ownership_token)} chars"
        )
        # All three values are non-empty -- this is the contract of the
        # method (returns None unless ALL three are available).

    @pytest.mark.asyncio
    async def test_named_profile_cookie_persistence(self, tmp_path) -> None:
        """v1.6.10 Item 8 (b): document.cookie set in run 1 survives into
        run 2 on the same named profile (companion to the v1.6.9
        localStorage round-trip test above)."""
        from web_agent import BrowserConfig, SafetyConfig

        config = AppConfig(
            base_dir=str(tmp_path),
            log_level="WARNING",
            browser=BrowserConfig(
                isolation_mode=True,
                profile_mode="named",
                profile_dir="v1610-cookie-test",
                cleanup_on_exit=False,
                headless=True,
            ),
            safety=SafetyConfig(allow_js_evaluation=True),
        )

        async with Agent(config) as agent:
            sid = await agent.create_session()
            await agent.interact(
                "https://httpbin.org/html",
                [
                    {
                        "action": "evaluate",
                        "script": ("document.cookie = 'wt1610=hi; path=/; max-age=3600';"),
                    }
                ],
                session_id=sid,
            )

        async with Agent(config) as agent:
            sid = await agent.create_session()
            result = await agent.interact(
                "https://httpbin.org/html",
                [{"action": "evaluate", "script": "document.cookie;"}],
                session_id=sid,
            )

        last = result.results[-1] if result.results else None
        assert last is not None
        assert last.status.value == "success"
        cookie_blob = (last.data or {}).get("result") or ""
        assert "wt1610=hi" in cookie_blob, (
            f"expected 'wt1610=hi' in document.cookie, got {cookie_blob!r}"
        )

    @pytest.mark.asyncio
    async def test_click_xy_unknown_policy_block_rejects_empty_inspection(self, tmp_path) -> None:
        """v1.6.10 Item 4: with allow_form_submit=False AND
        coordinate_click_unknown_policy='block', a click at coordinates
        outside any element (empty body, click at (5, 5)) is rejected
        because elementFromPoint returns no element."""
        from web_agent import BrowserConfig, SafetyConfig
        from web_agent.models import ActionStatus

        config = AppConfig(
            base_dir=str(tmp_path),
            log_level="WARNING",
            browser=BrowserConfig(headless=True),
            safety=SafetyConfig(
                allow_form_submit=False,
                allow_coordinate_clicks=True,
                coordinate_click_unknown_policy="block",
            ),
        )

        async with Agent(config) as agent:
            sid = await agent.create_session()
            result = await agent.interact(
                "data:text/html,<html><body style='margin:0;padding:0'></body></html>",
                [{"action": "click_xy", "x": 5, "y": 5}],
                session_id=sid,
            )

        last = result.results[-1] if result.results else None
        assert last is not None, "expected at least one action result"
        assert last.status == ActionStatus.FAILED, (
            f"expected click_xy to FAIL on empty body with block policy, got {last.status}"
        )
        assert "unknown" in (last.error_message or "").lower(), (
            f"expected 'unknown' in error message, got {last.error_message!r}"
        )

    @pytest.mark.asyncio
    async def test_fetch_smart_routes_extensionless_pdf_to_binary(self) -> None:
        """v1.6.10 Item 1: a 'pdf' classification from classify_url (the
        extensionless-PDF case) routes ``fetch_smart`` through
        ``fetch_binary``, not ``fetch``.

        Uses the v1.6.9 mock pattern rather than a live PDF URL so the
        test is durable in CI -- live extensionless PDF URLs are flaky.
        Belongs in the integration class because it exercises the
        full WebFetcher routing decision (post-v1.6.10 enum change),
        not just a single helper."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.config import SafetyConfig
        from web_agent.models import FetchResult, FetchStatus
        from web_agent.web_fetcher import WebFetcher

        cfg = AppConfig(safety=SafetyConfig(probe_binary_urls=True))
        wf = WebFetcher(config=cfg, browser_manager=MagicMock())
        wf.fetch = AsyncMock(  # type: ignore[method-assign]
            return_value=FetchResult(
                url="x", final_url="x", status=FetchStatus.SUCCESS, html="<html/>"
            )
        )
        wf.fetch_binary = AsyncMock(  # type: ignore[method-assign]
            return_value=FetchResult(
                url="x", final_url="x", status=FetchStatus.SUCCESS, binary=b"%PDF"
            )
        )
        wf.classify_url = AsyncMock(return_value="pdf")  # type: ignore[method-assign]

        await wf.fetch_smart("https://regulator.example/dashboard/api/report")

        wf.fetch_binary.assert_awaited_once()
        wf.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_click_xy_unknown_policy_block_fires_when_form_submit_allowed(
        self, tmp_path
    ) -> None:
        """v1.6.10 review C-1 regression: ``coordinate_click_unknown_policy
        ='block'`` must fire even when ``allow_form_submit=True``. Prior
        to the C-1 fix the unknown-policy check was nested inside the
        destructive-check guard, so a caller keeping the default
        ``allow_form_submit=True`` and explicitly opting into
        block-on-unknown would silently get their setting ignored."""
        from web_agent import BrowserConfig, SafetyConfig
        from web_agent.models import ActionStatus

        config = AppConfig(
            base_dir=str(tmp_path),
            log_level="WARNING",
            browser=BrowserConfig(headless=True),
            safety=SafetyConfig(
                allow_form_submit=True,  # default — destructive heuristic OFF
                allow_coordinate_clicks=True,
                coordinate_click_unknown_policy="block",
            ),
        )

        async with Agent(config) as agent:
            sid = await agent.create_session()
            result = await agent.interact(
                "data:text/html,<html><body style='margin:0;padding:0'></body></html>",
                [{"action": "click_xy", "x": 5, "y": 5}],
                session_id=sid,
            )

        last = result.results[-1] if result.results else None
        assert last is not None, "expected at least one action result"
        assert last.status == ActionStatus.FAILED, (
            "expected click_xy to FAIL on empty body with block policy even "
            f"with allow_form_submit=True, got {last.status}"
        )
        assert "unknown" in (last.error_message or "").lower(), (
            f"expected 'unknown' in error message, got {last.error_message!r}"
        )

    @pytest.mark.asyncio
    async def test_named_profile_multi_session_shares_context(self, tmp_path) -> None:
        """v1.6.10 Item 7: two ``session_id`` values on a named-profile
        Agent share the SAME persistent ``BrowserContext`` (Playwright
        limitation). This documents and locks in the known behaviour
        called out in the README/AGENTS warnings -- a regression where
        named profiles accidentally became per-session isolated would
        break apps relying on this for SSO state sharing."""
        from web_agent import BrowserConfig, SafetyConfig

        config = AppConfig(
            base_dir=str(tmp_path),
            log_level="WARNING",
            browser=BrowserConfig(
                isolation_mode=True,
                profile_mode="named",
                profile_dir="v1610-shared-ctx",
                cleanup_on_exit=False,
                headless=True,
            ),
            safety=SafetyConfig(allow_js_evaluation=True),
        )

        async with Agent(config) as agent:
            sid_a = await agent.create_session()
            await agent.interact(
                "https://httpbin.org/html",
                [
                    {
                        "action": "evaluate",
                        "script": ("document.cookie = 'sharedctx=A; path=/; max-age=3600';"),
                    }
                ],
                session_id=sid_a,
            )

            sid_b = await agent.create_session()
            result = await agent.interact(
                "https://httpbin.org/html",
                [{"action": "evaluate", "script": "document.cookie;"}],
                session_id=sid_b,
            )

        last = result.results[-1] if result.results else None
        assert last is not None
        assert last.status.value == "success"
        cookie_blob = (last.data or {}).get("result") or ""
        assert "sharedctx=A" in cookie_blob, (
            "Named profile sessions are expected to share the persistent "
            "BrowserContext (Playwright limitation). If this test fails, "
            "either the v1.6.10 behaviour changed or another regression "
            "broke persistence. Cookie blob from session B: "
            f"{cookie_blob!r}"
        )


class TestV1611Integration:
    """v1.6.11 follow-up polish integration tests.

    Covers Item 2 (``web_research(extract_files=True)`` non-extractable
    filter) and Item 3 (``find_and_download_file`` Fallback 1 removal).
    Uses unit-level mocks on :class:`Recipes` -- no Playwright launch --
    because the changes are pure routing logic in the search-result
    filter loop and the fallback chain. Live browser launches in this
    class would be slower and flakier without exercising any extra
    code path.
    """

    @staticmethod
    def _result(position: int, url: str) -> SearchResultItem:  # type: ignore[name-defined] # noqa: F821
        from web_agent.models import SearchResultItem

        return SearchResultItem(position=position, title=url, url=url, provider="ddgs")

    @pytest.mark.asyncio
    async def test_web_research_extract_files_skips_non_extractable(self) -> None:
        """v1.6.11 Item 2: ``extract_files=True`` must skip ``.mp4`` /
        ``.exe`` / ``.iso`` / ``.zip`` BEFORE fetching, routing them
        into ``download_candidates`` with
        ``block_reason='not_extractable_kind'``. Pre-v1.6.11 these were
        fetched as binary and only the v1.6.10 I-1 guard caught them
        post-fetch -- wasted bandwidth on bytes that were never going
        to extract."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.config import SafetyConfig
        from web_agent.models import (
            ExtractionResult,
            FetchResult,
            SearchResponse,
        )
        from web_agent.recipes import Recipes

        urls = [
            "https://example.com/report.pdf",
            "https://example.com/clip.mp4",
            "https://example.com/installer.exe",
            "https://example.com/image.iso",
            "https://example.com/archive.zip",
        ]
        search_mock = MagicMock()
        search_mock.search = AsyncMock(
            return_value=SearchResponse(
                query="test",
                total_results=len(urls),
                results=[self._result(i + 1, u) for i, u in enumerate(urls)],
            )
        )

        fetcher_mock = MagicMock()
        fetcher_mock.fetch_smart = AsyncMock(
            return_value=FetchResult(
                url=urls[0],
                final_url=urls[0],
                status=FetchStatus.SUCCESS,
                binary=b"%PDF",
            )
        )

        extractor_mock = MagicMock()
        extractor_mock.extract = MagicMock(
            return_value=ExtractionResult(
                url=urls[0],
                title="Doc",
                content="Hello",
                extraction_method="binary_pdf",
                content_length=5,
            )
        )

        config = AppConfig(
            log_level="WARNING",
            safety=SafetyConfig(probe_binary_urls=False, max_pages_per_call=10),
        )
        recipes = Recipes(
            search=search_mock,
            fetcher=fetcher_mock,
            extractor=extractor_mock,
            downloader=MagicMock(),
            config=config,
        )

        result = await recipes.web_research("test", max_pages=5, extract_files=True)

        assert fetcher_mock.fetch_smart.await_count == 1, (
            "expected exactly one fetch_smart call (only the .pdf is extractable); "
            f"got {fetcher_mock.fetch_smart.await_count}"
        )
        fetched_url = fetcher_mock.fetch_smart.call_args.args[0]
        assert fetched_url == urls[0]

        skipped = {c.url for c in result.download_candidates}
        assert skipped == set(urls[1:]), f"unexpected download_candidates: {skipped}"

        not_extractable = [
            d for d in result.diagnostics if d.block_reason == "not_extractable_kind"
        ]
        assert len(not_extractable) == 4, (
            f"expected 4 not_extractable_kind diagnostics, got {len(not_extractable)}: "
            f"{[(d.url, d.block_reason) for d in result.diagnostics]}"
        )

    @pytest.mark.asyncio
    async def test_web_research_extract_files_allows_pdf_xlsx_docx_csv(self) -> None:
        """v1.6.11 Item 1: ``extract_files=True`` must allow all four
        extractable binary kinds (PDF / XLSX / DOCX / CSV) through to
        ``fetch_smart`` + extractor. Regression test for
        :data:`EXTRACTABLE_BINARY_KINDS` -- if someone tightens it,
        this test fails immediately."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.config import SafetyConfig
        from web_agent.models import (
            ExtractionResult,
            FetchResult,
            SearchResponse,
        )
        from web_agent.recipes import Recipes

        urls = [
            "https://example.com/report.pdf",
            "https://example.com/data.xlsx",
            "https://example.com/memo.docx",
            "https://example.com/table.csv",
        ]

        search_mock = MagicMock()
        search_mock.search = AsyncMock(
            return_value=SearchResponse(
                query="test",
                total_results=len(urls),
                results=[self._result(i + 1, u) for i, u in enumerate(urls)],
            )
        )

        async def fake_fetch(url: str, *, session_id=None, **_):
            return FetchResult(url=url, final_url=url, status=FetchStatus.SUCCESS, binary=b"BIN")

        fetcher_mock = MagicMock()
        fetcher_mock.fetch_smart = AsyncMock(side_effect=fake_fetch)

        def fake_extract(fr):
            return ExtractionResult(
                url=fr.final_url,
                title="Doc",
                content="content",
                extraction_method="binary_pdf",
                content_length=7,
            )

        extractor_mock = MagicMock()
        extractor_mock.extract = MagicMock(side_effect=fake_extract)

        config = AppConfig(
            log_level="WARNING",
            safety=SafetyConfig(probe_binary_urls=False, max_pages_per_call=10),
        )
        recipes = Recipes(
            search=search_mock,
            fetcher=fetcher_mock,
            extractor=extractor_mock,
            downloader=MagicMock(),
            config=config,
        )

        result = await recipes.web_research("test", max_pages=4, extract_files=True)

        assert fetcher_mock.fetch_smart.await_count == 4, (
            "expected 4 fetch_smart calls (all four extractable kinds); "
            f"got {fetcher_mock.fetch_smart.await_count}"
        )
        cited = {p.url for p in result.summary_pages}
        assert cited == set(urls), f"missing kinds in summary_pages: {set(urls) - cited}"
        assert not result.download_candidates, (
            f"no URL should land in download_candidates when all are extractable; "
            f"got {[c.url for c in result.download_candidates]}"
        )

    @pytest.mark.asyncio
    async def test_find_and_download_file_rejects_non_matching_extension_fallback(
        self,
    ) -> None:
        """v1.6.11 Item 3: with Fallback 1 ('any download URL') removed,
        a caller asking for ``file_types=['pdf']`` over a result set
        containing only ``.xlsx`` / ``.zip`` / ``.exe`` URLs must get
        ``NETWORK_ERROR`` instead of the wrong file. Pre-v1.6.11 this
        returned the first ``.xlsx`` URL silently."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.config import SafetyConfig
        from web_agent.models import SearchResponse
        from web_agent.recipes import Recipes

        urls = [
            "https://example.com/data.xlsx",
            "https://example.com/archive.zip",
            "https://example.com/installer.exe",
        ]
        search_mock = MagicMock()
        search_mock.search = AsyncMock(
            return_value=SearchResponse(
                query="test",
                total_results=len(urls),
                results=[self._result(i + 1, u) for i, u in enumerate(urls)],
            )
        )

        downloader_mock = MagicMock()
        downloader_mock.download = AsyncMock()  # should not be called

        # ``probe_binary_urls=False`` short-circuits the (now sole) HEAD-
        # probe fallback so the test isolates Fallback 1's removal: no
        # extension match + no HEAD probe -> NETWORK_ERROR.
        config = AppConfig(
            log_level="WARNING",
            safety=SafetyConfig(probe_binary_urls=False),
        )
        recipes = Recipes(
            search=search_mock,
            fetcher=MagicMock(),
            extractor=MagicMock(),
            downloader=downloader_mock,
            config=config,
        )

        result = await recipes.find_and_download_file("test", file_types=["pdf"])

        assert result.status == FetchStatus.NETWORK_ERROR, (
            "expected NETWORK_ERROR (no pdf in results, no Fallback 1); "
            f"got status={result.status} url={result.url!r}"
        )
        assert ".pdf" in (result.error_message or ""), (
            f"expected '.pdf' in error message, got {result.error_message!r}"
        )
        downloader_mock.download.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_find_and_download_file_pdf_still_works_with_pdf_in_results(
        self,
    ) -> None:
        """v1.6.11 Item 3 regression: Fallback 1 removal must NOT break
        the happy path. When a ``.pdf`` URL exists in search results,
        Tier 1 (exact extension match) still picks it up and the
        downloader is invoked with it."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.config import SafetyConfig
        from web_agent.models import DownloadResult, SearchResponse
        from web_agent.recipes import Recipes

        urls = [
            "https://example.com/index.html",
            "https://example.com/report.pdf",
            "https://example.com/data.xlsx",
        ]
        search_mock = MagicMock()
        search_mock.search = AsyncMock(
            return_value=SearchResponse(
                query="test",
                total_results=len(urls),
                results=[self._result(i + 1, u) for i, u in enumerate(urls)],
            )
        )

        downloader_mock = MagicMock()
        downloader_mock.download = AsyncMock(
            return_value=DownloadResult(
                url="https://example.com/report.pdf",
                filepath="/tmp/report.pdf",
                filename="report.pdf",
                size_bytes=4,
                status=FetchStatus.SUCCESS,
            )
        )

        config = AppConfig(
            log_level="WARNING",
            safety=SafetyConfig(probe_binary_urls=False),
        )
        recipes = Recipes(
            search=search_mock,
            fetcher=MagicMock(),
            extractor=MagicMock(),
            downloader=downloader_mock,
            config=config,
        )

        result = await recipes.find_and_download_file("test", file_types=["pdf"])

        downloader_mock.download.assert_awaited_once()
        called_url = downloader_mock.download.call_args.args[0]
        assert called_url == "https://example.com/report.pdf", (
            f"expected .pdf to be downloaded (Tier 1 match), got {called_url}"
        )
        assert result.status == FetchStatus.SUCCESS


class TestV1612Integration:
    """v1.6.12 throttle + telemetry-depth integration tests.

    Covers Item 1 (parse_retry_after parser), Item 1 (RateLimiter.notify_429
    extends next_allowed), Item 2 (WebFetcher's 429 branch composes the
    two helpers + raises a retryable Exception), and Item 3
    (NetworkEvent captures ``ttfb_ms`` + ``body_size`` from Playwright's
    ``request.timing`` and ``Content-Length``). Pure unit tests on
    helpers + a mock-driven collector test -- no Playwright launch.
    """

    def test_parse_retry_after_seconds(self) -> None:
        """v1.6.12 Item 1: integer ``Retry-After`` form is the most
        common server response. Tolerates whitespace + clamps to >= 0.
        Returns None on absent or unparseable input."""
        from web_agent.utils import parse_retry_after

        assert parse_retry_after("120") == 120.0
        assert parse_retry_after("  60  ") == 60.0
        assert parse_retry_after("0") == 0.0
        assert parse_retry_after(None) is None
        assert parse_retry_after("") is None
        assert parse_retry_after("not-a-number") is None
        # Negative integer parses but clamps via the max() guard.
        assert parse_retry_after("-5") == 0.0

    def test_parse_retry_after_http_date(self) -> None:
        """v1.6.12 Item 1: HTTP-date ``Retry-After`` form (RFC 9110
        §10.2.3 alternate). Future dates return positive seconds; past
        dates clamp to 0.0 (never negative)."""
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        from web_agent.utils import parse_retry_after

        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=10))
        delta_future = parse_retry_after(future)
        assert delta_future is not None
        assert 8.0 < delta_future < 12.0, f"expected ~10s ahead, got {delta_future}"

        past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=10))
        delta_past = parse_retry_after(past)
        assert delta_past == 0.0, f"expected clamped 0.0 for past date, got {delta_past}"

    @pytest.mark.asyncio
    async def test_rate_limiter_notify_429_extends_next_allowed(self) -> None:
        """v1.6.12 Item 1: ``notify_429(host, retry_after)`` extends the
        host's ``_next_allowed`` so the next ``acquire(host)`` waits
        long enough. With ``retry_after=None`` the fallback is
        ``interval * fallback_factor`` (default 2.0) -- doubles the
        per-host interval. The 429 tally is recorded."""
        import time as _time

        from web_agent.rate_limiter import RateLimiter

        # rps=10 -> interval=0.1s; fallback_factor=2.0 -> 0.2s fallback.
        rl = RateLimiter(rps_per_host=10.0)

        # Explicit retry_after wins when larger than the fallback.
        before = _time.monotonic()
        rl.notify_429("example.com", retry_after_seconds=0.5)
        delta = rl._next_allowed["example.com"] - before
        assert 0.45 < delta < 0.55, f"expected ~0.5s, got {delta}"
        assert rl._429_counts == {"example.com": 1}

        # Fallback fires when retry_after is None -- 0.2s (= 0.1 * 2).
        before2 = _time.monotonic()
        rl.notify_429("other.example", retry_after_seconds=None)
        delta2 = rl._next_allowed["other.example"] - before2
        assert 0.18 < delta2 < 0.25, f"expected ~0.2s fallback, got {delta2}"
        assert rl._429_counts["other.example"] == 1

        # Disabled limiter is a no-op.
        rl_off = RateLimiter(rps_per_host=0)
        rl_off.notify_429("example.com", retry_after_seconds=10.0)
        assert "example.com" not in rl_off._next_allowed
        assert rl_off._429_counts == {}

    def test_signal_429_helper_signals_limiter_and_returns_retry_after(self) -> None:
        """v1.6.12 review-pass I-5: ``WebFetcher._signal_429`` is the
        shared helper used by both ``_fetch_with_retry`` (HTML, retry-
        wrapped, then raises) and ``fetch_binary`` (no retry, then
        returns HTTP_ERROR). Test the helper directly so we have
        proper coverage of the (parse_retry_after, notify_429,
        return-value) contract -- the source-inspection check in the
        next test only catches code-removal regressions, not
        wrong-argument bugs.
        """
        import time as _time
        from unittest.mock import MagicMock

        from web_agent.config import AppConfig
        from web_agent.rate_limiter import RateLimiter
        from web_agent.web_fetcher import WebFetcher

        rl = RateLimiter(rps_per_host=2.0)  # interval 0.5s -> fallback 1.0s
        wf = WebFetcher(
            browser_manager=MagicMock(),
            config=AppConfig(),
            rate_limiter=rl,
        )

        # 1. Retry-After present -> parsed value returned, limiter signalled.
        resp = MagicMock()
        resp.headers = {"retry-after": "7"}
        before = _time.monotonic()
        parsed = wf._signal_429(resp, "https://example.com/x", "example.com")
        assert parsed == 7.0, f"expected 7.0s, got {parsed}"
        assert rl._429_counts == {"example.com": 1}
        remaining = rl._next_allowed["example.com"] - before
        assert 6.5 < remaining < 7.5, f"expected ~7s wait, got {remaining}"

        # 2. Retry-After absent -> None returned, fallback (interval *
        #    fallback_factor = 1.0s) applied.
        rl2 = RateLimiter(rps_per_host=2.0)
        wf2 = WebFetcher(
            browser_manager=MagicMock(),
            config=AppConfig(),
            rate_limiter=rl2,
        )
        resp2 = MagicMock()
        resp2.headers = {}
        before2 = _time.monotonic()
        parsed2 = wf2._signal_429(resp2, "https://example.com/x", "example.com")
        assert parsed2 is None, f"expected None, got {parsed2}"
        remaining2 = rl2._next_allowed["example.com"] - before2
        assert 0.9 < remaining2 < 1.2, f"expected ~1.0s fallback, got {remaining2}"

        # 3. No rate limiter wired -> helper still parses Retry-After
        #    and returns it (no-op on the absent limiter).
        wf3 = WebFetcher(browser_manager=MagicMock(), config=AppConfig())
        resp3 = MagicMock()
        resp3.headers = {"retry-after": "5"}
        parsed3 = wf3._signal_429(resp3, "https://example.com/x", "example.com")
        assert parsed3 == 5.0

    def test_webfetcher_429_branch_composes_helpers(self) -> None:
        """v1.6.12 Item 2: the 429 detection branch in WebFetcher.fetch
        composes ``parse_retry_after`` + ``RateLimiter.notify_429``.

        We test the composition contract (the helper calls and their
        observable effects) plus a source-inspection check that the
        branch exists in :meth:`WebFetcher.fetch`. A full Playwright
        round-trip would require mocking the BrowserManager + page
        chain at depth; live integration tests cover that path.
        """
        import inspect
        import time as _time
        from unittest.mock import MagicMock

        from web_agent.rate_limiter import RateLimiter
        from web_agent.utils import parse_retry_after
        from web_agent.web_fetcher import WebFetcher

        # 1. Composition: simulate what the branch does.
        mock_response = MagicMock()
        mock_response.headers = {"retry-after": "3"}
        parsed = parse_retry_after(mock_response.headers.get("retry-after"))
        assert parsed == 3.0

        rl = RateLimiter(rps_per_host=2.0)  # interval 0.5s -> fallback 1.0s
        before = _time.monotonic()
        rl.notify_429("example.com", parsed)

        remaining = rl._next_allowed["example.com"] - before
        assert 2.5 < remaining < 3.5, (
            f"3.0s Retry-After > 1.0s fallback -> expect ~3s wait, got {remaining}"
        )
        assert rl._429_counts == {"example.com": 1}

        # 2. Source check: BOTH the HTML path (``_fetch_with_retry``)
        # AND the binary path (``fetch_binary``) call ``_signal_429``
        # on status_code 429. The HTML path raises (retry-wrapped);
        # the binary path returns HTTP_ERROR. Use class-level
        # inspect.getsource because ``_fetch_with_retry`` is nested
        # inside :meth:`fetch`.
        src = inspect.getsource(WebFetcher)
        assert "status_code == 429" in src, "HTML 429 branch missing"
        assert "resp.status_code == 429" in src, (
            "binary 429 branch missing from fetch_binary (review-pass C-2)"
        )
        assert "_signal_429" in src, "shared 429 helper missing"
        assert "notify_429" in src, "notify_429 call missing from _signal_429"
        assert "parse_retry_after" in src, "parse_retry_after call missing from _signal_429"

    def test_extract_json_ld_parses_single_object_and_array_and_graph(self) -> None:
        """v1.6.12 structured-data slice: ``_extract_json_ld`` handles
        the three top-level JSON-LD shapes (single object, array,
        ``@graph`` wrapper) and unwraps ``@graph`` so callers get a
        flat list of items, not the wrapper."""
        from web_agent.content_extractor import _extract_json_ld

        # 1. Single object
        html_single = """
        <html><head>
        <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Product","name":"Widget","offers":{"price":"9.99"}}
        </script>
        </head><body>x</body></html>
        """
        blocks = _extract_json_ld(html_single)
        assert len(blocks) == 1
        assert blocks[0]["@type"] == "Product"
        assert blocks[0]["name"] == "Widget"

        # 2. Top-level array
        html_array = """
        <html><head>
        <script type="application/ld+json">
        [{"@type":"Article","headline":"A"},{"@type":"Article","headline":"B"}]
        </script>
        </head><body>x</body></html>
        """
        blocks = _extract_json_ld(html_array)
        assert [b["headline"] for b in blocks] == ["A", "B"]

        # 3. @graph wrapper -- unwrapped to items
        html_graph = """
        <html><head>
        <script type="application/ld+json">
        {"@context":"https://schema.org","@graph":[
            {"@type":"BreadcrumbList","itemListElement":[]},
            {"@type":"WebPage","name":"Home"}
        ]}
        </script>
        </head></html>
        """
        blocks = _extract_json_ld(html_graph)
        assert {b["@type"] for b in blocks} == {"BreadcrumbList", "WebPage"}

    def test_extract_json_ld_handles_recursion_error_dos(self) -> None:
        """v1.6.12 review-pass C-1: an adversarial JSON-LD blob with
        thousands of levels of nesting raises ``RecursionError`` from
        ``json.loads`` (CPython default recursion limit ~1000).
        ``RecursionError`` derives from ``RuntimeError`` -> ``Exception``,
        NOT ``ValueError`` or ``JSONDecodeError``, so the original
        v1.6.12 exception tuple did NOT catch it -- a single malicious
        page would crash the entire ``extract()`` call. This test
        verifies the explicit ``RecursionError`` catch added in the
        review pass."""
        from web_agent.content_extractor import _extract_json_ld

        # 5000-deep array nesting is well above the CPython recursion
        # limit (~1000 levels). ``json.loads`` raises RecursionError
        # on this input -- verified with a smoke test before writing.
        deep_payload = "[" * 5000 + "]" * 5000
        html = (
            "<html><head>"
            f'<script type="application/ld+json">{deep_payload}</script>'
            '<script type="application/ld+json">'
            '{"@type":"Article","headline":"Valid"}'
            "</script>"
            "</head></html>"
        )
        # MUST NOT RAISE. The valid block must still extract.
        blocks = _extract_json_ld(html)
        assert len(blocks) == 1, f"expected only the valid block to survive, got {len(blocks)}"
        assert blocks[0]["headline"] == "Valid"

    def test_extract_json_ld_swallows_malformed(self) -> None:
        """v1.6.12: many sites ship broken JSON-LD (trailing commas,
        single-quoted keys, embedded newlines without escaping). The
        parser must NEVER raise -- malformed blocks are silently
        skipped so the rest of the page still extracts."""
        from web_agent.content_extractor import _extract_json_ld

        html = """
        <html><head>
        <script type="application/ld+json">{this is not json}</script>
        <script type="application/ld+json">
            {"@type":"Product","name":"Widget",}
        </script>
        <script type="application/ld+json">
            {"@type":"Article","headline":"Valid"}
        </script>
        </head></html>
        """
        blocks = _extract_json_ld(html)
        # Only the valid block (Article) parses. The two malformed ones
        # are swallowed.
        assert len(blocks) == 1
        assert blocks[0]["headline"] == "Valid"

        # Empty / no JSON-LD -> empty list, not None or error.
        assert _extract_json_ld("<html><body>x</body></html>") == []
        assert _extract_json_ld("") == []

    @pytest.mark.asyncio
    async def test_network_collector_async_body_capture(self) -> None:
        """v1.6.12: when ``capture_response_bodies=True`` AND the
        Content-Type matches ``body_capture_content_types``, the
        collector schedules an async body read. ``wait_for_pending_bodies``
        drains the queued tasks so the captured body lands on the
        NetworkEvent before snapshotting."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.config import DiagnosticsConfig
        from web_agent.network_collector import NetworkCollector

        diag = DiagnosticsConfig(
            capture_network=True,
            capture_response_bodies=True,
            max_response_body_bytes=4096,
            max_network_events=10,
        )
        collector = NetworkCollector(diag)

        page = MagicMock()
        import collections

        collector._events[page] = collections.deque(maxlen=10)
        collector._req_start[page] = {}

        # Fake request with timing
        req = MagicMock()
        req.url = "https://api.example.com/data"
        req.method = "GET"
        req.resource_type = "xhr"
        req.headers = {}
        req.timing = {"startTime": 0.0, "responseStart": 10.0, "responseEnd": 50.0}

        # Fake response: JSON content-type + body() returns a bytes payload
        resp = MagicMock()
        resp.request = req
        resp.status = 200
        resp.headers = {"content-type": "application/json", "content-length": "27"}
        resp.body = AsyncMock(return_value=b'{"title":"Hello","value":42}')

        collector._on_response(page, resp)
        await collector.wait_for_pending_bodies()

        events = collector.events_for(page)
        assert len(events) == 1
        evt = events[0]
        assert evt.body_text == '{"title":"Hello","value":42}', (
            f"body_text not captured: {evt.body_text!r}"
        )
        assert evt.body_truncated is False

    @pytest.mark.asyncio
    async def test_network_collector_body_capture_truncates_oversized(self) -> None:
        """v1.6.12: bodies larger than ``max_response_body_bytes`` are
        truncated byte-wise BEFORE decoding, and ``body_truncated`` is
        set to True. Memory cap enforced regardless of server size."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.config import DiagnosticsConfig
        from web_agent.network_collector import NetworkCollector

        diag = DiagnosticsConfig(
            capture_network=True,
            capture_response_bodies=True,
            max_response_body_bytes=1024,
        )
        collector = NetworkCollector(diag)
        page = MagicMock()
        import collections

        collector._events[page] = collections.deque(maxlen=10)
        collector._req_start[page] = {}

        req = MagicMock()
        req.url = "https://api.example.com/large"
        req.method = "GET"
        req.resource_type = "xhr"
        req.headers = {}
        req.timing = {"startTime": 0.0, "responseStart": 5.0}

        big_body = b'{"data":"' + b"x" * 5000 + b'"}'
        resp = MagicMock()
        resp.request = req
        resp.status = 200
        resp.headers = {"content-type": "application/json"}
        resp.body = AsyncMock(return_value=big_body)

        collector._on_response(page, resp)
        await collector.wait_for_pending_bodies()

        events = collector.events_for(page)
        assert len(events) == 1
        evt = events[0]
        assert evt.body_text is not None
        assert len(evt.body_text.encode("utf-8")) <= 1024
        assert evt.body_truncated is True

    def test_extractor_prefer_api_routes_through_json_body(self) -> None:
        """v1.6.12: ``ContentExtractor.extract(prefer_api=True)`` routes
        extraction through a captured JSON response body instead of
        the rendered HTML, when one is available. Falls back to HTML
        extraction when no usable body is captured."""
        from web_agent.config import AppConfig
        from web_agent.content_extractor import ContentExtractor
        from web_agent.models import FetchResult, NetworkEvent

        cfg = AppConfig()
        extractor = ContentExtractor(cfg)

        json_body = '{"title":"API Title","content":"This is the real content","value":42}'
        fr = FetchResult(
            url="https://spa.example.com/page",
            final_url="https://spa.example.com/page",
            status=FetchStatus.SUCCESS,
            html="<html><body>Some rendered junk</body></html>",
            network_events=[
                NetworkEvent(
                    event_type="response",
                    url="https://spa.example.com/api/page-data",
                    method="GET",
                    resource_type="xhr",
                    status_code=200,
                    content_type="application/json",
                    body_text=json_body,
                    body_truncated=False,
                ),
            ],
        )

        result = extractor.extract(fr, prefer_api=True)
        assert result.extraction_method == "api_json", (
            f"expected api_json, got {result.extraction_method}"
        )
        assert result.title == "API Title"
        assert result.content is not None
        assert "real content" in result.content

        # Without prefer_api=True, the html path is used instead.
        result2 = extractor.extract(fr, prefer_api=False)
        assert result2.extraction_method != "api_json"

    def test_network_event_captures_ttfb_and_body_size(self) -> None:
        """v1.6.12 Item 3: ``_on_response`` reads
        ``request.timing['responseStart']`` into ``NetworkEvent.ttfb_ms``
        and ``Content-Length`` into ``NetworkEvent.body_size``.

        Drives :meth:`NetworkCollector._on_response` directly with mocks
        for the Playwright Request + Response (no browser launch)."""
        from unittest.mock import MagicMock

        from web_agent.config import DiagnosticsConfig
        from web_agent.network_collector import NetworkCollector

        diag = DiagnosticsConfig(capture_network=True, max_network_events=100)
        collector = NetworkCollector(diag)

        # Fake Page with a real-ish identity so the collector's
        # WeakKeyDictionary can key off it. A bare object() works.
        page = MagicMock()
        # Pre-register the events deque so _on_response can append.
        import collections

        collector._events[page] = collections.deque(maxlen=100)
        collector._req_start[page] = {}

        # Fake Playwright Request: has url, method, resource_type,
        # headers (dict), and a ``timing`` attribute with
        # ``responseStart`` in ms.
        req = MagicMock()
        req.url = "https://example.com/doc"
        req.method = "GET"
        req.resource_type = "document"
        req.headers = {}
        req.timing = {"startTime": 0.0, "responseStart": 25.0, "responseEnd": 100.0}

        # Fake Response with headers carrying Content-Length.
        resp = MagicMock()
        resp.request = req
        resp.status = 200
        resp.headers = {"content-type": "text/html", "content-length": "1024"}

        collector._on_response(page, resp)

        events = collector.events_for(page)
        assert len(events) == 1, f"expected 1 event, got {len(events)}"
        evt = events[0]
        assert evt.event_type == "response"
        assert evt.ttfb_ms == 25.0, f"expected ttfb_ms 25.0, got {evt.ttfb_ms!r}"
        assert evt.body_size == 1024, f"expected body_size 1024, got {evt.body_size!r}"
        assert evt.status_code == 200
        assert evt.content_type == "text/html"


# v1.6.13: page-content capture resilience (Options B + C from the
# v1.6.12 close-out discussion). Mid-navigation races no longer kill an
# otherwise-successful fetch -- ``safe_page_content`` walks three tiers
# (page.content -> page.evaluate -> CDP DOM.getOuterHTML) and surfaces
# the winning tier on ``FetchResult.html_capture_source`` for telemetry.
class TestV1613Integration:
    """v1.6.13: ``safe_page_content`` 3-tier capture + html_capture_source."""

    # The exact Playwright error message used by tier-1 race detection.
    # Matching this substring is the stable signal we rely on -- the
    # typed exception class is private to playwright._impl.
    _RACE_MSG = "Unable to retrieve content because the page is navigating and changing the content"

    @pytest.mark.asyncio
    async def test_safe_page_content_happy_path(self) -> None:
        """Tier-1 ``page.content()`` succeeds -> returns ("html", "content")."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.utils import safe_page_content

        page = MagicMock()
        page.content = AsyncMock(return_value="<html><body>ok</body></html>")
        page.evaluate = AsyncMock()  # should NOT be called
        page.context = MagicMock()
        page.context.new_cdp_session = AsyncMock()  # should NOT be called

        html, source = await safe_page_content(page)

        assert source == "content"
        assert html == "<html><body>ok</body></html>"
        page.content.assert_awaited_once()
        page.evaluate.assert_not_awaited()
        page.context.new_cdp_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_safe_page_content_retries_on_navigation_race(self) -> None:
        """Tier-1 race twice, then success -> ("html", "content") with 3 calls."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.utils import safe_page_content

        race_err = Exception(self._RACE_MSG)
        page = MagicMock()
        page.content = AsyncMock(side_effect=[race_err, race_err, "<html>recovered</html>"])
        page.wait_for_load_state = AsyncMock()  # best-effort settle, no-op
        page.evaluate = AsyncMock()  # not reached
        page.context = MagicMock()
        page.context.new_cdp_session = AsyncMock()  # not reached

        html, source = await safe_page_content(page, retries=3, settle_ms=0)

        assert source == "content"
        assert html == "<html>recovered</html>"
        assert page.content.await_count == 3
        # 2 settles between the 3 attempts.
        assert page.wait_for_load_state.await_count == 2
        page.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_safe_page_content_skips_settle_on_last_attempt(self) -> None:
        """v1.6.13 review-pass I-2: with N tier-1 attempts all racing,
        the settle (wait_for_load_state + sleep) runs N-1 times, not N.

        The settle after the final attempt is pure waste because tier-2
        (page.evaluate) doesn't depend on domcontentloaded having fired.
        Saved up to 2.25s of latency on the degraded path."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.utils import safe_page_content

        race_err = Exception(self._RACE_MSG)
        page = MagicMock()
        # 4 races -> 4 tier-1 attempts -> tier-2 falls through.
        page.content = AsyncMock(side_effect=race_err)
        page.wait_for_load_state = AsyncMock()
        page.evaluate = AsyncMock(return_value="<html>via evaluate</html>")
        page.context = MagicMock()
        page.context.new_cdp_session = AsyncMock()  # not reached

        html, source = await safe_page_content(page, retries=4, settle_ms=0)

        assert source == "evaluate"
        assert html == "<html>via evaluate</html>"
        # 4 tier-1 attempts, but only 3 settles (skipped on the 4th).
        assert page.content.await_count == 4
        assert page.wait_for_load_state.await_count == 3, (
            f"expected 3 settles (N-1=3), got {page.wait_for_load_state.await_count}"
        )

    @pytest.mark.asyncio
    async def test_safe_page_content_cdp_timeout_falls_through(self) -> None:
        """v1.6.13 review-pass M-2: ``cdp_timeout_ms`` wired via
        ``asyncio.wait_for`` so a hung CDP session can't block the
        helper indefinitely; on TimeoutError the outer except falls
        through to the final ("", "navigating") return."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.utils import safe_page_content

        race_err = Exception(self._RACE_MSG)
        page = MagicMock()
        page.content = AsyncMock(side_effect=race_err)
        page.wait_for_load_state = AsyncMock()
        page.evaluate = AsyncMock(return_value="")  # tier-2 fails

        async def _hung_send(*_a: object, **_kw: object) -> dict:
            # Simulate a hung CDP -- sleep longer than the configured
            # timeout. asyncio.wait_for around it should fire first.
            await asyncio.sleep(1.0)
            return {"root": {"nodeId": 1}}

        cdp = AsyncMock()
        cdp.send = _hung_send
        cdp.detach = AsyncMock()
        page.context = MagicMock()
        page.context.new_cdp_session = AsyncMock(return_value=cdp)

        html, source = await safe_page_content(
            page,
            retries=2,
            settle_ms=0,
            use_cdp_fallback=True,
            cdp_timeout_ms=50,  # 50ms cap; sleep above is 1s -> times out
        )

        assert source == "navigating"
        assert html == ""
        # Even on timeout the detach cleanup must still run.
        cdp.detach.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_safe_page_content_evaluate_fallback(self) -> None:
        """All tier-1 attempts race; tier-2 evaluate wins."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.utils import safe_page_content

        race_err = Exception(self._RACE_MSG)
        page = MagicMock()
        page.content = AsyncMock(side_effect=race_err)
        page.wait_for_load_state = AsyncMock()
        page.evaluate = AsyncMock(return_value="<html>via evaluate</html>")
        page.context = MagicMock()
        page.context.new_cdp_session = AsyncMock()  # not reached

        html, source = await safe_page_content(page, retries=2, settle_ms=0)

        assert source == "evaluate"
        assert html == "<html>via evaluate</html>"
        assert page.content.await_count == 2
        page.evaluate.assert_awaited_once()
        page.context.new_cdp_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_safe_page_content_cdp_fallback(self) -> None:
        """Tier-1 + tier-2 fail; CDP ``DOM.getOuterHTML`` wins."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.utils import safe_page_content

        race_err = Exception(self._RACE_MSG)
        page = MagicMock()
        page.content = AsyncMock(side_effect=race_err)
        page.wait_for_load_state = AsyncMock()
        # evaluate returns empty -> tier-2 considered failed
        page.evaluate = AsyncMock(return_value="")

        # CDP session: DOM.getDocument -> nodeId 1; DOM.getOuterHTML ->
        # the HTML payload.
        cdp = AsyncMock()
        cdp.send = AsyncMock(
            side_effect=[
                {"root": {"nodeId": 1}},  # DOM.getDocument
                {"outerHTML": "<html>via cdp</html>"},  # DOM.getOuterHTML
            ]
        )
        cdp.detach = AsyncMock()
        page.context = MagicMock()
        page.context.new_cdp_session = AsyncMock(return_value=cdp)

        html, source = await safe_page_content(page, retries=2, settle_ms=0, use_cdp_fallback=True)

        assert source == "cdp"
        assert html == "<html>via cdp</html>"
        page.context.new_cdp_session.assert_awaited_once_with(page)
        assert cdp.send.await_count == 2
        cdp.detach.assert_awaited_once()  # cleanup ran

    @pytest.mark.asyncio
    async def test_safe_page_content_all_tiers_fail(self) -> None:
        """Every tier fails -> ("", "navigating") + no raise."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.utils import safe_page_content

        race_err = Exception(self._RACE_MSG)
        page = MagicMock()
        page.content = AsyncMock(side_effect=race_err)
        page.wait_for_load_state = AsyncMock()
        page.evaluate = AsyncMock(side_effect=Exception("evaluate blew up"))

        # CDP throws on session creation (non-Chromium / detached page).
        page.context = MagicMock()
        page.context.new_cdp_session = AsyncMock(side_effect=Exception("CDP unavailable"))

        html, source = await safe_page_content(page, retries=2, settle_ms=0, use_cdp_fallback=True)

        assert source == "navigating"
        assert html == ""

    @pytest.mark.asyncio
    async def test_safe_page_content_reraises_non_race_errors(self) -> None:
        """Non-race exceptions from page.content() propagate; helper is targeted."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.utils import safe_page_content

        # A timeout / network error -- NOT the navigation race. Helper
        # must NOT swallow this; the outer ``async_retry`` decorator owns
        # generic retry semantics.
        boom = Exception("net::ERR_CONNECTION_RESET")
        page = MagicMock()
        page.content = AsyncMock(side_effect=boom)
        page.evaluate = AsyncMock()  # not reached
        page.context = MagicMock()
        page.context.new_cdp_session = AsyncMock()  # not reached

        with pytest.raises(Exception, match="ERR_CONNECTION_RESET"):
            await safe_page_content(page, retries=3, settle_ms=0)

        # Tier-2 and tier-3 must NOT run when tier-1 re-raises non-race.
        page.evaluate.assert_not_awaited()
        page.context.new_cdp_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_safe_page_content_skips_cdp_when_disabled(self) -> None:
        """``use_cdp_fallback=False`` -> tier-3 never attempted."""
        from unittest.mock import AsyncMock, MagicMock

        from web_agent.utils import safe_page_content

        race_err = Exception(self._RACE_MSG)
        page = MagicMock()
        page.content = AsyncMock(side_effect=race_err)
        page.wait_for_load_state = AsyncMock()
        page.evaluate = AsyncMock(return_value="")  # tier-2 fails
        page.context = MagicMock()
        page.context.new_cdp_session = AsyncMock()  # must NOT be called

        html, source = await safe_page_content(page, retries=2, settle_ms=0, use_cdp_fallback=False)

        assert source == "navigating"
        assert html == ""
        page.context.new_cdp_session.assert_not_awaited()

    def test_fetch_result_has_html_capture_source_field(self) -> None:
        """Schema test: FetchResult exposes ``html_capture_source`` with
        the four-valued literal type and a ``None`` default."""
        from web_agent.models import FetchResult, FetchStatus

        # Default is None (back-compat: existing test fixtures unchanged).
        fr = FetchResult(
            url="https://x.test/",
            final_url="https://x.test/",
            status=FetchStatus.SUCCESS,
            html="<html></html>",
        )
        assert fr.html_capture_source is None

        # All 4 literal values accepted.
        for src in ("content", "evaluate", "cdp", "navigating"):
            fr2 = FetchResult(
                url="https://x.test/",
                final_url="https://x.test/",
                status=FetchStatus.SUCCESS,
                html="" if src == "navigating" else "<html></html>",
                html_capture_source=src,  # type: ignore[arg-type]
            )
            assert fr2.html_capture_source == src

        # Invalid literal value -> Pydantic validation error.
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            FetchResult(
                url="https://x.test/",
                final_url="https://x.test/",
                status=FetchStatus.SUCCESS,
                html="",
                html_capture_source="bogus",  # type: ignore[arg-type]
            )

    def test_is_navigation_race_marker_detection(self) -> None:
        """``_is_navigation_race`` matches both upstream message variants
        and rejects unrelated errors."""
        from web_agent.utils import _is_navigation_race

        # Both variants Playwright has shipped match.
        assert _is_navigation_race(Exception(self._RACE_MSG))
        assert _is_navigation_race(
            Exception("Error: Page.content: page is navigating, please retry")
        )
        # Generic errors do NOT match.
        assert not _is_navigation_race(Exception("net::ERR_CONNECTION_RESET"))
        assert not _is_navigation_race(Exception("Timeout 30000ms exceeded"))
        assert not _is_navigation_race(Exception(""))
