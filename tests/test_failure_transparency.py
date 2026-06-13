"""v1.7.0 Wave 1B: failure transparency tests.

Pain addressed: ``content_extractor`` used to collapse EVERY failed fetch
into a bare ``ExtractionResult(url, extraction_method="none")`` -- the
FetchResult's status / status_code / error_message were discarded, so an
LLM caller behind MCP could not distinguish a 403 bot-wall from a robots
block from a timeout. ``fill_form_and_extract`` was worst: seven distinct
failure modes all collapsed into the identical empty result.

Covered here:

1. Every ``fill_form_and_extract`` failure exit carries the right
   ``failure_stage`` plus a non-empty, actionable ``error_message`` that
   names the failed selector/stage.
2. ``ContentExtractor`` populates ``fetch_status`` / ``status_code`` /
   ``error_message`` / ``failure_stage`` for non-success FetchResults
   (robots-blocked and http-error variants), carrying the fetcher's
   message verbatim or synthesizing actionable text when it is empty.
3. Schema round-trip of the new ExtractionResult fields.
4. Old-style ``ExtractionResult(...)`` constructions still validate
   (all new fields are optional/defaulted).

All offline / mock-driven; no Playwright launch, no network. Mocking
idioms follow ``tests/test_v1614_pipeline.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout
from web_agent.config import AppConfig
from web_agent.content_extractor import (
    ContentExtractor,
    build_fetch_failure_result,
)
from web_agent.models import ExtractionResult, FetchResult, FetchStatus, FormFilterSpec
from web_agent.recipes import Recipes

# ----------------------------------------------------------------------
# Shared fixtures (style mirrors tests/test_v1614_pipeline.py)
# ----------------------------------------------------------------------


def _make_recipes_with_mock_page(page: MagicMock, config: AppConfig | None = None) -> Recipes:
    """Build a Recipes whose BrowserManager yields ``page``."""

    class _NewPageCM:
        async def __aenter__(self) -> MagicMock:
            return page

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    bm = MagicMock()
    bm.new_page = MagicMock(return_value=_NewPageCM())

    cfg = config or AppConfig()
    return Recipes(
        search=MagicMock(),
        fetcher=MagicMock(),
        extractor=ContentExtractor(cfg),
        downloader=MagicMock(),
        config=cfg,
        browser_manager=bm,
        sessions=None,
    )


def _make_page(url: str = "https://example.com/form") -> MagicMock:
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    type(page).url = property(lambda _self: url)
    return page


def _assert_failure(result: ExtractionResult, stage: str, *needles: str) -> None:
    """Common assertions: stage matches, message non-empty + contains needles."""
    assert result.extraction_method == "none"
    assert result.failure_stage == stage, (
        f"expected failure_stage={stage!r}, got {result.failure_stage!r} "
        f"(error_message={result.error_message!r})"
    )
    assert result.error_message, f"stage {stage}: error_message must be non-empty"
    for needle in needles:
        assert needle.lower() in result.error_message.lower(), (
            f"stage {stage}: expected {needle!r} in error_message "
            f"{result.error_message!r}"
        )


# ----------------------------------------------------------------------
# 1. fill_form_and_extract failure exits
# ----------------------------------------------------------------------


class TestFillFormFailureStages:
    @pytest.mark.asyncio
    async def test_domain_blocked_pre_gate(self) -> None:
        cfg = AppConfig(safety={"denied_domains": ["evil.example"]})
        page = _make_page()
        recipes = _make_recipes_with_mock_page(page, config=cfg)

        result = await recipes.fill_form_and_extract("https://evil.example/form", FormFilterSpec())

        _assert_failure(result, "navigation", "do not retry")
        assert result.fetch_status == "blocked"
        # Never navigated.
        page.goto.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_navigation_timeout(self) -> None:
        page = _make_page()
        page.goto = AsyncMock(side_effect=PlaywrightTimeout("nav timed out"))
        recipes = _make_recipes_with_mock_page(page)

        result = await recipes.fill_form_and_extract("https://example.com/form", FormFilterSpec())

        _assert_failure(result, "navigation", "timed out", "wait_timeout_ms")
        assert result.fetch_status == "timeout"

    @pytest.mark.asyncio
    async def test_query_fill_failure_names_selector(self) -> None:
        page = _make_page()
        locator = MagicMock()
        locator.fill = AsyncMock(side_effect=Exception("element not found"))
        page.locator = MagicMock(return_value=locator)
        recipes = _make_recipes_with_mock_page(page)
        spec = FormFilterSpec(query_selector="input#q", query_value="climate")

        result = await recipes.fill_form_and_extract("https://example.com/form", spec)

        _assert_failure(result, "query_fill", "input#q", "web_observe")

    @pytest.mark.asyncio
    async def test_filter_fill_failure_names_selector_and_value(self) -> None:
        page = _make_page()
        locator = MagicMock()
        locator.evaluate = AsyncMock(side_effect=Exception("detached"))
        page.locator = MagicMock(return_value=locator)
        recipes = _make_recipes_with_mock_page(page)
        spec = FormFilterSpec(filters=[("select#year", "2024")])

        result = await recipes.fill_form_and_extract("https://example.com/form", spec)

        _assert_failure(result, "filter_fill", "select#year", "2024", "web_observe")

    @pytest.mark.asyncio
    async def test_submit_click_failure_names_selector(self) -> None:
        page = _make_page()
        locator = MagicMock()
        locator.click = AsyncMock(side_effect=Exception("not clickable"))
        page.locator = MagicMock(return_value=locator)
        recipes = _make_recipes_with_mock_page(page)
        spec = FormFilterSpec(submit_selector="button#go")

        result = await recipes.fill_form_and_extract("https://example.com/form", spec)

        _assert_failure(result, "submit", "button#go", "web_observe")

    @pytest.mark.asyncio
    async def test_submit_enter_press_failure(self) -> None:
        page = _make_page()
        locator = MagicMock()
        locator.fill = AsyncMock()  # query fill succeeds
        locator.press = AsyncMock(side_effect=Exception("press failed"))
        page.locator = MagicMock(return_value=locator)
        recipes = _make_recipes_with_mock_page(page)
        spec = FormFilterSpec(query_selector="input#q", query_value="x")

        result = await recipes.fill_form_and_extract("https://example.com/form", spec)

        _assert_failure(result, "submit", "enter", "input#q")

    @pytest.mark.asyncio
    async def test_wait_for_selector_timeout(self) -> None:
        page = _make_page()
        locator = MagicMock()
        locator.wait_for = AsyncMock(side_effect=PlaywrightTimeout("not visible"))
        page.locator = MagicMock(return_value=locator)
        recipes = _make_recipes_with_mock_page(page)
        spec = FormFilterSpec(wait_for="#results")

        result = await recipes.fill_form_and_extract("https://example.com/form", spec)

        _assert_failure(result, "wait_for", "#results", "web_observe")

    @pytest.mark.asyncio
    async def test_wait_for_networkidle_timeout(self) -> None:
        page = _make_page()
        page.wait_for_load_state = AsyncMock(side_effect=PlaywrightTimeout("idle timeout"))
        recipes = _make_recipes_with_mock_page(page)

        result = await recipes.fill_form_and_extract("https://example.com/form", FormFilterSpec())

        _assert_failure(result, "wait_for", "network-idle", "wait_for")

    @pytest.mark.asyncio
    async def test_post_submit_ssrf_redirect(self) -> None:
        """Post-submit redirect to a link-local host -> 'ssrf_redirect'."""
        allowed_url = "https://example.com/form"
        internal_url = "http://169.254.169.254/latest/meta-data/"
        state = {"submitted": False}

        page = MagicMock()
        page.goto = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        type(page).url = property(
            lambda _self: internal_url if state["submitted"] else allowed_url
        )

        locator = MagicMock()

        async def _click(*_args, **_kwargs):
            state["submitted"] = True

        locator.click = AsyncMock(side_effect=_click)
        page.locator = MagicMock(return_value=locator)
        recipes = _make_recipes_with_mock_page(page)
        spec = FormFilterSpec(submit_selector="button[type=submit]")

        result = await recipes.fill_form_and_extract(allowed_url, spec)

        _assert_failure(result, "ssrf_redirect", "disallowed", "do not retry")
        assert result.fetch_status == "blocked"
        assert state["submitted"] is True

    @pytest.mark.asyncio
    async def test_capture_race_stage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        page = _make_page()

        async def _all_tiers_fail(_page, **_kwargs):
            return ("", "navigating")

        monkeypatch.setattr("web_agent.recipes.safe_page_content", _all_tiers_fail)
        recipes = _make_recipes_with_mock_page(page)

        result = await recipes.fill_form_and_extract("https://example.com/form", FormFilterSpec())

        _assert_failure(result, "capture", "wait_for")
        assert result.content_length == 0

    @pytest.mark.asyncio
    async def test_success_path_has_no_failure_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guard: the failure-transparency edits must not leak failure
        fields onto the happy path."""
        page = _make_page("https://example.com/results")
        real_html = (
            "<html><head><title>Results</title></head><body><main>"
            "<h1>Form results</h1>"
            "<p>This is the real post-submit content with enough text to "
            "satisfy the extraction chain's minimum content length "
            "threshold, spread over a couple of sentences so one of "
            "trafilatura, bs4, or raw reliably wins.</p>"
            "<p>Second paragraph adds more body text for good measure "
            "so the extractor never bails on min_content_length.</p>"
            "</main></body></html>"
        )

        async def _capture_success(_page, **_kwargs):
            return (real_html, "content")

        monkeypatch.setattr("web_agent.recipes.safe_page_content", _capture_success)
        recipes = _make_recipes_with_mock_page(page)

        result = await recipes.fill_form_and_extract(
            "https://example.com/results", FormFilterSpec()
        )

        assert result.extraction_method != "none"
        assert result.failure_stage is None
        assert result.error_message is None
        assert result.content_length > 0


