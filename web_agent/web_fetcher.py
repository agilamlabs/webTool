"""URL navigation, page rendering, and retry logic for fetching web pages.

Handles four common failure modes intelligently:

- **Download URLs** (.pdf, .doc, etc.) -- detected immediately without retrying.
- **networkidle timeouts** -- automatically fall back to ``load`` wait state.
- **HTTP 4xx** -- fail fast (non-retryable).
- **Domain not allowed** -- short-circuit with ``BLOCKED`` status.

Optional features:
- ``session_id`` reuses a persistent browser session for cookie continuity.
- Debug capture saves HTML/screenshot/error JSON on failure when enabled.
- Correlation IDs are echoed back into the FetchResult for tracing.
"""

from __future__ import annotations

import asyncio
from typing import Optional
from urllib.parse import urlparse

from loguru import logger
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .browser_manager import BrowserManager
from .config import AppConfig
from .correlation import get_correlation_id
from .debug import DebugCapture
from .models import FetchResult, FetchStatus
from .session_manager import SessionManager
from .utils import NonRetryableHTTPError, Timer, async_retry, check_domain_allowed

# File extensions that trigger browser downloads instead of rendering
_DOWNLOAD_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".rar", ".7z",
    ".csv", ".tsv",
    ".mp3", ".mp4", ".avi", ".mov", ".wav",
    ".exe", ".msi", ".dmg", ".deb", ".rpm",
    ".iso", ".img",
})


def _is_download_url(url: str) -> bool:
    """Check if a URL points to a file that would trigger a browser download."""
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _DOWNLOAD_EXTENSIONS)


