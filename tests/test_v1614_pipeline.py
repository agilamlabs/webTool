"""v1.6.14 pipeline + tab correctness fixes (C-6, C-8).

C-6 (web_agent/recipes.py): ``fill_form_and_extract`` must NOT wrap an
all-tier-failed ``safe_page_content`` capture in a SUCCESS FetchResult.
Doing so swallows the transport-level failure (the form may have
submitted but the post-submit page never settled); the caller gets back
``extraction_method="none"`` from the extractor because ``not fr.html``,
but with no obvious signal that the navigation race killed the capture.
The fix matches ``downloader._do_save_page``'s NETWORK_ERROR pattern:
short-circuit and return an ExtractionResult with ``extraction_method=
"none"`` and ``content_length=0`` so the caller can distinguish "form
worked, page is empty" from "navigation race killed extraction".

C-8 (web_agent/tab_manager.py): ``close_tab`` must hold ``_lock`` across
``page.close()`` so a concurrent ``switch_tab`` / ``list`` / ``new_tab``
cannot observe an inconsistent intermediate state during teardown. The
sync close-event handler (``_evict_on_close``) still runs lockless --
that's fine because we hold the lock during the await so no other
coroutine can interleave.
"""

from __future__ import annotations

import asyncio
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.config import AppConfig
from web_agent.models import ExtractionResult, FormFilterSpec
from web_agent.recipes import Recipes
from web_agent.tab_manager import INITIAL_TAB_ID, TabManager

# ----------------------------------------------------------------------
# Shared fixtures
# ----------------------------------------------------------------------


def _make_tab_page(url: str = "about:blank", title: str = "") -> MagicMock:
    """Build a fake Page with the surface area TabManager touches.

    Mirrors the helper in ``tests/test_v166_tabs.py`` so the assertion
    style remains identical across the tab-cluster regression tests.
    """
    page = MagicMock()
    page.is_closed = MagicMock(return_value=False)
    page.bring_to_front = AsyncMock()
    page.close = AsyncMock()
    page.title = AsyncMock(return_value=title)
    type(page).url = property(lambda _self: url)

    # page.on(event, callback) records the close-handler so we can fire
    # it manually in tests.
    page._on_close_handlers = []  # type: ignore[attr-defined]

    def _on(event: str, cb) -> None:
        if event == "close":
            page._on_close_handlers.append(cb)  # type: ignore[attr-defined]

    page.on = MagicMock(side_effect=_on)
    return page


def _make_tab_ctx() -> MagicMock:
    """Build a fake BrowserContext that tracks the 'page' event listener."""
    ctx = MagicMock()
    ctx._page_event_handler = None

    def _on(event: str, cb) -> None:
        if event == "page":
            ctx._page_event_handler = cb

    ctx.on = MagicMock(side_effect=_on)

    async def _new_page() -> MagicMock:
        return _make_tab_page()

    ctx.new_page = AsyncMock(side_effect=_new_page)
    return ctx


def _make_recipes_with_mock_page(page: MagicMock) -> Recipes:
    """Build a Recipes with a BrowserManager that yields ``page``.

    The BrowserManager.new_page() is an async context manager; we mock
    it so tests don't need a real Playwright browser. Search/fetcher/
    downloader/sessions are stubbed since fill_form_and_extract doesn't
    use them on the path we're exercising.
    """

    class _NewPageCM:
        """Mimics ``BrowserManager.new_page`` (an async-context-manager)."""

        async def __aenter__(self) -> MagicMock:
            return page

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    bm = MagicMock()
    bm.new_page = MagicMock(return_value=_NewPageCM())

    cfg = AppConfig()
    extractor = MagicMock()
    # Real extractor: lets test 2 traverse the actual extraction chain;
    # test 1 short-circuits before extractor.extract() is reached so the
    # mock is fine to leave un-stubbed there. We still need a real one
    # for test 2 to validate the success path, so import lazily.
    from web_agent.content_extractor import ContentExtractor

    extractor = ContentExtractor(cfg)

    return Recipes(
        search=MagicMock(),
        fetcher=MagicMock(),
        extractor=extractor,
        downloader=MagicMock(),
        config=cfg,
        browser_manager=bm,
        sessions=None,
    )


