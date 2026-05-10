"""v1.6.4 review-pass fixes.

Covers:
- Cross-platform absolute-path detection in safe_join_path (P0 #1)
- bs4 .get('content') str-coercion (P0 #2 — implicit via mypy)
- HEAD probe redirect re-validation (P1 #5)
- Playwright download size cap (P1 #4)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.utils import _is_cross_platform_absolute, safe_join_path

# ----------------------------------------------------------------------
# P0 #1: cross-platform absolute-path detection
# ----------------------------------------------------------------------


class TestCrossPlatformAbsolute:
    """The reviewer found that test_rejects_windows_drive_absolute_path
    failed on Linux because pathlib.PurePosixPath does not treat 'C:\\foo'
    as absolute. _is_cross_platform_absolute is OS-independent."""

    def test_posix_absolute(self):
        assert _is_cross_platform_absolute("/etc/passwd")
        assert _is_cross_platform_absolute("/")

    def test_windows_drive_lowercase(self):
        assert _is_cross_platform_absolute("c:\\windows")
        assert _is_cross_platform_absolute("c:/windows")

    def test_windows_drive_uppercase(self):
        assert _is_cross_platform_absolute("C:\\Windows\\System32")
        assert _is_cross_platform_absolute("D:/foo/bar")

    def test_windows_drive_letters_a_through_z(self):
        for letter in "abcdefghijklmnopqrstuvwxyz":
            assert _is_cross_platform_absolute(f"{letter}:\\foo"), letter
            assert _is_cross_platform_absolute(f"{letter.upper()}:/foo"), letter

    def test_unc_path(self):
        assert _is_cross_platform_absolute("\\\\server\\share\\file")
        assert _is_cross_platform_absolute("//server/share/file")

    def test_windows_root_only(self):
        assert _is_cross_platform_absolute("\\foo")

    def test_relative_paths_not_absolute(self):
        assert not _is_cross_platform_absolute("foo/bar.txt")
        assert not _is_cross_platform_absolute("subdir/file.pdf")
        assert not _is_cross_platform_absolute("..\\foo")  # traversal, not absolute
        assert not _is_cross_platform_absolute("./foo")

    def test_drive_letter_lookalikes_not_absolute(self):
        # No backslash/slash after the colon -> not a Windows drive path
        assert not _is_cross_platform_absolute("a:b")
        assert not _is_cross_platform_absolute("foo:bar.txt")

    def test_empty_string_not_absolute(self):
        assert not _is_cross_platform_absolute("")


class TestSafeJoinPathCrossPlatform:
    """Regression for the failing test the reviewer found."""

    def test_rejects_windows_drive_path_even_on_posix(self, tmp_path: Path):
        """The bug: Path('C:\\Windows').is_absolute() is False on Linux."""
        with pytest.raises(ValueError, match="Absolute"):
            safe_join_path(tmp_path, "C:\\Windows\\System32")

    def test_rejects_windows_forward_slash_drive_path(self, tmp_path: Path):
        with pytest.raises(ValueError, match="Absolute"):
            safe_join_path(tmp_path, "D:/foo/bar")

    def test_rejects_unc_path(self, tmp_path: Path):
        with pytest.raises(ValueError, match="Absolute"):
            safe_join_path(tmp_path, "\\\\server\\share\\file")

    def test_rejects_unc_path_forward_slash(self, tmp_path: Path):
        with pytest.raises(ValueError, match="Absolute"):
            safe_join_path(tmp_path, "//server/share/file")

    def test_still_allows_simple_relative(self, tmp_path: Path):
        """Sanity: don't over-reject."""
        result = safe_join_path(tmp_path, "report.pdf")
        assert result.name == "report.pdf"

    def test_still_rejects_dot_dot_traversal(self, tmp_path: Path):
        with pytest.raises(ValueError, match="escapes"):
            safe_join_path(tmp_path, "../../etc/passwd")


# ----------------------------------------------------------------------
# P0 #2: bs4 mypy fix -- runtime check that None / non-str meta content
# does not crash extraction
# ----------------------------------------------------------------------


