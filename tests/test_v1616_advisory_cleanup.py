"""v1.6.16 LOW-severity advisory-cleanup tests.

Focused unit tests for the advisory findings that were validated and
fixed in this pass. All unit-level: no real browser, no real network.
Mocks follow the patterns in ``tests/test_v1616_review_hardening.py``
(fake session/tab-manager + MagicMock page) and
``tests/test_v165_medium.py`` (Recipes with AsyncMock fetcher).

Finding -> test map:
  BR-7  : observe() URL-rollback re-validates prev_url before navigating
          back (no second path onto a disallowed host).
  BR-8  : scroll_until_text surfaces a closed page instead of swallowing
          it; still rides over benign per-scroll load-state timeouts.
  BR-9  : the submit-click heuristic is advisory only -- documented as
          best-effort, allow_form_submit is the real gate (behaviour
          regression guard: it still flags obvious submits + bypassable).
  BR-10 : the WaitInput(FUNCTION) pre-flight still blocks the WHOLE
          sequence atomically before any action runs (early-exit kept).
  REC-3 : find_and_download_file extensionless fallback matches the
          requested file_types by KIND (``['doc']`` accepts a ``docx``
          classification; mismatched kinds still rejected).
  AG-4  : save_results never derives a dotfile/empty filename from a
          query with no alphanumerics.

SKIPPED (no test):
  REC-4 : ``_resolve_domain_hints`` is NOT dead -- it is a documented
          back-compat public surface with its own tests in
          tests/test_v162_models_and_profiles.py. Left in place.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout
from web_agent.browser_actions import BrowserActions, _looks_like_submit
from web_agent.config import AppConfig, SafetyConfig
from web_agent.exceptions import ActionError
from web_agent.models import (
    ActionStatus,
    AgentResult,
    DownloadResult,
    EvaluateInput,
    FetchStatus,
    LocatorSpec,
    SearchResponse,
    SearchResultItem,
    WaitInput,
    WaitTarget,
)
from web_agent.recipes import Recipes


def _result(position: int, url: str) -> SearchResultItem:
    return SearchResultItem(position=position, title=url, url=url, provider="ddgs")


def _make_browser_actions(page: MagicMock, cfg: AppConfig) -> BrowserActions:
    """Real BrowserActions wired to a faked session that always returns
    ``page`` for the current tab (mirrors test_v1616_review_hardening).

    Wires both the ``observe``/``scroll_until_text`` lookup
    (``get_or_current``) and the ``execute_sequence`` lookup (``get`` +
    ``current``) so a single helper serves every test here.
    """
    fake_tab_mgr = MagicMock()
    fake_tab_mgr.get_or_current = MagicMock(return_value=page)
    fake_tab_mgr.current = MagicMock(return_value=page)
    fake_tab_mgr.current_tab_id = MagicMock(return_value="t0")
    fake_sessions = MagicMock()
    fake_sessions.get = MagicMock(return_value=MagicMock())
    fake_sessions.get_tab_manager = MagicMock(return_value=fake_tab_mgr)
    fake_sessions.touch = MagicMock()
    return BrowserActions(MagicMock(), cfg, sessions=fake_sessions)


# ---------------------------------------------------------------------------
# BR-7: observe() URL-rollback re-validates prev_url before navigating back
# ---------------------------------------------------------------------------
class TestBR7ObserveRollbackRevalidatesPrevUrl:
    def _stateful_page(self, prev_url: str, redirect_url: str) -> MagicMock:
        """Page whose .url is ``prev_url`` until goto() runs, then the
        redirect target. goto() records every navigation target."""
        page = MagicMock()
        state = {"navigated": False}

        async def _goto(target: str, **_k: object) -> None:
            page.goto_targets.append(target)
            # The first goto (to the requested url) lands on the redirect
            # host. Subsequent gotos (rollback) just record the target.
            if not state["navigated"]:
                state["navigated"] = True
                state["current"] = redirect_url

        page.goto_targets = []
        state["current"] = prev_url
        page.goto = AsyncMock(side_effect=_goto)
        type(page).url = property(lambda _self: state["current"])
        page.is_closed = MagicMock(return_value=False)
        return page

    @pytest.mark.asyncio
    async def test_disallowed_prev_url_is_not_navigated_back(self) -> None:
        """When the tab's previous URL is itself denied by the policy, the
        rollback must be skipped (no second path onto a disallowed host)."""
        # Deny both the redirect target AND the previous host.
        cfg = AppConfig(safety=SafetyConfig(denied_domains=["evil.com", "old-denied.com"]))
        page = self._stateful_page(
            prev_url="https://old-denied.com/page",
            redirect_url="https://evil.com/landing",
        )
        ba = _make_browser_actions(page, cfg)

        with pytest.raises(ValueError, match="redirected to disallowed domain"):
            await ba.observe(url="https://allowed.example.com/start", session_id="sid")

        # First goto = the requested URL. The denied prev_url must NOT be a
        # rollback target.
        assert page.goto_targets == ["https://allowed.example.com/start"]
        assert "https://old-denied.com/page" not in page.goto_targets

    @pytest.mark.asyncio
    async def test_allowed_prev_url_is_navigated_back(self) -> None:
        """Regression: a still-allowed previous URL is rolled back to (the
        BR-7 fix must not break the legitimate rollback)."""
        cfg = AppConfig(safety=SafetyConfig(denied_domains=["evil.com"]))
        page = self._stateful_page(
            prev_url="https://good.example.com/page",
            redirect_url="https://evil.com/landing",
        )
        ba = _make_browser_actions(page, cfg)

        with pytest.raises(ValueError, match="redirected to disallowed domain"):
            await ba.observe(url="https://good.example.com/start", session_id="sid")

        # Requested URL first, then the allowed prev_url as rollback.
        assert page.goto_targets == [
            "https://good.example.com/start",
            "https://good.example.com/page",
        ]


# ---------------------------------------------------------------------------
# BR-8: scroll_until_text surfaces a closed page; rides over benign timeouts
# ---------------------------------------------------------------------------
class TestBR8ScrollUntilTextErrorHandling:
    @pytest.mark.asyncio
    async def test_page_closed_mid_scroll_raises(self) -> None:
        """A page that closes during scrolling raises ActionError instead of
        silently looping to a misleading 'not found'."""
        cfg = AppConfig()
        page = MagicMock()
        # Initial quick-win read returns no match.
        page.evaluate = AsyncMock(return_value="nothing here")
        page.mouse.wheel = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        # Closed by the time the loop checks at the top of iteration 1.
        page.is_closed = MagicMock(return_value=True)
        ba = _make_browser_actions(page, cfg)

        with pytest.raises(ActionError, match="page was closed"):
            await ba.scroll_until_text("target", session_id="sid", max_scrolls=3)

    @pytest.mark.asyncio
    async def test_evaluate_raises_on_closed_page_is_fatal(self) -> None:
        """If the body read itself raises because the page closed, that is
        fatal (re-raised), not swallowed via ``continue``."""
        cfg = AppConfig()
        page = MagicMock()
        closed = {"v": False}
        page.mouse.wheel = AsyncMock()
        page.wait_for_load_state = AsyncMock()

        async def _evaluate(_expr: str) -> str:
            # First (quick-win) read succeeds with no match; then the page
            # closes and the in-loop read raises.
            if closed["v"]:
                raise RuntimeError("Target page, context or browser has been closed")
            return "no match yet"

        page.evaluate = AsyncMock(side_effect=_evaluate)

        def _is_closed() -> bool:
            return closed["v"]

        page.is_closed = MagicMock(side_effect=_is_closed)
        ba = _make_browser_actions(page, cfg)

        # Trip the close after the loop's top-of-iteration is_closed() check
        # passes but before/at the evaluate. Use wheel as the trigger point.
        async def _wheel(*_a: object, **_k: object) -> None:
            closed["v"] = True

        page.mouse.wheel = AsyncMock(side_effect=_wheel)

        with pytest.raises(ActionError, match="page was closed"):
            await ba.scroll_until_text("target", session_id="sid", max_scrolls=3)

    @pytest.mark.asyncio
    async def test_benign_load_state_timeout_is_tolerated(self) -> None:
        """A per-scroll load-state TimeoutError must NOT abort the loop --
        the scroll keeps going and eventually finds the text."""
        cfg = AppConfig()
        page = MagicMock()
        reads = {"n": 0}

        async def _evaluate(_expr: str) -> str:
            reads["n"] += 1
            # Quick-win + first loop read: no match. Then the text appears.
            return "FOUND IT" if reads["n"] >= 3 else "loading..."

        page.evaluate = AsyncMock(side_effect=_evaluate)
        page.mouse.wheel = AsyncMock()
        # Load-state always times out -- the benign infinite-scroll case.
        page.wait_for_load_state = AsyncMock(side_effect=PlaywrightTimeout("timeout"))
        page.is_closed = MagicMock(return_value=False)
        ba = _make_browser_actions(page, cfg)

        res = await ba.scroll_until_text("FOUND IT", session_id="sid", max_scrolls=5)
        assert res.data["found"] is True
        assert res.status == ActionStatus.SUCCESS


# ---------------------------------------------------------------------------
# BR-9: submit-click heuristic is advisory; allow_form_submit is the gate
# ---------------------------------------------------------------------------
class TestBR9SubmitHeuristicIsAdvisory:
    def test_docstring_does_not_promise_a_guarantee(self) -> None:
        """The fix reframes the heuristic as advisory and names the real
        gate; assert the docstring no longer over-promises."""
        doc = (_looks_like_submit.__doc__ or "").lower()
        assert "advisory" in doc
        assert "allow_form_submit" in doc
        # Must NOT claim a guarantee.
        assert "guarantee" not in doc or "not a guarantee" in doc

    def test_still_flags_obvious_submit_css(self) -> None:
        assert _looks_like_submit("button[type=submit]") is True

    def test_still_flags_submit_keyword_locator(self) -> None:
        assert _looks_like_submit(LocatorSpec(text="Log in")) is True

    def test_bypassable_div_handler_not_flagged(self) -> None:
        """Documents the advisory limitation: a non-button selector that
        submits via a JS handler is NOT caught (heuristic is best-effort)."""
        assert _looks_like_submit("div#go") is False
        assert _looks_like_submit(LocatorSpec(selector="div.cta")) is False


# ---------------------------------------------------------------------------
# BR-10: WaitInput(FUNCTION) pre-flight keeps all-or-nothing block semantics
# ---------------------------------------------------------------------------
class TestBR10PreflightEarlyExitPreserved:
    def _seq_page(self) -> MagicMock:
        page = MagicMock()
        page.goto = AsyncMock()
        page.on = MagicMock()
        page.remove_listener = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.fill = AsyncMock()
        page.wait_for_function = AsyncMock()
        type(page).url = property(lambda _self: "https://good.example/app")
        return page

    @pytest.mark.asyncio
    async def test_function_wait_blocks_whole_sequence_before_any_action(self) -> None:
        """With allow_js_evaluation=False, a sequence containing a
        WaitInput(FUNCTION) must block EVERY action up-front (no partial
        execution) -- the pre-flight early-exit the comment documents.

        The pre-flight runs before page acquisition, so the page is never
        touched (goto not awaited)."""
        cfg = AppConfig(safety=SafetyConfig(allow_js_evaluation=False))
        page = self._seq_page()
        ba = _make_browser_actions(page, cfg)

        actions = [
            EvaluateInput(expression="1+1"),  # would also be blocked
            WaitInput(target=WaitTarget.FUNCTION, value="() => true"),
        ]
        res = await ba.execute_sequence("https://good.example/app", actions, session_id="sid")

        # All actions reported blocked (SKIPPED) up-front; none executed.
        assert res.actions_total == 2
        assert res.actions_succeeded == 0
        assert all(r.status == ActionStatus.SKIPPED for r in res.results)
        assert any("allow_js_evaluation=False" in (r.error_message or "") for r in res.results)
        # Pre-flight short-circuits BEFORE navigation/dispatch.
        page.goto.assert_not_awaited()
        page.fill.assert_not_called()

    @pytest.mark.asyncio
    async def test_function_wait_allowed_when_js_enabled(self) -> None:
        """Sanity: the pre-flight does not block when JS-eval is enabled --
        the sequence runs and wait_for_function is dispatched."""
        from web_agent.browser_actions import _PAGE_DIALOG_STATES

        cfg = AppConfig(safety=SafetyConfig(allow_js_evaluation=True))
        page = self._seq_page()
        ba = _make_browser_actions(page, cfg)

        actions = [WaitInput(target=WaitTarget.FUNCTION, value="() => true")]
        try:
            res = await ba.execute_sequence("https://good.example/app", actions, session_id="sid")
        finally:
            _PAGE_DIALOG_STATES.pop(page, None)
        assert res.actions_succeeded == 1
        page.wait_for_function.assert_awaited_once()


# ---------------------------------------------------------------------------
# REC-3: extensionless fallback matches requested file_types by KIND
# ---------------------------------------------------------------------------
class TestREC3FindAndDownloadFileKindMatch:
    def _recipes(self, urls: list[str], classification: str, download_mock: MagicMock) -> Recipes:
        search_mock = MagicMock()
        search_mock.search = AsyncMock(
            return_value=SearchResponse(
                query="q",
                total_results=len(urls),
                results=[_result(i + 1, u) for i, u in enumerate(urls)],
            )
        )
        fetcher_mock = MagicMock()
        fetcher_mock.classify_url = AsyncMock(return_value=classification)
        cfg = AppConfig(log_level="WARNING", safety=SafetyConfig(probe_binary_urls=True))
        return Recipes(
            search=search_mock,
            fetcher=fetcher_mock,
            extractor=MagicMock(),
            downloader=download_mock,
            config=cfg,
        )

    @pytest.mark.asyncio
    async def test_doc_request_accepts_docx_classification(self) -> None:
        """The bug: ``file_types=['doc']`` could never match because
        classify_url returns the kind ``'docx'`` while the old code compared
        against the raw ``'.doc'``. After REC-3 it matches."""
        download_mock = MagicMock()
        download_mock.download = AsyncMock(
            return_value=DownloadResult(
                url="https://x.example.com/Archives/77",
                filepath="/tmp/77.doc",
                filename="77.doc",
                status=FetchStatus.SUCCESS,
            )
        )
        recipes = self._recipes(
            urls=["https://x.example.com/Archives/77"],  # extensionless
            classification="docx",
            download_mock=download_mock,
        )
        result = await recipes.find_and_download_file("filing", file_types=["doc"])
        assert result.status == FetchStatus.SUCCESS
        download_mock.download.assert_awaited_once()
        assert download_mock.download.await_args.args[0] == "https://x.example.com/Archives/77"

    @pytest.mark.asyncio
    async def test_xls_request_accepts_xlsx_classification(self) -> None:
        """``file_types=['xls']`` -> kind ``'xlsx'`` -> matches."""
        download_mock = MagicMock()
        download_mock.download = AsyncMock(
            return_value=DownloadResult(
                url="https://x.example.com/d/88",
                filepath="/tmp/88.xls",
                filename="88.xls",
                status=FetchStatus.SUCCESS,
            )
        )
        recipes = self._recipes(
            urls=["https://x.example.com/d/88"],
            classification="xlsx",
            download_mock=download_mock,
        )
        result = await recipes.find_and_download_file("sheet", file_types=["xls"])
        assert result.status == FetchStatus.SUCCESS
        download_mock.download.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mismatched_kind_still_rejected(self) -> None:
        """Regression: a caller asking for ['pdf'] must NOT accept an
        extensionless URL that classifies as 'xlsx'."""
        download_mock = MagicMock()
        download_mock.download = AsyncMock()
        recipes = self._recipes(
            urls=["https://x.example.com/d/99"],
            classification="xlsx",
            download_mock=download_mock,
        )
        result = await recipes.find_and_download_file("report", file_types=["pdf"])
        assert result.status == FetchStatus.NETWORK_ERROR
        download_mock.download.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_binary_other_skipped_when_types_pinned(self) -> None:
        """An opaque ``binary_other`` is not accepted when the caller pinned
        specific types (cannot prove it is the requested kind)."""
        download_mock = MagicMock()
        download_mock.download = AsyncMock()
        recipes = self._recipes(
            urls=["https://x.example.com/d/abc"],
            classification="binary_other",
            download_mock=download_mock,
        )
        result = await recipes.find_and_download_file("thing", file_types=["pdf"])
        assert result.status == FetchStatus.NETWORK_ERROR
        download_mock.download.assert_not_awaited()


# ---------------------------------------------------------------------------
# Cluster B advisories (folded into v1.6.16):
#   FC-2 : Secure-flagged cookies are NOT forwarded over plaintext http://.
#   CE-3 : prefer_api scores candidates (same-origin + JSON-root preferred,
#          analytics/telemetry URLs deprioritised) instead of taking the
#          largest body.
#   MC-4 : web_search / web_research docstrings now match the actual clamps
#          (doc-only -- verified by inspection).
#   MC-3 : lifespan removes only its own loguru handler (verified by
#          inspection; lifespan launches a real Agent, not unit-tested).
#   NC-1 : SKIPPED -- _capture_response_body already swallows closed-page
#          errors and its task is tracked + auto-discarded (already mitigated).
# ---------------------------------------------------------------------------
def _fetcher_with_session_cookies(cookies: list[dict]) -> object:
    from web_agent.web_fetcher import WebFetcher

    ctx = MagicMock()
    ctx.cookies = AsyncMock(return_value=cookies)
    sessions = MagicMock()
    sessions.get = MagicMock(return_value=ctx)
    fetcher = WebFetcher(MagicMock(), AppConfig())
    fetcher._sessions = sessions
    return fetcher


_FC2_COOKIES = [
    {"name": "sid", "value": "secret", "domain": "example.com", "secure": True},
    {"name": "pref", "value": "x", "domain": "example.com", "secure": False},
]


class TestFC2SecureCookiesNotSentOverHttp:
    @pytest.mark.asyncio
    async def test_secure_cookie_dropped_over_http(self) -> None:
        fetcher = _fetcher_with_session_cookies(_FC2_COOKIES)
        jar = await fetcher._cookies_for_session("s", "http://example.com/file.pdf")
        names = {c.name for c in jar.jar}
        assert "sid" not in names  # Secure cookie withheld over plaintext http
        assert "pref" in names

    @pytest.mark.asyncio
    async def test_secure_cookie_kept_over_https(self) -> None:
        fetcher = _fetcher_with_session_cookies(_FC2_COOKIES)
        jar = await fetcher._cookies_for_session("s", "https://example.com/file.pdf")
        names = {c.name for c in jar.jar}
        assert "sid" in names  # https target -> Secure cookie forwarded
        assert "pref" in names


class TestCE3PreferApiScoring:
    def test_same_origin_api_beats_larger_analytics_blob(self) -> None:
        import json

        from web_agent.content_extractor import ContentExtractor
        from web_agent.models import FetchResult, FetchStatus, NetworkEvent

        analytics_body = json.dumps({"events": [{"e": i} for i in range(200)]})
        api_body = json.dumps({"product_id": 42, "title": "Real API Payload"})
        assert len(analytics_body) > len(api_body)  # the analytics blob is larger

        events = [
            NetworkEvent(
                event_type="response",
                url="https://metrics.example.com/collect",
                method="POST",
                resource_type="xhr",
                status_code=200,
                content_type="application/json",
                body_text=analytics_body,
            ),
            NetworkEvent(
                event_type="response",
                url="https://example.com/api/data",
                method="GET",
                resource_type="fetch",
                status_code=200,
                content_type="application/json",
                body_text=api_body,
            ),
        ]
        fr = FetchResult(
            url="https://example.com/page",
            final_url="https://example.com/page",
            status=FetchStatus.SUCCESS,
            html="<html></html>",
            network_events=events,
        )
        res = ContentExtractor(AppConfig())._extract_from_api_candidates(
            fr, "https://example.com/page"
        )
        assert res is not None
        # the smaller SAME-ORIGIN API body wins over the larger analytics blob
        assert "product_id" in (res.content or "")
        assert "events" not in (res.content or "")


# ---------------------------------------------------------------------------
# AG-4: save_results never yields a dotfile / empty filename
# ---------------------------------------------------------------------------
class TestAG4SaveResultsFilenameSafety:
    def _agent(self, out_dir: Path):
        from web_agent.agent import Agent

        return Agent(AppConfig(output_dir=str(out_dir)))

    def _result_obj(self, query: str) -> AgentResult:
        return AgentResult(
            query=query,
            search=SearchResponse(query=query, total_results=0, results=[]),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("query", ["???", "", "...", "   ", "!@#$%"])
    async def test_degenerate_query_yields_safe_filename(self, tmp_path: Path, query: str) -> None:
        agent = self._agent(tmp_path)
        path = await agent.save_results(self._result_obj(query))
        # Non-empty stem, real .json suffix, NOT a dotfile, NOT all-underscore.
        assert path.suffix == ".json"
        assert path.stem == "results"
        assert not path.name.startswith(".")
        assert path.exists()

    @pytest.mark.asyncio
    async def test_normal_query_still_used_as_stem(self, tmp_path: Path) -> None:
        agent = self._agent(tmp_path)
        path = await agent.save_results(self._result_obj("Quarterly Report 2025"))
        assert path.name == "Quarterly_Report_2025.json"
        assert path.exists()


# ---------------------------------------------------------------------------
# Cluster C advisories (folded into v1.6.16):
#   BR-2 : _NoCloseContextProxy can be a WeakKeyDictionary key (__weakref__).
#   SP-1 : private-IP result URLs dropped for EVERY provider at the
#          SearchEngine choke point (_result_host_is_private helper).
#   SE-1 : a search cache hit does NOT mutate the dict the backend returned.
#   EC-2 : a negative/garbage max_results falls back to the default (not 1).
#   DS-1 : float skill inputs reject NaN / +-inf.
#   BR-3 / SM-2 / BR-4 : verified by inspection (route-teardown suppress; UA
#          probe moved outside the session lock; close() no-op is the proxy's
#          documented, DEBUG-logged purpose -> left as-is).
# ---------------------------------------------------------------------------
class TestBR2ProxyWeakReferenceable:
    def test_proxy_can_be_weakkeydict_key(self) -> None:
        import weakref

        from web_agent.browser_manager import _NoCloseContextProxy

        proxy = _NoCloseContextProxy(MagicMock())
        wkd = weakref.WeakKeyDictionary()
        # Without __weakref__ in __slots__ this raises TypeError.
        wkd[proxy] = "v"
        assert wkd[proxy] == "v"


class TestSP1ResultHostPrivateFilter:
    def test_helper_flags_literal_private_ips(self) -> None:
        from web_agent.search_engine import _result_host_is_private

        assert _result_host_is_private("http://127.0.0.1/x") is True
        assert _result_host_is_private("http://169.254.169.254/latest/meta-data") is True
        assert _result_host_is_private("http://10.0.0.5/internal") is True
        assert _result_host_is_private("https://example.com/x") is False
        assert _result_host_is_private("not a url") is False


class TestSE1CacheHitNoMutation:
    @pytest.mark.asyncio
    async def test_cache_hit_does_not_mutate_backend_dict(self) -> None:
        from web_agent.search_engine import SearchEngine

        cached_payload = {"query": "q", "total_results": 0, "results": []}
        cache = MagicMock()
        cache.get = AsyncMock(return_value=cached_payload)
        engine = SearchEngine(browser_manager=MagicMock(), config=AppConfig())
        engine._cache = cache
        resp = await engine.search("q")
        assert resp.from_cache is True
        # SE-1: the backend's stored dict must be left untouched.
        assert "from_cache" not in cached_payload


class TestEC2NegativeMaxResultsDefaults:
    @pytest.mark.asyncio
    async def test_negative_max_results_falls_back_to_default(self) -> None:
        from web_agent.builtin_skills.ec_europa_document_search import run

        captured: dict = {}

        async def _search_and_extract(query: str, max_results: int) -> object:
            captured["max_results"] = max_results
            result = MagicMock()
            result.pages = []
            return result

        agent = MagicMock()
        agent.search_and_extract = _search_and_extract
        await run(agent, "https://ec.europa.eu", {"query": "x", "max_results": -5})
        assert captured["max_results"] == 5  # negative -> default, not a silent 1


class TestDS1FloatRejectsNonFinite:
    def test_float_input_rejects_inf_and_nan(self) -> None:
        from web_agent.domain_skills import _coerce_input
        from web_agent.exceptions import SkillInputError

        class _Spec:
            type = "float"
            name = "ratio"

        spec = _Spec()
        assert _coerce_input("1.5", spec) == 1.5
        for bad in ("inf", "-inf", "nan"):
            with pytest.raises(SkillInputError):
                _coerce_input(bad, spec)


# ---------------------------------------------------------------------------
# Cluster D advisories (folded into v1.6.16):
#   AUDIT-1 : the audit scope redacts top-level sensitive kwargs.
#   OWN-1   : ownership token is written via os.open(0o600) (round-trips).
#   DEBUG-2 : debug artifact filenames carry a monotonic uniqueness counter.
#   MAIN-1  : run_interact bounds the actions-file size + handles parse errors
#             (verified by inspection -- driving the CLI launches an Agent).
#   DEBUG-1 : capture_page reserves slots before its awaits (inspection;
#             capture_no_page is synchronous so it never raced).
#   TRACE-4 : SKIPPED -- per-session trace locks are a delicate refactor on an
#             opt-in, off-by-default feature; the global lock preserves the
#             ordinal + append ordering (same call as the v1.6.16 main pass).
#   CORR-1  : SKIPPED -- the import-time loguru patcher is intentional (cid in
#             logs); loguru allows one patcher and exposes no compose API, so a
#             non-clobbering fix would be a behaviour change.
# ---------------------------------------------------------------------------
class TestAUDIT1ArgsRedacted:
    @pytest.mark.asyncio
    async def test_sensitive_args_redacted_in_audit_entry(self, tmp_path: Path) -> None:
        import json as _json

        from web_agent.audit import AuditLogger
        from web_agent.trace_recorder import is_redacted

        audit = AuditLogger(path=str(tmp_path / "audit.jsonl"), enabled=True)
        async with audit.scope("login", {"password": "hunter2", "user": "bob"}):
            pass
        lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
        entry = _json.loads(lines[-1])
        assert is_redacted(entry["args"]["password"])  # AUDIT-1: secret masked
        assert entry["args"]["user"] == "bob"  # benign field preserved


class TestOWN1TokenFileRoundTrip:
    def test_issue_then_read_round_trips(self, tmp_path: Path) -> None:
        from web_agent.ownership import OwnershipToken

        token = OwnershipToken.issue(tmp_path)
        assert token and len(token) == 64  # 32 bytes hex
        assert OwnershipToken.read(tmp_path) == token
        # re-issue (file already exists) overwrites cleanly via the new os.open path
        token2 = OwnershipToken.issue(tmp_path)
        assert token2 != token
        assert OwnershipToken.read(tmp_path) == token2


class TestDEBUG2UniqueArtifactFilenames:
    def test_consecutive_paths_are_unique(self, tmp_path: Path) -> None:
        from web_agent.debug import DebugCapture

        dc = DebugCapture(AppConfig(debug={"enabled": True, "debug_dir": str(tmp_path)}))
        p1 = dc._next_artifact_path("fetch", "html")
        p2 = dc._next_artifact_path("fetch", "html")
        assert p1 != p2  # monotonic seq guarantees uniqueness within a microsecond
