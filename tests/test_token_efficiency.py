"""v1.7.0 Wave 1B: token-efficiency tests (MCP-boundary response shaping).

Pain addressed: MCP responses could carry ~1MB of content, often
DUPLICATED (``content`` + ``markdown`` both populated), with no per-call
size/format/offset controls and no truncation signal -- the top complaint
class against web MCP tools ("Prompt is too long" after 2-3 calls).

Covered here:

1. ``slice_text_window`` math: offset/next_offset/total/truncated
   including boundaries (offset beyond end -> empty + next_offset None;
   max_chars > content -> untruncated) and the newline-boundary snap.
2. ``ContentExtractor.extract(max_chars=, offset=)`` windowing -- and the
   unchanged unlimited default (no breaking change to the Python API).
3. MCP ``web_fetch`` returns exactly ONE representation by default
   (markdown preferred), text on request, html only on explicit request.
4. ``ExtractionConfig.default_max_chars`` applies when the caller passes
   nothing; the truncation hint is present; per-page caps on the
   multi-page tools (web_search / web_research).

All offline / mock-driven. MCP tools are driven directly as functions
(FastMCP's @tool decorator returns the original fn -- same approach as
``tests/test_deep_review_entrypoints.py``) with a mocked Context/Agent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent import mcp_server
from web_agent.config import AppConfig
from web_agent.content_extractor import ContentExtractor, slice_text_window
from web_agent.models import (
    AgentResult,
    ExtractionResult,
    FetchResult,
    FetchStatus,
    ResearchResult,
    SearchResponse,
)

# ----------------------------------------------------------------------
# 1. slice_text_window math
# ----------------------------------------------------------------------


class TestSliceTextWindow:
    def test_basic_window_and_continuation(self) -> None:
        text = "x" * 100  # no newlines -> exact cuts
        w1, n1 = slice_text_window(text, offset=0, max_chars=40)
        assert (w1, n1) == ("x" * 40, 40)
        w2, n2 = slice_text_window(text, offset=n1 or 0, max_chars=40)
        assert (w2, n2) == ("x" * 40, 80)
        w3, n3 = slice_text_window(text, offset=n2 or 0, max_chars=40)
        assert (w3, n3) == ("x" * 20, None)  # final chunk: no more
        assert w1 + w2 + w3 == text  # lossless rejoin

    def test_offset_beyond_end_returns_empty_and_no_next(self) -> None:
        assert slice_text_window("abc", offset=3, max_chars=10) == ("", None)
        assert slice_text_window("abc", offset=999, max_chars=10) == ("", None)

    def test_max_chars_larger_than_content_untruncated(self) -> None:
        assert slice_text_window("abc", offset=0, max_chars=1000) == ("abc", None)

    def test_max_chars_none_unlimited(self) -> None:
        text = "line1\nline2\nline3"
        assert slice_text_window(text) == (text, None)
        assert slice_text_window(text, offset=6) == ("line2\nline3", None)

    def test_exact_fit_is_untruncated(self) -> None:
        assert slice_text_window("abcde", offset=0, max_chars=5) == ("abcde", None)

    def test_newline_snap_within_last_200(self) -> None:
        text = "a" * 50 + "\n" + "b" * 100
        window, next_off = slice_text_window(text, offset=0, max_chars=120)
        # Cut snaps back to the newline at index 50 (included in window).
        assert window == "a" * 50 + "\n"
        assert next_off == 51
        # Continuation is lossless.
        rest, n2 = slice_text_window(text, offset=next_off or 0, max_chars=10_000)
        assert window + rest == text
        assert n2 is None

    def test_no_newline_in_last_200_hard_cut(self) -> None:
        # Newline exists but EARLIER than the final-200-char region of the
        # window -> hard cut at max_chars.
        text = "a" * 10 + "\n" + "b" * 500
        window, next_off = slice_text_window(text, offset=0, max_chars=300)
        assert len(window) == 300
        assert next_off == 300

    def test_negative_offset_clamped_to_zero(self) -> None:
        assert slice_text_window("abc", offset=-5, max_chars=2) == ("ab", 2)

    def test_nonpositive_max_chars_still_progresses(self) -> None:
        window, next_off = slice_text_window("abc", offset=0, max_chars=0)
        assert window == "a" and next_off == 1  # clamped to 1: no zero-progress loop

    def test_empty_text(self) -> None:
        assert slice_text_window("", offset=0, max_chars=10) == ("", None)


# ----------------------------------------------------------------------
# 2. Extractor-level windowing (Python API)
# ----------------------------------------------------------------------


def _html_fetch_result(body_text: str) -> FetchResult:
    return FetchResult(
        url="https://e/x",
        final_url="https://e/x",
        status=FetchStatus.SUCCESS,
        html=f"<html><body><p>{body_text}</p></body></html>",
    )


class TestExtractorWindowing:
    def test_default_stays_unlimited_no_breaking_change(self) -> None:
        ext = ContentExtractor(AppConfig())
        result = ext.extract(_html_fetch_result("hello world " * 50))
        assert result.truncated is False
        assert result.total_content_chars is None
        assert result.content_offset == 0
        assert result.next_offset is None

    def test_max_chars_windows_and_stamps_metadata(self) -> None:
        ext = ContentExtractor(AppConfig())
        fr = _html_fetch_result("A" * 5000)
        full = ext.extract(fr)
        assert full.content is not None
        total = len(full.content)
        assert total >= 5000

        sliced = ext.extract(fr, max_chars=100)
        assert sliced.content is not None
        expected_window, expected_next = slice_text_window(
            full.content, offset=0, max_chars=100
        )
        assert sliced.content == expected_window
        assert sliced.content_length == len(expected_window)
        assert sliced.truncated is True
        assert sliced.total_content_chars == total
        assert sliced.content_offset == 0
        assert sliced.next_offset == expected_next

    def test_offset_continuation_matches_full_text(self) -> None:
        ext = ContentExtractor(AppConfig())
        fr = _html_fetch_result("B" * 3000)
        full = ext.extract(fr)
        assert full.content is not None
        first = ext.extract(fr, max_chars=1000)
        assert first.next_offset is not None
        second = ext.extract(fr, max_chars=1_000_000, offset=first.next_offset)
        assert second.content is not None
        assert (first.content or "") + second.content == full.content
        assert second.next_offset is None
        assert second.content_offset == first.next_offset

    def test_offset_beyond_end_empty_content(self) -> None:
        ext = ContentExtractor(AppConfig())
        fr = _html_fetch_result("C" * 500)
        full = ext.extract(fr)
        assert full.content is not None
        beyond = ext.extract(fr, max_chars=100, offset=len(full.content) + 50)
        assert beyond.content == ""
        assert beyond.next_offset is None
        assert beyond.truncated is False
        assert beyond.total_content_chars == len(full.content)


# ----------------------------------------------------------------------
# Shared MCP fixtures
# ----------------------------------------------------------------------


def _ctx_for(agent: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"agent": agent}
    return ctx


def _agent_with(config: AppConfig | None = None) -> MagicMock:
    agent = MagicMock()
    agent._config = config or AppConfig()
    return agent


def _page_result(
    url: str = "https://e/x",
    *,
    content: str | None = None,
    markdown: str | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        url=url,
        content=content,
        markdown=markdown,
        extraction_method="trafilatura",
        content_length=len(content or ""),
    )


# ----------------------------------------------------------------------
# 3. web_fetch: single representation + format selection
# ----------------------------------------------------------------------


class TestWebFetchSingleRepresentation:
    @pytest.mark.asyncio
    async def test_default_prefers_markdown_drops_content(self) -> None:
        agent = _agent_with()
        agent.fetch_and_extract = AsyncMock(
            return_value=_page_result(content="plain text", markdown="# md")
        )
        result = await mcp_server.web_fetch(_ctx_for(agent), "https://e/x")
        assert result.markdown == "# md"
        assert result.content is None  # duplication killed
        assert result.truncated is False
        assert result.truncation_hint is None

    @pytest.mark.asyncio
    async def test_default_falls_back_to_text_when_no_markdown(self) -> None:
        agent = _agent_with()
        agent.fetch_and_extract = AsyncMock(return_value=_page_result(content="plain text"))
        result = await mcp_server.web_fetch(_ctx_for(agent), "https://e/x")
        assert result.content == "plain text"
        assert result.markdown is None

    @pytest.mark.asyncio
    async def test_format_text_keeps_content_drops_markdown(self) -> None:
        agent = _agent_with()
        agent.fetch_and_extract = AsyncMock(
            return_value=_page_result(content="plain text", markdown="# md")
        )
        result = await mcp_server.web_fetch(_ctx_for(agent), "https://e/x", format="text")
        assert result.content == "plain text"
        assert result.markdown is None

    @pytest.mark.asyncio
    async def test_format_html_returns_raw_html_only_on_request(self) -> None:
        agent = _agent_with()
        html = "<html><body><form id='f'></form></body></html>"
        agent._fetcher.fetch_smart = AsyncMock(
            return_value=FetchResult(
                url="https://e/x",
                final_url="https://e/x",
                status=FetchStatus.SUCCESS,
                html=html,
            )
        )
        result = await mcp_server.web_fetch(_ctx_for(agent), "https://e/x", format="html")
        assert result.content == html
        assert result.markdown is None
        assert result.extraction_method == "html"
        # The normal extraction pipeline must NOT have run.
        assert not agent.fetch_and_extract.called

    @pytest.mark.asyncio
    async def test_format_html_failure_carries_failure_fields(self) -> None:
        agent = _agent_with()
        agent._fetcher.fetch_smart = AsyncMock(
            return_value=FetchResult(
                url="https://e/x",
                final_url="https://e/x",
                status=FetchStatus.HTTP_ERROR,
                status_code=403,
            )
        )
        result = await mcp_server.web_fetch(_ctx_for(agent), "https://e/x", format="html")
        assert result.extraction_method == "none"
        assert result.fetch_status == "http_error"
        assert result.status_code == 403
        assert result.error_message is not None and "403" in result.error_message

    @pytest.mark.asyncio
    async def test_invalid_format_raises_value_error(self) -> None:
        agent = _agent_with()
        with pytest.raises(ValueError, match="markdown"):
            await mcp_server.web_fetch(_ctx_for(agent), "https://e/x", format="xml")

    @pytest.mark.asyncio
    async def test_html_format_rejected_on_multipage_tools(self) -> None:
        agent = _agent_with()
        with pytest.raises(ValueError, match="web_fetch"):
            await mcp_server.web_search(_ctx_for(agent), "q", format="html")

    @pytest.mark.asyncio
    async def test_failure_results_pass_through_with_failure_fields(self) -> None:
        """A failed fetch shaped at the boundary keeps its v1.7.0 failure
        fields and gains no bogus truncation metadata."""
        agent = _agent_with()
        agent.fetch_and_extract = AsyncMock(
            return_value=ExtractionResult(
                url="https://e/x",
                extraction_method="none",
                fetch_status="blocked",
                error_message="robots.txt for e disallows this URL",
                failure_stage="fetch",
            )
        )
        result = await mcp_server.web_fetch(_ctx_for(agent), "https://e/x")
        assert result.fetch_status == "blocked"
        assert result.error_message == "robots.txt for e disallows this URL"
        assert result.truncated is False
        assert result.next_offset is None


# ----------------------------------------------------------------------
# 4. Default cap, hint, continuation, per-page caps
# ----------------------------------------------------------------------


class TestMcpBoundaryCaps:
    @pytest.mark.asyncio
    async def test_default_max_chars_applies_when_caller_passes_nothing(self) -> None:
        agent = _agent_with()  # AppConfig default: 40000
        agent.fetch_and_extract = AsyncMock(
            return_value=_page_result(content="x" * 50_000)
        )
        result = await mcp_server.web_fetch(_ctx_for(agent), "https://e/x")
        assert result.content is not None
        assert len(result.content) == 40_000  # no newlines -> exact cut
        assert result.truncated is True
        assert result.total_content_chars == 50_000
        assert result.next_offset == 40_000

    @pytest.mark.asyncio
    async def test_truncation_hint_present_and_actionable(self) -> None:
        agent = _agent_with()
        agent.fetch_and_extract = AsyncMock(
            return_value=_page_result(content="x" * 50_000)
        )
        result = await mcp_server.web_fetch(_ctx_for(agent), "https://e/x")
        assert result.truncation_hint is not None
        assert "truncated at 40000 of 50000 chars" in result.truncation_hint
        assert "offset=40000" in result.truncation_hint

    @pytest.mark.asyncio
    async def test_configured_default_max_chars_honored(self) -> None:
        agent = _agent_with(AppConfig(extraction={"default_max_chars": 1000}))
        agent.fetch_and_extract = AsyncMock(return_value=_page_result(content="y" * 5000))
        result = await mcp_server.web_fetch(_ctx_for(agent), "https://e/x")
        assert result.content is not None and len(result.content) == 1000
        assert result.next_offset == 1000

    @pytest.mark.asyncio
    async def test_explicit_max_chars_overrides_default(self) -> None:
        agent = _agent_with()
        agent.fetch_and_extract = AsyncMock(return_value=_page_result(content="z" * 5000))
        result = await mcp_server.web_fetch(_ctx_for(agent), "https://e/x", max_chars=500)
        assert result.content is not None and len(result.content) == 500
        assert result.truncated is True
        assert result.total_content_chars == 5000

    @pytest.mark.asyncio
    async def test_offset_continuation_on_web_fetch(self) -> None:
        full = "w" * 1500
        agent = _agent_with()
        agent.fetch_and_extract = AsyncMock(return_value=_page_result(content=full))
        result = await mcp_server.web_fetch(
            _ctx_for(agent), "https://e/x", max_chars=1000, offset=1000
        )
        assert result.content == "w" * 500
        assert result.content_offset == 1000
        assert result.next_offset is None
        assert result.truncated is False
        assert result.truncation_hint is None

    @pytest.mark.asyncio
    async def test_offset_beyond_end_returns_empty_no_next(self) -> None:
        agent = _agent_with()
        agent.fetch_and_extract = AsyncMock(return_value=_page_result(content="abc"))
        result = await mcp_server.web_fetch(_ctx_for(agent), "https://e/x", offset=10)
        assert result.content == ""
        assert result.next_offset is None
        assert result.truncated is False

    @pytest.mark.asyncio
    async def test_small_content_untruncated_no_hint(self) -> None:
        agent = _agent_with()
        agent.fetch_and_extract = AsyncMock(return_value=_page_result(content="short"))
        result = await mcp_server.web_fetch(_ctx_for(agent), "https://e/x")
        assert result.content == "short"
        assert result.truncated is False
        assert result.total_content_chars == 5
        assert result.truncation_hint is None

    @pytest.mark.asyncio
    async def test_web_search_per_page_caps_and_single_representation(self) -> None:
        agent = _agent_with()
        pages = [
            _page_result("https://a", content="a" * 50_000, markdown="m" * 60_000),
            _page_result("https://b", content="b" * 100),
        ]
        agent.search_and_extract = AsyncMock(
            return_value=AgentResult(
                query="q",
                search=SearchResponse(query="q"),
                pages=pages,
            )
        )
        result = await mcp_server.web_search(_ctx_for(agent), "q")

        big = result.pages[0]
        assert big.content is None  # markdown preferred, content dropped
        assert big.markdown is not None and len(big.markdown) == 40_000
        assert big.truncated is True
        assert big.total_content_chars == 60_000
        assert big.next_offset == 40_000
        assert big.truncation_hint is not None
        assert "web_fetch" in big.truncation_hint  # continuation affordance

        small = result.pages[1]
        assert small.content == "b" * 100
        assert small.truncated is False
        assert small.truncation_hint is None

    @pytest.mark.asyncio
    async def test_web_research_per_page_caps(self) -> None:
        agent = _agent_with(AppConfig(extraction={"default_max_chars": 2000}))
        agent.web_research = AsyncMock(
            return_value=ResearchResult(
                query="q",
                summary_pages=[
                    _page_result("https://a", content="r" * 10_000),
                    _page_result("https://b", content="s" * 50),
                ],
            )
        )
        result = await mcp_server.web_research(_ctx_for(agent), "q")
        first, second = result.summary_pages
        assert first.content is not None and len(first.content) == 2000
        assert first.truncated is True
        assert first.next_offset == 2000
        assert second.content == "s" * 50
        assert second.truncated is False

    @pytest.mark.asyncio
    async def test_web_search_best_shapes_single_result(self) -> None:
        agent = _agent_with()
        agent.search_and_open_best_result = AsyncMock(
            return_value=_page_result(content="t" * 45_000, markdown="u" * 45_000)
        )
        result = await mcp_server.web_search_best(_ctx_for(agent), "q")
        assert result.content is None
        assert result.markdown is not None and len(result.markdown) == 40_000
        assert result.truncated is True

    @pytest.mark.asyncio
    async def test_web_fill_form_shapes_and_keeps_failure_fields(self) -> None:
        agent = _agent_with()
        agent.fill_form_and_extract = AsyncMock(
            return_value=ExtractionResult(
                url="https://e/form",
                extraction_method="none",
                failure_stage="query_fill",
                error_message="query selector 'input#q' could not be filled",
            )
        )
        from web_agent.models import FormFilterSpec

        result = await mcp_server.web_fill_form_and_extract(
            _ctx_for(agent), "https://e/form", FormFilterSpec()
        )
        assert result.failure_stage == "query_fill"
        assert result.error_message is not None and "input#q" in result.error_message

    @pytest.mark.asyncio
    async def test_web_observe_caps_visible_text(self) -> None:
        agent = _agent_with(AppConfig(extraction={"default_max_chars": 1000}))
        obs = MagicMock()
        obs.model_dump = MagicMock(
            return_value={"url": "https://e/x", "visible_text": "v" * 5000}
        )
        agent.observe = AsyncMock(return_value=obs)
        payload = await mcp_server.web_observe(_ctx_for(agent), url="https://e/x")
        assert len(payload["visible_text"]) == 1000
        assert payload["visible_text_truncated"] is True
        assert payload["visible_text_total_chars"] == 5000

    @pytest.mark.asyncio
    async def test_markdown_and_content_never_both_present(self) -> None:
        """Property sweep over format values: at most one representation."""
        for fmt in (None, "markdown", "text"):
            agent = _agent_with()
            agent.fetch_and_extract = AsyncMock(
                return_value=_page_result(content="c" * 100, markdown="m" * 100)
            )
            result = await mcp_server.web_fetch(_ctx_for(agent), "https://e/x", format=fmt)
            populated = [r for r in (result.content, result.markdown) if r is not None]
            assert len(populated) == 1, f"format={fmt!r} returned {len(populated)} representations"
