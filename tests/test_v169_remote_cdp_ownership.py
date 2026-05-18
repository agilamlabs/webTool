"""v1.6.9 remote_cdp ownership-token tests.

Covers:
  * OwnershipToken.issue / read / verify round-trip
  * BrowserConfig validator: backend='remote_cdp' now requires token + profile_dir
  * BrowserManager.start() rejects a remote_cdp connect when the token
    file is missing or contents do not match the configured token
  * Successful remote_cdp connect when the token matches
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from web_agent import AppConfig, BrowserConfig, OwnershipToken
from web_agent.browser_manager import BrowserManager
from web_agent.exceptions import BrowserError, ConfigError

# ---------------------------------------------------------------------------
# OwnershipToken: pure round-trip
# ---------------------------------------------------------------------------


def test_token_issue_writes_file_with_64_hex_chars(tmp_path: Path) -> None:
    token = OwnershipToken.issue(tmp_path)
    assert len(token) == 64
    assert all(c in "0123456789abcdef" for c in token)
    on_disk = (tmp_path / OwnershipToken.FILENAME).read_text(encoding="utf-8").strip()
    assert on_disk == token


def test_token_read_returns_written_value(tmp_path: Path) -> None:
    token = OwnershipToken.issue(tmp_path)
    assert OwnershipToken.read(tmp_path) == token


def test_token_read_missing_returns_none(tmp_path: Path) -> None:
    assert OwnershipToken.read(tmp_path) is None


def test_token_read_empty_file_returns_none(tmp_path: Path) -> None:
    (tmp_path / OwnershipToken.FILENAME).write_text("   \n", encoding="utf-8")
    assert OwnershipToken.read(tmp_path) is None


def test_token_verify_match(tmp_path: Path) -> None:
    token = OwnershipToken.issue(tmp_path)
    assert OwnershipToken.verify(tmp_path, token) is True


def test_token_verify_mismatch(tmp_path: Path) -> None:
    OwnershipToken.issue(tmp_path)
    assert OwnershipToken.verify(tmp_path, "a" * 64) is False


def test_token_verify_missing_file(tmp_path: Path) -> None:
    assert OwnershipToken.verify(tmp_path, "a" * 64) is False


def test_token_verify_empty_candidate_returns_false(tmp_path: Path) -> None:
    OwnershipToken.issue(tmp_path)
    assert OwnershipToken.verify(tmp_path, "") is False


def test_token_issue_creates_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    token = OwnershipToken.issue(nested)
    assert (nested / OwnershipToken.FILENAME).read_text(encoding="utf-8").strip() == token


# ---------------------------------------------------------------------------
# BrowserConfig validator: token + profile_dir required for remote_cdp
# ---------------------------------------------------------------------------


def test_remote_cdp_requires_token() -> None:
    with pytest.raises((ConfigError, ValidationError), match="remote_cdp_ownership_token"):
        BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
            remote_cdp_profile_dir="/some/path",
        )


def test_remote_cdp_requires_profile_dir() -> None:
    with pytest.raises((ConfigError, ValidationError), match="remote_cdp_profile_dir"):
        BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
            remote_cdp_ownership_token="a" * 64,
        )


def test_remote_cdp_accepts_full_config(tmp_path: Path) -> None:
    # No exception
    cfg = BrowserConfig(
        backend="remote_cdp",
        remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
        remote_cdp_ownership_token="a" * 64,
        remote_cdp_profile_dir=str(tmp_path),
    )
    assert cfg.remote_cdp_ownership_token == "a" * 64
    assert cfg.remote_cdp_profile_dir == str(tmp_path)


# ---------------------------------------------------------------------------
# BrowserManager.start() token verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_manager_rejects_remote_cdp_with_missing_token_file(
    tmp_path: Path,
) -> None:
    # Profile dir exists but has no .webtool-ownership file
    cfg = AppConfig(
        browser=BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
            remote_cdp_ownership_token="a" * 64,
            remote_cdp_profile_dir=str(tmp_path),
        )
    )
    bm = BrowserManager(cfg)
    with pytest.raises(BrowserError, match="Ownership token verification failed"):
        await bm.start()


@pytest.mark.asyncio
async def test_browser_manager_rejects_remote_cdp_with_wrong_token(
    tmp_path: Path,
) -> None:
    OwnershipToken.issue(tmp_path)  # writes a different random token
    cfg = AppConfig(
        browser=BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
            remote_cdp_ownership_token="b" * 64,  # known mismatch
            remote_cdp_profile_dir=str(tmp_path),
        )
    )
    bm = BrowserManager(cfg)
    with pytest.raises(BrowserError, match="Ownership token verification failed"):
        await bm.start()


@pytest.mark.asyncio
async def test_browser_manager_accepts_remote_cdp_with_matching_token(
    tmp_path: Path,
) -> None:
    token = OwnershipToken.issue(tmp_path)
    cfg = AppConfig(
        browser=BrowserConfig(
            backend="remote_cdp",
            remote_cdp_url="ws://127.0.0.1:9222/devtools/browser/x",
            remote_cdp_ownership_token=token,
            remote_cdp_profile_dir=str(tmp_path),
        )
    )
    bm = BrowserManager(cfg)

    # Mock playwright.chromium.connect_over_cdp -- we only care that
    # token verification passed and the connect call was reached.
    fake_browser = MagicMock()
    fake_browser.close = AsyncMock()
    fake_chromium = MagicMock()
    fake_chromium.connect_over_cdp = AsyncMock(return_value=fake_browser)
    fake_pw = MagicMock()
    fake_pw.chromium = fake_chromium

    with patch("web_agent.browser_manager.async_playwright") as p_apw:
        cm_mock = MagicMock()
        cm_mock.__aenter__ = AsyncMock(return_value=fake_pw)
        cm_mock.__aexit__ = AsyncMock(return_value=None)
        p_apw.return_value = cm_mock
        # Stealth.use_async wraps async_playwright(); patch it to pass-through.
        with patch.object(bm._stealth, "use_async", return_value=cm_mock):
            await bm.start()

    assert bm._is_remote_cdp is True
    fake_chromium.connect_over_cdp.assert_awaited_once_with(
        "ws://127.0.0.1:9222/devtools/browser/x"
    )
    # Cleanup
    bm._browser = None  # avoid double-close in implicit shutdown


# ---------------------------------------------------------------------------
# Token issuance on isolated launch
# ---------------------------------------------------------------------------


def test_browser_manager_get_ownership_token_none_before_start() -> None:
    cfg = AppConfig()  # no isolation_mode
    bm = BrowserManager(cfg)
    assert bm.get_ownership_token() is None
    assert bm.get_effective_profile_dir() is None