# ----------------------------------------------------------------------
# 2. ContentExtractor failure propagation for non-success FetchResults
# ----------------------------------------------------------------------


class TestExtractorFailurePropagation:
    def _extract(self, fr: FetchResult) -> ExtractionResult:
        return ContentExtractor(AppConfig()).extract(fr)

    def test_robots_blocked_carries_fetcher_message_verbatim(self) -> None:
        robots_msg = "robots.txt for example.com disallows this URL for User-Agent 'webtool'"
        fr = FetchResult(
            url="https://example.com/private",
            final_url="https://example.com/private",
            status=FetchStatus("blocked"),
            error_message=robots_msg,
        )
        result = self._extract(fr)
        assert result.extraction_method == "none"
        assert result.fetch_status == "blocked"
        assert result.failure_stage == "fetch"
        assert result.error_message == robots_msg  # verbatim, not rewritten

    def test_blocked_without_message_synthesizes_do_not_retry(self) -> None:
        fr = FetchResult(
            url="https://example.com/x",
            final_url="https://example.com/x",
            status=FetchStatus("blocked"),
        )
        result = self._extract(fr)
        assert result.fetch_status == "blocked"
        assert result.error_message is not None
        assert "do not retry" in result.error_message
        assert "robots.txt" in result.error_message

    def test_http_403_without_message_synthesizes_auth_hint(self) -> None:
        fr = FetchResult(
            url="https://example.com/x",
            final_url="https://example.com/x",
            status=FetchStatus.HTTP_ERROR,
            status_code=403,
        )
        result = self._extract(fr)
        assert result.fetch_status == "http_error"
        assert result.status_code == 403
        assert result.failure_stage == "fetch"
        assert result.error_message is not None
        assert "403" in result.error_message
        assert "authenticated session" in result.error_message

    def test_http_error_with_message_carries_verbatim_and_code(self) -> None:
        fr = FetchResult(
            url="https://example.com/x",
            final_url="https://example.com/x",
            status=FetchStatus.HTTP_ERROR,
            status_code=503,
            error_message="HTTP 503 Service Unavailable",
        )
        result = self._extract(fr)
        assert result.status_code == 503
        assert result.error_message == "HTTP 503 Service Unavailable"

    def test_timeout_without_message_synthesized(self) -> None:
        fr = FetchResult(
            url="https://slow.example/x",
            final_url="https://slow.example/x",
            status=FetchStatus.TIMEOUT,
        )
        result = self._extract(fr)
        assert result.fetch_status == "timeout"
        assert result.error_message is not None
        assert "timed out" in result.error_message

    def test_network_error_verbatim(self) -> None:
        fr = FetchResult(
            url="https://down.example/x",
            final_url="https://down.example/x",
            status=FetchStatus.NETWORK_ERROR,
            error_message="net::ERR_NAME_NOT_RESOLVED",
        )
        result = self._extract(fr)
        assert result.fetch_status == "network_error"
        assert result.error_message == "net::ERR_NAME_NOT_RESOLVED"
        assert result.failure_stage == "fetch"

    def test_unknown_future_status_value_propagates_generically(self) -> None:
        """The propagation is written off the status VALUE, so any status
        the enum grows (concurrent slices add members) flows through
        without changes here. Simulated via a stub object."""
        er = build_fetch_failure_result(
            FetchResult.model_construct(
                url="https://e/x",
                final_url="https://e/x",
                status="challenge_required",  # not a current member
                status_code=None,
                error_message=None,
                correlation_id=None,
            )
        )
        assert er.fetch_status == "challenge_required"
        assert er.error_message is not None and er.error_message
        assert er.failure_stage == "fetch"

    def test_success_with_empty_html_explains_itself(self) -> None:
        fr = FetchResult(
            url="https://e/x",
            final_url="https://e/x",
            status=FetchStatus.SUCCESS,
            html="",
        )
        result = self._extract(fr)
        assert result.extraction_method == "none"
        assert result.error_message is not None
        assert "no html content" in result.error_message.lower()

    def test_strict_still_raises_on_non_success(self) -> None:
        from web_agent.exceptions import ExtractionError

        fr = FetchResult(
            url="https://e/x",
            final_url="https://e/x",
            status=FetchStatus.TIMEOUT,
        )
        with pytest.raises(ExtractionError):
            ContentExtractor(AppConfig()).extract(fr, strict=True)


