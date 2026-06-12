"""H4: ``Agent.new_tab`` must re-gate the *landed* url after redirects.

The input-url gate in ``Agent.new_tab`` only checks the URL the caller
passed. A whitelisted host that server-redirects into private/denied
space (e.g. ``http://169.254.169.254/`` -- the AWS IMDS endpoint) would
otherwise silently park a tab there. The fix re-checks ``page.url``
after ``TabManager.new_tab`` navigates, and on failure closes the
just-created tab and raises ``DomainNotAllowedError`` -- the SAME
contract the input gate uses.

These tests drive ``Agent.new_tab`` with a mocked ``TabManager`` (via a
stubbed ``SessionManager.get_tab_manager``) so no real browser is
touched. ``Agent(cfg)`` constructs cleanly without a live browser,
matching tests/test_v1614_security.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent import Agent
from web_agent.config import AppConfig, SafetyConfig
from web_agent.exceptions import DomainNotAllowedError

# The whitelisted host the caller asks for. With allowed_domains=
# ["example.com"] and block_private_ips=True this passes the INPUT gate.
ALLOWED_INPUT_URL = "https://allowed.example.com/go"
# Where the server redirects us -- AWS IMDS, blocked by block_private_ips.
PRIVATE_REDIRECT_URL = "http://169.254.169.254/latest/meta-data/"
# A benign redirect target that is still allowed.
ALLOWED_REDIRECT_URL = "https://other.example.com/landing"


def _make_landed_page(url: str) -> MagicMock:
    """A fake Page whose ``.url`` property reports the post-redirect URL."""
    page = MagicMock()
    type(page).url = property(lambda _self: url)
    return page


def _agent_with_tab_manager(cfg: AppConfig, tab_mgr: MagicMock) -> Agent:
    agent = Agent(cfg)
    sessions = MagicMock()
    sessions.get_tab_manager = MagicMock(return_value=tab_mgr)
    agent._sessions = sessions  # type: ignore[attr-defined]
    return agent


def _cfg() -> AppConfig:
    return AppConfig(
        safety=SafetyConfig(
            allowed_domains=["example.com"],
            block_private_ips=True,
        )
    )


@pytest.mark.asyncio
async def test_new_tab_redirect_to_private_host_is_denied_and_tab_closed() -> None:
    """A redirect into private space must raise DomainNotAllowedError AND
    close the just-created tab (so it isn't left registered/parked)."""
    tab_mgr = MagicMock()
    tab_mgr.new_tab = AsyncMock(return_value="tab-xyz")
    # After goto, the tab landed on the private IMDS host.
    tab_mgr.get_or_current = MagicMock(
        return_value=_make_landed_page(PRIVATE_REDIRECT_URL)
    )
    tab_mgr.close_tab = AsyncMock()

    agent = _agent_with_tab_manager(_cfg(), tab_mgr)

    with pytest.raises(DomainNotAllowedError) as excinfo:
        await agent.new_tab(ALLOWED_INPUT_URL, session_id="s1")

    # Signalled denial mirrors the input-url contract (host populated).
    assert excinfo.value.host == "169.254.169.254"

    # The tab the input gate let through must be torn down, not parked.
    tab_mgr.close_tab.assert_awaited_once_with("tab-xyz")


@pytest.mark.asyncio
async def test_new_tab_allowed_redirect_is_kept_open() -> None:
    """Positive control: a redirect that lands on another allowed host
    must NOT raise and must NOT close the tab."""
    tab_mgr = MagicMock()
    tab_mgr.new_tab = AsyncMock(return_value="tab-ok")
    tab_mgr.get_or_current = MagicMock(
        return_value=_make_landed_page(ALLOWED_REDIRECT_URL)
    )
    tab_mgr.close_tab = AsyncMock()

    agent = _agent_with_tab_manager(_cfg(), tab_mgr)

    tid = await agent.new_tab(ALLOWED_INPUT_URL, session_id="s1")

    assert tid == "tab-ok"
    tab_mgr.close_tab.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_tab_blocked_input_url_never_creates_tab() -> None:
    """Sanity: the pre-existing INPUT-url gate still fires first -- a
    denied input never even reaches TabManager.new_tab."""
    tab_mgr = MagicMock()
    tab_mgr.new_tab = AsyncMock(return_value="should-not-happen")
    tab_mgr.get_or_current = MagicMock()
    tab_mgr.close_tab = AsyncMock()

    agent = _agent_with_tab_manager(_cfg(), tab_mgr)

    with pytest.raises(DomainNotAllowedError):
        await agent.new_tab("https://evil.test/phish", session_id="s1")

    tab_mgr.new_tab.assert_not_awaited()
    tab_mgr.close_tab.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_tab_without_url_skips_regate() -> None:
    """No url => no navigation => no re-gate; the blank tab is returned
    as-is (the re-check must be guarded by ``url is not None``)."""
    tab_mgr = MagicMock()
    tab_mgr.new_tab = AsyncMock(return_value="blank-tab")
    # get_or_current would return a page on about:blank; the re-gate must
    # be skipped entirely so this is never consulted.
    tab_mgr.get_or_current = MagicMock(
        side_effect=AssertionError("re-gate ran for a url-less new_tab")
    )
    tab_mgr.close_tab = AsyncMock()

    agent = _agent_with_tab_manager(_cfg(), tab_mgr)

    tid = await agent.new_tab(None, session_id="s1")

    assert tid == "blank-tab"
    tab_mgr.close_tab.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_tab_goto_failure_about_blank_is_not_a_domain_denial() -> None:
    """Deep-review regression: when ``TabManager.new_tab`` swallows an
    uncommitted goto failure (DNS error, connection refused, a download-
    triggering URL, a timeout before commit) the page is left on
    ``about:blank`` -- a HOSTLESS url. The landed-url re-gate must NOT
    mis-report that transient failure as a ``DomainNotAllowedError`` (empty
    host) nor destroy the tab the TabManager keeps open for retry; the tid is
    returned as-is."""
    tab_mgr = MagicMock()
    tab_mgr.new_tab = AsyncMock(return_value="tab-blank")
    tab_mgr.get_or_current = MagicMock(return_value=_make_landed_page("about:blank"))
    tab_mgr.close_tab = AsyncMock()

    agent = _agent_with_tab_manager(_cfg(), tab_mgr)

    tid = await agent.new_tab(ALLOWED_INPUT_URL, session_id="s1")

    assert tid == "tab-blank"
    tab_mgr.close_tab.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_tab_empty_landed_url_is_not_a_domain_denial() -> None:
    """A page whose ``.url`` is empty (a failed/uncommitted navigation) is
    likewise NOT a redirect into denied space -- no raise, no tab teardown."""
    tab_mgr = MagicMock()
    tab_mgr.new_tab = AsyncMock(return_value="tab-empty")
    tab_mgr.get_or_current = MagicMock(return_value=_make_landed_page(""))
    tab_mgr.close_tab = AsyncMock()

    agent = _agent_with_tab_manager(_cfg(), tab_mgr)

    tid = await agent.new_tab(ALLOWED_INPUT_URL, session_id="s1")

    assert tid == "tab-empty"
    tab_mgr.close_tab.assert_not_awaited()
