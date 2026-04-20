"""Pipeline orchestrator: search -> fetch -> extract -> download -> automate.

The :class:`Agent` is the main entry point for AI agents to interact with the web.
It composes all subsystems (browser, search, fetch, extract, download, automate)
behind a clean async context manager API.

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
"""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger

from .browser_actions import BrowserActions
from .browser_manager import BrowserManager
from .config import AppConfig
from .content_extractor import ContentExtractor
from .downloader import Downloader
from .models import (
    Action,
    ActionSequenceResult,
    AgentResult,
    DownloadResult,
    ExtractionResult,
    ScreenshotFormat,
    ScreenshotResult,
)
from .search_engine import SearchEngine
from .web_fetcher import WebFetcher


class Agent:
    """Main entry point for the web_agent toolkit.

    Orchestrates browser lifecycle, web search, page fetching, content
    extraction, file downloading, and browser automation through a single
    async context manager.

    Args:
        config: Application configuration. If ``None``, uses all defaults
            (no config file needed).

    Example::

        from web_agent import Agent, AppConfig

        # With defaults:
        async with Agent() as agent:
            result = await agent.search_and_extract("query")

        # With custom config:
        config = AppConfig(browser={"headless": False}, log_level="DEBUG")
        async with Agent(config) as agent:
            result = await agent.fetch_and_extract("https://example.com")
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        self._config = config or AppConfig()
        self._bm = BrowserManager(self._config)
        self._search = SearchEngine(self._bm, self._config)
        self._fetcher = WebFetcher(self._bm, self._config)
        self._extractor = ContentExtractor(self._config)
        self._downloader = Downloader(self._bm, self._config)
        self._actions = BrowserActions(self._bm, self._config)

    async def __aenter__(self) -> Agent:
        await self._bm.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._bm.stop()

    # ------------------------------------------------------------------
    # Pipeline: Search + Fetch + Extract
    # ------------------------------------------------------------------

    async def search_and_extract(
        self, query: str, max_results: int | None = None
    ) -> AgentResult:
        """Full pipeline: Google search -> fetch top pages -> extract content.

        Returns an AgentResult containing all search results, extracted page
        content, any errors encountered, and total execution time.
        """
        start = time.perf_counter()
        errors: list[str] = []

        # Step 1: Search Google
        logger.info("Starting pipeline for query: {q}", q=query)
        search_response = await self._search.search(query, max_results)
        logger.info(
            "Search returned {n} results", n=search_response.total_results
        )

        if not search_response.results:
            return AgentResult(
                query=query,
                search=search_response,
                errors=["No search results found"],
                total_time_ms=(time.perf_counter() - start) * 1000,
            )

        # Step 2: Separate page URLs from file download URLs
        from .web_fetcher import _is_download_url

        page_urls: list[str] = []
        file_urls: list[str] = []
        for r in search_response.results:
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
                errors.append(
                    f"File URL skipped (use agent.download()): {furl}"
                )

        # Step 3: Fetch page URLs concurrently
        logger.info("Fetching {n} pages...", n=len(page_urls))
        fetch_results = await self._fetcher.fetch_many(page_urls)

        # Step 4: Extract content from each fetched page
        pages: list[ExtractionResult] = []
        for fr in fetch_results:
            if fr.html:
                extraction = self._extractor.extract(fr)
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
        )

    # ------------------------------------------------------------------
    # Single URL: Fetch + Extract
    # ------------------------------------------------------------------

    async def fetch_and_extract(self, url: str) -> ExtractionResult:
        """Fetch a single URL and extract its content (no search step)."""
        logger.info("Fetching and extracting: {url}", url=url)
        fr = await self._fetcher.fetch(url)
        return self._extractor.extract(fr)

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    async def download(
        self, url: str, filename: str | None = None
    ) -> DownloadResult:
        """Download a file from a URL."""
        return await self._downloader.download(url, filename)

    # ------------------------------------------------------------------
    # Browser Automation
    # ------------------------------------------------------------------

    async def interact(
        self,
        url: str,
        actions: list[Action],
        stop_on_error: bool | None = None,
    ) -> ActionSequenceResult:
        """Execute a scripted sequence of browser actions on a URL."""
        logger.info(
            "Starting interaction sequence on {url} ({n} actions)",
            url=url,
            n=len(actions),
        )
        return await self._actions.execute_sequence(url, actions, stop_on_error)

    async def screenshot(
        self,
        url: str,
        path: str | None = None,
        full_page: bool = False,
    ) -> ScreenshotResult:
        """Navigate to a URL and take a screenshot."""
        logger.info("Taking screenshot of {url}", url=url)
        return await self._actions.take_screenshot(url, path, full_page)

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
            safe_query = "".join(
                c if c.isalnum() else "_" for c in result.query
            )[:50]
            output_path = out_dir / f"{safe_query}.json"
        else:
            output_path = Path(output_path)

        output_path.write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )
        logger.info("Results saved to {path}", path=output_path)
        return output_path
