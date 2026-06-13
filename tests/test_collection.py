"""v1.7.0 Wave 3B: scroll-to-exhaustion + paginated collection.

Covers the two new capabilities, fully offline (AsyncMock / MagicMock --
no Playwright launch, no network):

1. ``BrowserActions.scroll_to_bottom`` -- scroll-to-exhaustion assembly.
   - stops when scrollHeight stabilizes for ``stable_rounds`` rounds
     (height sequence like [1000, 2000, 3000, 3000, 3000]);
   - stops at ``max_scrolls`` when height keeps growing;
   - honors the 1000 clamp;
   - reports scrolls_used + reached_bottom.

2. ``Recipes.collect_across_pages`` -- paginated collection.
   - next_link: walks N pages following a mocked next control, concatenates
     content, stops at no-next; stops at max_pages; CYCLE GUARD (a next that
     loops back stops with stopped_reason='cycle' and does not double-count);
     a blocked/failed page mid-walk is recorded and stops cleanly.
   - budget: max_pages and the per-call budget both bound the walk.
   - dedup: a repeated URL / repeated content is not collected twice.
   - page_param: increments ?page= until an empty/duplicate page.
   - CollectionResult schema round-trips.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.config import AppConfig
from web_agent.models import (
    ActionStatus,
    CollectedPage,
    CollectionResult,
    ExtractionResult,
    FetchResult,
    FetchStatus,
    InjectionReport,
)
from web_agent.recipes import Recipes

# ----------------------------------------------------------------------
# Shared builders
# ----------------------------------------------------------------------


def _fetch_result(url: str, html: str = "<html></html>", *, status: FetchStatus = FetchStatus.SUCCESS) -> FetchResult:
    return FetchResult(url=url, final_url=url, status=status, html=html)


def _extraction(url: str, content: str, method: str = "raw") -> ExtractionResult:
    return ExtractionResult(
        url=url,
        content=content,
        content_length=len(content),
        extraction_method=method if content else "none",
    )


def _offline_config(**safety: object) -> AppConfig:
    """AppConfig with the private-IP guard OFF so the offline tests'
    unresolvable fake hosts (``site``, ``s``, ``api``) don't each pay a
    real ~1s ``getaddrinfo`` timeout. The deny/allow-list domain gating the
    cycle/blocked tests rely on still works without DNS, and ``fetch`` is
    mocked so the real SSRF re-gate inside ``fetch`` never runs here.
    """
    merged = {"block_private_ips": False, **safety}
    return AppConfig(safety=merged)


def _make_recipes(
    *,
    fetch_side: object = None,
    extract_side: object = None,
    sessions: object = None,
    actions: object = None,
    config: Optional[AppConfig] = None,
) -> Recipes:
    """Build a Recipes whose fetcher/extractor are AsyncMocks.

    ``fetch_side`` -> WebFetcher.fetch side_effect (callable or list).
    ``extract_side`` -> ContentExtractor.extract_async side_effect.
    """
    cfg = config or _offline_config()
    fetcher = MagicMock()
    fetcher.fetch = AsyncMock(side_effect=fetch_side)
    extractor = MagicMock()
    extractor.extract_async = AsyncMock(side_effect=extract_side)
    return Recipes(
        search=MagicMock(),
        fetcher=fetcher,
        extractor=extractor,
        downloader=MagicMock(),
        config=cfg,
        browser_manager=MagicMock(),
        sessions=sessions,
        actions=actions,
    )


# ======================================================================
# Capability 1: scroll_to_bottom (scroll-to-exhaustion)
# ======================================================================


def _make_scroll_page(heights: list[int], *, url: str = "https://example.com/feed") -> MagicMock:
    """Build a fake Page whose ``evaluate`` returns a scrollHeight sequence.

    The action calls page.evaluate(scrollHeight) once up-front, then per
    round: scrollTo (returns None), then scrollHeight again. We feed the
    height reads from ``heights`` in order; the scrollTo calls (which the
    JS string contains 'scrollTo') return None.
    """
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    type(page).url = property(lambda _self: url)

    seq = list(heights)

    async def _evaluate(expr: str, *args: object, **kwargs: object) -> object:
        if "scrollTo" in expr:
            return None
        # scrollHeight read
        if seq:
            return seq.pop(0)
        return heights[-1] if heights else 0

    page.evaluate = AsyncMock(side_effect=_evaluate)
    return page


def _actions_with_page(page: MagicMock, cfg: Optional[AppConfig] = None) -> object:
    """Build a BrowserActions whose session tab resolves to ``page``."""
    from web_agent.browser_actions import BrowserActions

    cfg = cfg or AppConfig()
    sessions = MagicMock()
    tab_mgr = MagicMock()
    tab_mgr.get_or_current = MagicMock(return_value=page)
    sessions.get_tab_manager = MagicMock(return_value=tab_mgr)
    sessions.touch = MagicMock()
    return BrowserActions(MagicMock(), cfg, sessions=sessions)


@pytest.mark.asyncio
async def test_scroll_to_bottom_stops_on_stable_height() -> None:
    """Height [1000, 2000, 3000, 3000, 3000] with stable_rounds=2 stops
    after the height repeats twice. Up-front read = 1000; rounds read
    2000, 3000, 3000, 3000. Two consecutive unchanged (3000==3000 twice)
    trip the stable bottom.
    """
    page = _make_scroll_page([1000, 2000, 3000, 3000, 3000])
    actions = _actions_with_page(page)

    res = await actions.scroll_to_bottom(
        session_id="s1", settle_ms=0, stable_rounds=2, max_scrolls=50
    )

    assert res.status == ActionStatus.SUCCESS
    assert res.data is not None
    assert res.data["reached_bottom"] is True
    # rounds: r1 height=2000 (grew), r2=3000 (grew), r3=3000 (stable=1),
    # r4=3000 (stable=2 -> break). 4 scrolls used.
    assert res.data["scrolls_used"] == 4
    assert res.data["final_height"] == 3000


@pytest.mark.asyncio
async def test_scroll_to_bottom_hits_max_scrolls_when_growing() -> None:
    """Height keeps growing every round -> never stabilizes -> stops at
    max_scrolls with reached_bottom=False.
    """
    # Up-front 100, then strictly-increasing reads for every round.
    heights = [100] + [200 * i for i in range(1, 20)]
    page = _make_scroll_page(heights)
    actions = _actions_with_page(page)

    res = await actions.scroll_to_bottom(
        session_id="s1", settle_ms=0, stable_rounds=2, max_scrolls=5
    )

    assert res.status == ActionStatus.SUCCESS
    assert res.data is not None
    assert res.data["scrolls_used"] == 5
    assert res.data["reached_bottom"] is False


@pytest.mark.asyncio
async def test_scroll_to_bottom_clamps_max_scrolls() -> None:
    """A hostile max_scrolls (10**8) is clamped to 1000. We assert the
    clamp by checking the loop cannot exceed 1000 even though the page
    grows forever -- feed an always-growing height and a huge cap, then
    assert scrolls_used == 1000.
    """
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    type(page).url = property(lambda _self: "https://example.com/feed")

    counter = {"n": 0}

    async def _evaluate(expr: str, *args: object, **kwargs: object) -> object:
        if "scrollTo" in expr:
            return None
        counter["n"] += 1
        return counter["n"] * 10  # strictly grows forever

    page.evaluate = AsyncMock(side_effect=_evaluate)
    actions = _actions_with_page(page)

    res = await actions.scroll_to_bottom(
        session_id="s1", settle_ms=0, stable_rounds=2, max_scrolls=10**8
    )
    assert res.data is not None
    assert res.data["scrolls_used"] == 1000
    assert res.data["reached_bottom"] is False


@pytest.mark.asyncio
async def test_scroll_to_bottom_blocks_disallowed_domain() -> None:
    """The session tab parked on a denied host must be refused before any
    scroll/read (deny-list bypass guard).
    """
    cfg = _offline_config(denied_domains=["evil.com"])
    page = _make_scroll_page([1000, 1000, 1000], url="https://evil.com/feed")
    actions = _actions_with_page(page, cfg)

    res = await actions.scroll_to_bottom(session_id="s1", settle_ms=0)
    assert res.status == ActionStatus.FAILED
    assert "disallowed domain" in (res.error_message or "")


@pytest.mark.asyncio
async def test_scroll_to_bottom_honors_config_defaults() -> None:
    """When stable_rounds / settle_ms / max_scrolls are None, the action
    reads automation.scroll_stable_rounds (default 2) etc. A height that
    stabilizes after 2 repeats with default config stops cleanly.
    """
    page = _make_scroll_page([500, 900, 900, 900])
    actions = _actions_with_page(page)  # default AppConfig: stable_rounds=2

    res = await actions.scroll_to_bottom(session_id="s1", settle_ms=0)
    assert res.data is not None
    assert res.data["reached_bottom"] is True
    # up-front 500; r1=900 (grew), r2=900 (stable=1), r3=900 (stable=2 -> break)
    assert res.data["scrolls_used"] == 3


# ======================================================================
# Capability 2: collect_across_pages -- next_link strategy
# ======================================================================


def _page_html(body: str, *, next_href: Optional[str] = None) -> str:
    nxt = f'<a rel="next" href="{next_href}">Next</a>' if next_href else ""
    return f"<html><body><main>{body}</main>{nxt}</body></html>"


@pytest.mark.asyncio
async def test_next_link_walks_pages_and_concatenates() -> None:
    """Walk page1 -> page2 -> page3 via rel=next; no next on page3 stops
    the walk. Content from all three is collected in order.
    """
    htmls = {
        "https://site/list?p=1": _page_html("AAA", next_href="https://site/list?p=2"),
        "https://site/list?p=2": _page_html("BBB", next_href="https://site/list?p=3"),
        "https://site/list?p=3": _page_html("CCC"),  # no next
    }
    contents = {
        "https://site/list?p=1": "AAA",
        "https://site/list?p=2": "BBB",
        "https://site/list?p=3": "CCC",
    }

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, htmls[url])

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction(fr.url, contents[fr.url])

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)

    result = await recipes.collect_across_pages(
        "https://site/list?p=1", strategy="next_link", max_pages=10
    )

    assert isinstance(result, CollectionResult)
    assert result.pages_collected == 3
    assert [p.content for p in result.pages] == ["AAA", "BBB", "CCC"]
    assert result.stopped_reason == "no_next"
    assert result.total_content_length == 9


@pytest.mark.asyncio
async def test_next_link_stops_at_max_pages() -> None:
    """An infinite chain of rel=next links is bounded by max_pages."""

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        # Every page links to a fresh next page forever.
        n = int(url.rsplit("=", 1)[-1])
        return _fetch_result(url, _page_html(f"P{n}", next_href=f"https://site/p?n={n + 1}"))

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        n = int(fr.url.rsplit("=", 1)[-1])
        return _extraction(fr.url, f"P{n}")

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)

    result = await recipes.collect_across_pages(
        "https://site/p?n=1", strategy="next_link", max_pages=3
    )
    assert result.pages_collected == 3
    assert result.stopped_reason == "max_pages"


@pytest.mark.asyncio
async def test_next_link_cycle_guard_no_double_count() -> None:
    """A next link on page2 that points BACK to page1 must stop with
    stopped_reason='cycle' and must NOT collect page1 a second time.
    """
    htmls = {
        "https://site/a": _page_html("PAGE-A", next_href="https://site/b"),
        # page b loops back to a (already visited)
        "https://site/b": _page_html("PAGE-B", next_href="https://site/a"),
    }
    contents = {"https://site/a": "PAGE-A", "https://site/b": "PAGE-B"}

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, htmls[url])

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction(fr.url, contents[fr.url])

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)

    result = await recipes.collect_across_pages(
        "https://site/a", strategy="next_link", max_pages=10
    )
    assert result.stopped_reason == "cycle"
    # a and b each once -- the loop back to 'a' is refused.
    urls = [p.url for p in result.pages]
    assert urls == ["https://site/a", "https://site/b"]
    assert urls.count("https://site/a") == 1


@pytest.mark.asyncio
async def test_next_link_blocked_page_midwalk_recorded_and_stops() -> None:
    """A page that fails/blocks mid-walk is recorded in diagnostics and the
    walk stops cleanly (no crash).
    """
    htmls = {
        "https://site/1": _page_html("ONE", next_href="https://site/2"),
    }
    contents = {"https://site/1": "ONE"}

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        if url == "https://site/2":
            return _fetch_result(url, "", status=FetchStatus.BLOCKED)
        return _fetch_result(url, htmls[url])

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction(fr.url, contents.get(fr.url, ""))

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)

    result = await recipes.collect_across_pages(
        "https://site/1", strategy="next_link", max_pages=10
    )
    assert result.pages_collected == 1
    assert result.pages[0].content == "ONE"
    assert result.stopped_reason == "blocked"
    # The blocked page-2 fetch attempt is captured in diagnostics.
    assert any(d.url == "https://site/2" and d.status == FetchStatus.BLOCKED for d in result.diagnostics)
    assert any("Failed to fetch" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_next_link_text_match_and_relative_href() -> None:
    """No rel=next; the control is an <a> with text 'Older posts' and a
    RELATIVE href. The recipe must match via the vocabulary and resolve
    the relative href against the page URL.
    """
    page1 = (
        "<html><body><main>FIRST</main>"
        '<a href="/list?page=2">Older posts</a></body></html>'
    )
    page2 = "<html><body><main>SECOND</main></body></html>"  # no next

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, page1 if url.endswith("page=1") else page2)

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction(fr.url, "FIRST" if fr.url.endswith("page=1") else "SECOND")

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)

    result = await recipes.collect_across_pages(
        "https://blog.example/list?page=1", strategy="next_link", max_pages=10
    )
    assert [p.url for p in result.pages] == [
        "https://blog.example/list?page=1",
        "https://blog.example/list?page=2",
    ]
    assert result.stopped_reason == "no_next"


# ======================================================================
# collect_across_pages -- budget bounding
# ======================================================================


@pytest.mark.asyncio
async def test_budget_pages_bounds_walk() -> None:
    """The per-call page budget (SafetyConfig.max_pages_per_call) stops the
    walk independently of max_pages.
    """
    cfg = _offline_config(max_pages_per_call=2)

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        n = int(url.rsplit("=", 1)[-1])
        return _fetch_result(url, _page_html(f"P{n}", next_href=f"https://s/p?n={n + 1}"))

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        n = int(fr.url.rsplit("=", 1)[-1])
        return _extraction(fr.url, f"P{n}")

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract, config=cfg)

    result = await recipes.collect_across_pages(
        "https://s/p?n=1", strategy="next_link", max_pages=50
    )
    # max_pages clamps to pagination_max_pages (10), but the page BUDGET (2)
    # bites first.
    assert result.pages_collected == 2
    assert result.stopped_reason == "budget"
    assert any("budget" in e.lower() for e in result.errors)


@pytest.mark.asyncio
async def test_max_pages_clamped_to_config_ceiling() -> None:
    """A caller asking for max_pages=999 is clamped to
    automation.pagination_max_pages (default 10).
    """

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        n = int(url.rsplit("=", 1)[-1])
        return _fetch_result(url, _page_html(f"P{n}", next_href=f"https://s/p?n={n + 1}"))

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        n = int(fr.url.rsplit("=", 1)[-1])
        return _extraction(fr.url, f"P{n}")

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)
    result = await recipes.collect_across_pages(
        "https://s/p?n=1", strategy="next_link", max_pages=999
    )
    assert result.pages_collected == 10  # default pagination_max_pages
    assert result.stopped_reason == "max_pages"


# ======================================================================
# collect_across_pages -- dedup
# ======================================================================


@pytest.mark.asyncio
async def test_dedup_identical_content_not_collected_twice() -> None:
    """Two distinct URLs that render IDENTICAL content: the second is a
    duplicate and is not collected (cycle stop).
    """
    htmls = {
        "https://s/1": _page_html("SAME", next_href="https://s/2"),
        "https://s/2": _page_html("SAME-different-markup", next_href="https://s/3"),
        "https://s/3": _page_html("END"),
    }

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, htmls[url])

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        # page 1 and 2 yield the SAME extracted content -> dup.
        content = "DUPLICATE-BODY" if fr.url in ("https://s/1", "https://s/2") else "END"
        return _extraction(fr.url, content)

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)
    result = await recipes.collect_across_pages(
        "https://s/1", strategy="next_link", max_pages=10
    )
    # page 1 collected; page 2 duplicate content -> stop with cycle, not collected.
    assert [p.url for p in result.pages] == ["https://s/1"]
    assert result.stopped_reason == "cycle"


# ======================================================================
# collect_across_pages -- page_param strategy
# ======================================================================


@pytest.mark.asyncio
async def test_page_param_increments_until_empty() -> None:
    """?page=1 -> ?page=2 -> ?page=3 (empty) stops with 'empty_page'."""
    bodies = {
        "https://api/list?page=1": "rows1",
        "https://api/list?page=2": "rows2",
        "https://api/list?page=3": "",  # empty -> end
    }

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, _page_html(bodies.get(url, "")))

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction(fr.url, bodies.get(fr.url, ""))

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)
    result = await recipes.collect_across_pages(
        "https://api/list?page=1", strategy="page_param", max_pages=10
    )
    assert [p.url for p in result.pages] == [
        "https://api/list?page=1",
        "https://api/list?page=2",
    ]
    assert result.stopped_reason == "empty_page"


@pytest.mark.asyncio
async def test_page_param_no_param_stops_after_one() -> None:
    """A start URL with no ?page=/&p= param collects the one page then
    stops with 'no_next' (nothing to increment).
    """

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, _page_html("only"))

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction(fr.url, "only")

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)
    result = await recipes.collect_across_pages(
        "https://api/list", strategy="page_param", max_pages=10
    )
    assert result.pages_collected == 1
    assert result.stopped_reason == "no_next"


# ======================================================================
# collect_across_pages -- guards / schema
# ======================================================================


@pytest.mark.asyncio
async def test_blocked_start_url_returns_blocked() -> None:
    cfg = _offline_config(denied_domains=["evil.com"])
    recipes = _make_recipes(config=cfg)
    result = await recipes.collect_across_pages(
        "https://evil.com/list", strategy="next_link"
    )
    assert result.pages_collected == 0
    assert result.stopped_reason == "blocked"
    assert any(d.status == FetchStatus.BLOCKED for d in result.diagnostics)


@pytest.mark.asyncio
async def test_unknown_strategy_errors() -> None:
    recipes = _make_recipes()
    result = await recipes.collect_across_pages(
        "https://site/x", strategy="bogus"
    )
    assert result.stopped_reason == "error"
    assert any("Unknown collection strategy" in e for e in result.errors)


@pytest.mark.asyncio
async def test_scroll_strategy_requires_actions() -> None:
    """strategy='scroll' without an injected BrowserActions reports a clean
    error (does not crash).
    """
    sessions = MagicMock()
    recipes = _make_recipes(sessions=sessions, actions=None)
    result = await recipes.collect_across_pages(
        "https://site/feed", strategy="scroll", session_id="s1"
    )
    assert result.stopped_reason == "error"
    assert any("BrowserActions" in e for e in result.errors)


@pytest.mark.asyncio
async def test_scroll_strategy_requires_session() -> None:
    recipes = _make_recipes(actions=MagicMock())
    result = await recipes.collect_across_pages(
        "https://site/feed", strategy="scroll", session_id=None
    )
    assert result.stopped_reason == "error"
    assert any("session_id" in e for e in result.errors)


def test_collection_result_schema_round_trips() -> None:
    """CollectionResult + CollectedPage round-trip through JSON unchanged."""
    cr = CollectionResult(
        start_url="https://x/list",
        strategy="next_link",
        pages=[
            CollectedPage(
                url="https://x/list",
                title="Page 1",
                content="hello",
                content_length=5,
                extraction_method="raw",
            )
        ],
        pages_collected=1,
        total_content_length=5,
        stopped_reason="no_next",
        warnings=["one warning"],
        correlation_id="cid-123",
    )
    dumped = cr.model_dump_json()
    restored = CollectionResult.model_validate_json(dumped)
    assert restored == cr
    assert restored.pages[0].content == "hello"
    assert restored.stopped_reason == "no_next"


def test_collected_page_defaults_backward_compat() -> None:
    """A minimal CollectedPage (only url) fills sane defaults -- guards the
    additive contract.
    """
    p = CollectedPage(url="https://x")
    assert p.content == ""
    assert p.content_length == 0
    assert p.extraction_method == "none"
    assert p.final_url is None
    # v1.7.0 additive defaults (B3/B4)
    assert p.injection is None
    assert p.blocked_reason is None


# ======================================================================
# B3: collection carries the per-page injection report + a rollup
# ======================================================================


def _extraction_with_injection(
    url: str, content: str, *, risk: str, method: str = "raw"
) -> ExtractionResult:
    """An ExtractionResult that carries an advisory InjectionReport, mirroring
    what ContentExtractor stamps when detect_prompt_injection is on.
    """
    return ExtractionResult(
        url=url,
        content=content,
        content_length=len(content),
        extraction_method=method if content else "none",
        injection=InjectionReport(
            risk=risk,  # type: ignore[arg-type]
            indicators=["..."] if risk != "none" else [],
            score={"none": 0.0, "low": 1.0, "medium": 3.0, "high": 6.0}[risk],
        ),
    )


@pytest.mark.asyncio
async def test_b3_each_page_carries_injection_report() -> None:
    """Every CollectedPage carries the per-page injection report (was dropped
    pre-fix) and the CollectionResult rolls the highest risk up.
    """
    htmls = {
        "https://site/list?p=1": _page_html("AAA", next_href="https://site/list?p=2"),
        "https://site/list?p=2": _page_html("BBB", next_href="https://site/list?p=3"),
        "https://site/list?p=3": _page_html("CCC"),  # no next
    }
    risks = {
        "https://site/list?p=1": "none",
        "https://site/list?p=2": "high",  # the worst page mid-walk
        "https://site/list?p=3": "low",
    }
    contents = {
        "https://site/list?p=1": "AAA",
        "https://site/list?p=2": "BBB",
        "https://site/list?p=3": "CCC",
    }

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, htmls[url])

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction_with_injection(fr.url, contents[fr.url], risk=risks[fr.url])

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)
    result = await recipes.collect_across_pages(
        "https://site/list?p=1", strategy="next_link", max_pages=10
    )

    assert result.pages_collected == 3
    # Each page kept its report (B3: was discarded before the fix).
    assert [p.injection.risk for p in result.pages if p.injection] == ["none", "high", "low"]
    # Rollup reflects the HIGHEST risk across pages, ordered none<low<medium<high.
    assert result.max_injection_risk == "high"
    # 'high' + 'low' score above none; 'none' does not.
    assert result.pages_with_injection == 2


@pytest.mark.asyncio
async def test_b3_clean_walk_rolls_up_to_none() -> None:
    """A walk where every page scans clean -> max_injection_risk='none',
    pages_with_injection=0 (distinct from None = detection disabled).
    """
    htmls = {
        "https://s/a": _page_html("A", next_href="https://s/b"),
        "https://s/b": _page_html("B"),
    }
    contents = {"https://s/a": "A", "https://s/b": "B"}

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, htmls[url])

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction_with_injection(fr.url, contents[fr.url], risk="none")

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)
    result = await recipes.collect_across_pages("https://s/a", strategy="next_link")

    assert result.pages_collected == 2
    assert result.max_injection_risk == "none"
    assert result.pages_with_injection == 0


@pytest.mark.asyncio
async def test_b3_detection_disabled_rolls_up_to_none_sentinel() -> None:
    """When pages carry NO injection report (detection disabled), the rollup
    is None (not 'none') and pages_with_injection stays 0.
    """

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, _page_html("only"))

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction(fr.url, "only")  # no injection report attached

    recipes = _make_recipes(fetch_side=_fetch, extract_side=_extract)
    result = await recipes.collect_across_pages("https://s/x", strategy="next_link")

    assert result.pages_collected == 1
    assert result.pages[0].injection is None
    assert result.max_injection_risk is None
    assert result.pages_with_injection == 0


# ======================================================================
# B4: injection_action='block' does not fight the walk
# ======================================================================


def _blocked_extraction(url: str) -> ExtractionResult:
    """Mirror what ContentExtractor produces under injection_action='block'
    on a HIGH-risk page: content emptied, failure_stage stamped, report kept.
    """
    return ExtractionResult(
        url=url,
        content=None,
        content_length=0,
        extraction_method="raw",
        failure_stage="injection_blocked",
        error_message="content blocked: high-confidence prompt-injection indicators",
        injection=InjectionReport(risk="high", indicators=["ignore all..."], score=6.0),
    )


@pytest.mark.asyncio
async def test_b4_page_param_does_not_stop_at_blocked_page() -> None:
    """With injection_action='block', a HIGH-risk page mid-walk (content
    emptied + failure_stage='injection_blocked') must NOT be read as the
    page_param empty-page terminator. The walk continues; a genuinely empty
    page later still terminates.
    """
    # page1 ok, page2 BLOCKED (empty by policy), page3 ok, page4 genuinely empty.
    bodies = {
        "https://api/list?page=1": "rows1",
        "https://api/list?page=2": "__BLOCKED__",
        "https://api/list?page=3": "rows3",
        "https://api/list?page=4": "",  # genuinely empty -> end of listing
    }

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, _page_html(bodies.get(url, "")))

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        if bodies.get(fr.url) == "__BLOCKED__":
            return _blocked_extraction(fr.url)
        return _extraction(fr.url, bodies.get(fr.url, ""))

    recipes = _make_recipes(
        fetch_side=_fetch,
        extract_side=_extract,
        config=_offline_config(injection_action="block"),
    )
    result = await recipes.collect_across_pages(
        "https://api/list?page=1", strategy="page_param", max_pages=10
    )

    # The walk did NOT halt at the blocked page2 -- it reached page3 and
    # terminated only at the genuinely-empty page4.
    assert result.stopped_reason == "empty_page"
    urls = [p.url for p in result.pages]
    assert urls == [
        "https://api/list?page=1",
        "https://api/list?page=2",  # blocked page IS recorded (flagged)
        "https://api/list?page=3",
    ]
    # The blocked page is flagged so the caller sees WHY it's blank.
    blocked = result.pages[1]
    assert blocked.blocked_reason == "injection_blocked"
    assert blocked.content == ""
    assert blocked.injection is not None and blocked.injection.risk == "high"
    # Warning + diagnostic surfaced for it.
    assert any("injection_action='block'" in w for w in result.warnings)
    assert any(
        d.url == "https://api/list?page=2" and d.block_reason == "injection_blocked"
        for d in result.diagnostics
    )
    # Rollup reflects the blocked page's HIGH risk.
    assert result.max_injection_risk == "high"


@pytest.mark.asyncio
async def test_b4_next_link_blocked_page_flagged_and_walk_continues() -> None:
    """For next_link, a blocked page is appended flagged (not a blank
    mystery page) and the walk follows that page's own next control.
    """
    htmls = {
        "https://site/1": _page_html("ONE", next_href="https://site/2"),
        "https://site/2": _page_html("BLOCKED-MARKUP", next_href="https://site/3"),
        "https://site/3": _page_html("THREE"),  # no next
    }

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, htmls[url])

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        if fr.url == "https://site/2":
            return _blocked_extraction(fr.url)
        return _extraction(fr.url, {"https://site/1": "ONE", "https://site/3": "THREE"}[fr.url])

    recipes = _make_recipes(
        fetch_side=_fetch,
        extract_side=_extract,
        config=_offline_config(injection_action="block"),
    )
    result = await recipes.collect_across_pages(
        "https://site/1", strategy="next_link", max_pages=10
    )

    # All three pages recorded; the blocked one is flagged but the walk
    # followed its next link to page3 and stopped there naturally.
    assert [p.url for p in result.pages] == [
        "https://site/1",
        "https://site/2",
        "https://site/3",
    ]
    assert result.stopped_reason == "no_next"
    assert result.pages[1].blocked_reason == "injection_blocked"
    assert result.pages[0].blocked_reason is None
    assert result.pages[2].blocked_reason is None


@pytest.mark.asyncio
async def test_b4_genuinely_empty_page_still_terminates_page_param() -> None:
    """Regression guard: with injection_action='block' active but NO page
    blocked, a genuinely empty page_param page still terminates the walk
    (the B4 fix must not break normal empty-page termination).
    """
    bodies = {
        "https://api/p?page=1": "rows1",
        "https://api/p?page=2": "",  # genuinely empty
    }

    async def _fetch(url: str, **_kw: object) -> FetchResult:
        return _fetch_result(url, _page_html(bodies.get(url, "")))

    async def _extract(fr: FetchResult, **_kw: object) -> ExtractionResult:
        return _extraction(fr.url, bodies.get(fr.url, ""))

    recipes = _make_recipes(
        fetch_side=_fetch,
        extract_side=_extract,
        config=_offline_config(injection_action="block"),
    )
    result = await recipes.collect_across_pages(
        "https://api/p?page=1", strategy="page_param", max_pages=10
    )
    assert result.pages_collected == 1
    assert result.stopped_reason == "empty_page"


# ======================================================================
# Contract: scroll strategy uses a RAW navigation (no robots / rate-limit)
# ======================================================================


def test_contract_scroll_docstring_narrowed_to_raw_navigation() -> None:
    """Contract fix: the scroll strategy does a raw page.goto (SSRF +
    injection only) -- it does NOT route through WebFetcher.fetch, so robots
    + rate-limiting do not apply. We chose to NARROW the docstring (the
    honest, lower-risk option) rather than add gates Recipes can't reach:
    the RobotsChecker / RateLimiter are encapsulated inside WebFetcher and
    are not handed to Recipes. Assert the docstrings now say so.
    """
    # Normalize wrapped whitespace so line-broken phrases match as one token.
    main_doc = " ".join((Recipes.collect_across_pages.__doc__ or "").split())
    scroll_doc = " ".join((Recipes._collect_scroll.__doc__ or "").split())
    # The main docstring no longer claims robots/rate-limit apply to EVERY page.
    assert "safety gates differ by strategy" in main_doc.lower()
    assert "robots.txt and rate limiting are NOT consulted" in main_doc
    assert "raw ``page.goto``" in main_doc
    # The scroll helper docstring is explicit about the raw navigation.
    assert "raw" in scroll_doc.lower()
    assert "robots.txt obedience and per-host rate limiting are NOT consulted" in scroll_doc
