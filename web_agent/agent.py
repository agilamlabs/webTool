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

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from loguru import logger

from .audit import AuditLogger
from .browser_actions import BrowserActions
from .browser_manager import BrowserManager
from .cache import Cache, DiskCache
from .config import AppConfig
from .content_extractor import ContentExtractor
from .correlation import correlation_scope
from .debug import DebugCapture
from .downloader import Downloader
from .models import (
    Action,
    ActionSequenceResult,
    AgentResult,
    DownloadResult,
    ExtractionResult,
    FetchDiagnostic,
    FetchResult,
    FetchStatus,
    FormFilterSpec,
    ResearchResult,
    ScreenshotResult,
    SearchResultItem,
    SessionInfo,
)
from .rate_limiter import RateLimiter
from .recipes import Recipes
from .robots import RobotsChecker
from .search_engine import SearchEngine
from .session_manager import SessionManager
from .utils import BudgetTracker, check_domain_allowed
from .web_fetcher import WebFetcher, _is_download_url


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
    "google.",       # google.com, google.co.uk, etc.
    "bing.com",
    "duckduckgo.com",
    "search.brave.com",
    "searx.",        # searx.* (searx.tiekoetter.com, searx.be, etc.)
    "searxng.",
)


def _unwrap_search_url(query: str) -> Optional[str]:
    """If query is a search-engine SERP URL, return the embedded query string.

    Returns None when the URL is not a recognized SERP or has no ``q`` param.
    Only invoked when ``_query_is_url(query)`` already returned True.
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
    return inner or None


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
    return _BLOCK_REASON_BY_STATUS.get(fr.status)


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
        self._bm = BrowserManager(self._config)
        self._sessions = SessionManager(self._bm, self._config)
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
        )
        self._fetcher = WebFetcher(
            self._bm,
            self._config,
            sessions=self._sessions,
            debug=self._debug,
            rate_limiter=self._rate_limiter,
            robots=self._robots,
            cache=self._cache,
        )
        self._extractor = ContentExtractor(self._config)
        self._downloader = Downloader(
            self._bm,
            self._config,
            sessions=self._sessions,
            debug=self._debug,
            rate_limiter=self._rate_limiter,
            robots=self._robots,
        )
        self._actions = BrowserActions(
            self._bm, self._config, sessions=self._sessions, debug=self._debug
        )
        self._recipes = Recipes(
            self._search,
            self._fetcher,
            self._extractor,
            self._downloader,
            self._config,
            browser_manager=self._bm,
            sessions=self._sessions,
        )

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
            errors: list[str] = []
            warnings: list[str] = []
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
                    fr = await self._fetcher.fetch(query, session_id=session_id)
                    url_pages: list[ExtractionResult] = []
                    if fr.html:
                        extracted = self._extractor.extract(fr)
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
                        errors.append(f"Failed to fetch {query}: {fr.error_message or 'unknown'}")
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
                        errors=errors,
                        warnings=warnings,
                        download_candidates=download_candidates,
                        diagnostics=diagnostics,
                        total_time_ms=(time.perf_counter() - start) * 1000,
                        correlation_id=cid,
                    )

            logger.info("Starting pipeline for query: {q}", q=query)
            search_response = await self._search.search(query, max_results, strict=strict)
            logger.info("Search returned {n} results", n=search_response.total_results)

            if not search_response.results:
                return AgentResult(
                    query=query,
                    search=search_response,
                    errors=["No search results found"],
                    warnings=warnings,
                    download_candidates=download_candidates,
                    diagnostics=diagnostics,
                    total_time_ms=(time.perf_counter() - start) * 1000,
                    correlation_id=cid,
                )

            # Separate file URLs from page URLs and filter blocked domains.
            # Each result either becomes (a) a page_url to fetch, (b) a
            # download_candidate, or (c) a warning + diagnostic.
            page_items: list[SearchResultItem] = []
            file_items: list[SearchResultItem] = []
            for r in search_response.results:
                if not check_domain_allowed(r.url, self._config.safety):
                    warnings.append(f"Domain blocked: {r.url}")
                    diagnostics.append(
                        FetchDiagnostic(
                            url=r.url,
                            status=FetchStatus.BLOCKED,
                            provider=r.provider,
                            block_reason="domain_blocked",
                        )
                    )
                    continue
                if _is_download_url(r.url):
                    file_items.append(r)
                else:
                    page_items.append(r)

            # Handle file URLs: surface as structured candidates, optionally
            # extract inline if extract_files=True (PDF/XLSX path).
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
                            errors.append(str(exc))
                            break
                        bin_fr = await self._fetcher.fetch_binary(
                            fr_item.url, session_id=session_id
                        )
                        if bin_fr.binary:
                            extraction = self._extractor.extract(bin_fr)
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
                        else:
                            warnings.append(
                                f"Binary extraction failed for {fr_item.url}: "
                                f"{bin_fr.error_message or 'no content'}"
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
                        warnings.append(
                            "1 downloadable file URL skipped; see download_candidates"
                        )
                    else:
                        warnings.append(
                            f"{len(file_items)} downloadable file URLs skipped; "
                            f"see download_candidates"
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

            page_urls = [p.url for p in page_items]
            url_to_provider = {p.url: p.provider for p in page_items}
            logger.info("Fetching {n} pages...", n=len(page_urls))
            fetch_results = await self._fetcher.fetch_many(page_urls, session_id=session_id)

            for fr in fetch_results:
                try:
                    budget.check_time()
                except Exception as exc:
                    errors.append(str(exc))
                    break

                provider = url_to_provider.get(fr.url, "unknown")

                if fr.html:
                    try:
                        budget.add_page()
                    except Exception as exc:
                        errors.append(str(exc))
                        break
                    extraction = self._extractor.extract(fr)
                    extraction.correlation_id = cid
                    try:
                        budget.add_chars(extraction.content_length)
                    except Exception as exc:
                        errors.append(str(exc))
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
                    warnings.append(f"Failed to fetch {fr.url}: {fr.error_message}")
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
            if not pages and page_urls and not errors:
                errors.append("All page fetches failed; see warnings/diagnostics for detail")

            elapsed = (time.perf_counter() - start) * 1000
            logger.info(
                "Pipeline complete: {n} pages, {w} warnings, {e} errors in {t:.0f}ms",
                n=len(pages),
                w=len(warnings),
                e=len(errors),
                t=elapsed,
            )

            return AgentResult(
                query=query,
                search=search_response,
                pages=pages,
                errors=errors,
                warnings=warnings,
                download_candidates=download_candidates,
                diagnostics=diagnostics,
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
    ) -> ExtractionResult:
        """Fetch a single URL and extract its content.

        Args:
            url: The URL to fetch.
            session_id: Optional persistent browser session.
            strict: If True, raise :class:`NavigationError` when the fetch
                fails (HTTP error, timeout, blocked, etc.). Default False
                (return ExtractionResult with extraction_method="none").

        Raises:
            NavigationError: Only when ``strict=True`` and fetch fails.
        """
        from .exceptions import NavigationError

        async with self._call_scope("fetch_and_extract", {"url": url}) as cid:
            self._debug.reset()
            logger.info("Fetching and extracting: {url}", url=url)
            fr = await self._fetcher.fetch(url, session_id=session_id)
            if strict and fr.status != FetchStatus.SUCCESS:
                raise NavigationError(
                    f"Fetch failed: {fr.error_message}",
                    url=fr.url,
                    status_code=fr.status_code,
                )
            extraction = self._extractor.extract(fr)
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
    ) -> ExtractionResult:
        """Recipe: search, rank results, fetch + extract the top hit.

        Args:
            prefer_domains: Optional caller-supplied host hints (e.g.
                ``["ec.europa.eu", "esma.europa.eu"]``); matching results
                receive a strong ranking bonus.
        """
        async with self._call_scope(
            "search_and_open_best_result", {"query": query, "ranking": ranking}
        ) as cid:
            self._debug.reset()
            result = await self._recipes.search_and_open_best_result(
                query, ranking, session_id, prefer_domains=prefer_domains
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
        async with self._call_scope(
            "fill_form_and_extract", {"url": url}
        ) as cid:
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
    ) -> ResearchResult:
        """Recipe: search + parallel fetch + extract top N pages, return citations.

        Args:
            prefer_domains: Optional caller-supplied host hints; matching
                results receive a strong ranking bonus.
        """
        async with self._call_scope(
            "web_research",
            {"query": query, "depth": depth, "max_pages": max_pages},
        ) as cid:
            self._debug.reset()
            result = await self._recipes.web_research(
                query, depth, max_pages, session_id, prefer_domains=prefer_domains
            )
            result.correlation_id = cid
            return result

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    async def save_results(
        self, result: AgentResult, output_path: str | Path | None = None
    ) -> Path:
        """Save an AgentResult to a JSON file."""
        out_dir = Path(self._config.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        if output_path is None:
            safe_query = "".join(c if c.isalnum() else "_" for c in result.query)[:50]
            output_path = out_dir / f"{safe_query}.json"
        else:
            output_path = Path(output_path)

        output_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        logger.info("Results saved to {path}", path=output_path)
        return output_path
