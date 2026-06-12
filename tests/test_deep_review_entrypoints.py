"""Deep-review (post-v1.6.16) entrypoint / MCP / CLI regression tests.

  * ``BrowserActions.scroll_until_text`` clamps ``max_scrolls`` to <= 1000 so an
    LLM/prompt-injection value can't pin the session tab for months (this path
    bypasses the ScrollInput pydantic bound).
  * The MCP ``web_interact`` tool's ``stop_on_error`` defaults to None and the
    CLI passes None when ``--no-stop-on-error`` is absent, so the operator's
    ``automation.stop_on_error`` config is actually consulted.
  * ``mcp_server._load_mcp_config`` FAILS CLOSED when WEB_AGENT_CONFIG exists
    but is unparseable (a missing file still falls back to defaults).
  * The CLI ``run_interact`` surfaces a non-UTF-8 actions file
    (UnicodeDecodeError) and an invalid action shape (ValidationError) as a
    clean SystemExit, not an opaque traceback (MAIN-1).
"""

from __future__ import annotations

import argparse
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest
from web_agent import main as main_mod
from web_agent import mcp_server
from web_agent.browser_actions import BrowserActions
from web_agent.config import AppConfig
from web_agent.exceptions import ConfigError
from web_agent.models import ActionSequenceResult, ActionStatus


class TestScrollUntilTextClamp:
    def _ba_and_page(self) -> tuple[BrowserActions, MagicMock]:
        page = MagicMock()
        page.url = "https://good.example/app"
        page.is_closed = MagicMock(return_value=False)
        page.evaluate = AsyncMock(return_value="")  # target text never present
        page.wait_for_load_state = AsyncMock()
        page.mouse = MagicMock()
        page.mouse.wheel = AsyncMock()

        tab_mgr = MagicMock()
        tab_mgr.get_or_current = MagicMock(return_value=page)
        sessions = MagicMock()
        sessions.get_tab_manager = MagicMock(return_value=tab_mgr)
        sessions.touch = MagicMock()
        ba = BrowserActions(MagicMock(), AppConfig(), sessions=sessions)
        return ba, page

    @pytest.mark.asyncio
    async def test_huge_max_scrolls_clamped_to_1000(self) -> None:
        ba, page = self._ba_and_page()
        res = await ba.scroll_until_text("never", session_id="s", max_scrolls=10**8)
        assert res.status == ActionStatus.FAILED
        assert res.data["scrolls_used"] == 1000  # NOT 10**8
        assert page.mouse.wheel.await_count == 1000

    @pytest.mark.asyncio
    async def test_small_max_scrolls_unchanged(self) -> None:
        ba, page = self._ba_and_page()
        res = await ba.scroll_until_text("never", session_id="s", max_scrolls=5)
        assert res.data["scrolls_used"] == 5
        assert page.mouse.wheel.await_count == 5


class TestStopOnErrorReachesConfig:
    def test_mcp_web_interact_default_is_none(self) -> None:
        sig = inspect.signature(mcp_server.web_interact)
        assert sig.parameters["stop_on_error"].default is None

    @pytest.mark.asyncio
    async def test_cli_passes_none_when_flag_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        actions_file = tmp_path / "a.json"
        actions_file.write_text(
            '[{"action": "wait", "target": "load_state", "value": "load"}]', encoding="utf-8"
        )
        captured: dict = {}

        class _FakeAgent:
            def __init__(self, *a, **k) -> None:
                pass

            async def __aenter__(self) -> _FakeAgent:
                return self

            async def __aexit__(self, *a) -> bool:
                return False

            async def interact(self, url, actions, *, stop_on_error=None, **k):
                captured["stop_on_error"] = stop_on_error
                return ActionSequenceResult(
                    url=url,
                    actions_total=0,
                    actions_succeeded=0,
                    actions_failed=0,
                    results=[],
                    total_time_ms=0.0,
                )

        monkeypatch.setattr(main_mod, "Agent", _FakeAgent)
        args = argparse.Namespace(
            url="https://good.example/x",
            actions=str(actions_file),
            config=None,
            no_stop_on_error=False,
        )
        await main_mod.run_interact(args)
        assert captured["stop_on_error"] is None  # deferred to config, not True

    @pytest.mark.asyncio
    async def test_cli_flag_forces_false(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        actions_file = tmp_path / "a.json"
        actions_file.write_text(
            '[{"action": "wait", "target": "load_state", "value": "load"}]', encoding="utf-8"
        )
        captured: dict = {}

        class _FakeAgent:
            def __init__(self, *a, **k) -> None:
                pass

            async def __aenter__(self) -> _FakeAgent:
                return self

            async def __aexit__(self, *a) -> bool:
                return False

            async def interact(self, url, actions, *, stop_on_error=None, **k):
                captured["stop_on_error"] = stop_on_error
                return ActionSequenceResult(
                    url=url,
                    actions_total=0,
                    actions_succeeded=0,
                    actions_failed=0,
                    results=[],
                    total_time_ms=0.0,
                )

        monkeypatch.setattr(main_mod, "Agent", _FakeAgent)
        args = argparse.Namespace(
            url="https://good.example/x",
            actions=str(actions_file),
            config=None,
            no_stop_on_error=True,
        )
        await main_mod.run_interact(args)
        assert captured["stop_on_error"] is False


class TestLoadMcpConfigFailClosed:
    def test_existing_but_unparseable_file_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("- not: a-mapping\n", encoding="utf-8")  # list root -> ConfigError
        monkeypatch.setenv("WEB_AGENT_CONFIG", str(bad))
        with pytest.raises(ConfigError):
            mcp_server._load_mcp_config()

    def test_missing_file_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setenv("WEB_AGENT_CONFIG", str(tmp_path / "nope.yaml"))
        cfg = mcp_server._load_mcp_config()
        assert isinstance(cfg, AppConfig)

    def test_no_env_var_uses_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("WEB_AGENT_CONFIG", raising=False)
        assert isinstance(mcp_server._load_mcp_config(), AppConfig)


class TestRunInteractErrorMessages:
    @pytest.mark.asyncio
    async def test_non_utf8_actions_file_is_systemexit(self, tmp_path) -> None:
        # PowerShell's default Out-File writes UTF-16 on Windows -- the project's
        # primary platform. read_text(encoding="utf-8") raises UnicodeDecodeError.
        f = tmp_path / "utf16.json"
        f.write_bytes('[{"action": "wait"}]'.encode("utf-16"))
        args = argparse.Namespace(url="https://x.example", actions=str(f), config=None,
                                  no_stop_on_error=False)
        with pytest.raises(SystemExit) as exc:
            await main_mod.run_interact(args)
        assert "read/parse" in str(exc.value)

    @pytest.mark.asyncio
    async def test_invalid_action_shape_is_systemexit(self, tmp_path) -> None:
        f = tmp_path / "bad.json"
        f.write_text('[{"action": "definitely-not-a-real-action"}]', encoding="utf-8")
        args = argparse.Namespace(url="https://x.example", actions=str(f), config=None,
                                  no_stop_on_error=False)
        with pytest.raises(SystemExit) as exc:
            await main_mod.run_interact(args)
        assert "Invalid actions" in str(exc.value)
