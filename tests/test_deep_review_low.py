"""Deep-review (post-v1.6.16) low-severity regression tests: skills + extraction.

  * The sec_gov_filing_search builtin skill sanitizes its query terms (so a
    prompt-injection-steered ``company`` can't escape the ``site:sec.gov``
    fence) AND post-filters results to sec.gov hosts (mirrors ec_europa).
  * ``ContentExtractor.extract_async`` runs the synchronous CPU-heavy extract
    off the event loop in a worker thread, producing the same result.
"""

from __future__ import annotations

import threading

import pytest
from web_agent.config import AppConfig
from web_agent.content_extractor import ContentExtractor
from web_agent.models import ExtractionResult, FetchResult, FetchStatus


class _Result:
    def __init__(self, pages: list[ExtractionResult]) -> None:
        self.pages = pages


class _FakeAgent:
    def __init__(self, pages: list[ExtractionResult]) -> None:
        self._pages = pages
        self.last_query: str | None = None

    async def search_and_extract(self, query: str, max_results: int = 5) -> _Result:
        self.last_query = query
        return _Result(self._pages)


class TestSecGovSkillHardening:
    @pytest.mark.asyncio
    async def test_injection_sanitized_and_host_filtered(self) -> None:
        from web_agent.builtin_skills.sec_gov_filing_search import run

        agent = _FakeAgent(
            [
                ExtractionResult(url="https://evil.com/x", content="evil", extraction_method="raw"),
                ExtractionResult(
                    url="https://www.sec.gov/Archives/edgar/data/123/f.htm",
                    content="real filing",
                    extraction_method="raw",
                ),
            ]
        )
        out = await run(
            agent,  # type: ignore[arg-type]
            "https://www.sec.gov",
            {"company": '" OR site:evil.com', "form_type": "10-K"},
        )
        # The injected search operators are stripped from the composed query.
        assert "site:evil.com" not in (agent.last_query or "")
        assert '"' not in (agent.last_query or "")
        assert " OR " not in (agent.last_query or "")
        # Only the sec.gov result is surfaced; the evil.com page is filtered out.
        assert "sec.gov" in out["filing_url"]
        assert out["extracted_text"] == "real filing"

    @pytest.mark.asyncio
    async def test_all_off_domain_results_yield_empty(self) -> None:
        from web_agent.builtin_skills.sec_gov_filing_search import run

        agent = _FakeAgent(
            [ExtractionResult(url="https://evil.com/x", content="evil", extraction_method="raw")]
        )
        out = await run(agent, "https://www.sec.gov", {"company": "Apple"})  # type: ignore[arg-type]
        assert out["filing_url"] == ""
        assert out["extracted_text"] == ""

    @pytest.mark.asyncio
    async def test_subdomain_spoof_rejected(self) -> None:
        from web_agent.builtin_skills.sec_gov_filing_search import run

        agent = _FakeAgent(
            [ExtractionResult(url="https://sec.gov.evil.com/x", content="x", extraction_method="raw")]
        )
        out = await run(agent, "https://www.sec.gov", {"company": "Apple"})  # type: ignore[arg-type]
        assert out["filing_url"] == ""  # label-boundary match rejects the spoof


class TestExtractAsyncOffload:
    _HTML = "<html><body><article><p>hello world this is the body content</p></article></body></html>"

    @pytest.mark.asyncio
    async def test_runs_off_the_event_loop_thread(self) -> None:
        ext = ContentExtractor(AppConfig())
        main_ident = threading.get_ident()
        seen: dict[str, int] = {}
        real_extract = ext.extract

        def _spy(*a: object, **k: object) -> ExtractionResult:
            seen["thread"] = threading.get_ident()
            return real_extract(*a, **k)  # type: ignore[arg-type]

        ext.extract = _spy  # type: ignore[method-assign]
        fr = FetchResult(
            url="https://x.example/p",
            final_url="https://x.example/p",
            status=FetchStatus.SUCCESS,
            html=self._HTML,
        )
        res = await ext.extract_async(fr)
        assert seen["thread"] != main_ident, "extract must run off the event-loop thread"
        assert res.extraction_method  # produced a non-empty result

    @pytest.mark.asyncio
    async def test_async_result_matches_sync(self) -> None:
        ext = ContentExtractor(AppConfig())
        fr = FetchResult(
            url="https://x.example/p",
            final_url="https://x.example/p",
            status=FetchStatus.SUCCESS,
            html=self._HTML,
        )
        sync_res = ext.extract(fr)
        async_res = await ext.extract_async(fr)
        assert async_res.extraction_method == sync_res.extraction_method
        assert async_res.content == sync_res.content
