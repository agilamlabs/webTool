"""Pipeline orchestrator: search -> fetch -> extract -> download -> automate.

The :class:`Agent` is the main entry point for AI agents to interact with the web.
It composes all subsystems (browser, search, fetch, extract, download, automate,
sessions, recipes) behind a clean async context manager API.

Example::

    from web_agent import Agent

    async with Agent() as agent:
        # Search and extract
        result = await agent.search_and_extract("AI research papers 2025")

        # Fetch a single page
        page = await agent.fetch_and_extract("https://example.com")

        # Download a file
        dl = await agent.download("https://example.com/data.csv")

        # Browser automation
        from web_agent.models import ClickInput, FillInput
        seq = await agent.interact("https://example.com", [
            FillInput(selector="#search", value="query"),
            ClickInput(selector="button[type=submit]"),
        ])

        # Persistent sessions for multi-call workflows (login, etc.)
        sid = await agent.create_session(name="my-login")
        await agent.interact(login_url, login_actions, session_id=sid)
        result = await agent.fetch_and_extract(dashboard_url, session_id=sid)
        await agent.close_session(sid)

        # High-level recipes
        best = await agent.search_and_open_best_result("Python FastAPI tutorial")
        report = await agent.find_and_download_file(
            "Tesla 10-K annual report 2024", file_types=["pdf"]
        )
        research = await agent.web_research("vector databases comparison", max_pages=3)
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional, cast
from urllib.parse import parse_qs, urlparse

from loguru import logger

from .audit import AuditLogger
from .browser_actions import BrowserActions
from .browser_manager import BrowserManager
from .cache import Cache, DiskCache
from .config import AppConfig
from .content_extractor import ContentExtractor
from .correlation import correlation_scope, get_correlation_id
from .debug import DebugCapture
from .downloader import Downloader
from .models import (
    Action,
    ActionResult,
    ActionSequenceResult,
    AgentResult,
    CdpConnectionInfo,
    ClickXYInput,
    CollectionResult,
    DoctorReport,
    DomainSkill,
    DownloadResult,
    ExtractionResult,
    FetchDiagnostic,
    FetchResult,
    FetchStatus,
    FormFilterSpec,
    MetricsSnapshot,
    MouseButton,
    ObserveResult,
    PressKeyInput,
    ResearchResult,
    ScreenshotResult,
    SearchResponse,
    SearchResultItem,
    SelectorLike,
    SessionInfo,
    SkillApplicationResult,
    StorageStateResult,
    TabInfo,
    ToolMessage,
    ToolSeverity,
    TypeTextInput,
)
from .rate_limiter import RateLimiter
from .recipes import Recipes
from .robots import RobotsChecker
from .search_engine import SearchEngine
from .session_manager import SessionManager
from .trace_recorder import is_redacted, redact_sensitive_mapping
from .utils import BudgetTracker, check_domain_allowed
from .web_fetcher import WebFetcher, _url_ext_classification, is_binary_kind


def _query_is_url(query: str) -> bool:
    """Detect a search query that is itself a single URL.

    True iff the (stripped) query starts with ``http://`` or ``https://``
    AND contains no whitespace -- avoids matching natural-language
    queries like "fetch https://example.com please" which should still
    go through the search pipeline.
    """
    s = query.strip()
    return s.startswith(("http://", "https://")) and " " not in s


# Hosts that act as search-engine SERPs. When a caller passes one of
# these as a "URL query", we unwrap the embedded ?q= parameter and run
# our own search instead of fetching the SERP HTML (which is rarely
# useful and triggers anti-bot measures on the SERP host).
_SEARCH_ENGINE_HOST_PATTERNS = (
    "google.",  # google.com, google.co.uk, etc.
    "bing.com",
    "duckduckgo.com",
    "search.brave.com",
    "searx.",  # searx.* (searx.tiekoetter.com, searx.be, etc.)
    "searxng.",
)


# Cap on the unwrapped SERP query length. Real-world search queries are
# < 500 chars; a 1MB ?q= payload (which urllib.parse.parse_qs WILL parse)
# would otherwise propagate through the entire pipeline.
_MAX_UNWRAPPED_QUERY_LEN = 1024


def _unwrap_search_url(query: str) -> Optional[str]:
    """If query is a search-engine SERP URL, return the embedded query string.

    Returns None when the URL is not a recognized SERP or has no ``q`` param.
    Only invoked when ``_query_is_url(query)`` already returned True.

    The unwrapped query is truncated to ``_MAX_UNWRAPPED_QUERY_LEN`` chars
    so a hostile SERP URL with a giant ``q=`` payload cannot poison the
    downstream pipeline.
    """
    try:
        parsed = urlparse(query.strip())
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if not any(p in host for p in _SEARCH_ENGINE_HOST_PATTERNS):
        return None
    qs = parse_qs(parsed.query)
    raw = qs.get("q") or qs.get("query")
    if not raw:
        return None
    inner = raw[0].strip()
    if not inner:
        return None
    if len(inner) > _MAX_UNWRAPPED_QUERY_LEN:
        logger.warning(
            "Unwrapped SERP query truncated from {n} to {cap} chars",
            n=len(inner),
            cap=_MAX_UNWRAPPED_QUERY_LEN,
        )
        inner = inner[:_MAX_UNWRAPPED_QUERY_LEN]
    return inner


_BLOCK_REASON_BY_STATUS = {
    FetchStatus.BLOCKED: "domain_blocked",
    FetchStatus.TIMEOUT: "timeout",
    FetchStatus.HTTP_ERROR: "http_error",
    FetchStatus.NETWORK_ERROR: "network_error",
}


def _block_reason_for(fr: FetchResult) -> Optional[str]:
    """Map a FetchResult onto a coarse-grained block_reason for diagnostics."""
    if fr.status == FetchStatus.SUCCESS:
        return None
    # v1.7.0: a fetch stopped by a bot-mitigation wall carries the
    # ChallengeInfo fingerprint -- surface the specific 'bot_challenge'
    # reason (instead of the generic per-status mapping) so multi-URL
    # flows can branch on it. SUCCESS results with an advisory/settled
    # challenge are excluded by the early return above.
    if fr.challenge is not None:
        return "bot_challenge"
    return _BLOCK_REASON_BY_STATUS.get(fr.status)


class _MessageBag:
    """Internal collector that records warnings / errors with structured codes.

    The hot path (search_and_extract, web_research) records codes at the
    source via :meth:`warn` / :meth:`err`, instead of round-tripping
    through prefix-based classification. Both string and structured
    representations stay in sync because they're produced together.

    Attributes:
        warnings: Legacy free-form string list (kept for back-compat).
        errors: Same shape as warnings, for fatal issues.
        structured_warnings: ToolMessage list at WARNING severity.
        structured_errors: ToolMessage list at ERROR severity.
    """

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.errors: list[str] = []
        self.structured_warnings: list[ToolMessage] = []
        self.structured_errors: list[ToolMessage] = []

    def warn(
        self,
        code: str,
        message: str,
        *,
        url: Optional[str] = None,
        severity: ToolSeverity = ToolSeverity.WARNING,
    ) -> None:
        self.warnings.append(message)
        self.structured_warnings.append(
            ToolMessage(code=code, message=message, url=url, severity=severity)
        )

    def err(
        self,
        code: str,
        message: str,
        *,
        url: Optional[str] = None,
        severity: ToolSeverity = ToolSeverity.ERROR,
    ) -> None:
        self.errors.append(message)
        self.structured_errors.append(
            ToolMessage(code=code, message=message, url=url, severity=severity)
        )


class Agent:
    """Main entry point for the web_agent toolkit.

    Orchestrates browser lifecycle, web search, page fetching, content
    extraction, file downloading, browser automation, persistent sessions,
    and high-level research recipes through a single async context manager.

    Args:
        config: Application configuration. If ``None``, uses all defaults
            (no config file needed).
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        self._config = config or AppConfig()
        # v1.7.0 (Wave 4A): the Agent owns its own metrics registry so two
        # Agents in one process don't share counters. When disabled, every
        # increment is a no-op enforced inside the registry.
        from .metrics import MetricsRegistry

        self._metrics = MetricsRegistry(
            enabled=self._config.metrics.enabled,
            max_label_cardinality=self._config.metrics.max_label_cardinality,
        )
        # v1.6.8: NetworkCollector is created first so every subsystem
        # below can pass it down to its Page handlers. The collector is
        # a pure data sink -- when both capture switches are False (the
        # default), attach() is a no-op and no listeners are registered.
        from .network_collector import NetworkCollector

        self._network_collector = NetworkCollector(self._config.diagnostics)
        # v1.6.8: SessionTraceRecorder writes per-session JSONL action
        # traces under diagnostics.trace_dir. Disabled by default.
        from .trace_recorder import SessionTraceRecorder

        self._trace_recorder = SessionTraceRecorder(self._config.diagnostics, self._config.base_dir)
        self._bm = BrowserManager(
            self._config, network_collector=self._network_collector, metrics=self._metrics
        )
        self._sessions = SessionManager(
            self._bm, self._config, network_collector=self._network_collector
        )
        self._debug = DebugCapture(self._config)

        # Politeness layer: per-host rate gate + robots.txt checker.
        # Both are passed to fetcher/downloader/search so they short-
        # circuit before any network I/O. Each is None when disabled
        # via SafetyConfig so the fast path is a single None check.
        safety = self._config.safety
        self._rate_limiter: RateLimiter | None = (
            RateLimiter(rps_per_host=safety.rate_limit_per_host_rps)
            if safety.rate_limit_per_host_rps > 0
            else None
        )
        self._robots: RobotsChecker | None = (
            RobotsChecker(user_agent=safety.robots_user_agent)
            if safety.respect_robots_txt
            else None
        )

        # Audit log: append-only JSONL of every Agent operation.
        self._audit = AuditLogger(
            path=self._config.audit.audit_log_path,
            enabled=self._config.audit.enabled,
        )

        # Cache: disk-backed TTL cache for fetch + search results.
        # None when disabled so subsystems can short-circuit on a single
        # `if self._cache is not None` check.
        cache_cfg = self._config.cache
        self._cache: Cache | None = (
            DiskCache(
                cache_dir=cache_cfg.cache_dir,
                ttl_seconds=cache_cfg.ttl_seconds,
                max_cache_mb=cache_cfg.max_cache_mb,
            )
            if cache_cfg.enabled
            else None
        )

        self._search = SearchEngine(
            self._bm,
            self._config,
            rate_limiter=self._rate_limiter,
            cache=self._cache,
            circuit_cooldown_s=self._config.search.circuit_cooldown_s,
            metrics=self._metrics,
        )
        self._fetcher = WebFetcher(
            self._bm,
            self._config,
            sessions=self._sessions,
            debug=self._debug,
            rate_limiter=self._rate_limiter,
            robots=self._robots,
            cache=self._cache,
            network_collector=self._network_collector,
            metrics=self._metrics,
        )
        self._extractor = ContentExtractor(self._config)
        self._downloader = Downloader(
            self._bm,
            self._config,
            sessions=self._sessions,
            debug=self._debug,
            rate_limiter=self._rate_limiter,
            robots=self._robots,
            network_collector=self._network_collector,
        )
        self._actions = BrowserActions(
            self._bm,
            self._config,
            sessions=self._sessions,
            debug=self._debug,
            network_collector=self._network_collector,
            trace_recorder=self._trace_recorder,
        )
        self._recipes = Recipes(
            self._search,
            self._fetcher,
            self._extractor,
            self._downloader,
            self._config,
            browser_manager=self._bm,
            sessions=self._sessions,
            actions=self._actions,
        )
        # v1.6.7: domain-skills registry. Loaded lazily so an Agent
        # configured with both skills disabled does no filesystem walk.
        # Always instantiated (cheap when both flags are False -- the
        # constructor short-circuits).
        from .domain_skills import SkillRegistry
        from .workspace import Workspace

        self._skills = SkillRegistry(self._config)
        # v1.6.7: agent-editable workspace. Always instantiated; gates
        # are enforced on read/write, not on construction. Audit-logged
        # when both audit.enabled and workspace.audit_helper_usage are True.
        self._workspace = Workspace(self._config, audit=self._audit)

    @asynccontextmanager
    async def _call_scope(
        self, method: str, args: dict[str, Any] | None = None
    ) -> AsyncIterator[Optional[str]]:
        """Wrap one public Agent call with correlation scope + audit log.

        ``correlation_scope`` generates / reuses a UUID4 that propagates
        through every loguru record made inside the call. ``audit.scope``
        appends a JSONL entry on completion (no-op when audit is disabled).
        Yields the correlation-id so the caller can echo it back into
        result models.
        """
        with correlation_scope() as cid:
            async with self._audit.scope(method, args):
                yield cid

    async def __aenter__(self) -> Agent:
        await self._bm.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        try:
            await self._sessions.close_all()
        except Exception as exc:
            logger.warning("Error closing sessions on exit: {e}", e=exc)
        await self._bm.stop()

    # ------------------------------------------------------------------
    # Pipeline: Search + Fetch + Extract
    # ------------------------------------------------------------------

    async def search_and_extract(
        self,
        query: str,
        max_results: int | None = None,
        *,
        session_id: Optional[str] = None,
        strict: bool = False,
        extract_files: bool = False,
    ) -> AgentResult:
        """Full pipeline: search -> fetch top pages -> extract content.

        Args:
            query: The search query, or a bare URL, or a search-engine
                SERP URL (auto-unwrapped to its embedded ``?q=`` query).
            max_results: Maximum number of results to process.
            session_id: Optional persistent browser session for the fetches.
            strict: If True, raise :class:`SearchError` when every
                configured search provider (SearXNG / DDGS / Playwright)
                returns zero results. Default False (return empty
                AgentResult).
            extract_files: If True, fetch downloadable files (PDF/XLSX)
                inline and extract their text into ``pages`` instead of
                surfacing them in ``download_candidates``. Requires the
                ``[binary]`` extra (pypdf/openpyxl). Default False.

        Returns:
            AgentResult with:
                - ``pages``: extracted text per successfully fetched URL.
                - ``errors``: fatal issues (all fetches failed, no results).
                - ``warnings``: non-fatal issues (blocked domains, partial fetches).
                - ``download_candidates``: skipped file URLs as structured items.
                - ``diagnostics``: per-URL outcome (status, provider, block_reason).

        Raises:
            SearchError: Only when ``strict=True`` and the entire
                provider chain exhausts.
        """
        async with self._call_scope(
            "search_and_extract", {"query": query, "max_results": max_results}
        ) as cid:
            self._debug.reset()
            start = time.perf_counter()
            bag = _MessageBag()
            download_candidates: list[SearchResultItem] = []
            diagnostics: list[FetchDiagnostic] = []
            budget = BudgetTracker(self._config.safety)

            # URL-as-query short-circuit. If the caller passed a bare URL
            # instead of a search query, either unwrap a SERP URL into
            # its embedded query OR fetch + extract the URL directly.
            from .models import SearchResponse

            if _query_is_url(query):
                unwrapped = _unwrap_search_url(query)
                if unwrapped is not None:
                    logger.info(
                        "Search-engine SERP URL detected, unwrapping to query: {q}",
                        q=unwrapped,
                    )
                    query = unwrapped
                    # fall through to the regular search path below
                else:
                    logger.info("Query is a URL, skipping search: {q}", q=query)
                    # v1.6.9: route through fetch_smart (single routing
                    # source of truth). Binary URLs land in fetch_binary;
                    # everything else uses the HTML path. HEAD probe is
                    # gated internally by safety.probe_binary_urls.
                    fr = await self._fetcher.fetch_smart(query, session_id=session_id)

                    url_pages: list[ExtractionResult] = []
                    if fr.status == FetchStatus.SUCCESS and (fr.html or fr.binary):
                        extracted = await self._extractor.extract_async(fr)
                        extracted.correlation_id = cid
                        url_pages.append(extracted)
                        diagnostics.append(
                            FetchDiagnostic(
                                url=query,
                                final_url=fr.final_url,
                                status=fr.status,
                                status_code=fr.status_code,
                                provider="direct",
                                content_length=extracted.content_length,
                                response_time_ms=fr.response_time_ms,
                                from_cache=fr.from_cache,
                            )
                        )
                    else:
                        bag.err(
                            "fetch_failed",
                            f"Failed to fetch {query}: {fr.error_message or 'unknown'}",
                            url=query,
                        )
                        diagnostics.append(
                            FetchDiagnostic(
                                url=query,
                                final_url=fr.final_url,
                                status=fr.status,
                                status_code=fr.status_code,
                                provider="direct",
                                block_reason=_block_reason_for(fr),
                                response_time_ms=fr.response_time_ms,
                                from_cache=fr.from_cache,
                            )
                        )
                    return AgentResult(
                        query=query,
                        search=SearchResponse(query=query),
                        pages=url_pages,
                        errors=bag.errors,
                        warnings=bag.warnings,
                        download_candidates=download_candidates,
                        diagnostics=diagnostics,
                        structured_warnings=bag.structured_warnings,
                        structured_errors=bag.structured_errors,
                        total_time_ms=(time.perf_counter() - start) * 1000,
                        correlation_id=cid,
                    )

            logger.info("Starting pipeline for query: {q}", q=query)
            search_response = await self._search.search(query, max_results, strict=strict)
            logger.info("Search returned {n} results", n=search_response.total_results)

            if not search_response.results:
                bag.err("no_search_results", "No search results found")
                return AgentResult(
                    query=query,
                    search=search_response,
                    errors=bag.errors,
                    warnings=bag.warnings,
                    download_candidates=download_candidates,
                    diagnostics=diagnostics,
                    structured_warnings=bag.structured_warnings,
                    structured_errors=bag.structured_errors,
                    total_time_ms=(time.perf_counter() - start) * 1000,
                    correlation_id=cid,
                )

            # Separate file URLs from page URLs and filter blocked domains.
            # Each result either becomes (a) a page_url to fetch, (b) a
            # download_candidate, or (c) a warning + diagnostic.
            page_items: list[SearchResultItem] = []
            file_items: list[SearchResultItem] = []
            unknown_items: list[SearchResultItem] = []
            for r in search_response.results:
                if not check_domain_allowed(r.url, self._config.safety):
                    bag.warn("domain_blocked", f"Domain blocked: {r.url}", url=r.url)
                    diagnostics.append(
                        FetchDiagnostic(
                            url=r.url,
                            status=FetchStatus.BLOCKED,
                            provider=r.provider,
                            block_reason="domain_blocked",
                        )
                    )
                    continue
                ext_class = _url_ext_classification(r.url)
                if is_binary_kind(ext_class):
                    file_items.append(r)
                elif ext_class == "html":
                    page_items.append(r)
                else:
                    unknown_items.append(r)

            # NEW in 1.6.3: parallel HEAD-probe extensionless URLs so
            # extensionless PDFs / XLSX from search results are routed
            # to fetch_binary instead of failing in HTML extraction.
            # Bounded to one round-trip's worth of latency via gather.
            if unknown_items and self._config.safety.probe_binary_urls:
                probe_tasks = [
                    self._fetcher.classify_url(item.url, session_id=session_id)
                    for item in unknown_items
                ]
                probe_results: list[Any] = await asyncio.gather(
                    *probe_tasks, return_exceptions=True
                )
                for item, classification in zip(unknown_items, probe_results, strict=True):
                    if isinstance(classification, asyncio.CancelledError):
                        # v1.6.16 AG-1: never swallow cancellation. gather(
                        # return_exceptions=True) hands back a CancelledError
                        # as a result object (it is a BaseException, not an
                        # Exception, since Python 3.8). Treating it as a
                        # generic "probe failed -> default to HTML" would mask
                        # a real task cancellation and let search_and_extract
                        # keep running (fetch_many, extraction, holding browser
                        # resources) after the caller cancelled. Re-raise so
                        # cooperative cancellation propagates -- mirrors the
                        # web_research E-4 fix in recipes.py.
                        raise classification
                    if isinstance(classification, BaseException):
                        # Probe failure -> default to HTML, will be caught downstream
                        page_items.append(item)
                    elif is_binary_kind(classification):
                        file_items.append(item)
                    else:
                        # 'html' or 'unknown' -> treat as HTML
                        page_items.append(item)
            else:
                # probe disabled -> extensionless URLs default to HTML
                page_items.extend(unknown_items)

            # Handle file URLs: surface as structured candidates, optionally
            # extract inline if extract_files=True (PDF/XLSX/DOCX/CSV path).
            pages: list[ExtractionResult] = []
            if file_items:
                if extract_files:
                    logger.info(
                        "extract_files=True; running binary extraction on {n} file URLs",
                        n=len(file_items),
                    )
                    for fr_item in file_items:
                        try:
                            budget.check_time()
                            budget.add_page()
                        except Exception as exc:
                            bag.err("budget_exceeded", str(exc))
                            break
                        bin_fr = await self._fetcher.fetch_binary(
                            fr_item.url, session_id=session_id
                        )
                        if bin_fr.binary:
                            extraction = await self._extractor.extract_async(bin_fr)
                            extraction.correlation_id = cid
                            pages.append(extraction)
                            diagnostics.append(
                                FetchDiagnostic(
                                    url=fr_item.url,
                                    final_url=bin_fr.final_url,
                                    status=bin_fr.status,
                                    status_code=bin_fr.status_code,
                                    provider=fr_item.provider,
                                    content_length=extraction.content_length,
                                    response_time_ms=bin_fr.response_time_ms,
                                )
                            )
                            # M2: charge the char budget for binary content
                            # too (mirrors the HTML branch). Without this,
                            # large PDFs/XLSX bypass max_chars_per_call. The
                            # extraction + diagnostic are already recorded
                            # above, so on overflow we just break -- same
                            # net shape as the HTML branch.
                            try:
                                budget.add_chars(extraction.content_length)
                            except Exception as exc:
                                bag.err("budget_exceeded", str(exc))
                                break
                        else:
                            bag.warn(
                                "binary_extraction_failed",
                                f"Binary extraction failed for {fr_item.url}: "
                                f"{bin_fr.error_message or 'no content'}",
                                url=fr_item.url,
                            )
                            diagnostics.append(
                                FetchDiagnostic(
                                    url=fr_item.url,
                                    final_url=bin_fr.final_url,
                                    status=bin_fr.status,
                                    status_code=bin_fr.status_code,
                                    provider=fr_item.provider,
                                    block_reason=_block_reason_for(bin_fr),
                                    response_time_ms=bin_fr.response_time_ms,
                                )
                            )
                else:
                    download_candidates.extend(file_items)
                    if len(file_items) == 1:
                        bag.warn(
                            "download_skipped",
                            "1 downloadable file URL skipped; see download_candidates",
                        )
                    else:
                        bag.warn(
                            "download_skipped",
                            f"{len(file_items)} downloadable file URLs skipped; "
                            f"see download_candidates",
                        )
                    for fi in file_items:
                        diagnostics.append(
                            FetchDiagnostic(
                                url=fi.url,
                                status=FetchStatus.SUCCESS,
                                provider=fi.provider,
                                block_reason="download_skipped",
                            )
                        )

            # M1: bound the fetch fan-out by the *remaining* page budget so
            # max_pages_per_call limits fetching, not just extraction. The
            # binary loop above already spent pages via budget.add_page(),
            # so remaining["pages"] correctly accounts for that. The common
            # case (max_pages_per_call default 50 >= search-result count) is
            # a no-op slice. The post-fetch add_page()/add_chars() checks
            # below stay as belt-and-suspenders.
            remaining_pages = max(0, int(budget.remaining["pages"]))
            # v1.6.16 deep-review fix: also stop the fan-out when the CHAR budget
            # is already spent (the extract_files binary loop above charges chars
            # via budget.add_chars). The slice below only consulted the PAGE
            # dimension, so a char-exhausted call still fetched up to
            # remaining_pages pages over the network -- and extracted one more
            # full page before its own add_chars check tripped -- wasting I/O and
            # overshooting max_chars_per_call. Clamp the fan-out to 0 on char
            # exhaustion so no further network fetches happen.
            if int(budget.remaining["chars"]) <= 0:
                remaining_pages = 0
            if len(page_items) > remaining_pages:
                dropped = len(page_items) - remaining_pages
                bag.warn(
                    "page_budget_truncated",
                    f"{dropped} page URL(s) not fetched: per-call budget reached; "
                    f"fetching {remaining_pages} of {len(page_items)}",
                )
                page_items = page_items[:remaining_pages]
            page_urls = [p.url for p in page_items]
            url_to_provider = {p.url: p.provider for p in page_items}
            logger.info("Fetching {n} pages...", n=len(page_urls))
            fetch_results = await self._fetcher.fetch_many(page_urls, session_id=session_id)

            for fr in fetch_results:
                try:
                    budget.check_time()
                except Exception as exc:
                    bag.err("budget_exceeded", str(exc))
                    break

                provider = url_to_provider.get(fr.url, "unknown")

                if fr.html:
                    try:
                        budget.add_page()
                    except Exception as exc:
                        bag.err("budget_exceeded", str(exc))
                        break
                    extraction = await self._extractor.extract_async(fr)
                    extraction.correlation_id = cid
                    try:
                        budget.add_chars(extraction.content_length)
                    except Exception as exc:
                        bag.err("budget_exceeded", str(exc))
                        pages.append(extraction)
                        diagnostics.append(
                            FetchDiagnostic(
                                url=fr.url,
                                final_url=fr.final_url,
                                status=fr.status,
                                status_code=fr.status_code,
                                provider=provider,
                                content_length=extraction.content_length,
                                response_time_ms=fr.response_time_ms,
                                from_cache=fr.from_cache,
                            )
                        )
                        break
                    pages.append(extraction)
                    diagnostics.append(
                        FetchDiagnostic(
                            url=fr.url,
                            final_url=fr.final_url,
                            status=fr.status,
                            status_code=fr.status_code,
                            provider=provider,
                            content_length=extraction.content_length,
                            response_time_ms=fr.response_time_ms,
                            from_cache=fr.from_cache,
                        )
                    )
                else:
                    bag.warn(
                        "fetch_failed",
                        f"Failed to fetch {fr.url}: {fr.error_message}",
                        url=fr.url,
                    )
                    diagnostics.append(
                        FetchDiagnostic(
                            url=fr.url,
                            final_url=fr.final_url,
                            status=fr.status,
                            status_code=fr.status_code,
                            provider=provider,
                            block_reason=_block_reason_for(fr),
                            response_time_ms=fr.response_time_ms,
                            from_cache=fr.from_cache,
                        )
                    )

            # Promote "no usable pages at all" from warnings to a fatal
            # error so callers checking `if not result.errors` behave
            # correctly.
            if not pages and page_urls and not bag.errors:
                bag.err(
                    "all_fetches_failed",
                    "All page fetches failed; see warnings/diagnostics for detail",
                )

            elapsed = (time.perf_counter() - start) * 1000
            logger.info(
                "Pipeline complete: {n} pages, {w} warnings, {e} errors in {t:.0f}ms",
                n=len(pages),
                w=len(bag.warnings),
                e=len(bag.errors),
                t=elapsed,
            )

            return AgentResult(
                query=query,
                search=search_response,
                pages=pages,
                errors=bag.errors,
                warnings=bag.warnings,
                download_candidates=download_candidates,
                diagnostics=diagnostics,
                structured_warnings=bag.structured_warnings,
                structured_errors=bag.structured_errors,
                total_time_ms=elapsed,
                correlation_id=cid,
            )

    # ------------------------------------------------------------------
    # Single URL: Fetch + Extract
    # ------------------------------------------------------------------

    async def fetch_and_extract(
        self,
        url: str,
        *,
        session_id: Optional[str] = None,
        strict: bool = False,
        binary_probe: bool = True,
    ) -> ExtractionResult:
        """Fetch a single URL and extract its content.

        Smart routing (NEW in 1.6.2):
          1. Known download extension (``.pdf``, ``.xlsx``, ``.docx``,
             ``.csv``, ...) -> :meth:`WebFetcher.fetch_binary`.
          2. ``binary_probe=True`` AND HEAD response indicates binary
             (Content-Type or Content-Disposition: attachment) ->
             :meth:`WebFetcher.fetch_binary`.
          3. Otherwise -> :meth:`WebFetcher.fetch` (HTML path).

        Args:
            url: The URL to fetch.
            session_id: Optional persistent browser session.
            strict: If True, raise :class:`NavigationError` when the fetch
                fails (HTTP error, timeout, blocked, etc.). Default False
                (return ExtractionResult with extraction_method="none").
            binary_probe: If True and the URL has no known download
                extension, send a HEAD request to detect extensionless
                PDF/XLSX/DOCX/CSV documents via Content-Type. Adds one
                round-trip but recovers many real-world document URLs.
                Disable to rely solely on URL extension.

        Raises:
            NavigationError: Only when ``strict=True`` and fetch fails.
        """
        from .exceptions import NavigationError

        async with self._call_scope("fetch_and_extract", {"url": url}) as cid:
            self._debug.reset()
            logger.info("Fetching and extracting: {url}", url=url)

            # v1.6.9: single source of truth for binary-vs-HTML routing
            # is WebFetcher.fetch_smart. Prior versions duplicated the
            # if/elif/else across fetch_and_extract / search_and_extract /
            # recipes; v1.6.9 consolidates to keep behavior consistent.
            fr = await self._fetcher.fetch_smart(
                url, session_id=session_id, binary_probe=binary_probe
            )

            if strict and fr.status != FetchStatus.SUCCESS:
                raise NavigationError(
                    f"Fetch failed: {fr.error_message}",
                    url=fr.url,
                    status_code=fr.status_code,
                )
            extraction = await self._extractor.extract_async(fr)
            extraction.correlation_id = cid
            return extraction

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def download(
        self,
        url: str,
        filename: str | None = None,
        *,
        session_id: Optional[str] = None,
        strict: bool = False,
    ) -> DownloadResult:
        """Download a file from a URL.

        Args:
            url: The file URL to download.
            filename: Optional output filename.
            session_id: Optional persistent browser session.
            strict: If True, raise :class:`DownloadError` on failure.

        Raises:
            DownloadError: Only when ``strict=True`` and the download fails.
        """
        from .exceptions import DownloadError

        async with self._call_scope("download", {"url": url, "filename": filename}):
            self._debug.reset()
            result = await self._downloader.download(url, filename, session_id=session_id)
            if strict and result.status != FetchStatus.SUCCESS:
                raise DownloadError(f"Download failed: {result.error_message}", url=result.url)
            return result

    # ------------------------------------------------------------------
    # Browser Automation
    # ------------------------------------------------------------------

    async def interact(
        self,
        url: str,
        actions: list[Action],
        stop_on_error: bool | None = None,
        *,
        session_id: Optional[str] = None,
    ) -> ActionSequenceResult:
        """Execute a scripted sequence of browser actions on a URL."""
        async with self._call_scope("interact", {"url": url, "n_actions": len(actions)}):
            self._debug.reset()
            logger.info(
                "Starting interaction sequence on {url} ({n} actions, session={s})",
                url=url,
                n=len(actions),
                s=session_id or "ephemeral",
            )
            return await self._actions.execute_sequence(
                url, actions, stop_on_error=stop_on_error, session_id=session_id
            )

    async def screenshot(
        self,
        url: str,
        path: str | None = None,
        full_page: bool = False,
        *,
        session_id: Optional[str] = None,
    ) -> ScreenshotResult:
        """Navigate to a URL and take a screenshot."""
        async with self._call_scope("screenshot", {"url": url, "full_page": full_page}):
            self._debug.reset()
            logger.info("Taking screenshot of {url}", url=url)
            return await self._actions.take_screenshot(url, path, full_page, session_id=session_id)

    # ------------------------------------------------------------------
    # Browser Sessions
    # ------------------------------------------------------------------

    async def create_session(self, name: str | None = None) -> str:
        """Create a persistent browser session and return its session_id.

        Pass the session_id to subsequent fetch/download/screenshot/interact
        calls to retain cookies, localStorage, and origin tokens. Sessions
        live until ``close_session`` or until the Agent context exits.
        """
        async with self._call_scope("create_session", {"name": name}):
            return await self._sessions.create(name=name)

    async def close_session(self, session_id: str) -> None:
        """Close and discard a persistent browser session."""
        async with self._call_scope("close_session", {"session_id": session_id}):
            await self._sessions.close(session_id)

    def list_sessions(self) -> list[SessionInfo]:
        """Return SessionInfo snapshots for all live sessions."""
        return self._sessions.list()

    def metrics(self) -> MetricsSnapshot:
        """Return a point-in-time snapshot of this Agent's in-process metrics.

        v1.7.0 (Wave 4A): cheap to call. Reflects counters accumulated since
        start -- fetch totals + outcomes, bot-wall detections, search provider
        results + circuit trips, and browser launch/crash/relaunch -- plus
        ``bytes_downloaded`` / ``ttfb_ms`` distributions. Returns an empty
        snapshot when ``metrics.enabled`` is False.
        """
        raw = self._metrics.snapshot()
        return MetricsSnapshot(
            enabled=bool(raw.get("enabled", False)),
            counters=cast("dict[str, int]", raw.get("counters", {})),
            distributions=cast("dict[str, dict[str, float]]", raw.get("distributions", {})),
            uptime_s=cast("float", raw.get("uptime_s", 0.0)),
            correlation_id=get_correlation_id(),
        )

    async def export_session_state(
        self, session_id: str, path: str | Path
    ) -> StorageStateResult:
        """Capture a logged-in session's auth (cookies + origins) to a file.

        v1.7.0: after a login has been performed on ``session_id`` (by a
        human in a headed session, or by the agent), this saves Playwright's
        portable storage_state to ``path`` so a later run can reuse the
        authentication without re-entering credentials or 2FA. ``path`` is
        confined to the download directory -- traversal / absolute escapes
        are rejected. Pair with :meth:`import_session_state`.

        Raises:
            KeyError: If ``session_id`` is unknown or no longer live.
        """
        async with self._call_scope(
            "export_session_state", {"session_id": session_id}
        ):
            return await self._sessions.export_state(session_id, path)

    async def import_session_state(
        self, path: str | Path, *, name: str | None = None
    ) -> str:
        """Create a NEW session pre-loaded with auth saved by export.

        v1.7.0: rehydrates the storage_state file written by
        :meth:`export_session_state` into a fresh session so the agent is
        already authenticated. Cookies are restored; per-origin
        localStorage is best-effort. Returns the new ``session_id`` to pass
        to subsequent fetch/interact calls. ``path`` is confined to the
        download directory.
        """
        async with self._call_scope("import_session_state", {"name": name}):
            return await self._sessions.import_state(path, name=name)

    async def search(
        self,
        query: str,
        max_results: int | None = None,
        *,
        strict: bool = False,
    ) -> SearchResponse:
        """Links-only web search: SERP items (title/url/snippet), NO fetch.

        v1.7.0: the cheap entry point -- search, read snippets, then fetch
        only the 1-2 URLs you actually want with :meth:`fetch_and_extract`.
        Contrast :meth:`search_and_extract`, which fetches AND extracts
        every result (N browser page loads + the token cost of every page
        body). On an empty result, ``SearchResponse.search_blocked``
        distinguishes provider blocking (CAPTCHA / rate-limit / breaker
        cooldown) from a genuine no-hits answer.

        Raises:
            SearchError: Only when ``strict=True`` and the entire provider
                chain is exhausted or blocked.
        """
        async with self._call_scope(
            "search", {"query": query, "max_results": max_results}
        ):
            outcome = await self._search.search_with_outcome(
                query, max_results, strict=strict
            )
            response = outcome.response
            response.search_blocked = outcome.blocked
            return response

    async def scroll_to_bottom(
        self,
        *,
        session_id: str,
        tab_id: str | None = None,
        max_scrolls: int | None = None,
        settle_ms: int | None = None,
        stable_rounds: int | None = None,
    ) -> ActionResult:
        """Scroll a session tab to exhaustion so lazy / infinite-scroll content loads.

        v1.7.0: repeatedly scrolls to the bottom (waiting ``settle_ms`` for
        lazy content) until the page stops growing for ``stable_rounds``
        rounds or ``max_scrolls`` is hit (bounded). Pair with
        ``observe`` / ``fetch_and_extract`` on the SAME tab to read the
        full assembled DOM. Returns scrolls_used + reached_bottom.
        """
        async with self._call_scope(
            "scroll_to_bottom",
            {"session_id": session_id, "tab_id": tab_id, "max_scrolls": max_scrolls},
        ):
            return await self._actions.scroll_to_bottom(
                session_id=session_id,
                tab_id=tab_id,
                max_scrolls=max_scrolls,
                settle_ms=settle_ms,
                stable_rounds=stable_rounds,
            )

    async def collect_across_pages(
        self,
        url: str,
        *,
        strategy: str = "next_link",
        max_pages: int | None = None,
        session_id: str | None = None,
        next_texts: list[str] | None = None,
        settle_ms: int | None = None,
        stable_rounds: int | None = None,
        max_scrolls: int | None = None,
    ) -> CollectionResult:
        """Collect a full multi-page listing into one result.

        v1.7.0: walks a paginated / infinite-scroll listing and assembles the
        extracted content across pages, so below-the-fold and next-page items
        are not missed. ``strategy``:

        - ``"next_link"`` (default): follow the page's "next" control
          (rel=next, an aria-label*=next link, or an anchor whose text
          matches ``next_texts``) until there is none, a cycle, ``max_pages``,
          or budget exhaustion.
        - ``"page_param"``: increment a ``?page=`` / ``?p=`` query param until
          an empty or duplicate page.
        - ``"scroll"``: a single infinite-scroll URL -- scroll to exhaustion
          then extract once. Requires ``session_id``.

        Every page is fetched + extracted through the normal pipeline, so
        robots, rate limiting, bot-wall detection, injection-sanitize, and
        SSRF gating all apply. Pages are de-duplicated (URL + content) and the
        walk is bounded by ``max_pages`` and the per-call budget.
        """
        async with self._call_scope(
            "collect_across_pages",
            {"url": url, "strategy": strategy, "max_pages": max_pages},
        ):
            self._debug.reset()
            return await self._recipes.collect_across_pages(
                url,
                strategy=strategy,
                max_pages=max_pages,
                session_id=session_id,
                next_texts=next_texts,
                settle_ms=settle_ms,
                stable_rounds=stable_rounds,
                max_scrolls=max_scrolls,
            )

    # ------------------------------------------------------------------
    # v1.6.6 Tabs (Feature 3)
    # ------------------------------------------------------------------

    async def list_tabs(self, session_id: str) -> list[TabInfo]:
        """Return snapshots of every tab in a session.

        The active tab (the one execute_sequence will target by default)
        has ``active=True``. Use ``switch_tab`` to change it.
        """
        async with self._call_scope("list_tabs", {"session_id": session_id}):
            tm = self._sessions.get_tab_manager(session_id)
            return await tm.list()

    async def current_tab(self, session_id: str) -> Optional[TabInfo]:
        """Return the active tab's snapshot, or None if the session has none."""
        async with self._call_scope("current_tab", {"session_id": session_id}):
            tm = self._sessions.get_tab_manager(session_id)
            tabs = await tm.list()
            for t in tabs:
                if t.active:
                    return t
            return None

    async def new_tab(
        self,
        url: str | None = None,
        *,
        session_id: str,
    ) -> str:
        """Open a fresh tab in a session and return its tab_id.

        The new tab becomes the session's active tab. If ``url`` is
        provided, the tab navigates to it.

        Raises:
            DomainNotAllowedError: if ``url`` is set and the host fails
                ``SafetyConfig.allowed_domains`` / ``denied_domains`` /
                ``block_private_ips`` checks. This matches the SSRF
                protection applied to ``fetch_and_extract`` /
                ``interact`` / ``observe`` -- ``new_tab`` cannot be a
                back door into the private-IP space.
        """
        async with self._call_scope("new_tab", {"url": url, "session_id": session_id}):
            if url is not None and not check_domain_allowed(url, self._config.safety):
                from .exceptions import DomainNotAllowedError

                host = urlparse(url).hostname or ""
                raise DomainNotAllowedError(
                    f"new_tab: domain not allowed: {host!r}", url=url, host=host
                )
            tm = self._sessions.get_tab_manager(session_id)
            tid = await tm.new_tab(url=url)
            # v1.6.x H4: the INPUT url gate above cannot catch a server-side
            # redirect from a whitelisted host into private/denied space.
            # Re-check the tab's *landed* url; if it is now forbidden, close
            # the just-created tab so it isn't left parked on the forbidden
            # host, then signal denial identically to the input-url gate.
            if url is not None:
                page = tm.get_or_current(tid)
                landed = page.url if page is not None else ""
                landed_host = urlparse(landed).hostname or ""
                # Deep-review fix: gate the denial on a NON-EMPTY landed host.
                # ``TabManager.new_tab`` deliberately swallows an uncommitted
                # ``page.goto`` failure (DNS error, connection refused, a
                # download-triggering URL, a timeout before commit) and leaves
                # the page parked on ``about:blank`` -- a hostless url.
                # ``check_domain_allowed`` rejects that as "no host", so the
                # old unconditional re-gate turned a transient network failure
                # on a perfectly ALLOWED host into a spurious
                # ``DomainNotAllowedError`` (empty host) AND destroyed the tab
                # the TabManager intentionally keeps open for retry. A genuine
                # redirect into denied/private space always has a host, so we
                # only raise + close when ``landed_host`` is non-empty.
                if (
                    page is not None
                    and landed_host
                    and not check_domain_allowed(landed, self._config.safety)
                ):
                    from .exceptions import DomainNotAllowedError

                    await tm.close_tab(tid)
                    raise DomainNotAllowedError(
                        f"new_tab: domain not allowed after redirect: {landed_host!r}",
                        url=landed,
                        host=landed_host,
                    )
            return tid

    async def switch_tab(self, tab_id: str, *, session_id: str) -> None:
        """Make ``tab_id`` the active tab. Brings it to front when possible."""
        async with self._call_scope("switch_tab", {"tab_id": tab_id, "session_id": session_id}):
            tm = self._sessions.get_tab_manager(session_id)
            await tm.switch_tab(tab_id)

    async def close_tab(self, tab_id: str, *, session_id: str) -> None:
        """Close a tab. If it was the active tab, another tab becomes active."""
        async with self._call_scope("close_tab", {"tab_id": tab_id, "session_id": session_id}):
            tm = self._sessions.get_tab_manager(session_id)
            await tm.close_tab(tab_id)

    # ------------------------------------------------------------------
    # v1.6.6 Coordinate-level fallbacks (Feature 4)
    # ------------------------------------------------------------------

    async def click_xy(
        self,
        x: float,
        y: float,
        *,
        session_id: str,
        tab_id: Optional[str] = None,
        button: str = "left",
        clicks: int = 1,
        delay: int = 0,
    ) -> ActionResult:
        """Click at viewport coordinates (CSS pixels) on a session's tab.

        Requires a live session_id -- coord clicks are meaningful only
        against an observed page (see :meth:`observe`). Bypasses selector
        resolution; pair with ``observe()`` for verify-after-act loops.
        """
        async with self._call_scope(
            "click_xy", {"x": x, "y": y, "session_id": session_id, "tab_id": tab_id}
        ):
            action = ClickXYInput(
                x=x,
                y=y,
                button=MouseButton(button),
                clicks=clicks,
                delay=delay,
                tab_id=tab_id,
            )
            return await self._actions.execute_single_on_session(
                action, session_id=session_id, tab_id=tab_id
            )

    async def type_text(
        self,
        text: str,
        *,
        session_id: str,
        tab_id: Optional[str] = None,
        delay: int = 0,
    ) -> ActionResult:
        """Type ``text`` into whatever currently has keyboard focus.

        Pair with a preceding ``click_xy`` (or any focusing action) to
        direct keystrokes at the right element. Requires session_id.
        """
        async with self._call_scope(
            "type_text", {"length": len(text), "session_id": session_id, "tab_id": tab_id}
        ):
            action = TypeTextInput(text=text, delay=delay, tab_id=tab_id)
            return await self._actions.execute_single_on_session(
                action, session_id=session_id, tab_id=tab_id
            )

    async def press_key(
        self,
        key: str,
        *,
        session_id: str,
        tab_id: Optional[str] = None,
        modifiers: list[str] | None = None,
    ) -> ActionResult:
        """Press ``key`` (with optional modifiers) at page level.

        Modifiers list values: ``'Shift'``, ``'Control'``, ``'Alt'``, ``'Meta'``.
        Requires session_id.
        """
        async with self._call_scope(
            "press_key", {"key": key, "session_id": session_id, "tab_id": tab_id}
        ):
            action = PressKeyInput(
                key=key,
                modifiers=list(modifiers) if modifiers else [],
                tab_id=tab_id,
            )
            return await self._actions.execute_single_on_session(
                action, session_id=session_id, tab_id=tab_id
            )

    # ------------------------------------------------------------------
    # v1.6.6 Observe (Feature 5)
    # ------------------------------------------------------------------

    async def observe(
        self,
        url: Optional[str] = None,
        *,
        session_id: Optional[str] = None,
        tab_id: Optional[str] = None,
        include_text: bool = True,
        include_aria: bool = False,
        include_elements: bool = True,
    ) -> ObserveResult:
        """Capture a page's visual + structural state.

        Use cases:
          * Decide where to click next (returns ``screenshot_path``,
            ``device_pixel_ratio`` for screenshot-to-CSS-px mapping).
          * Verify the result of a previous action ran successfully
            (URL, title, visible_text, scroll position).
          * Snapshot the accessibility tree for assistive flows
            (``include_aria=True``).

        Args:
            url: Open this URL (ephemeral page if no session_id; or
                navigate the session's current tab to it). Optional when
                session_id is provided -- omit to observe the current
                state in place.
            session_id: Live session whose tab to observe.
            tab_id: Specific tab to observe within the session.
            include_text: Capture ``document.body.innerText`` (truncated
                to safety.max_chars_per_call). Default True.
            include_aria: Capture ``page.accessibility.snapshot()``.
                Default False (snapshots can be megabytes).
            include_elements: Enumerate a bounded, numbered "set of marks"
                list of interactive elements (ref/role/name/bbox) in
                ``ObserveResult.elements``. Pass an element's ``ref`` back as
                a ``LocatorSpec(ref=...)`` to act on it without guessing a
                selector. Default True.
        """
        async with self._call_scope(
            "observe", {"url": url, "session_id": session_id, "tab_id": tab_id}
        ):
            return await self._actions.observe(
                url,
                session_id=session_id,
                tab_id=tab_id,
                include_text=include_text,
                include_aria=include_aria,
                include_elements=include_elements,
            )

    # ------------------------------------------------------------------
    # v1.6.6 CDP endpoint accessor (Feature 2)
    # ------------------------------------------------------------------

    def get_cdp_endpoint(self) -> str | None:
        """Return the CDP WebSocket endpoint of webTool's browser, or None.

        Returns ``None`` when ``browser.cdp_enabled=False`` or before
        the browser has started. External CDP tools (chrome://inspect,
        custom debuggers) can connect to this endpoint -- webTool never
        attaches to other endpoints.
        """
        return self._bm.get_cdp_endpoint()

    def get_owned_cdp_connection_info(self) -> CdpConnectionInfo | None:
        """v1.6.10: return the full CDP attach bundle, or None.

        Bundles the three values a co-resident ``remote_cdp`` Agent
        needs (cdp_url, profile_dir, ownership_token) so callers don't
        have to discover the corresponding ``BrowserManager`` getters
        separately. Returns ``None`` unless all three are available --
        that is, the Agent is a started, isolated, ``cdp_owned`` launch.

        Use :meth:`get_cdp_endpoint` if you only need the WS URL (for
        external chrome://inspect tools, which don't require the
        ownership token).
        """
        url = self._bm.get_cdp_endpoint()
        profile = self._bm.get_effective_profile_dir()
        token = self._bm.get_ownership_token()
        if url is None or profile is None or token is None:
            return None
        return CdpConnectionInfo(
            cdp_url=url,
            profile_dir=str(profile),
            ownership_token=token,
        )

    # ------------------------------------------------------------------
    # v1.6.6 Doctor (Feature 6)
    # ------------------------------------------------------------------

    async def doctor(self, *, quick: bool = False) -> DoctorReport:
        """Run a self-diagnostic and return a structured report.

        Probes Python/web_agent versions, Playwright + Chromium install,
        optional providers (DDGS, SearXNG), MCP, binary extras
        (pypdf/openpyxl/python-docx), directory writability, and basic
        network connectivity.

        Bypasses ``_call_scope`` (no audit log entry) and SafetyConfig
        gates by design -- doctor is a capability self-check, not a
        regular operation.

        Args:
            quick: Skip the actual chromium.launch test (saves ~3-5s).
        """
        from .doctor import run_doctor

        return await run_doctor(self._config, quick=quick)

    # ------------------------------------------------------------------
    # v1.6.7 Domain Skills (Features 1+2+3)
    # ------------------------------------------------------------------

    def list_domain_skills(self) -> list[DomainSkill]:
        """Return every domain skill registered in this Agent's registry.

        Skills come from three tiers (priority: project > workspace >
        builtin). Returns both runnable bundled skills and informational
        user markdown skills. Sort order is unspecified.
        """
        return self._skills.list_all()

    def get_domain_skills(self, url: str) -> list[DomainSkill]:
        """Return skills matching the host of ``url`` (suffix match).

        Example: ``sec.gov`` matches ``www.sec.gov``,
        ``cgi-bin.sec.gov``. The host suffix must align on a label
        boundary, so ``ec.europa.eu`` won't accidentally match
        ``not-ec.europa.eu``.
        """
        return self._skills.get_for_url(url)

    async def apply_domain_skill(
        self,
        url: str,
        name: str,
        inputs: dict[str, Any] | None = None,
    ) -> SkillApplicationResult:
        """Dispatch a runnable skill against ``url`` with ``inputs``.

        Resolves the most-specific matching skill for the URL's domain
        (longest domain wins on ambiguity). Validates ``inputs`` against
        the skill's declared schema, then invokes the bundled Python
        runner.

        Raises:
            SkillNotFoundError: no matching skill for this URL + name.
            SkillNotRunnableError: skill is markdown-only (informational).
            SkillInputError: caller-supplied inputs failed validation.
        """
        # v1.6.16 AG-2: domain skills are exactly where authenticated/login
        # flows live, so the free-form ``inputs`` dict routinely carries
        # passwords / tokens / API keys. The audit sink (AuditLogger.scope)
        # persists args verbatim to audit.jsonl with no redaction, so pass a
        # key-name-redacted copy instead of the raw dict. Reuses
        # ``redact_sensitive_mapping`` from trace_recorder so the audit and
        # trace sinks share one secret-handling convention; the record's
        # shape is unchanged -- only sensitive values become the sentinel.
        # The UN-redacted ``inputs`` still flows to the skill runner below.
        async with self._call_scope(
            "apply_domain_skill",
            {"url": url, "name": name, "inputs": redact_sensitive_mapping(inputs)},
        ):
            return await self._skills.apply(self, url, name, inputs or {})

    # ------------------------------------------------------------------
    # v1.6.7 Interaction Skill Library (Feature 5)
    # ------------------------------------------------------------------

    async def handle_dialog(
        self,
        action: str = "accept",
        prompt_text: str | None = None,
        *,
        session_id: str,
        tab_id: Optional[str] = None,
    ) -> ActionResult:
        """Pre-arm the next browser dialog (alert/confirm/prompt) handler.

        Wraps ``DialogInput`` to keep a single source of truth for the
        per-page WeakKeyDictionary state. ``action`` is ``'accept'`` or
        ``'dismiss'``; ``prompt_text`` populates a prompt() box.
        """
        from .models import DialogInput, DialogResponse

        async with self._call_scope(
            "handle_dialog",
            {"action": action, "session_id": session_id, "tab_id": tab_id},
        ):
            di = DialogInput(
                dialog_action=DialogResponse(action),
                prompt_text=prompt_text,
                tab_id=tab_id,
            )
            return await self._actions.execute_single_on_session(
                di, session_id=session_id, tab_id=tab_id
            )

    async def select_dropdown(
        self,
        selector: SelectorLike,
        *,
        session_id: str,
        tab_id: Optional[str] = None,
        value: str | None = None,
        label: str | None = None,
        index: int | None = None,
    ) -> ActionResult:
        """Select an option from a ``<select>`` element.

        Pass exactly one of ``value`` / ``label`` / ``index``. Wraps the
        existing ``SelectInput`` action so the SELECT handler's dispatch
        path is reused.
        """
        from .models import SelectInput

        async with self._call_scope(
            "select_dropdown",
            {
                "session_id": session_id,
                "tab_id": tab_id,
                "value": value,
                "label": label,
                "index": index,
            },
        ):
            si = SelectInput(
                selector=selector,
                value=value,
                label=label,
                index=index,
                tab_id=tab_id,
            )
            return await self._actions.execute_single_on_session(
                si, session_id=session_id, tab_id=tab_id
            )

    async def upload_file(
        self,
        selector: SelectorLike,
        paths: str | list[str],
        *,
        session_id: str,
        tab_id: Optional[str] = None,
    ) -> ActionResult:
        """Upload one or more files to an ``<input type="file">``.

        Path safety: each path is validated against
        ``download.download_dir`` unless
        ``safety.allow_upload_outside_download_dir=True``. Blocks
        prompt-injection from uploading arbitrary local files.
        """
        from .models import UploadFileInput

        path_list = [paths] if isinstance(paths, str) else list(paths)
        async with self._call_scope(
            "upload_file",
            {"session_id": session_id, "tab_id": tab_id, "n_paths": len(path_list)},
        ):
            uf = UploadFileInput(selector=selector, paths=path_list, tab_id=tab_id)
            return await self._actions.execute_single_on_session(
                uf, session_id=session_id, tab_id=tab_id
            )

    async def drag_and_drop(
        self,
        source: SelectorLike,
        target: SelectorLike,
        *,
        session_id: str,
        tab_id: Optional[str] = None,
    ) -> ActionResult:
        """Drag from one element and drop on another."""
        from .models import DragAndDropInput

        async with self._call_scope("drag_and_drop", {"session_id": session_id, "tab_id": tab_id}):
            dd = DragAndDropInput(source=source, target=target, tab_id=tab_id)
            return await self._actions.execute_single_on_session(
                dd, session_id=session_id, tab_id=tab_id
            )

    async def scroll_until_text(
        self,
        text: str,
        *,
        session_id: str,
        tab_id: Optional[str] = None,
        max_scrolls: int = 10,
        scroll_step: int = 800,
    ) -> ActionResult:
        """Scroll a session's tab in fixed increments until ``text``
        appears in the visible page, or ``max_scrolls`` is exhausted.

        Useful for infinite-scroll feeds.
        """
        async with self._call_scope(
            "scroll_until_text",
            {"text": text[:80], "session_id": session_id, "tab_id": tab_id},
        ):
            return await self._actions.scroll_until_text(
                text,
                session_id=session_id,
                tab_id=tab_id,
                max_scrolls=max_scrolls,
                scroll_step=scroll_step,
            )

    async def click_inside_iframe(
        self,
        iframe_selector: str,
        inner_selector: str,
        *,
        session_id: str,
        tab_id: Optional[str] = None,
    ) -> ActionResult:
        """Click a target inside a same-origin iframe.

        Uses Playwright's ``frame_locator`` to scope the click into the
        iframe document. Cross-origin iframes raise -- use coord-click
        as the fallback.
        """
        from .models import IframeClickInput

        async with self._call_scope(
            "click_inside_iframe",
            {
                "iframe_selector": iframe_selector,
                "inner_selector": inner_selector,
                "session_id": session_id,
                "tab_id": tab_id,
            },
        ):
            ic = IframeClickInput(
                iframe_selector=iframe_selector,
                inner_selector=inner_selector,
                tab_id=tab_id,
            )
            return await self._actions.execute_single_on_session(
                ic, session_id=session_id, tab_id=tab_id
            )

    async def click_shadow_dom(
        self,
        host_selector: str,
        inner_selector: str,
        *,
        session_id: str,
        tab_id: Optional[str] = None,
    ) -> ActionResult:
        """Click an element inside a shadow DOM tree using Playwright's
        pierce-selector chain (``host >> inner``)."""
        from .models import ShadowDomClickInput

        async with self._call_scope(
            "click_shadow_dom",
            {
                "host_selector": host_selector,
                "inner_selector": inner_selector,
                "session_id": session_id,
                "tab_id": tab_id,
            },
        ):
            sd = ShadowDomClickInput(
                host_selector=host_selector,
                inner_selector=inner_selector,
                tab_id=tab_id,
            )
            return await self._actions.execute_single_on_session(
                sd, session_id=session_id, tab_id=tab_id
            )

    async def print_page_as_pdf(
        self,
        url: str | None = None,
        output_path: str | None = None,
        *,
        session_id: str | None = None,
        tab_id: Optional[str] = None,
    ) -> ScreenshotResult:
        """Render the current page (or ``url``) as PDF via Chromium's
        ``page.pdf()``. Output goes under ``automation.screenshot_dir``
        unless ``output_path`` is absolute. Returns the same
        ``ScreenshotResult`` shape used by ``Agent.screenshot``."""
        async with self._call_scope(
            "print_page_as_pdf",
            {"url": url, "session_id": session_id, "tab_id": tab_id},
        ):
            return await self._actions.print_page_as_pdf(
                url=url,
                output_path=output_path,
                session_id=session_id,
                tab_id=tab_id,
            )

    # ------------------------------------------------------------------
    # High-Level Recipes
    # ------------------------------------------------------------------

    async def search_and_open_best_result(
        self,
        query: str,
        ranking: str = "default",
        *,
        session_id: Optional[str] = None,
        prefer_domains: Optional[list[str]] = None,
        domain_profile: Optional[str] = None,
    ) -> ExtractionResult:
        """Recipe: search, rank results, fetch + extract the top hit.

        Args:
            prefer_domains: Optional caller-supplied host hints (e.g.
                ``["ec.europa.eu", "esma.europa.eu"]``); matching results
                receive a strong ranking bonus.
            domain_profile: Optional named ranking profile -- one of
                ``"official_sources" | "docs" | "research" | "news" | "files"``.
                Combined with ``prefer_domains`` to form the hint set.
        """
        async with self._call_scope(
            "search_and_open_best_result", {"query": query, "ranking": ranking}
        ) as cid:
            self._debug.reset()
            result = await self._recipes.search_and_open_best_result(
                query,
                ranking,
                session_id,
                prefer_domains=prefer_domains,
                domain_profile=domain_profile,
            )
            result.correlation_id = cid
            return result

    async def find_and_download_file(
        self,
        query: str,
        file_types: list[str] | None = None,
        *,
        session_id: Optional[str] = None,
    ) -> DownloadResult:
        """Recipe: search, find the first matching file URL, download it."""
        async with self._call_scope(
            "find_and_download_file", {"query": query, "file_types": file_types}
        ) as cid:
            self._debug.reset()
            result = await self._recipes.find_and_download_file(query, file_types, session_id)
            result.correlation_id = cid
            return result

    async def fill_form_and_extract(
        self,
        url: str,
        spec: FormFilterSpec,
        *,
        session_id: Optional[str] = None,
    ) -> ExtractionResult:
        """Recipe: open URL, fill a search/filter form, then extract content.

        Targets dynamic calendar / regulator-filings / event-listing pages
        where content is gated behind a search box and/or filter controls.
        See :class:`FormFilterSpec` for the locator/value contract.
        """
        async with self._call_scope("fill_form_and_extract", {"url": url}) as cid:
            self._debug.reset()
            result = await self._recipes.fill_form_and_extract(url, spec, session_id=session_id)
            result.correlation_id = cid
            return result

    async def web_research(
        self,
        query: str,
        depth: int = 1,
        max_pages: int = 5,
        *,
        session_id: Optional[str] = None,
        prefer_domains: Optional[list[str]] = None,
        domain_profile: Optional[str] = None,
        extract_files: bool = False,
    ) -> ResearchResult:
        """Recipe: search + parallel fetch + extract top N pages, return citations.

        Args:
            prefer_domains: Optional caller-supplied host hints; matching
                results receive a strong ranking bonus.
            domain_profile: Optional named ranking profile -- one of
                ``"official_sources" | "docs" | "research" | "news" | "files"``.
            extract_files: v1.6.10. When True, file URLs (PDF/XLSX/...)
                are extracted inline through ``fetch_smart`` instead of
                being routed to ``download_candidates``. Default False
                preserves the v1.6.9 read-pages-only behaviour. Mirrors
                :meth:`search_and_extract` ``extract_files``.
        """
        async with self._call_scope(
            "web_research",
            {
                "query": query,
                "depth": depth,
                "max_pages": max_pages,
                "extract_files": extract_files,
            },
        ) as cid:
            self._debug.reset()
            result = await self._recipes.web_research(
                query,
                depth,
                max_pages,
                session_id,
                prefer_domains=prefer_domains,
                domain_profile=domain_profile,
                extract_files=extract_files,
            )
            result.correlation_id = cid
            return result

    # ------------------------------------------------------------------
    # v1.6.8 Diagnostics + Replay + Remote CDP
    # ------------------------------------------------------------------

    def get_remote_cdp_url(self) -> str | None:
        """v1.6.8: return the remote_cdp ws:// URL we connected to, or None.

        Returns the configured ``BrowserConfig.remote_cdp_url`` after a
        successful ``connect_over_cdp`` start. Returns None for the
        ``playwright`` and ``cdp_owned`` backends and before the Agent
        is entered. Mirror of :meth:`get_cdp_endpoint` -- the two are
        mutually exclusive (config validator rejects both).
        """
        return self._bm.get_remote_cdp_url()

    def list_traces(self) -> list[str]:
        """v1.6.8: session_ids of replay traces under ``diagnostics.trace_dir``.

        Returns an empty list when tracing was never enabled. Use the
        returned id with :meth:`replay_trace` to re-run a session.
        """
        return self._trace_recorder.list_traces()

    async def replay_trace(
        self,
        trace_file: str | Path,
        secrets: dict[int, str] | None = None,
    ) -> ActionSequenceResult:
        """v1.6.8: re-execute the action list recorded in *trace_file*.

        Reconstructs ``Action`` discriminated-union entries from the JSONL
        log written during a previous live sequence, then dispatches them
        against a fresh ephemeral page. Network events / verification
        screenshots are NOT replayed (they were observed, not directed).

        The starting URL is taken from the first entry that includes a
        ``url`` field. If no entry has one (e.g. older traces), the
        first action's ``url`` attribute is used. If neither is present,
        raises ValueError.

        Redacted secrets (v1.6.16 AG-3 / TRACE-2): trace redaction is
        ONE-WAY -- fill/type/type_text/evaluate values that carried a secret
        were written as the ``***REDACTED***`` sentinel (see
        ``trace_recorder._SENSITIVE_ARG_BY_METHOD``), so the original value
        is NOT recoverable from the trace. Faithful replay of such an action
        therefore requires the caller to re-supply the value via *secrets*.
        For any redacted action with NO override this method SKIPS the action
        (it is excluded from the executed sequence) and emits a
        ``logger.warning`` -- it never types the sentinel literally into the
        field. Non-redacted actions (clicks, navigation, non-secret
        fill/type, ...) always replay normally.

        Args:
            trace_file: Path to a ``<session_id>.jsonl`` trace file.
            secrets: Optional mapping ``{action_index: real_value}`` used to
                re-supply redacted values. The key is the **zero-based index
                of the action within the replayable action list** (the same
                order they appear in the trace, counting only
                ``action.*`` entries). The value replaces the sentinel for
                that action's secret field before execution. Defaults to None
                (no overrides -> every redacted action is skipped+warned).

        Returns:
            ``ActionSequenceResult`` from the replay run. ``correlation_id``
            is fresh -- the replay has its own audit identity. Skipped
            redacted actions are NOT counted in ``actions_total``.

        Raises:
            FileNotFoundError: trace_file does not exist.
            ValueError: trace has no replayable action entries or no URL.
        """
        from pydantic import TypeAdapter

        from .models import Action
        from .trace_recorder import _SENSITIVE_ARG_BY_METHOD

        # v1.6.14 C-3: Local-File-Inclusion defense. ``trace_file`` is
        # accepted directly from the LLM via the ``web_replay_trace`` MCP
        # tool and flows straight into a file open below. Without this
        # containment check an attacker could pass ``/etc/passwd`` (or a
        # ``..`` chain escaping ``trace_dir``) and the JSONL loader would
        # happily read it. Resolve to an absolute path and verify it
        # lives under the configured trace_dir. ValueError surfaces
        # cleanly to the MCP layer.
        p = Path(trace_file).resolve()

        async with self._call_scope("replay_trace", {"trace_file": str(trace_file)}) as cid:
            # v1.6.14 C-3 + review B-2: Local-File-Inclusion defense, now
            # INSIDE _call_scope so a rejected path is recorded in the audit
            # log (a blocked LFI attempt is exactly what a security monitor
            # wants to see -- the prior placement checked before the scope and
            # left rejections invisible). ``trace_file`` arrives straight from
            # the LLM via the web_replay_trace MCP tool and flows into a file
            # open; resolve to absolute and verify it lives under the
            # configured trace_dir. Name the var ``exc`` (not ``e``) -- the
            # for-loops below bind ``e`` and mypy flags reuse of a deleted
            # except variable.
            trace_root = self._trace_recorder.trace_dir.resolve()
            try:
                p.relative_to(trace_root)
            except ValueError as exc:
                raise ValueError(
                    f"trace_file must be inside trace_dir ({trace_root}); got {p}"
                ) from exc
            # v1.6.14 review F-5: load from the already-resolved, containment-
            # checked path -- not the raw trace_file string -- so the loader
            # reads exactly what we validated.
            entries = self._trace_recorder.load_entries(p)
            # Only entries whose method starts with "action." replay cleanly.
            # Other (future) entry types like "scope.start" are skipped.
            action_entries = [e for e in entries if str(e.get("method", "")).startswith("action.")]
            if not action_entries:
                raise ValueError(f"Trace {trace_file} has no replayable action entries")
            start_url: str | None = None
            for e in entries:
                if e.get("url"):
                    start_url = str(e["url"])
                    break
            overrides = secrets or {}
            action_dicts: list[dict[str, Any]] = []
            skipped_redacted = 0
            for idx, e in enumerate(action_entries):
                method = str(e["method"])
                args = dict(e.get("args") or {})
                # Rebuild the discriminator -- audit args dropped it during
                # exclude=. The method tail IS the action name.
                args["action"] = method.removeprefix("action.")
                # v1.6.16 AG-3 / TRACE-2: trace redaction is ONE-WAY -- the
                # secret field for fill/type/evaluate/wait was persisted as
                # the ``***REDACTED***`` sentinel, not the real value. Detect
                # the sentinel and either re-inject a caller-supplied override
                # (keyed by the action's index in this replayable list) or
                # SKIP the action with a warning. We must never type the
                # sentinel verbatim into the field.
                secret_key = _SENSITIVE_ARG_BY_METHOD.get(method)
                if secret_key is not None and is_redacted(args.get(secret_key)):
                    if idx in overrides:
                        args[secret_key] = overrides[idx]
                    else:
                        skipped_redacted += 1
                        logger.warning(
                            "replay_trace: skipping redacted {m} at action "
                            "index {i} (no secret supplied via secrets[{i}]); "
                            "trace redaction is one-way -- supply the real "
                            "value to replay this action",
                            m=method,
                            i=idx,
                        )
                        continue
                action_dicts.append(args)
            if not action_dicts:
                raise ValueError(
                    f"Trace {trace_file} has {len(action_entries)} replayable "
                    f"action(s) but all were redacted with no override supplied "
                    f"via 'secrets'; nothing to replay"
                )
            adapter = TypeAdapter(list[Action])
            actions = adapter.validate_python(action_dicts)
            if start_url is None:
                # Fall back to the first (kept) action's `url` attribute if any.
                start_url = getattr(actions[0], "url", None)
            if not start_url:
                raise ValueError(
                    f"Trace {trace_file} lacks a starting URL "
                    "(no entry with 'url' and first action has no url field)"
                )
            logger.info(
                "Replaying {n} actions from {p} (cid={cid}){skipped}",
                n=len(actions),
                p=trace_file,
                cid=cid,
                skipped=(
                    f"; {skipped_redacted} redacted action(s) skipped" if skipped_redacted else ""
                ),
            )
            return await self._actions.execute_sequence(start_url, actions)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    async def save_results(
        self, result: AgentResult, output_path: str | Path | None = None
    ) -> Path:
        """Save an AgentResult to a JSON file."""
        out_dir = Path(self._config.output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        if output_path is None:
            safe_query = "".join(c if c.isalnum() else "_" for c in result.query)[:50]
            # AG-4: a query with no alphanumerics (e.g. "???" -> "___", or an
            # empty query -> "") yields a useless or dangerous stem: ""
            # produces the dotfile ".json", and an all-"_" stem is opaque.
            # Fall back to a fixed name when nothing meaningful survives, so
            # the output is always a safe, non-empty, non-dotfile filename.
            safe_query = safe_query.strip("_")
            if not safe_query:
                safe_query = "results"
            output_path = out_dir / f"{safe_query}.json"
        else:
            # v1.6.14 B-5: confine a caller/CLI-supplied output_path to
            # output_dir. Previously any absolute or ``..`` path was written
            # verbatim, so a path from the CLI / an MCP caller could clobber
            # arbitrary files. Absolute paths must already resolve under
            # output_dir; relative paths go through safe_join_path (which
            # rejects ``..`` escapes). Either violation raises ValueError.
            from .utils import _is_cross_platform_absolute, safe_join_path

            raw = str(output_path)
            if _is_cross_platform_absolute(raw):
                resolved = Path(raw).resolve()
                try:
                    resolved.relative_to(out_dir)
                except ValueError as exc:
                    raise ValueError(
                        f"output_path must be inside output_dir ({out_dir}); got {resolved}"
                    ) from exc
                output_path = resolved
            else:
                try:
                    output_path = safe_join_path(out_dir, raw)
                except ValueError as exc:
                    raise ValueError(f"output_path escapes output_dir ({out_dir}): {exc}") from exc

        # v1.6.16 deep-review fix: a caller/CLI/MCP-supplied output_path may
        # include a subdirectory (e.g. ``runs/today/out.json``) that passes the
        # containment checks above but whose parent dir does not exist yet, so
        # the write below would raise FileNotFoundError. Create the parent
        # (guaranteed inside output_dir by the checks) before writing.
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        logger.info("Results saved to {path}", path=output_path)
        return output_path
