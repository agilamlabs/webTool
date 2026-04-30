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
from typing import Optional
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

# Extensions that are web pages (should be saved via page.content(), not expect_download)
_WEB_PAGE_EXTENSIONS = frozenset(
    {".html", ".htm", ".xhtml", ".mhtml", ".asp", ".aspx", ".php", ".jsp"}
)

# Extensions that are binary files (expect_download works for these)
_BINARY_EXTENSIONS = frozenset(
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
    ) -> None:
        self._bm = browser_manager
        self._config = config
        self._sessions = sessions
        self._debug = debug or DebugCapture(config)
        self._rate_limiter = rate_limiter
        self._robots = robots
        self._download_dir = Path(config.download.download_dir)

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
                try:
                    return await self._do_save_page(page, url, filepath)
                finally:
                    await page.close()
            else:
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
        """Inner page-save: navigate and write page.content() to disk."""
        response = await page.goto(url, wait_until="load")
        content_type = None
        if response:
            ct = response.headers.get("content-type", "")
            content_type = ct.split(";")[0].strip() if ct else None

        html = await page.content()
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

    async def _download_with_playwright(
        self,
        url: str,
        filepath: Path,
        session_id: Optional[str] = None,
    ) -> DownloadResult:
        """Strategy 3: Use Playwright's download event for JS-triggered downloads."""
        try:
            if session_id and self._sessions is not None:
                ctx = self._sessions.get(session_id)
                self._sessions.touch(session_id)
                page = await ctx.new_page()
                try:
                    async with page.expect_download(timeout=60000) as download_info:
                        await page.goto(url)
                    download = await download_info.value
                    await download.save_as(str(filepath))
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
                    async with page.expect_download(timeout=60000) as download_info:
                        await page.goto(url)
                    download = await download_info.value
                    await download.save_as(str(filepath))
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
