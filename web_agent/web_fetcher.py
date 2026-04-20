"""URL navigation, page rendering, and retry logic for fetching web pages.

Handles three common failure modes intelligently:
- **Download URLs** (.pdf, .doc, etc.) are detected immediately without retrying.
- **networkidle timeouts** automatically fall back to 'load' wait state.
- **HTTP 4xx** errors fail immediately without retrying.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from loguru import logger
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .browser_manager import BrowserManager
from .config import AppConfig
from .models import FetchResult, FetchStatus
from .utils import NonRetryableHTTPError, Timer, async_retry

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
    """Fetches web pages using Playwright with retry logic and error classification.

    Intelligently routes requests:
    - File URLs (.pdf, .xlsx, etc.) are detected upfront and reported without
      wasting time on retries.
    - Page navigations use ``networkidle`` by default but fall back to ``load``
      if the first attempt times out (handles sites with persistent connections).
    - HTTP 4xx errors fail immediately; 5xx errors are retried.

    Args:
        browser_manager: Shared browser lifecycle manager.
        config: Application configuration.
    """

    def __init__(self, browser_manager: BrowserManager, config: AppConfig) -> None:
        self._bm = browser_manager
        self._config = config

    async def fetch(self, url: str) -> FetchResult:
        """Navigate to URL, wait for rendering, and return HTML content.

        Args:
            url: The URL to fetch.

        Returns:
            FetchResult with HTML on success, or error details on failure.
        """
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
            )

        timer = Timer()
        try:
            with timer:
                return await self._do_fetch(url)
        except NonRetryableHTTPError as e:
            return FetchResult(
                url=url,
                final_url=url,
                status_code=e.status_code,
                status=FetchStatus.HTTP_ERROR,
                error_message=str(e),
                response_time_ms=timer.elapsed_ms,
            )
        except PlaywrightTimeout:
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.TIMEOUT,
                error_message="Navigation timed out",
                response_time_ms=timer.elapsed_ms,
            )
        except Exception as e:
            error_msg = str(e)
            # Detect "Download is starting" errors from Playwright
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
                )
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.NETWORK_ERROR,
                error_message=error_msg,
                response_time_ms=timer.elapsed_ms,
            )

    async def _do_fetch(self, url: str) -> FetchResult:
        """Internal fetch with retry and networkidle->load fallback."""
        cfg = self._config.fetch

        @async_retry(
            max_retries=cfg.max_retries,
            base_delay=cfg.retry_base_delay,
            max_delay=cfg.retry_max_delay,
            non_retryable_exceptions=(NonRetryableHTTPError, _DownloadStartedError),
        )
        async def _fetch_with_retry() -> FetchResult:
            async with self._bm.new_page() as page:
                # Try configured wait_until first; fall back to "load" on timeout
                wait_strategy = cfg.wait_until
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

                # Classify HTTP status
                if status_code and status_code in cfg.non_retryable_status_codes:
                    raise NonRetryableHTTPError(status_code, url)
                if status_code and status_code >= 500:
                    raise Exception(f"Server error HTTP {status_code}")

                # Optional: wait for a specific selector to appear
                if cfg.wait_for_selector:
                    await page.wait_for_selector(
                        cfg.wait_for_selector, timeout=10000
                    )

                # Optional extra wait for JS rendering
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
                )

        return await _fetch_with_retry()

    async def fetch_many(self, urls: list[str]) -> list[FetchResult]:
        """Fetch multiple URLs concurrently, bounded by BrowserManager's semaphore.

        Args:
            urls: List of URLs to fetch.

        Returns:
            List of FetchResult in the same order as input URLs.
        """
        tasks = [self.fetch(url) for url in urls]
        return list(await asyncio.gather(*tasks))


class _DownloadStartedError(Exception):
    """Internal: URL triggered a file download instead of page navigation."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"Download started for {url}")
