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

from loguru import logger

from .audit import AuditLogger
from .browser_actions import BrowserActions
from .browser_manager import BrowserManager
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
    FetchStatus,
    ResearchResult,
    ScreenshotResult,
    SessionInfo,
)
from .rate_limiter import RateLimiter
from .recipes import Recipes
from .robots import RobotsChecker
from .search_engine import SearchEngine
from .session_manager import SessionManager
from .utils import BudgetTracker, check_domain_allowed
from .web_fetcher import WebFetcher, _is_download_url


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

        self._search = SearchEngine(self._bm, self._config, rate_limiter=self._rate_limiter)
        self._fetcher = WebFetcher(
            self._bm,
            self._config,
            sessions=self._sessions,
            debug=self._debug,
            rate_limiter=self._rate_limiter,
            robots=self._robots,
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
    ) -> AgentResult:
        """Full pipeline: search -> fetch top pages -> extract content.

        Args:
            query: The search query.
            max_results: Maximum number of results to process.
            session_id: Optional persistent browser session for the fetches.
            strict: If True, raise :class:`SearchError` when both engines
                return zero results. Default False (return empty AgentResult).

        Returns:
            AgentResult with search results, extracted pages, errors, and timing.

        Raises:
            SearchError: Only when ``strict=True`` and both engines fail.
        """
        async with self._call_scope(
            "search_and_extract", {"query": query, "max_results": max_results}
        ) as cid:
            self._debug.reset()
            start = time.perf_counter()
            errors: list[str] = []
            budget = BudgetTracker(self._config.safety)

            logger.info("Starting pipeline for query: {q}", q=query)
            search_response = await self._search.search(query, max_results, strict=strict)
            logger.info("Search returned {n} results", n=search_response.total_results)

            if not search_response.results:
                return AgentResult(
                    query=query,
                    search=search_response,
                    errors=["No search results found"],
                    total_time_ms=(time.perf_counter() - start) * 1000,
                    correlation_id=cid,
                )

            # Separate file URLs from page URLs and filter blocked domains
            page_urls: list[str] = []
            file_urls: list[str] = []
            for r in search_response.results:
                if not check_domain_allowed(r.url, self._config.safety):
                    errors.append(f"Domain blocked: {r.url}")
                    continue
                if _is_download_url(r.url):
                    file_urls.append(r.url)
                else:
                    page_urls.append(r.url)

            if file_urls:
                logger.info(
                    "Detected {n} file download URLs (skipping fetch, use agent.download())",
                    n=len(file_urls),
                )
                for furl in file_urls:
                    errors.append(f"File URL skipped (use agent.download()): {furl}")

            logger.info("Fetching {n} pages...", n=len(page_urls))
            fetch_results = await self._fetcher.fetch_many(page_urls, session_id=session_id)

            pages: list[ExtractionResult] = []
            for fr in fetch_results:
                try:
                    budget.check_time()
                except Exception as exc:
                    errors.append(str(exc))
                    break

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
                        break
                    pages.append(extraction)
                else:
                    errors.append(f"Failed to fetch {fr.url}: {fr.error_message}")

            elapsed = (time.perf_counter() - start) * 1000
            logger.info(
                "Pipeline complete: {n} pages extracted in {t:.0f}ms",
                n=len(pages),
                t=elapsed,
            )

            return AgentResult(
                query=query,
                search=search_response,
                pages=pages,
                errors=errors,
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
    ) -> ExtractionResult:
        """Recipe: search, rank results, fetch + extract the top hit."""
        async with self._call_scope(
            "search_and_open_best_result", {"query": query, "ranking": ranking}
        ) as cid:
            self._debug.reset()
            result = await self._recipes.search_and_open_best_result(query, ranking, session_id)
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

    async def web_research(
        self,
        query: str,
        depth: int = 1,
        max_pages: int = 5,
        *,
        session_id: Optional[str] = None,
    ) -> ResearchResult:
        """Recipe: search + parallel fetch + extract top N pages, return citations."""
        async with self._call_scope(
            "web_research",
            {"query": query, "depth": depth, "max_pages": max_pages},
        ) as cid:
            self._debug.reset()
            result = await self._recipes.web_research(query, depth, max_pages, session_id)
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
