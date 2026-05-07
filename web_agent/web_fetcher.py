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
from .cache import Cache
from .config import AppConfig
from .correlation import get_correlation_id
from .debug import DebugCapture
from .models import FetchResult, FetchStatus
from .rate_limiter import RateLimiter
from .robots import RobotsChecker
from .session_manager import SessionManager
from .utils import (
    NonRetryableHTTPError,
    Timer,
    async_retry,
    check_domain_allowed,
    get_random_user_agent,
)

# File extensions that trigger browser downloads instead of rendering
_DOWNLOAD_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".zip",
        ".tar",
        ".gz",
        ".rar",
        ".7z",
        ".csv",
        ".tsv",
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".wav",
        ".exe",
        ".msi",
        ".dmg",
        ".deb",
        ".rpm",
        ".iso",
        ".img",
    }
)


def _is_download_url(url: str) -> bool:
    """Check if a URL points to a file that would trigger a browser download."""
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _DOWNLOAD_EXTENSIONS)


# Content-Type prefixes / fragments that mean "this is a binary document".
# trafilatura/bs4 cannot do anything useful with these — they belong to
# the binary extraction branch (PDF/XLSX/DOCX/CSV).
_BINARY_CONTENT_TYPE_HINTS = (
    "application/pdf",
    "application/vnd.ms-excel",  # legacy XLS (detected, not extracted yet)
    "application/vnd.openxmlformats-officedocument.spreadsheetml",  # XLSX
    "application/vnd.openxmlformats-officedocument.wordprocessingml",  # DOCX
    "application/vnd.openxmlformats-officedocument.presentationml",  # PPTX
    "application/msword",
    "application/vnd.ms-powerpoint",
    "application/zip",
    "application/x-zip",
    "application/octet-stream",
    "application/x-bzip",
    "application/x-rar",
    "application/x-7z-compressed",
    "text/csv",
    "text/tab-separated-values",
)


def _content_type_is_binary(content_type: str | None) -> bool:
    """True if a Content-Type header value indicates a binary document."""
    if not content_type:
        return False
    ct = content_type.lower().split(";", 1)[0].strip()
    return any(ct.startswith(hint) for hint in _BINARY_CONTENT_TYPE_HINTS)


