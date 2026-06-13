"""v1.7.0 Wave 8: headed login-handoff helper (offline).

Exercises ``SessionManager.login_handoff`` -- the "log in by hand, automate
afterwards" front door -- without a real browser or human. The session's live
page, the context resolver, ``create``, and ``export_state`` are stubbed; the
``_clock`` / ``_sleep`` seams are injected so the wait loop runs in zero real
time. Also a thin Agent-delegation check.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent.agent import Agent
from web_agent.config import AppConfig
from web_agent.models import LoginHandoffResult, StorageStateResult
from web_agent.session_manager import SessionManager


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _FakePage:
    def __init__(self, url: str = "https://login.example/start") -> None:
        self.url = url
        self.goto = AsyncMock()
        self.query_selector = AsyncMock(return_value=None)


def _sm(
    *,
    headless: bool = False,
    export: StorageStateResult | None = None,
    get_raises: bool = False,
) -> tuple[SessionManager, _FakePage, _Clock]:
    sm = SessionManager.__new__(SessionManager)
    sm._config = AppConfig(browser={"headless": headless})
    clock = _Clock()
    sm._clock = clock  # type: ignore[assignment]
    page = _FakePage()
    tabs = MagicMock()
    tabs.get.return_value = page
    sm._tabs = {"sid": tabs}
    ctx = MagicMock()
    ctx.pages = [page]
    if get_raises:
        sm.get = MagicMock(side_effect=KeyError("sid"))  # type: ignore[method-assign]
    else:
        sm.get = MagicMock(return_value=ctx)  # type: ignore[method-assign]
    sm.create = AsyncMock(return_value="sid")  # type: ignore[method-assign]
    sm.export_state = AsyncMock(  # type: ignore[method-assign]
        return_value=export
        or StorageStateResult(
            session_id="sid", path="state.json", cookie_count=3, origin_count=1, saved=True
        )
    )
    return sm, page, clock


class TestLoginHandoff:
    @pytest.mark.asyncio
    async def test_url_match_success(self) -> None:
        sm, page, clock = _sm()

        async def fake_sleep(s: float) -> None:
            clock.now += s
            page.url = "https://app.example/dashboard"  # flips to success after 1st poll

        sm._sleep = fake_sleep  # type: ignore[assignment]
        out = await sm.login_handoff(
            login_url="https://login.example/start",
            storage_state_path="state.json",
            session_id="sid",
            success_url_substring="/dashboard",
            timeout_s=100.0,
        )
        assert isinstance(out, LoginHandoffResult)
        assert out.success_detected is True
        assert out.success_signal == "url_match"
        assert out.saved is True
        assert out.cookie_count == 3
        page.goto.assert_awaited_once()
        sm.export_state.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_selector_success(self) -> None:
        sm, page, clock = _sm()
        page.query_selector = AsyncMock(side_effect=[None, object()])

        async def fake_sleep(s: float) -> None:
            clock.now += s

        sm._sleep = fake_sleep  # type: ignore[assignment]
        out = await sm.login_handoff(
            login_url="https://login.example/start",
            storage_state_path="state.json",
            session_id="sid",
            success_selector="nav.user-menu",
            timeout_s=100.0,
        )
        assert out.success_detected is True
        assert out.success_signal == "selector"

    @pytest.mark.asyncio
    async def test_timeout_still_exports(self) -> None:
        sm, _page, clock = _sm()

        async def fake_sleep(s: float) -> None:
            clock.now += s

        sm._sleep = fake_sleep  # type: ignore[assignment]
        out = await sm.login_handoff(
            login_url="https://login.example/start",
            storage_state_path="state.json",
            session_id="sid",
            success_url_substring="/never",
            timeout_s=3.0,
            poll_interval_s=1.0,
        )
        assert out.success_detected is False
        assert out.success_signal == "timeout"
        assert out.saved is True  # exported anyway
        sm.export_state.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_condition_waits_full_window(self) -> None:
        sm, _page, clock = _sm()
        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)
            clock.now += s

        sm._sleep = fake_sleep  # type: ignore[assignment]
        out = await sm.login_handoff(
            login_url="https://login.example/start",
            storage_state_path="state.json",
            session_id="sid",
            timeout_s=42.0,
        )
        assert out.success_signal == "elapsed"
        assert out.success_detected is False
        # Single sleep for the whole remaining window.
        assert slept == [42.0]
        assert out.elapsed_s == 42.0

    @pytest.mark.asyncio
    async def test_creates_session_when_none(self) -> None:
        sm, _page, clock = _sm()

        async def fake_sleep(s: float) -> None:
            clock.now += s

        sm._sleep = fake_sleep  # type: ignore[assignment]
        out = await sm.login_handoff(
            login_url="https://login.example/start",
            storage_state_path="state.json",
            timeout_s=1.0,
        )
        sm.create.assert_awaited_once()
        assert out.session_id == "sid"

    @pytest.mark.asyncio
    async def test_export_failure_surfaced(self) -> None:
        bad = StorageStateResult(
            session_id="sid", path=None, saved=False, error="Invalid storage_state path: .."
        )
        sm, _page, clock = _sm(export=bad)

        async def fake_sleep(s: float) -> None:
            clock.now += s

        sm._sleep = fake_sleep  # type: ignore[assignment]
        out = await sm.login_handoff(
            login_url="https://login.example/start",
            storage_state_path="../escape.json",
            session_id="sid",
            timeout_s=1.0,
        )
        assert out.saved is False
        assert out.error is not None and "Invalid storage_state path" in out.error

    @pytest.mark.asyncio
    async def test_headless_note_in_message(self) -> None:
        sm, _page, clock = _sm(headless=True)

        async def fake_sleep(s: float) -> None:
            clock.now += s

        sm._sleep = fake_sleep  # type: ignore[assignment]
        out = await sm.login_handoff(
            login_url="https://login.example/start",
            storage_state_path="state.json",
            session_id="sid",
            timeout_s=1.0,
        )
        assert "headless is True" in out.message

    @pytest.mark.asyncio
    async def test_headed_has_no_headless_note(self) -> None:
        sm, _page, clock = _sm(headless=False)

        async def fake_sleep(s: float) -> None:
            clock.now += s

        sm._sleep = fake_sleep  # type: ignore[assignment]
        out = await sm.login_handoff(
            login_url="https://login.example/start",
            storage_state_path="state.json",
            session_id="sid",
            timeout_s=1.0,
        )
        assert "headless is True" not in out.message

    @pytest.mark.asyncio
    async def test_dead_session_returns_error_no_export(self) -> None:
        sm, _page, _clock = _sm(get_raises=True)
        out = await sm.login_handoff(
            login_url="https://login.example/start",
            storage_state_path="state.json",
            session_id="sid",
        )
        assert out.saved is False
        assert out.error is not None
        sm.export_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_goto_failure_is_non_fatal(self) -> None:
        sm, page, clock = _sm()
        page.goto = AsyncMock(side_effect=RuntimeError("nav blew up"))

        async def fake_sleep(s: float) -> None:
            clock.now += s

        sm._sleep = fake_sleep  # type: ignore[assignment]
        out = await sm.login_handoff(
            login_url="https://login.example/start",
            storage_state_path="state.json",
            session_id="sid",
            timeout_s=1.0,
        )
        # Wait + export still happened despite the navigation error.
        assert out.success_signal == "elapsed"
        sm.export_state.assert_awaited_once()


class TestAgentDelegation:
    @pytest.mark.asyncio
    async def test_agent_login_handoff_delegates(self) -> None:
        agent = Agent.__new__(Agent)
        agent._config = AppConfig()

        @asynccontextmanager
        async def _noop_scope(_method, _args=None):  # type: ignore[no-untyped-def]
            yield None

        agent._call_scope = _noop_scope  # type: ignore[method-assign]
        expected = LoginHandoffResult(session_id="s1", login_url="https://x/login", saved=True)
        agent._sessions = MagicMock()
        agent._sessions.login_handoff = AsyncMock(return_value=expected)

        out = await agent.login_handoff(
            "https://x/login",
            "state.json",
            success_url_substring="/home",
            timeout_s=5.0,
        )
        assert out is expected
        agent._sessions.login_handoff.assert_awaited_once_with(
            login_url="https://x/login",
            storage_state_path="state.json",
            session_id=None,
            name=None,
            success_url_substring="/home",
            success_selector=None,
            timeout_s=5.0,
            poll_interval_s=1.0,
        )
