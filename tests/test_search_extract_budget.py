"""Budget-enforcement regressions in ``Agent.search_and_extract``.

M1 (page budget on fan-out): the fetch fan-out (``fetch_many``) must be
sliced to the *remaining* page budget BEFORE fetching, not merely have
its results truncated AFTER every fetch already ran. Previously
``max_pages_per_call`` bounded extraction only; fetching ignored it.

M2 (char budget on the binary path): the ``extract_files=True`` binary
loop charged ``budget.add_page()`` but never ``budget.add_chars(...)``,
so a large PDF/XLSX could bypass ``max_chars_per_call``. The HTML branch
always charged chars; the binary branch must match.

Both tests drive ``search_and_extract`` against an ``Agent`` whose
``_search`` / ``_fetcher`` / ``_extractor`` are mocked so no real
browser or network is touched. ``Agent(cfg)`` constructs cleanly without
a live browser (the browser stack only starts inside ``__aenter__`` /
on demand), matching the harness used in tests/test_v1614_security.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent import Agent
from web_agent.config import AppConfig, SafetyConfig
from web_agent.models import (
    ExtractionResult,
    FetchResult,
    FetchStatus,
    SearchResponse,
    SearchResultItem,
)

# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _search_item(position: int, url: str) -> SearchResultItem:
    return SearchResultItem(
        position=position,
        title=f"result {position}",
        url=url,
        provider="ddgs",
    )


def _agent_with_mocks(cfg: AppConfig) -> Agent:
    """Build an Agent and stub the pipeline collaborators.

    ``_search`` / ``_fetcher`` / ``_extractor`` are replaced post-init so
    the test exercises the real ``search_and_extract`` orchestration
    (budget slicing, routing, message bag) without any I/O.
    """
    agent = Agent(cfg)
    agent._search = MagicMock()  # type: ignore[attr-defined]
    agent._fetcher = MagicMock()  # type: ignore[attr-defined]
    agent._extractor = MagicMock()  # type: ignore[attr-defined]
    # extract_async runs extract off the event loop in the real
    # ContentExtractor; mirror that delegation so tests that configure
    # ``_extractor.extract`` per-case keep working (resolved at call time).
    agent._extractor.extract_async = AsyncMock(  # type: ignore[attr-defined]
        side_effect=lambda fr, **kw: agent._extractor.extract(fr, **kw)
    )
    return agent


# ----------------------------------------------------------------------
# M1: fan-out respects the remaining page budget
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_many_sliced_to_page_budget() -> None:
    """With ``max_pages_per_call=2`` and a search returning 5 HTML page
    items, ``fetch_many`` must be invoked with at most 2 URLs, and a
    truncation warning must be surfaced.
    """
    cfg = AppConfig(
        safety=SafetyConfig(
            max_pages_per_call=2,
            # extensionless URLs -> page_items deterministically (no probe)
            probe_binary_urls=False,
            block_private_ips=False,
        )
    )
    agent = _agent_with_mocks(cfg)

    # 5 extensionless (HTML-routed) results.
    results = [
        _search_item(i + 1, f"https://site{i}.example.com/article")
        for i in range(5)
    ]
    agent._search.search = AsyncMock(  # type: ignore[attr-defined]
        return_value=SearchResponse(
            query="q", total_results=len(results), results=results
        )
    )

    # fetch_many records its call args; return empty so the result loop is
    # a no-op (we only care about how many URLs were *fetched*).
    fetch_many = AsyncMock(return_value=[])
    agent._fetcher.fetch_many = fetch_many  # type: ignore[attr-defined]

    result = await agent.search_and_extract("q", max_results=5)

    fetch_many.assert_awaited_once()
    fetched_urls = fetch_many.call_args.args[0]
    assert len(fetched_urls) == 2, (
        f"M1 regression: fan-out ignored the page budget; fetched "
        f"{len(fetched_urls)} urls, expected <= 2"
    )

    warn_codes = {w.code for w in result.structured_warnings}
    assert "page_budget_truncated" in warn_codes, (
        "M1: dropping page items must emit an observable truncation warning; "
        f"got warning codes {warn_codes}"
    )


@pytest.mark.asyncio
async def test_fetch_many_not_sliced_when_budget_exceeds_results() -> None:
    """Common case: when ``max_pages_per_call`` (default 50) exceeds the
    search-result count, the slice is a no-op -- all items are fetched
    and NO truncation warning is emitted.
    """
    cfg = AppConfig(
        safety=SafetyConfig(probe_binary_urls=False, block_private_ips=False)
    )
    agent = _agent_with_mocks(cfg)

    results = [
        _search_item(i + 1, f"https://site{i}.example.com/article")
        for i in range(3)
    ]
    agent._search.search = AsyncMock(  # type: ignore[attr-defined]
        return_value=SearchResponse(
            query="q", total_results=len(results), results=results
        )
    )
    fetch_many = AsyncMock(return_value=[])
    agent._fetcher.fetch_many = fetch_many  # type: ignore[attr-defined]

    result = await agent.search_and_extract("q", max_results=3)

    fetch_many.assert_awaited_once()
    assert len(fetch_many.call_args.args[0]) == 3
    warn_codes = {w.code for w in result.structured_warnings}
    assert "page_budget_truncated" not in warn_codes


# ----------------------------------------------------------------------
# M2: binary extraction charges the char budget
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_binary_extraction_charges_char_budget() -> None:
    """With ``extract_files=True`` and a binary extraction whose
    ``content_length`` exceeds ``max_chars_per_call``, the char budget
    must be enforced (a ``budget_exceeded`` error recorded / loop breaks)
    -- i.e. ``add_chars`` is now charged on the binary path.
    """
    cfg = AppConfig(
        safety=SafetyConfig(
            max_chars_per_call=100,  # tiny -> a single big PDF blows it
            max_pages_per_call=50,  # not the constraint under test
            block_private_ips=False,
        )
    )
    agent = _agent_with_mocks(cfg)

    # Two PDF results so we can confirm the loop *breaks* after the first
    # blows the char budget (the second must NOT be fetched).
    results = [
        _search_item(1, "https://site0.example.com/big.pdf"),
        _search_item(2, "https://site1.example.com/next.pdf"),
    ]
    agent._search.search = AsyncMock(  # type: ignore[attr-defined]
        return_value=SearchResponse(
            query="q", total_results=len(results), results=results
        )
    )

    fetch_binary = AsyncMock(
        return_value=FetchResult(
            url="https://site0.example.com/big.pdf",
            final_url="https://site0.example.com/big.pdf",
            status=FetchStatus.SUCCESS,
            binary=b"%PDF-1.7 ...",
        )
    )
    agent._fetcher.fetch_binary = fetch_binary  # type: ignore[attr-defined]
    # No HTML page items, so fetch_many gets an empty list.
    fetch_many = AsyncMock(return_value=[])
    agent._fetcher.fetch_many = fetch_many  # type: ignore[attr-defined]

    # Extractor returns content far larger than max_chars_per_call.
    agent._extractor.extract = MagicMock(  # type: ignore[attr-defined]
        return_value=ExtractionResult(
            url="https://site0.example.com/big.pdf",
            content="x" * 5000,
            extraction_method="pdf",
            content_length=5000,
        )
    )

    result = await agent.search_and_extract(
        "q", max_results=2, extract_files=True
    )

    err_codes = {e.code for e in result.structured_errors}
    assert "budget_exceeded" in err_codes, (
        "M2 regression: binary extraction did not charge the char budget; "
        f"expected a 'budget_exceeded' error, got {err_codes}"
    )
    # The loop must break: the FIRST pdf overflows chars, so the second
    # pdf is never fetched.
    assert fetch_binary.await_count == 1, (
        "M2: char-budget overflow on the binary path must break the loop "
        f"(second file should not be fetched); await_count={fetch_binary.await_count}"
    )


@pytest.mark.asyncio
async def test_char_exhaustion_skips_html_page_fanout() -> None:
    """Deep-review fix: when the extract_files binary loop exhausts the CHAR
    budget, the subsequent HTML page fan-out is skipped -- the M1 slice now
    consults the char dimension, so no fetch_many network I/O happens after
    chars are spent. Previously only the PAGE dimension was checked, so an
    HTML page was still fetched (and one more page extracted) past the budget.
    """
    cfg = AppConfig(
        safety=SafetyConfig(
            max_chars_per_call=100,  # the PDF below blows this
            max_pages_per_call=50,  # NOT the constraint under test
            block_private_ips=False,
        )
    )
    agent = _agent_with_mocks(cfg)

    # A binary (PDF) result that exhausts the char budget, plus an HTML page.
    results = [
        _search_item(1, "https://s0.example.com/big.pdf"),
        _search_item(2, "https://s1.example.com/page.html"),  # .html -> page item
    ]
    agent._search.search = AsyncMock(  # type: ignore[attr-defined]
        return_value=SearchResponse(query="q", total_results=2, results=results)
    )
    agent._fetcher.fetch_binary = AsyncMock(  # type: ignore[attr-defined]
        return_value=FetchResult(
            url="https://s0.example.com/big.pdf",
            final_url="https://s0.example.com/big.pdf",
            status=FetchStatus.SUCCESS,
            binary=b"%PDF",
        )
    )
    fetch_many = AsyncMock(return_value=[])
    agent._fetcher.fetch_many = fetch_many  # type: ignore[attr-defined]
    agent._extractor.extract = MagicMock(  # type: ignore[attr-defined]
        return_value=ExtractionResult(
            url="https://s0.example.com/big.pdf",
            content="x" * 5000,
            extraction_method="pdf",
            content_length=5000,
        )
    )

    await agent.search_and_extract("q", max_results=2, extract_files=True)

    # The HTML page fan-out is clamped to empty once chars are exhausted.
    assert fetch_many.await_count == 1
    fanned_out = fetch_many.await_args.args[0]
    assert fanned_out == [], (
        "char-exhausted call must not fan out HTML pages over the network; "
        f"got {fanned_out}"
    )
