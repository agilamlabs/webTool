"""Deep-review (post-v1.6.16) miscellaneous low-severity regression tests.

  * ``redact_sensitive_mapping`` recurses into nested dicts/lists so a secret
    nested under a non-sensitive key (skill inputs accept nested dicts) is
    masked, closing the AG-2 channel for nested shapes.
  * The action-parameter enums ScrollDirection / NavigateDirection / WaitTarget
    / ScreenshotFormat are re-exported from the package root.
  * ``Agent.save_results`` creates a subdirectory in a caller-supplied
    output_path instead of crashing with FileNotFoundError.
  * A timed-out infinite-scroll evaluate is classified as TIMEOUT (execute_action
    now catches the builtin TimeoutError asyncio.wait_for raises), not FAILED.
  * Importing ``web_agent.__main__`` does NOT dispatch the CLI (it is guarded by
    ``if __name__ == "__main__"``).
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from unittest.mock import MagicMock

import pytest
import web_agent
from web_agent.browser_actions import BrowserActions
from web_agent.config import AppConfig
from web_agent.models import (
    ActionStatus,
    AgentResult,
    ScrollInput,
    SearchResponse,
)
from web_agent.trace_recorder import redact_sensitive_mapping


class TestRecursiveRedaction:
    def test_nested_sensitive_key_masked(self) -> None:
        out = redact_sensitive_mapping(
            {"login": {"password": "hunter2", "user": "me"}, "note": "ok"}
        )
        assert out["login"]["password"] == "***REDACTED***"
        assert out["login"]["user"] == "me"  # non-sensitive preserved
        assert out["note"] == "ok"

    def test_list_of_dicts_recursed(self) -> None:
        out = redact_sensitive_mapping({"creds": [{"token": "t1"}, {"token": "t2"}]})
        assert out["creds"] == [{"token": "***REDACTED***"}, {"token": "***REDACTED***"}]

    def test_sensitive_key_masks_whole_container(self) -> None:
        out = redact_sensitive_mapping({"api_key": {"v": 1, "extra": [1, 2]}})
        assert out["api_key"] == "***REDACTED***"

    def test_top_level_scalars_unchanged(self) -> None:
        out = redact_sensitive_mapping({"q": "search terms", "count": 5})
        assert out == {"q": "search terms", "count": 5}

    def test_input_not_mutated(self) -> None:
        src = {"login": {"password": "x"}}
        redact_sensitive_mapping(src)
        assert src == {"login": {"password": "x"}}


class TestActionEnumExports:
    def test_enums_exported_from_root(self) -> None:
        for name in ("ScrollDirection", "NavigateDirection", "WaitTarget", "ScreenshotFormat"):
            assert hasattr(web_agent, name), f"{name} not exported"
            assert name in web_agent.__all__, f"{name} missing from __all__"

    def test_no_phantom_all_entries(self) -> None:
        missing = [n for n in web_agent.__all__ if not hasattr(web_agent, n)]
        assert missing == []


class TestSaveResultsSubdir:
    @pytest.mark.asyncio
    async def test_subdirectory_output_path_is_created(self, tmp_path: pathlib.Path) -> None:
        from web_agent import Agent

        agent = Agent(AppConfig(output_dir=str(tmp_path)))
        res = AgentResult(query="q", search=SearchResponse(query="q"))
        out = pathlib.Path(await agent.save_results(res, "runs/today/out.json"))
        # Before the fix this raised FileNotFoundError (parent never created).
        assert out.exists()
        assert out.name == "out.json"
        assert out.parent.name == "today"


class TestInfiniteScrollTimeoutClassification:
    @pytest.mark.asyncio
    async def test_evaluate_timeout_is_timeout_status(self) -> None:
        ba = BrowserActions(MagicMock(), AppConfig())
        page = MagicMock()

        async def _eval_timeout(*a: object, **k: object) -> object:
            # asyncio.wait_for raises the BUILTIN TimeoutError on a hung evaluate.
            raise TimeoutError("simulated infinite-scroll evaluate deadline")

        page.evaluate = _eval_timeout
        action = ScrollInput(infinite_scroll=True, infinite_scroll_max=1)
        result = await ba.execute_action(page, action)
        # Was FAILED before the fix (builtin TimeoutError missed the catch).
        assert result.status == ActionStatus.TIMEOUT


class TestMainModuleImportSafe:
    def test_importing_main_module_does_not_dispatch_cli(self) -> None:
        # Before the guard, importing web_agent.__main__ ran argparse against
        # the subprocess's empty argv -> SystemExit(2) (returncode 2).
        r = subprocess.run(
            [sys.executable, "-c", "import web_agent.__main__"],
            capture_output=True,
            timeout=120,
        )
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
