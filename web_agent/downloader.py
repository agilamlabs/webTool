"""File and page download with three strategies.

Download strategy chain:
1. **httpx streaming** -- fastest, no browser overhead.
2. **Playwright page save** -- navigates with a real browser (handles 403s,
   JS-rendered pages, and web page URLs like ``.html``/``.htm``).
3. **Playwright expect_download** -- for URLs that trigger JS-initiated
   file downloads (binary files served via JavaScript).

Example::

    from web_agent import Agent

    async with Agent() as agent:
        # Download a PDF
        result = await agent.download("https://example.com/report.pdf")

        # Download/save a web page
        result = await agent.download("https://example.com/page.html")
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from loguru import logger
from playwright.async_api import Page

from .browser_manager import BrowserManager
from .config import AppConfig
from .correlation import get_correlation_id
from .debug import DebugCapture
from .models import DownloadResult, FetchStatus
from .rate_limiter import RateLimiter
from .robots import RobotsChecker
from .session_manager import SessionManager
from .utils import check_domain_allowed, get_random_user_agent, safe_join_path
from .web_fetcher import _OFFICE_AND_ARCHIVE_EXTENSIONS

# Extensions that are web pages (should be saved via page.content(), not expect_download)
_WEB_PAGE_EXTENSIONS = frozenset(
    {".html", ".htm", ".xhtml", ".mhtml", ".asp", ".aspx", ".php", ".jsp"}
)

# Extensions that ``Page.expect_download`` is suitable for (binary
# content). Superset of the shared office/archive set plus images and
# OS installers that browsers also download rather than render. Note
# that this set deliberately does NOT include ``.iso`` / ``.deb`` /
# ``.rpm`` -- those are in ``web_fetcher._DOWNLOAD_EXTENSIONS`` because
# we want to route around them in the fetcher, but we don't expect to
# save them with this strategy.
_BINARY_EXTENSIONS = _OFFICE_AND_ARCHIVE_EXTENSIONS | frozenset(
    {
        ".mp3",
        ".mp4",
        ".avi",
        ".mov",
        ".wav",
        ".exe",
        ".msi",
        ".dmg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".bmp",
        ".webp",
    }
)


def _get_url_extension(url: str) -> str:
    """Extract the file extension from a URL path (lowercase, with dot)."""
    path = urlparse(url).path.lower()
    if "." in path.split("/")[-1]:
        return "." + path.split("/")[-1].rsplit(".", 1)[-1]
    return ""


def _is_web_page_url(url: str) -> bool:
    """Check if URL points to a web page (should be rendered, not downloaded)."""
    ext = _get_url_extension(url)
    if ext in _WEB_PAGE_EXTENSIONS:
        return True
    # No extension at all usually means a web page
    return not ext


class Downloader:
    """Downloads files and saves web pages from URLs.

    Uses a three-strategy chain:

    1. **httpx** -- fast streaming download, works for most direct file URLs.
    2. **Playwright page save** -- renders the page in a real browser and saves
       the HTML. Handles 403 blocks, JS rendering, and web page URLs.
    3. **Playwright expect_download** -- waits for a JS-triggered download
       event. Used only for binary file URLs.

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
        network_collector: Optional[Any] = None,
    ) -> None:
        self._bm = browser_manager
        self._config = config
        self._sessions = sessions
        self._debug = debug or DebugCapture(config)
        self._rate_limiter = rate_limiter
        self._robots = robots
        self._download_dir = Path(config.download.download_dir)
        # v1.6.8: shared NetworkCollector. The downloader's own
        # expect_download() consumer still owns saving the file; this
        # collector layer adds a separate page.on("download") notification
        # so the download URL is recorded even when capture_download_intents
        # is on. None when no Agent provided one (older test scaffolding).
        self._network_collector = network_collector

    async def download(
        self,
        url: str,
        filename: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> DownloadResult:
        """Download a file or save a web page from a URL.

        Args:
            url: The URL to download from.
            filename: Output filename. Auto-derived from URL if not provided.
            session_id: Optional persistent browser session for the download.

        Returns:
            DownloadResult with file path, size, and status.
        """
        cid = get_correlation_id()

        # Domain allow/deny gate
        if not check_domain_allowed(url, self._config.safety):
            host = urlparse(url).hostname or ""
            return DownloadResult(
                url=url,
                filepath="",
                filename="",
                status=FetchStatus.BLOCKED,
                error_message=f"Domain not allowed by SafetyConfig: {host}",
                correlation_id=cid,
            )

        # Granular safety: file downloads gated by allow_downloads
        if not self._config.safety.allow_downloads:
            return DownloadResult(
                url=url,
                filepath="",
                filename="",
                status=FetchStatus.BLOCKED,
                error_message=(
                    "File downloads blocked: safety.allow_downloads=False "
                    "(set allow_downloads=True or disable safe_mode to opt in)"
                ),
                correlation_id=cid,
            )

        # Validate extension if a non-empty allowlist is configured
        ext = _get_url_extension(url)
        allowed_exts = self._config.download.allowed_extensions
        if ext and allowed_exts and ext not in allowed_exts:
            return DownloadResult(
                url=url,
                filepath="",
                filename="",
                status=FetchStatus.BLOCKED,
                error_message=(
                    f"Extension {ext} not in allowed_extensions. Allowed: {sorted(allowed_exts)}"
                ),
                correlation_id=cid,
            )

        # Politeness layer: robots.txt check
        if self._robots is not None and not await self._robots.is_allowed(url):
            host = urlparse(url).hostname or ""
            return DownloadResult(
                url=url,
                filepath="",
                filename="",
                status=FetchStatus.BLOCKED,
                error_message=(
                    f"robots.txt for {host} disallows this URL "
                    f"for User-Agent {self._robots.user_agent!r}"
                ),
                correlation_id=cid,
            )

        # Politeness layer: per-host rate limit (may sleep)
        if self._rate_limiter is not None:
            await self._rate_limiter.acquire(urlparse(url).hostname or "")

        self._download_dir.mkdir(parents=True, exist_ok=True)

        if not filename:
            raw_name = url.split("/")[-1].split("?")[0]
            filename = raw_name if raw_name else "downloaded_file"

        # Defend against path-traversal in caller-supplied filename
        try:
            filepath = safe_join_path(self._download_dir, filename)
        except ValueError as exc:
            return DownloadResult(
                url=url,
                filepath="",
                filename=filename,
                status=FetchStatus.BLOCKED,
                error_message=f"Invalid filename: {exc}",
                correlation_id=cid,
            )

        # Strategy 1: Try httpx streaming (fastest)
        try:
            result = await self._download_httpx(url, filepath)
            result.correlation_id = cid
            return result
        except httpx.HTTPStatusError as e:
            logger.info(
                "httpx got HTTP {code} for {url}, trying Playwright browser",
                code=e.response.status_code,
                url=url,
            )
            if self._debug.enabled:
                self._debug.capture_no_page(
                    e, "httpx_download", context={"url": url, "status": e.response.status_code}
                )
        except Exception as e:
            logger.info(
                "httpx failed for {url}: {e}, trying Playwright browser",
                url=url,
                e=e,
            )
            if self._debug.enabled:
                self._debug.capture_no_page(e, "httpx_download", context={"url": url})

        # Strategy 2 or 3 depending on URL type
        if _is_web_page_url(url):
            result = await self._save_page_with_playwright(url, filepath, session_id)
        else:
            result = await self._download_with_playwright(url, filepath, session_id)
            if result.status != FetchStatus.SUCCESS:
                result = await self._save_page_with_playwright(url, filepath, session_id)
        result.correlation_id = cid
        return result

    async def _download_httpx(self, url: str, filepath: Path) -> DownloadResult:
        """Strategy 1: Stream download using httpx (no browser needed).

        Re-validates each redirect target against the safety config, so a
        whitelisted host cannot redirect us to AWS IMDS / RFC1918 / a
        denied domain (SSRF protection).
        """
        from .exceptions import NavigationError

        max_bytes = self._config.download.max_file_size_mb * 1024 * 1024
        headers = {
            "User-Agent": get_random_user_agent(),
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
        }
        safety = self._config.safety

        async def _check_redirect(response: httpx.Response) -> None:
            """Event hook: re-validate redirect targets against safety config."""
            if 300 <= response.status_code < 400:
                next_url = response.headers.get("location", "")
                if next_url and not check_domain_allowed(next_url, safety):
                    raise NavigationError(
                        f"Redirect to disallowed URL blocked: {next_url}",
                        url=next_url,
                        status_code=response.status_code,
                    )

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=60.0,
            event_hooks={"response": [_check_redirect]},
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                # Final check: even if redirects were allowed, verify the final
                # resolved URL passes the gate (defense-in-depth).
                final_url = str(response.url)
                if final_url != url and not check_domain_allowed(final_url, safety):
                    raise NavigationError(
                        f"Final redirect resolved to disallowed URL: {final_url}",
                        url=final_url,
                    )

                content_type = response.headers.get("content-type")
                total = 0
                try:
                    with open(filepath, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            f.write(chunk)
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError(
                                    f"File exceeds {self._config.download.max_file_size_mb}MB limit"
                                )
                except BaseException:
                    # Clean up partial file before re-raising (Phase D1)
                    if filepath.exists():
                        with suppress(OSError):
                            filepath.unlink()
                    raise

        logger.info(
            "Downloaded via httpx: {url} -> {path} ({size} bytes)",
            url=url,
            path=filepath,
            size=total,
        )
        return DownloadResult(
            url=url,
            filepath=str(filepath),
            filename=filepath.name,
            size_bytes=total,
            content_type=content_type,
            status=FetchStatus.SUCCESS,
        )

    async def _save_page_with_playwright(
        self,
        url: str,
        filepath: Path,
        session_id: Optional[str] = None,
    ) -> DownloadResult:
        """Strategy 2: Navigate with Playwright and save the rendered page content."""
        try:
            if session_id and self._sessions is not None:
                ctx = self._sessions.get(session_id)
                self._sessions.touch(session_id)
                page = await ctx.new_page()
                # v1.6.8: attach network capture (no-op when disabled).
                if self._network_collector is not None:
                    self._network_collector.attach(page)
                try:
                    return await self._do_save_page(page, url, filepath)
                finally:
                    await page.close()
            else:
                # BrowserManager.new_page() already attaches the collector.
                async with self._bm.new_page(block_resources=False) as page:
                    return await self._do_save_page(page, url, filepath)
        except Exception as e:
            error_msg = f"Page save failed: {e}"
            logger.error(error_msg)
            return DownloadResult(
                url=url,
                filepath=str(filepath),
                filename=filepath.name,
                status=FetchStatus.NETWORK_ERROR,
                error_message=error_msg,
            )

    async def _do_save_page(self, page: Page, url: str, filepath: Path) -> DownloadResult:
        """Inner page-save: navigate and write page.content() to disk.

        Enforces ``max_file_size_mb`` BEFORE writing to disk (strategy 2):
          1. Pre-check ``Content-Length`` header from the navigation response;
             abort if the server-declared size already exceeds the cap.
          2. Pre-check the in-memory ``page.content()`` byte length; abort
             before any write so we never leave a partial file behind.

        Also re-validates the post-redirect URL against the safety policy:
        a whitelisted host that 302s to a denied host or private IP must
        not have its content written to disk (SSRF defense-in-depth,
        mirrors the httpx download path).
        """
        max_bytes = self._config.download.max_file_size_mb * 1024 * 1024
        response = await page.goto(url, wait_until="load")

        # Post-redirect re-check: page.url is the final URL after redirects.
        # If a whitelisted host bounced us to a denied / private host, do
        # NOT save its content -- return BLOCKED before any disk write.
        if not check_domain_allowed(page.url, self._config.safety):
            host = urlparse(page.url).hostname or ""
            return DownloadResult(
                url=url,
                filepath="",
                filename=filepath.name,
                status=FetchStatus.BLOCKED,
                error_message=f"Page redirected to disallowed URL: {host}",
            )

        content_type = None
        if response:
            ct = response.headers.get("content-type", "")
            content_type = ct.split(";")[0].strip() if ct else None
            # Cheap pre-check: trust Content-Length when the server sent one.
            cl_raw = response.headers.get("content-length")
            if cl_raw:
                try:
                    declared = int(cl_raw)
                except ValueError:
                    declared = -1
                if declared > max_bytes:
                    return DownloadResult(
                        url=url,
                        filepath="",
                        filename=filepath.name,
                        status=FetchStatus.HTTP_ERROR,
                        error_message=(
                            f"Page Content-Length {declared} bytes exceeds "
                            f"{self._config.download.max_file_size_mb} MB cap"
                        ),
                        content_type=content_type,
                    )

        html = await page.content()
        # In-memory pre-check: stop before write if the rendered DOM is too large.
        encoded_size = len(html.encode("utf-8"))
        if encoded_size > max_bytes:
            return DownloadResult(
                url=url,
                filepath="",
                filename=filepath.name,
                status=FetchStatus.HTTP_ERROR,
                error_message=(
                    f"Rendered page {encoded_size} bytes exceeds "
                    f"{self._config.download.max_file_size_mb} MB cap"
                ),
                content_type=content_type,
            )
        filepath.write_text(html, encoding="utf-8")
        size = filepath.stat().st_size

        logger.info(
            "Saved page via Playwright: {url} -> {path} ({size} bytes)",
            url=url,
            path=filepath,
            size=size,
        )
        return DownloadResult(
            url=url,
            filepath=str(filepath),
            filename=filepath.name,
            size_bytes=size,
            content_type=content_type,
            status=FetchStatus.SUCCESS,
        )

    def _enforce_size_cap(self, filepath: Path, url: str) -> Optional[DownloadResult]:
        """Enforce ``max_file_size_mb`` after a Playwright download.

        Playwright's ``download.save_as()`` writes the full file before
        returning, so we stat-check and unlink any oversize result.
        Returns ``None`` if the file is within budget; an error
        DownloadResult (and unlinks the file) if oversize.
        """
        max_bytes = self._config.download.max_file_size_mb * 1024 * 1024
        try:
            size = filepath.stat().st_size
        except OSError:
            return None
        if size > max_bytes:
            with suppress(OSError):
                filepath.unlink()
            return DownloadResult(
                url=url,
                filepath="",
                filename=filepath.name,
                size_bytes=size,
                status=FetchStatus.HTTP_ERROR,
                error_message=(
                    f"Downloaded file {size} bytes exceeds "
                    f"{self._config.download.max_file_size_mb} MB cap (deleted)"
                ),
            )
        return None

    def _blocked_by_redirect(self, url: str, filepath: Path) -> Optional[DownloadResult]:
        """Return a BLOCKED DownloadResult if ``url`` violates the safety policy.

        Used by the Playwright download strategy to re-validate
        ``download.url`` (the post-redirect download origin) before
        ``save_as`` writes anything to disk.
        """
        if check_domain_allowed(url, self._config.safety):
            return None
        host = urlparse(url).hostname or ""
        return DownloadResult(
            url=url,
            filepath="",
            filename=filepath.name,
            status=FetchStatus.BLOCKED,
            error_message=f"Download redirected to disallowed URL: {host}",
        )

    async def _download_with_playwright(
        self,
        url: str,
        filepath: Path,
        session_id: Optional[str] = None,
    ) -> DownloadResult:
        """Strategy 3: Use Playwright's download event for JS-triggered downloads.

        Enforces ``max_file_size_mb`` POST-save: Playwright's
        ``Download.save_as`` writes the full file before returning, so we
        stat the result and unlink if oversize. Less ideal than the httpx
        streaming path (which can abort mid-stream) but at least bounds
        the impact -- the oversize file does not stay on disk.

        Re-validates the actual download origin (``download.url``, set
        after Playwright follows redirects) against the safety policy
        before any disk write -- closes the SSRF gap where a whitelisted
        host could 302 to a denied host's payload.
        """
        try:
            if session_id and self._sessions is not None:
                ctx = self._sessions.get(session_id)
                self._sessions.touch(session_id)
                page = await ctx.new_page()
                # v1.6.8: attach network capture (no-op when disabled).
                if self._network_collector is not None:
                    self._network_collector.attach(page)
                try:
                    async with page.expect_download(timeout=60000) as download_info:
                        await page.goto(url)
                    download = await download_info.value
                    blocked = self._blocked_by_redirect(download.url, filepath)
                    if blocked is not None:
                        return blocked
                    await download.save_as(str(filepath))
                    over = self._enforce_size_cap(filepath, url)
                    if over is not None:
                        return over
                    size = filepath.stat().st_size
                    return DownloadResult(
                        url=url,
                        filepath=str(filepath),
                        filename=filepath.name,
                        size_bytes=size,
                        status=FetchStatus.SUCCESS,
                    )
                finally:
                    await page.close()
            else:
                async with self._bm.new_context(block_resources=False) as ctx:
                    page = await ctx.new_page()
                    # v1.6.8: attach network capture (no-op when disabled).
                    if self._network_collector is not None:
                        self._network_collector.attach(page)
                    async with page.expect_download(timeout=60000) as download_info:
                        await page.goto(url)
                    download = await download_info.value
                    blocked = self._blocked_by_redirect(download.url, filepath)
                    if blocked is not None:
                        return blocked
                    await download.save_as(str(filepath))
                    over = self._enforce_size_cap(filepath, url)
                    if over is not None:
                        return over
                    size = filepath.stat().st_size

                    logger.info(
                        "Downloaded via Playwright: {url} -> {path} ({size} bytes)",
                        url=url,
                        path=filepath,
                        size=size,
                    )
                    return DownloadResult(
                        url=url,
                        filepath=str(filepath),
                        filename=filepath.name,
                        size_bytes=size,
                        status=FetchStatus.SUCCESS,
                    )
        except Exception as e:
            error_msg = f"Browser download failed: {e}"
            logger.warning(error_msg)
            return DownloadResult(
                url=url,
                filepath=str(filepath),
                filename=filepath.name,
                status=FetchStatus.NETWORK_ERROR,
                error_message=error_msg,
            )