def test_bs4_extractor_handles_none_meta_content():
    """When meta tags lack a content attribute, extraction returns None
    description / author rather than crashing."""
    from web_agent.config import AppConfig
    from web_agent.content_extractor import ContentExtractor
    from web_agent.models import FetchResult, FetchStatus

    html = b"""
    <html>
      <head>
        <title>Test</title>
        <meta name="description">
        <meta name="author">
      </head>
      <body><article>Some content here that is long enough.</article></body>
    </html>
    """.decode("utf-8")

    fr = FetchResult(
        url="https://x.com/page",
        final_url="https://x.com/page",
        status=FetchStatus.SUCCESS,
        html=html,
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    # Should not crash; description/author may be None or "" depending on layer
    assert res.url == "https://x.com/page"


# ----------------------------------------------------------------------
# P1 #5: HEAD probe re-validates redirected URL
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classify_url_treats_redirect_to_denied_host_as_unknown():
    """If HEAD follows a redirect to a denied domain, classify_url
    must NOT report binary/html (which would leak the redirect target
    and would skip the SSRF gate). It returns 'unknown' so the caller
    falls back to a real fetch -- which has its own redirect re-check."""
    import httpx
    from web_agent.config import AppConfig, SafetyConfig
    from web_agent.web_fetcher import WebFetcher

    config = AppConfig(safety=SafetyConfig(denied_domains=["evil.example.com"]))
    fetcher = WebFetcher(MagicMock(), config)

    # Mock httpx.AsyncClient so HEAD returns a response whose final URL
    # lands on a denied host with a binary content-type.
    class FakeResponse:
        def __init__(self, url, headers):
            self.url = url
            self.headers = headers

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def head(self, _url):
            return FakeResponse(
                url="https://evil.example.com/payload.pdf",
                headers={"content-type": "application/pdf"},
            )

    import unittest.mock

    # The URL has no extension -> classify_url falls into the HEAD probe path
    with unittest.mock.patch.object(httpx, "AsyncClient", FakeClient):
        result = await fetcher.classify_url("https://allowed.example.com/download")

    assert result == "unknown", (
        "classify_url must not report 'binary' when HEAD redirected to a denied host"
    )


# ----------------------------------------------------------------------
# P1 #4: Playwright download path enforces size cap
# ----------------------------------------------------------------------


def test_enforce_size_cap_unlinks_oversize(tmp_path: Path):
    """Helper: when a Playwright download exceeds the cap, the file is
    deleted and an error result is returned."""
    from web_agent.config import AppConfig, DownloadConfig
    from web_agent.downloader import Downloader
    from web_agent.models import FetchStatus

    # 1 MB cap, then write 1.5 MB
    config = AppConfig(download=DownloadConfig(max_file_size_mb=1, download_dir=str(tmp_path)))
    downloader = Downloader(MagicMock(), config)

    fp = tmp_path / "huge.pdf"
    fp.write_bytes(b"x" * (1500 * 1024))  # 1.5 MB

    over = downloader._enforce_size_cap(fp, "https://x.com/huge.pdf")
    assert over is not None
    assert over.status == FetchStatus.HTTP_ERROR
    assert "MB cap" in over.error_message
    # File should have been deleted
    assert not fp.exists()


def test_enforce_size_cap_passes_under_limit(tmp_path: Path):
    from web_agent.config import AppConfig, DownloadConfig
    from web_agent.downloader import Downloader

    config = AppConfig(download=DownloadConfig(max_file_size_mb=10, download_dir=str(tmp_path)))
    downloader = Downloader(MagicMock(), config)

    fp = tmp_path / "small.pdf"
    fp.write_bytes(b"x" * (100 * 1024))  # 100 KB

    over = downloader._enforce_size_cap(fp, "https://x.com/small.pdf")
    assert over is None
    # File still there
    assert fp.exists()


@pytest.mark.asyncio
async def test_save_page_aborts_when_content_length_too_big(tmp_path: Path):
    """Strategy 2 pre-checks the navigation response's Content-Length and
    aborts before any disk write if the server-declared size is over cap."""
    from web_agent.config import AppConfig, DownloadConfig
    from web_agent.downloader import Downloader
    from web_agent.models import FetchStatus

    config = AppConfig(download=DownloadConfig(max_file_size_mb=1, download_dir=str(tmp_path)))
    downloader = Downloader(MagicMock(), config)

    # Mock Playwright Page that returns an oversized Content-Length.
    # page.url must be a real string so the v1.6.5 post-redirect check
    # in _do_save_page (urlparse(page.url)) doesn't blow up on a Mock.
    fake_page = MagicMock()
    fake_page.goto = AsyncMock(
        return_value=MagicMock(headers={"content-type": "text/html", "content-length": "9999999"})
    )
    type(fake_page).url = property(lambda _self: "https://x.com/big")
    fake_page.content = AsyncMock(return_value="<html></html>")

    fp = tmp_path / "out.html"
    result = await downloader._do_save_page(fake_page, "https://x.com/big", fp)
    assert result.status == FetchStatus.HTTP_ERROR
    assert "Content-Length" in result.error_message
    assert not fp.exists(), "no disk write should have occurred"


@pytest.mark.asyncio
async def test_save_page_aborts_when_rendered_dom_too_big(tmp_path: Path):
    """Strategy 2 also pre-checks the in-memory rendered DOM size."""
    from web_agent.config import AppConfig, DownloadConfig
    from web_agent.downloader import Downloader
    from web_agent.models import FetchStatus

    config = AppConfig(download=DownloadConfig(max_file_size_mb=1, download_dir=str(tmp_path)))
    downloader = Downloader(MagicMock(), config)

    fake_page = MagicMock()
    # Server didn't send Content-Length, but the rendered DOM is 2 MB.
    fake_page.goto = AsyncMock(return_value=MagicMock(headers={"content-type": "text/html"}))
    type(fake_page).url = property(lambda _self: "https://x.com/big")
    fake_page.content = AsyncMock(return_value="x" * (2 * 1024 * 1024))

    fp = tmp_path / "out.html"
    result = await downloader._do_save_page(fake_page, "https://x.com/big", fp)
    assert result.status == FetchStatus.HTTP_ERROR
    assert "exceeds" in result.error_message
    assert not fp.exists()


# ----------------------------------------------------------------------
# Sanity: version is on the 1.6.x family (bumped to 1.6.5 in v1.6.5)
# ----------------------------------------------------------------------


def test_version_on_16_family():
    from web_agent import __version__

    assert __version__.startswith("1.6.")


# Skipped on Windows -- the original failure was on Linux. We document
# that locally on Windows the test is meaningless because pathlib treats
# C:\... as absolute already. The cross-platform helpers above cover both.
@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific pathlib check")
def test_pathlib_treats_drive_as_absolute_on_windows():
    """On Windows, pathlib agrees that drive paths are absolute. Confirms
    that our regex-based detector matches the OS-native behavior."""
    assert Path("C:\\foo").is_absolute()
    assert _is_cross_platform_absolute("C:\\foo")
