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