# ----------------------------------------------------------------------
# 3 + 4. Schema round-trip and back-compat constructions
# ----------------------------------------------------------------------


class TestSchemaAndBackCompat:
    def test_new_fields_round_trip_json(self) -> None:
        er = ExtractionResult(
            url="https://e/x",
            extraction_method="none",
            fetch_status="http_error",
            status_code=403,
            error_message="server returned 403; consider an authenticated session",
            failure_stage="fetch",
            truncated=True,
            total_content_chars=183000,
            content_offset=40000,
            next_offset=80000,
            truncation_hint="content truncated at 80000 of 183000 chars",
        )
        revived = ExtractionResult.model_validate_json(er.model_dump_json())
        assert revived.fetch_status == "http_error"
        assert revived.status_code == 403
        assert revived.failure_stage == "fetch"
        assert revived.truncated is True
        assert revived.total_content_chars == 183000
        assert revived.content_offset == 40000
        assert revived.next_offset == 80000
        assert revived.truncation_hint is not None
        assert revived.error_message == er.error_message

    def test_old_style_minimal_construction_validates(self) -> None:
        er = ExtractionResult(url="https://e/x", extraction_method="none")
        assert er.fetch_status is None
        assert er.status_code is None
        assert er.error_message is None
        assert er.failure_stage is None
        assert er.truncated is False
        assert er.total_content_chars is None
        assert er.content_offset == 0
        assert er.next_offset is None
        assert er.truncation_hint is None

    def test_old_style_full_construction_validates(self) -> None:
        er = ExtractionResult(
            url="https://e/x",
            title="T",
            content="body text",
            markdown="# body",
            extraction_method="trafilatura",
            content_length=9,
        )
        assert er.truncated is False
        assert er.content == "body text"

    def test_pre_v17_json_dump_still_parses(self) -> None:
        """A serialized pre-v1.7.0 result (no new keys) must validate."""
        legacy = {
            "url": "https://e/x",
            "extraction_method": "bs4",
            "content": "text",
            "content_length": 4,
        }
        er = ExtractionResult.model_validate(legacy)
        assert er.fetch_status is None
        assert er.truncated is False
