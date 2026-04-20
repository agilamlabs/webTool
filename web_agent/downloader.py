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

from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from loguru import logger

from .browser_manager import BrowserManager
from .config import AppConfig
from .models import DownloadResult, FetchStatus
from .utils import get_random_user_agent

# Extensions that are web pages (should be saved via page.content(), not expect_download)
_WEB_PAGE_EXTENSIONS = frozenset({".html", ".htm", ".xhtml", ".mhtml", ".asp", ".aspx", ".php", ".jsp"})

# Extensions that are binary files (expect_download works for these)
_BINARY_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".rar", ".7z",
    ".csv", ".tsv",
    ".mp3", ".mp4", ".avi", ".mov", ".wav",
    ".exe", ".msi", ".dmg",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".bmp", ".webp",
})


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
    if not ext:
        return True
    return False


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
    """

    def __init__(self, browser_manager: BrowserManager, config: AppConfig) -> None:
        self._bm = browser_manager
        self._config = config
        self._download_dir = Path(config.download.download_dir)

    async def download(
        self, url: str, filename: Optional[str] = None
    ) -> DownloadResult:
        """Download a file or save a web page from a URL.

        Args:
            url: The URL to download from.
            filename: Output filename. Auto-derived from URL if not provided.

        Returns:
            DownloadResult with file path, size, and status.
        """
        self._download_dir.mkdir(parents=True, exist_ok=True)

        if not filename:
            raw_name = url.split("/")[-1].split("?")[0]
            filename = raw_name if raw_name else "downloaded_file"

        filepath = self._download_dir / filename

        # Strategy 1: Try httpx streaming (fastest)
        try:
            return await self._download_httpx(url, filepath)
        except httpx.HTTPStatusError as e:
            logger.info(
                "httpx got HTTP {code} for {url}, trying Playwright browser",
                code=e.response.status_code,
                url=url,
            )
        except Exception as e:
            logger.info(
                "httpx failed for {url}: {e}, trying Playwright browser",
                url=url,
                e=e,
            )

        # Strategy 2 or 3 depending on URL type
        if _is_web_page_url(url):
            return await self._save_page_with_playwright(url, filepath)
        else:
            result = await self._download_with_playwright(url, filepath)
            if result.status != FetchStatus.SUCCESS:
                # Binary download failed, try page save as last resort
                return await self._save_page_with_playwright(url, filepath)
            return result

    async def _download_httpx(self, url: str, filepath: Path) -> DownloadResult:
        """Strategy 1: Stream download using httpx (no browser needed)."""
        max_bytes = self._config.download.max_file_size_mb * 1024 * 1024
        headers = {
            "User-Agent": get_random_user_agent(),
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
        }

        async with httpx.AsyncClient(
            follow_redirects=True, timeout=60.0
        ) as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type")
                total = 0
                with open(filepath, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        f.write(chunk)
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError(
                                f"File exceeds {self._config.download.max_file_size_mb}MB limit"
                            )

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
        self, url: str, filepath: Path
    ) -> DownloadResult:
        """Strategy 2: Navigate with Playwright and save the rendered page content."""
        try:
            async with self._bm.new_page(block_resources=False) as page:
                response = await page.goto(url, wait_until="load")
                content_type = None
                if response:
                    ct = response.headers.get("content-type", "")
                    content_type = ct.split(";")[0].strip() if ct else None

                html = await page.content()
                # Save as text (HTML) or binary depending on content
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

    async def _download_with_playwright(
        self, url: str, filepath: Path
    ) -> DownloadResult:
        """Strategy 3: Use Playwright's download event for JS-triggered downloads."""
        try:
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