def _disposition_is_attachment(disposition: str | None) -> bool:
    """True if a Content-Disposition header is an attachment."""
    if not disposition:
        return False
    return "attachment" in disposition.lower()


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
        rate_limiter: Optional[RateLimiter] = None,
        robots: Optional[RobotsChecker] = None,
        cache: Optional[Cache] = None,
    ) -> None:
        self._bm = browser_manager
        self._config = config
        self._sessions = sessions
        self._debug = debug or DebugCapture(config)
        self._rate_limiter = rate_limiter
        self._robots = robots
        self._cache = cache

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

        # Politeness layer: robots.txt check (before any network I/O)
        if self._robots is not None and not await self._robots.is_allowed(url):
            host = urlparse(url).hostname or ""
            logger.info("robots.txt disallows {url} for {ua}", url=url, ua=self._robots.user_agent)
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.BLOCKED,
                error_message=(
                    f"robots.txt for {host} disallows this URL "
                    f"for User-Agent {self._robots.user_agent!r}"
                ),
                correlation_id=cid,
            )

        # Cache lookup. NOTE: robots.txt has already been checked above --
        # this is intentional. If robots.txt now disallows a path that we
        # cached earlier (under more permissive rules), we want the new
        # disallow to win. Compliance > a few extra robots fetches per
        # cached URL (those are themselves cached by RobotsChecker).
        # Cache lookup runs before the rate limiter so a cache hit
        # doesn't burn a per-host token. Key is just the URL.
        if self._cache is not None:
            cached = await self._cache.get(f"fetch:{url}")
            if cached is not None:
                logger.debug("Cache hit: {url}", url=url)
                cached["correlation_id"] = cid
                cached["from_cache"] = True
                return FetchResult(**cached)

        # Politeness layer: per-host rate limit (may sleep)
        if self._rate_limiter is not None:
            await self._rate_limiter.acquire(urlparse(url).hostname or "")

        timer = Timer()
        try:
            with timer:
                result = await self._do_fetch(url, session_id=session_id)
            result.correlation_id = cid
            # Cache successful fetches only -- caching errors / blocks
            # would lock in transient failures across the TTL window.
            if self._cache is not None and result.status == FetchStatus.SUCCESS:
                await self._cache.set(f"fetch:{url}", result.model_dump(mode="json"))
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

        # Outer scope -- accumulates debug artifacts across all retry attempts
        # so failures-then-success leaves the saved file paths visible to the
        # caller via FetchResult.debug_artifacts.
        debug_artifacts: list[str] = []
        # Persist the wait strategy across retries so once networkidle fails
        # for a given URL, subsequent retries use 'load' directly (Phase D2).
        wait_strategy = [cfg.wait_until]  # list for nonlocal-style mutation

        @async_retry(
            max_retries=cfg.max_retries,
            base_delay=cfg.retry_base_delay,
            max_delay=cfg.retry_max_delay,
            non_retryable_exceptions=(NonRetryableHTTPError, _DownloadStartedError),
        )
        async def _fetch_with_retry() -> FetchResult:
            page: Page
            if session_id and self._sessions is not None:
                ctx = self._sessions.get(session_id)
                self._sessions.touch(session_id)
                page = await ctx.new_page()
                try:
                    return await self._navigate_and_extract(
                        page, url, debug_artifacts, wait_strategy
                    )
                finally:
                    await page.close()
            else:
                async with self._bm.new_page() as page:
                    return await self._navigate_and_extract(
                        page, url, debug_artifacts, wait_strategy
                    )

        result: FetchResult = await _fetch_with_retry()
        # Final aggregated artifacts list -- overrides whatever the inner call
        # populated, so the caller sees ALL captures across retries.
        result.debug_artifacts = list(debug_artifacts)
        return result

    async def _navigate_and_extract(
        self,
        page: Page,
        url: str,
        debug_artifacts: list[str],
        wait_strategy_box: list[str] | None = None,
    ) -> FetchResult:
        """Perform the actual navigation + content read on an open page.

        ``wait_strategy_box`` is a single-element list used by ``_do_fetch``
        to persist the wait strategy across retries: once networkidle fails
        for this URL, the box is updated to ``"load"`` so subsequent retries
        skip the doomed networkidle attempt entirely (Phase D2).
        """
        cfg = self._config.fetch
        if wait_strategy_box is None:
            wait_strategy_box = [cfg.wait_until]
        wait_strategy = wait_strategy_box[0]
        try:
            try:
                response = await page.goto(url, wait_until=wait_strategy)  # type: ignore[arg-type]
            except PlaywrightError as e:
                if "download is starting" in str(e).lower():
                    raise _DownloadStartedError(url) from e
                raise
            except PlaywrightTimeout:
                if wait_strategy == "networkidle":
                    logger.debug(
                        "networkidle timed out for {url}, falling through to 'load' "
                        "(persisted for subsequent retries)",
                        url=url,
                    )
                    wait_strategy_box[0] = "load"  # persist across retries
                    response = await page.goto(url, wait_until="load")
                else:
                    raise

            status_code = response.status if response else None

            # Re-validate the URL after Playwright follows any redirects
            # (defense-in-depth SSRF protection: a whitelisted host could
            # redirect to a private IP / denied domain).
            final_url = page.url
            if final_url != url and not check_domain_allowed(final_url, self._config.safety):
                from .exceptions import NavigationError

                raise NavigationError(
                    f"Page redirected to disallowed URL: {final_url}",
                    url=final_url,
                    status_code=status_code,
                )

            if status_code and status_code in cfg.non_retryable_status_codes:
                raise NonRetryableHTTPError(status_code, url)
            if status_code and status_code >= 500:
                raise Exception(f"Server error HTTP {status_code}")

            if cfg.wait_for_selector:
                await page.wait_for_selector(cfg.wait_for_selector, timeout=10000)

            if cfg.extra_wait_ms > 0:
                await asyncio.sleep(cfg.extra_wait_ms / 1000)

            html = await page.content()

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
                artifacts = await self._debug.capture_page(page, exc, "fetch", context={"url": url})
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

    async def classify_url(self, url: str) -> str:
        """Return ``'binary' | 'html' | 'unknown'`` for a URL.

        Cheap classification used by :meth:`Agent.fetch_and_extract` to
        decide whether to route to :meth:`fetch_binary` or :meth:`fetch`.

        Resolution order:
          1. URL extension matches a known download extension -> ``'binary'``
          2. Content-Type/Content-Disposition probe via HEAD (skipped when
             :attr:`SafetyConfig.probe_binary_urls` is False)
          3. ``'unknown'`` otherwise (caller should default to HTML).
        """
        if _is_download_url(url):
            return "binary"
        if not self._config.safety.probe_binary_urls:
            return "unknown"

        # HEAD probe with redirects + UA + cookies (when session present).
        # We deliberately swallow all errors -- a failing HEAD must NEVER
        # block the subsequent fetch, only inform routing.
        try:
            import httpx

            cookie_jar = await self._cookies_for_session(None)
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=10.0,
                headers={"User-Agent": get_random_user_agent()},
                cookies=cookie_jar,
            ) as client:
                resp = await client.head(url)
                ct = resp.headers.get("content-type")
                disp = resp.headers.get("content-disposition")
                if _content_type_is_binary(ct) or _disposition_is_attachment(disp):
                    return "binary"
                if ct and ct.lower().startswith(("text/html", "application/xhtml")):
                    return "html"
                return "unknown"
        except Exception as exc:
            logger.debug("HEAD probe failed for {url}: {e}", url=url, e=exc)
            return "unknown"

    async def _cookies_for_session(self, session_id: Optional[str]) -> dict[str, str]:
        """Extract Playwright BrowserContext cookies as an httpx-friendly dict.

        When the caller threads a session_id through, we want authenticated
        downloads (regulator dashboards, intranet docs) to inherit the
        login state established earlier via ``Agent.interact``. This pulls
        all cookies from the persistent context and returns them in a form
        ``httpx.AsyncClient(cookies=...)`` accepts.

        Returns an empty dict when the session is None / unknown / cookie
        retrieval fails -- never raises.
        """
        if not session_id or self._sessions is None:
            return {}
        try:
            ctx = self._sessions.get(session_id)
        except Exception:
            return {}
        try:
            cookies = await ctx.cookies()
        except Exception as exc:
            logger.debug("Failed to read session cookies: {e}", e=exc)
            return {}
        return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}

    async def fetch_binary(
        self,
        url: str,
        session_id: Optional[str] = None,
    ) -> FetchResult:
        """Fetch a binary resource (PDF/XLSX/CSV/DOCX/etc.) into memory via httpx.

        Used by :meth:`Agent.search_and_extract` with ``extract_files=True``
        and by :meth:`Agent.fetch_and_extract` when the URL points to a
        downloadable file. Skips the browser entirely (httpx is faster and
        binary files don't need rendering). Honors the same domain /
        robots / rate-limit / size-cap / cookie gates as
        :meth:`fetch`.

        Streams the response in chunks and aborts when the accumulated
        size exceeds ``DownloadConfig.max_file_size_mb`` -- prevents a
        rogue large file from exhausting memory.

        When ``session_id`` is supplied and the SessionManager has that
        session, cookies are copied from the Playwright context into the
        httpx request so authenticated downloads succeed.

        Args:
            url: Binary resource URL.
            session_id: Optional persistent session whose cookies should
                be applied to the httpx request.

        Returns:
            FetchResult with ``binary`` populated on success and
            ``content_type`` set from the response header. ``html`` is
            always None for binary fetches. Returns HTTP_ERROR with a
            clear message when the size cap is exceeded.
        """
        import httpx

        cid = get_correlation_id()

        if not check_domain_allowed(url, self._config.safety):
            host = urlparse(url).hostname or ""
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.BLOCKED,
                error_message=f"Domain not allowed by SafetyConfig: {host}",
                correlation_id=cid,
            )

        if self._robots is not None and not await self._robots.is_allowed(url):
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.BLOCKED,
                error_message="robots.txt disallows this URL for binary fetch",
                correlation_id=cid,
            )

        if self._rate_limiter is not None:
            await self._rate_limiter.acquire(urlparse(url).hostname or "")

        max_bytes = max(1, self._config.download.max_file_size_mb) * 1024 * 1024
        cookie_jar = await self._cookies_for_session(session_id)

        timer = Timer()
        try:
            with timer:
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=self._config.browser.navigation_timeout / 1000,
                    headers={"User-Agent": get_random_user_agent()},
                    cookies=cookie_jar,
                ) as client:
                    async with client.stream("GET", url) as resp:
                        final_url = str(resp.url)
                        if final_url != url and not check_domain_allowed(
                            final_url, self._config.safety
                        ):
                            return FetchResult(
                                url=url,
                                final_url=final_url,
                                status=FetchStatus.BLOCKED,
                                error_message=f"Redirected to disallowed URL: {final_url}",
                                response_time_ms=timer.elapsed_ms,
                                correlation_id=cid,
                            )
                        if resp.status_code >= 400:
                            return FetchResult(
                                url=url,
                                final_url=final_url,
                                status_code=resp.status_code,
                                status=FetchStatus.HTTP_ERROR,
                                error_message=f"HTTP {resp.status_code}",
                                response_time_ms=timer.elapsed_ms,
                                correlation_id=cid,
                            )
                        # Stream chunks with running cap. We can't trust
                        # Content-Length (some servers omit / lie); enforce
                        # the cap by accumulator instead.
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in resp.aiter_bytes(chunk_size=8192):
                            total += len(chunk)
                            if total > max_bytes:
                                return FetchResult(
                                    url=url,
                                    final_url=final_url,
                                    status_code=resp.status_code,
                                    status=FetchStatus.HTTP_ERROR,
                                    error_message=(
                                        f"Binary exceeded "
                                        f"{self._config.download.max_file_size_mb} MB cap "
                                        f"(stopped at {total} bytes)"
                                    ),
                                    response_time_ms=timer.elapsed_ms,
                                    correlation_id=cid,
                                )
                            chunks.append(chunk)
                        return FetchResult(
                            url=url,
                            final_url=final_url,
                            status_code=resp.status_code,
                            status=FetchStatus.SUCCESS,
                            binary=b"".join(chunks),
                            content_type=resp.headers.get("content-type"),
                            response_time_ms=timer.elapsed_ms,
                            correlation_id=cid,
                        )
        except httpx.TimeoutException:
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.TIMEOUT,
                error_message="Binary fetch timed out",
                response_time_ms=timer.elapsed_ms,
                correlation_id=cid,
            )
        except Exception as exc:
            return FetchResult(
                url=url,
                final_url=url,
                status=FetchStatus.NETWORK_ERROR,
                error_message=str(exc),
                response_time_ms=timer.elapsed_ms,
                correlation_id=cid,
            )


class _DownloadStartedError(Exception):
    """Internal: URL triggered a file download instead of page navigation."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"Download started for {url}")