# ----------------------------------------------------------------------
# C-6: recipes.py fill_form_and_extract
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_form_and_extract_returns_none_extraction_on_nav_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-6: when ``safe_page_content`` exhausts all 3 tiers and returns
    ``("", "navigating")``, ``fill_form_and_extract`` must short-circuit
    with ``extraction_method="none"`` and ``content_length=0`` -- NOT
    build a misleading SUCCESS FetchResult.
    """
    # Build a minimal mock Page that satisfies the recipe's steps up to
    # safe_page_content. We supply NO query / NO filters / NO submit /
    # NO wait_for so steps 2-5 are mostly no-ops; only goto + Step-5
    # (default networkidle) need to be exercised.
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    # page.url is a property on real Playwright Page objects.
    type(page).url = property(lambda _self: "https://example.com/form")

    # Track which FetchResult-constructor calls happen (so we can assert
    # NO SUCCESS FetchResult is built on the navigating path).
    fetch_result_calls: list[dict] = []
    real_fetch_result_cls = __import__("web_agent.models", fromlist=["FetchResult"]).FetchResult

    class _SpyFetchResult(real_fetch_result_cls):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
            fetch_result_calls.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr("web_agent.models.FetchResult", _SpyFetchResult)

    # Mock safe_page_content to simulate the all-tier-failed path.
    async def _all_tiers_fail(_page, **_kwargs):  # type: ignore[no-untyped-def]
        return ("", "navigating")

    monkeypatch.setattr("web_agent.recipes.safe_page_content", _all_tiers_fail)

    recipes = _make_recipes_with_mock_page(page)
    spec = FormFilterSpec()  # all defaults -> minimal driving

    result = await recipes.fill_form_and_extract("https://example.com/form", spec)

    # The fix: extraction_method="none" + content_length=0 + a real
    # correlation_id (not the empty SUCCESS-with-empty-html lie).
    assert isinstance(result, ExtractionResult)
    assert result.extraction_method == "none", (
        f"navigating path must short-circuit to extraction_method='none', "
        f"got {result.extraction_method!r}"
    )
    assert result.content_length == 0
    assert result.url == "https://example.com/form"

    # And critically: no SUCCESS FetchResult should have been built.
    success_calls = [
        call
        for call in fetch_result_calls
        if str(call.get("status", "")).endswith("SUCCESS") or call.get("html") == ""
    ]
    assert not success_calls, (
        f"C-6 regression: navigating path built a FetchResult: {success_calls}"
    )


@pytest.mark.asyncio
async def test_fill_form_and_extract_preserves_success_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity check: when ``safe_page_content`` returns real HTML, the
    extractor still runs and ``extraction_method`` is something other
    than ``"none"``. Guards against the C-6 fix accidentally
    short-circuiting the happy path too.
    """
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    type(page).url = property(lambda _self: "https://example.com/results")

    real_html = (
        "<html><head><title>Real results page</title></head>"
        "<body><main><h1>Form results</h1>"
        "<p>This is the real post-submit content with enough text to "
        "satisfy trafilatura's minimum content length threshold. The "
        "page lists several search results that are visible to the "
        "reader and would normally be extracted into structured form. "
        "We include multiple sentences here so the extractor has a "
        "fighting chance of pulling something meaningful out of the "
        "markup rather than bailing on min_content_length.</p>"
        "<p>Second paragraph adds more body text so the extraction "
        "chain reliably picks one of trafilatura, bs4, or raw.</p>"
        "</main></body></html>"
    )

    async def _capture_success(_page, **_kwargs):  # type: ignore[no-untyped-def]
        return (real_html, "content")

    monkeypatch.setattr("web_agent.recipes.safe_page_content", _capture_success)

    recipes = _make_recipes_with_mock_page(page)
    spec = FormFilterSpec()

    result = await recipes.fill_form_and_extract("https://example.com/results", spec)

    assert isinstance(result, ExtractionResult)
    # Whichever extractor wins (trafilatura / bs4 / raw), it must NOT
    # be the "none" short-circuit from C-6.
    assert result.extraction_method != "none", (
        f"happy path regressed -- extractor should produce real content, "
        f"got extraction_method={result.extraction_method!r}"
    )
    assert result.content_length > 0