class WebFetcher:
    """Fetches web pages using Playwright with retry, safety, and debug support.

    Args:
        browser_manager: Shared browser lifecycle manager.
        config: Application configuration.
        sessions: Optional SessionManager for persistent browser sessions.
        debug: Optional DebugCapture for failure artifact capture.
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        config: AppConfig,
        sessions: Optional[SessionManager] = None,
        debug: Optional[DebugCapture] = None,
    ) -> None:
        self._bm = browser_manager
        self._config = config
        self._sessions = sessions
        self._debug = debug or DebugCapture(config)

    async def fetch(
        self,
        url: str,
        session_id: Optional[str] = None,
    ) -> FetchResult:
        """Navigate to URL, wait for rendering, and return HTML content.

        Args:
            url: The URL to fetch.
            session_id: Optional persistent browser session to use.

        Returns:
            FetchResult with HTML on success, or error details on failure.
        """
        cid = get_correlation_id()

        # Domain allow/deny gate
        if not check_domain_allowed(url, self._config.safety):
            host = urlparse(url).hostname or ""
            logger.warning("Domain blocked by safety policy: {host}", host=host)
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.BLOCKED,
                error_message=f"Domain not allowed by SafetyConfig: {host}",
                correlation_id=cid,
            )

        # Fast-path: detect file download URLs without launching a browser page
        if _is_download_url(url):
            logger.info(
                "Skipping fetch for download URL: {url} (use agent.download() instead)",
                url=url,
            )
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.NETWORK_ERROR,
                error_message=(
                    f"URL points to a downloadable file. "
                    f"Use agent.download('{url}') instead of fetch."
                ),
                correlation_id=cid,
            )

        timer = Timer()
        try:
            with timer:
                result = await self._do_fetch(url, session_id=session_id)
            result.correlation_id = cid
            return result
        except NonRetryableHTTPError as e:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=e.status_code,
                status=FetchStatus.HTTP_ERROR,
                error_message=str(e),
                response_time_ms=timer.elapsed_ms,
                correlation_id=cid,
            )
        except PlaywrightTimeout:
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.TIMEOUT,
                error_message="Navigation timed out",
                response_time_ms=timer.elapsed_ms,
                correlation_id=cid,
            )
        except Exception as e:
            error_msg = str(e)
            if "download is starting" in error_msg.lower():
                logger.info(
                    "URL triggered a download: {url} (use agent.download() instead)",
                    url=url,
                )
                return FetchResult(
                    url=url,
                    final_url=url,
                    status=FetchStatus.NETWORK_ERROR,
                    error_message=(
                        f"URL triggered a file download. "
                        f"Use agent.download('{url}') instead of fetch."
                    ),
                    response_time_ms=timer.elapsed_ms,
                    correlation_id=cid,
                )
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.NETWORK_ERROR,
                error_message=error_msg,
                response_time_ms=timer.elapsed_ms,
                correlation_id=cid,
            )

    async def _do_fetch(
        self,
        url: str,
        session_id: Optional[str] = None,
    ) -> FetchResult:
        """Internal fetch with retry and networkidle->load fallback."""
        cfg = self._config.fetch

        @async_retry(
            max_retries=cfg.max_retries,
            base_delay=cfg.retry_base_delay,
            max_delay=cfg.retry_max_delay,
            non_retryable_exceptions=(NonRetryableHTTPError, _DownloadStartedError),
        )
        async def _fetch_with_retry() -> FetchResult:
            page: Page
            page_owner = "ephemeral"
            debug_artifacts: list[str] = []

            if session_id and self._sessions is not None:
                ctx = self._sessions.get(session_id)
                self._sessions.touch(session_id)
                page = await ctx.new_page()
                page_owner = "session"
                try:
                    return await self._navigate_and_extract(
                        page, url, debug_artifacts
                    )
                finally:
                    await page.close()
            else:
                async with self._bm.new_page() as page:
                    return await self._navigate_and_extract(
                        page, url, debug_artifacts
                    )

        return await _fetch_with_retry()

    async def _navigate_and_extract(
        self,
        page: Page,
        url: str,
        debug_artifacts: list[str],
    ) -> FetchResult:
        """Perform the actual navigation + content read on an open page."""
        cfg = self._config.fetch
        wait_strategy = cfg.wait_until
        try:
            try:
                response = await page.goto(url, wait_until=wait_strategy)
            except PlaywrightError as e:
                if "download is starting" in str(e).lower():
                    raise _DownloadStartedError(url)
                raise
            except PlaywrightTimeout:
                if wait_strategy == "networkidle":
                    logger.debug(
                        "networkidle timed out for {url}, retrying with 'load'",
                        url=url,
                    )
                    response = await page.goto(url, wait_until="load")
                else:
                    raise

            status_code = response.status if response else None

            if status_code and status_code in cfg.non_retryable_status_codes:
                raise NonRetryableHTTPError(status_code, url)
            if status_code and status_code >= 500:
                raise Exception(f"Server error HTTP {status_code}")

            if cfg.wait_for_selector:
                await page.wait_for_selector(cfg.wait_for_selector, timeout=10000)

            if cfg.extra_wait_ms > 0:
                await asyncio.sleep(cfg.extra_wait_ms / 1000)

            html = await page.content()
            final_url = page.url

            return FetchResult(
                url=url,
                final_url=final_url,
                status_code=status_code,
                status=FetchStatus.SUCCESS,
                html=html,
                debug_artifacts=debug_artifacts,
            )
        except Exception as exc:
            # Capture debug artifacts on any failure path before re-raising
            if self._debug.enabled:
                artifacts = await self._debug.capture_page(
                    page, exc, "fetch", context={"url": url}
                )
                debug_artifacts.extend(artifacts)
            raise

    async def fetch_many(
        self,
        urls: list[str],
        session_id: Optional[str] = None,
    ) -> list[FetchResult]:
        """Fetch multiple URLs concurrently, bounded by BrowserManager's semaphore.

        Args:
            urls: List of URLs to fetch.
            session_id: Optional shared persistent session for all fetches.

        Returns:
            List of FetchResult in the same order as input URLs.
        """
        tasks = [self.fetch(url, session_id=session_id) for url in urls]
        return list(await asyncio.gather(*tasks))


class _DownloadStartedError(Exception):
    """Internal: URL triggered a file download instead of page navigation."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"Download started for {url}")
