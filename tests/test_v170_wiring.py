"""v1.7.0 public-surface wiring tests (Agent methods + new MCP tools).

The Wave 2 cores (session_manager auth, search_engine circuit breaker,
config ProxyConfig) are tested in their own files. This file covers the
thin integration layer that exposes them on the public API:

* ``Agent.search`` -> ``SearchEngine.search_with_outcome`` with the
  blocked-vs-empty signal mapped onto ``SearchResponse.search_blocked``;
* ``Agent.export_session_state`` / ``import_session_state`` delegating to
  ``SessionManager``;
* the new MCP tools (web_search_links, web_create_session,
  web_list_sessions, web_close_session, web_export_session,
  web_import_session) calling through to the Agent with the right shapes.

All offline -- the Agent and SessionManager are AsyncMock/MagicMock; no
browser, no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent import mcp_server
from web_agent.agent import Agent
from web_agent.config import AppConfig
from web_agent.models import (
    ActionResult,
    ActionStatus,
    CollectionResult,
    MetricsSnapshot,
    ObserveResult,
    SearchResponse,
    SessionInfo,
    StorageStateResult,
)


def _ctx_for(agent: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"agent": agent}
    return ctx


def _observe_result(**overrides) -> ObserveResult:
    """A minimal valid ObserveResult (all the required dimension fields)."""
    base: dict[str, object] = {
        "url": "https://e/x",
        "screenshot_path": "/tmp/s.png",
        "viewport_width": 1280,
        "viewport_height": 720,
        "page_width": 1280,
        "page_height": 2000,
        "scroll_x": 0,
        "scroll_y": 0,
        "device_pixel_ratio": 1.0,
    }
    base.update(overrides)
    return ObserveResult(**base)


# ----------------------------------------------------------------------
# Agent.search -- links-only, maps blocked-vs-empty
# ----------------------------------------------------------------------


def _bare_agent() -> Agent:
    """An Agent instance without entering its async context (no browser).

    Only the attributes the method-under-test touches are set, plus a
    no-op _call_scope so the correlation/audit wrapper is bypassed.
    """
    from contextlib import asynccontextmanager

    agent = Agent.__new__(Agent)
    agent._config = AppConfig()

    @asynccontextmanager
    async def _noop_scope(_method, _args=None):
        yield None

    agent._call_scope = _noop_scope  # type: ignore[method-assign]
    return agent


class TestAgentSearch:
    @pytest.mark.asyncio
    async def test_search_maps_blocked_flag_from_outcome(self) -> None:
        agent = _bare_agent()
        resp = SearchResponse(query="q", total_results=0)
        outcome = SimpleNamespace(response=resp, blocked=True)
        agent._search = MagicMock()
        agent._search.search_with_outcome = AsyncMock(return_value=outcome)

        out = await agent.search("q", max_results=5)

        agent._search.search_with_outcome.assert_awaited_once_with("q", 5, strict=False)
        assert out is resp
        assert out.search_blocked is True

    @pytest.mark.asyncio
    async def test_search_not_blocked_on_real_hits(self) -> None:
        agent = _bare_agent()
        resp = SearchResponse(query="q", total_results=3)
        outcome = SimpleNamespace(response=resp, blocked=False)
        agent._search = MagicMock()
        agent._search.search_with_outcome = AsyncMock(return_value=outcome)

        out = await agent.search("q")

        assert out.search_blocked is False

    @pytest.mark.asyncio
    async def test_search_passes_strict_through(self) -> None:
        agent = _bare_agent()
        resp = SearchResponse(query="q")
        agent._search = MagicMock()
        agent._search.search_with_outcome = AsyncMock(
            return_value=SimpleNamespace(response=resp, blocked=False)
        )

        await agent.search("q", 7, strict=True)

        agent._search.search_with_outcome.assert_awaited_once_with("q", 7, strict=True)


# ----------------------------------------------------------------------
# Agent auth wrappers delegate to SessionManager
# ----------------------------------------------------------------------


class TestAgentAuthWrappers:
    @pytest.mark.asyncio
    async def test_export_delegates(self) -> None:
        agent = _bare_agent()
        expected = StorageStateResult(session_id="s1", saved=True, cookie_count=4)
        agent._sessions = MagicMock()
        agent._sessions.export_state = AsyncMock(return_value=expected)

        out = await agent.export_session_state("s1", "state.json")

        agent._sessions.export_state.assert_awaited_once_with("s1", "state.json")
        assert out is expected

    @pytest.mark.asyncio
    async def test_import_delegates(self) -> None:
        agent = _bare_agent()
        agent._sessions = MagicMock()
        agent._sessions.import_state = AsyncMock(return_value="new-sid")

        out = await agent.import_session_state("state.json", name="acct")

        agent._sessions.import_state.assert_awaited_once_with("state.json", name="acct")
        assert out == "new-sid"


# ----------------------------------------------------------------------
# New MCP tools call through with the right shapes
# ----------------------------------------------------------------------


class TestMcpSessionTools:
    @pytest.mark.asyncio
    async def test_web_create_session(self) -> None:
        agent = MagicMock()
        agent.create_session = AsyncMock(return_value="sid-123")
        out = await mcp_server.web_create_session(_ctx_for(agent), name="acct")
        agent.create_session.assert_awaited_once_with(name="acct")
        assert out == {"session_id": "sid-123"}

    @pytest.mark.asyncio
    async def test_web_list_sessions(self) -> None:
        agent = MagicMock()
        agent.list_sessions = MagicMock(
            return_value=[SessionInfo(session_id="a"), SessionInfo(session_id="b")]
        )
        out = await mcp_server.web_list_sessions(_ctx_for(agent))
        assert out["count"] == 2
        assert {s["session_id"] for s in out["sessions"]} == {"a", "b"}

    @pytest.mark.asyncio
    async def test_web_close_session(self) -> None:
        agent = MagicMock()
        agent.close_session = AsyncMock()
        out = await mcp_server.web_close_session(_ctx_for(agent), "sid-9")
        agent.close_session.assert_awaited_once_with("sid-9")
        assert out == {"closed": "sid-9"}

    @pytest.mark.asyncio
    async def test_web_export_session_returns_result_dict(self) -> None:
        agent = MagicMock()
        agent.export_session_state = AsyncMock(
            return_value=StorageStateResult(
                session_id="s", path="state.json", cookie_count=7, saved=True
            )
        )
        out = await mcp_server.web_export_session(_ctx_for(agent), "s", "state.json")
        agent.export_session_state.assert_awaited_once_with("s", "state.json")
        assert out["saved"] is True
        assert out["cookie_count"] == 7

    @pytest.mark.asyncio
    async def test_web_import_session(self) -> None:
        agent = MagicMock()
        agent.import_session_state = AsyncMock(return_value="hydrated-sid")
        out = await mcp_server.web_import_session(_ctx_for(agent), "state.json", name="acct")
        agent.import_session_state.assert_awaited_once_with("state.json", name="acct")
        assert out == {"session_id": "hydrated-sid"}


class TestMcpSearchLinks:
    @pytest.mark.asyncio
    async def test_web_search_links_calls_agent_search(self) -> None:
        agent = MagicMock()
        resp = SearchResponse(query="q", total_results=2)
        agent.search = AsyncMock(return_value=resp)

        out = await mcp_server.web_search_links(_ctx_for(agent), "q", max_results=5)

        agent.search.assert_awaited_once_with("q", max_results=5, strict=False)
        assert out is resp

    @pytest.mark.asyncio
    async def test_web_search_links_clamps_max_results(self) -> None:
        agent = MagicMock()
        agent.search = AsyncMock(return_value=SearchResponse(query="q"))

        await mcp_server.web_search_links(_ctx_for(agent), "q", max_results=10**6)

        _args, kwargs = agent.search.call_args
        assert kwargs["max_results"] == 50  # clamped to the ceiling

    @pytest.mark.asyncio
    async def test_web_search_links_clamps_floor(self) -> None:
        agent = MagicMock()
        agent.search = AsyncMock(return_value=SearchResponse(query="q"))

        await mcp_server.web_search_links(_ctx_for(agent), "q", max_results=0)

        _args, kwargs = agent.search.call_args
        assert kwargs["max_results"] == 1  # clamped to the floor


# ----------------------------------------------------------------------
# Wave 3B: page-collection wiring (Agent + MCP)
# ----------------------------------------------------------------------


class TestAgentCollection:
    @pytest.mark.asyncio
    async def test_collect_across_pages_delegates(self) -> None:
        agent = _bare_agent()
        agent._debug = MagicMock()
        expected = CollectionResult(start_url="https://e/list", strategy="next_link")
        agent._recipes = MagicMock()
        agent._recipes.collect_across_pages = AsyncMock(return_value=expected)

        out = await agent.collect_across_pages("https://e/list", strategy="next_link", max_pages=5)

        agent._recipes.collect_across_pages.assert_awaited_once()
        _args, kwargs = agent._recipes.collect_across_pages.call_args
        assert kwargs["strategy"] == "next_link"
        assert kwargs["max_pages"] == 5
        assert out is expected

    @pytest.mark.asyncio
    async def test_scroll_to_bottom_delegates(self) -> None:
        agent = _bare_agent()
        expected = ActionResult(action="scroll", status=ActionStatus.SUCCESS)
        agent._actions = MagicMock()
        agent._actions.scroll_to_bottom = AsyncMock(return_value=expected)

        out = await agent.scroll_to_bottom(session_id="s1", max_scrolls=20)

        agent._actions.scroll_to_bottom.assert_awaited_once()
        _args, kwargs = agent._actions.scroll_to_bottom.call_args
        assert kwargs["session_id"] == "s1"
        assert kwargs["max_scrolls"] == 20
        assert out is expected


class TestMcpCollection:
    @pytest.mark.asyncio
    async def test_web_collect_pages_calls_agent(self) -> None:
        agent = MagicMock()
        expected = CollectionResult(start_url="https://e/list", strategy="next_link")
        agent.collect_across_pages = AsyncMock(return_value=expected)

        out = await mcp_server.web_collect_pages(_ctx_for(agent), "https://e/list", max_pages=5)

        agent.collect_across_pages.assert_awaited_once()
        _args, kwargs = agent.collect_across_pages.call_args
        assert kwargs["max_pages"] == 5
        assert out is expected

    @pytest.mark.asyncio
    async def test_web_collect_pages_clamps_max_pages(self) -> None:
        agent = MagicMock()
        agent.collect_across_pages = AsyncMock(
            return_value=CollectionResult(start_url="u", strategy="next_link")
        )

        await mcp_server.web_collect_pages(_ctx_for(agent), "u", max_pages=10**6)

        _args, kwargs = agent.collect_across_pages.call_args
        assert kwargs["max_pages"] == 100  # clamped to the ceiling

    @pytest.mark.asyncio
    async def test_web_scroll_to_bottom_returns_dict(self) -> None:
        agent = MagicMock()
        agent.scroll_to_bottom = AsyncMock(
            return_value=ActionResult(action="scroll", status=ActionStatus.SUCCESS)
        )

        out = await mcp_server.web_scroll_to_bottom(_ctx_for(agent), "s1")

        agent.scroll_to_bottom.assert_awaited_once()
        assert isinstance(out, dict)
        assert out["status"] == "success"


# ----------------------------------------------------------------------
# Wave 4A: metrics wiring (Agent + MCP)
# ----------------------------------------------------------------------


class TestMetricsWiring:
    def test_agent_metrics_maps_registry_snapshot(self) -> None:
        agent = _bare_agent()
        agent._metrics = MagicMock()
        agent._metrics.snapshot = MagicMock(
            return_value={
                "enabled": True,
                "uptime_s": 12.5,
                "counters": {"fetch_total": 3, "fetch_outcome{status=success}": 2},
                "distributions": {"ttfb_ms": {"count": 2, "sum": 100.0, "avg": 50.0}},
            }
        )

        snap = agent.metrics()

        assert isinstance(snap, MetricsSnapshot)
        assert snap.enabled is True
        assert snap.counters["fetch_total"] == 3
        assert snap.distributions["ttfb_ms"]["avg"] == 50.0
        assert snap.uptime_s == 12.5

    @pytest.mark.asyncio
    async def test_web_metrics_calls_agent(self) -> None:
        agent = MagicMock()
        expected = MetricsSnapshot(enabled=True, counters={"fetch_total": 5})
        agent.metrics = MagicMock(return_value=expected)

        out = await mcp_server.web_metrics(_ctx_for(agent))

        agent.metrics.assert_called_once()
        assert out is expected


# ----------------------------------------------------------------------
# Wave 4C: observe set-of-marks passthrough
# ----------------------------------------------------------------------


class TestObserveElementsWiring:
    @pytest.mark.asyncio
    async def test_agent_observe_passes_include_elements(self) -> None:
        agent = _bare_agent()
        agent._actions = MagicMock()
        agent._actions.observe = AsyncMock(return_value=_observe_result())

        await agent.observe("https://e/x", session_id="s", include_elements=False)

        _args, kwargs = agent._actions.observe.call_args
        assert kwargs["include_elements"] is False

    @pytest.mark.asyncio
    async def test_web_observe_surfaces_elements(self) -> None:
        agent = MagicMock()
        agent._config = AppConfig()
        obs = _observe_result(
            elements=[
                {"ref": "e1", "role": "button", "name": "Submit", "tag": "button"},
            ],
        )
        agent.observe = AsyncMock(return_value=obs)

        out = await mcp_server.web_observe(_ctx_for(agent), "https://e/x")

        assert out["elements"][0]["ref"] == "e1"
        _args, kwargs = agent.observe.call_args
        assert kwargs["include_elements"] is True