# ----------------------------------------------------------------------
# H1 (v1.6.16): post-submit navigation must be re-gated before extraction
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_form_and_extract_regates_post_submit_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H1: the form SUBMIT is itself a navigation. If it 302 / JS-redirects
    to an internal host (e.g. AWS IMDS at 169.254.169.254), the post-submit
    URL must be re-gated BEFORE extraction. The page's ``.url`` is an
    ALLOWED host at initial-nav time but becomes a private/link-local host
    after submit; ``fill_form_and_extract`` must return
    ``extraction_method="none"`` and must NOT run ``safe_page_content`` /
    extraction on the post-submit (internal) page.
    """
    allowed_url = "https://example.com/form"
    internal_url = "http://169.254.169.254/latest/meta-data/"

    # Shared flag: the page URL is the allowed host until the submit click
    # fires, then flips to the internal (link-local) host -- modelling a
    # post-submit 302 / JS redirect to AWS IMDS.
    state = {"submitted": False}

    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    # ``page.url`` is a property on real Playwright Page objects; here it
    # returns the allowed host pre-submit and the internal host post-submit
    # so the INITIAL-nav re-check is satisfied but the POST-submit gate
    # must reject.
    type(page).url = property(
        lambda _self: internal_url if state["submitted"] else allowed_url
    )

    # The submit click (step 4) resolves via ``page.locator(<selector>)``;
    # its ``.click()`` flips the redirect flag, mimicking the navigation.
    submit_locator = MagicMock()

    async def _click(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        state["submitted"] = True

    submit_locator.click = AsyncMock(side_effect=_click)
    page.locator = MagicMock(return_value=submit_locator)

    # safe_page_content MUST NOT be invoked: the post-submit gate fires
    # before extraction. If it is called, the SSRF gap is still open.
    safe_content_calls: list = []

    async def _spy_safe_content(_page, **_kwargs):  # type: ignore[no-untyped-def]
        safe_content_calls.append(_page)
        return ("<html><body>INTERNAL METADATA</body></html>", "content")

    monkeypatch.setattr("web_agent.recipes.safe_page_content", _spy_safe_content)

    recipes = _make_recipes_with_mock_page(page)
    # A submit_selector forces step 4 to click (and thus flip the flag).
    spec = FormFilterSpec(submit_selector="button[type=submit]")

    result = await recipes.fill_form_and_extract(allowed_url, spec)

    assert isinstance(result, ExtractionResult)
    assert result.extraction_method == "none", (
        f"post-submit redirect to an internal host must short-circuit to "
        f"extraction_method='none', got {result.extraction_method!r}"
    )
    # ``url`` is reported as the original (allowed) request URL, matching
    # the function's other early-returns.
    assert result.url == allowed_url
    # The submit must have actually fired (otherwise the test is vacuous).
    assert state["submitted"] is True
    # Critically: no extraction may have run on the post-submit page.
    assert safe_content_calls == [], (
        "H1 regression: safe_page_content/extraction ran on the post-submit "
        "internal page; content from the disallowed host was captured."
    )


# ----------------------------------------------------------------------
# C-8: tab_manager.py close_tab holds the lock across page.close()
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_tab_holds_lock_across_page_close() -> None:
    """C-8: while ``page.close()`` is awaiting, a concurrent coroutine
    attempting ``async with tm._lock`` must NOT acquire the lock until
    the close completes (and its sync ``_evict_on_close`` handler has
    finished mutating state).

    Before the fix: close_tab released the lock before awaiting
    page.close(), so a parallel switch_tab could observe an
    inconsistent intermediate state.
    """
    ctx = _make_tab_ctx()
    tm = TabManager(ctx)
    await tm.register_initial_page(_make_tab_page(), INITIAL_TAB_ID)
    second_id = await tm.new_tab()
    second_page = tm.get(second_id)

    # Track whether the parallel acquisition happened during or after
    # page.close()'s await.
    lock_acquired_during_close = asyncio.Event()
    close_observed_blocked_parallel = asyncio.Event()
    parallel_task: dict[str, Optional[asyncio.Task[None]]] = {"task": None}

    async def _parallel_acquire() -> None:
        # If close_tab properly holds the lock across the await,
        # this acquisition will block until close_tab releases.
        async with tm._lock:
            lock_acquired_during_close.set()

    async def _page_close_impl(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        # Kick off the parallel acquire from inside the close.
        task = asyncio.create_task(_parallel_acquire())
        parallel_task["task"] = task
        # Give the event loop a chance to schedule the parallel task.
        # asyncio.sleep(0) yields once; we yield several times to be
        # robust against scheduler ordering.
        for _ in range(10):
            await asyncio.sleep(0)
        # The lock must still be held by close_tab here, so the
        # parallel task cannot have acquired it yet.
        if not lock_acquired_during_close.is_set():
            close_observed_blocked_parallel.set()
        # Now fire the synthetic Playwright "close" event so
        # _evict_on_close mutates _tabs/_current_tab_id BEFORE we
        # return from page.close().
        for cb in second_page._on_close_handlers:  # type: ignore[attr-defined]
            cb(second_page)

    second_page.close = AsyncMock(side_effect=_page_close_impl)

    await tm.close_tab(second_id)

    # Now the lock has been released; the parallel task can finally
    # acquire it.
    task = parallel_task["task"]
    assert task is not None
    await asyncio.wait_for(task, timeout=1.0)

    assert close_observed_blocked_parallel.is_set(), (
        "C-8 regression: a parallel coroutine acquired tm._lock while "
        "close_tab's page.close() was in-flight; lock was not held "
        "across the await."
    )
    assert lock_acquired_during_close.is_set(), (
        "parallel acquire should have succeeded once close_tab released the lock"
    )

    # And state should be consistent post-close: the closed tab is
    # gone, the remaining tab is current.
    assert second_id not in tm._tabs
    assert tm.current_tab_id() == INITIAL_TAB_ID


@pytest.mark.asyncio
async def test_close_tab_idempotent_when_called_twice() -> None:
    """C-8 side property: closing the same tab twice must not crash
    with AttributeError, double-close, or race-on-state. The second
    call may legitimately raise KeyError (the existing public
    contract -- preserved from the pre-fix code), but it must do so
    cleanly without disturbing the rest of the tab map.
    """
    ctx = _make_tab_ctx()
    tm = TabManager(ctx)
    await tm.register_initial_page(_make_tab_page(), INITIAL_TAB_ID)
    second_id = await tm.new_tab()
    second_page = tm.get(second_id)

    # First close: fire the synthetic close-event handler so
    # _evict_on_close removes the tab (matching real Playwright
    # behaviour where page.close() triggers the "close" event).
    async def _first_close(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        for cb in second_page._on_close_handlers:  # type: ignore[attr-defined]
            cb(second_page)

    second_page.close = AsyncMock(side_effect=_first_close)

    await tm.close_tab(second_id)
    assert second_id not in tm._tabs
    assert tm.current_tab_id() == INITIAL_TAB_ID
    assert len(tm._tabs) == 1

    # Second close: the tab is already gone, so close_tab must raise
    # KeyError cleanly (the documented public contract). The KEY
    # assertion is that no other state mutates -- the surviving "main"
    # tab must remain intact.
    with pytest.raises(KeyError):
        await tm.close_tab(second_id)

    # State unchanged after the failed second close.
    assert tm.current_tab_id() == INITIAL_TAB_ID
    assert len(tm._tabs) == 1
    assert INITIAL_TAB_ID in tm._tabs
