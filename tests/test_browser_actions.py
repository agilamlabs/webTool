"""Integration tests for browser automation actions.

Requires Playwright browsers installed: playwright install chromium
Requires network access for live page interaction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from web_agent.agent import Agent
from web_agent.config import AppConfig
from web_agent.models import (
    ActionSequenceResult,
    ActionStatus,
    ActionType,
    ClickInput,
    EvaluateInput,
    FillInput,
    HoverInput,
    KeyboardInput,
    NavigateDirection,
    NavigateInput,
    ScreenshotFormat,
    ScreenshotInput,
    ScrollInput,
    WaitInput,
    WaitTarget,
)


@pytest.fixture
def auto_config(tmp_path) -> AppConfig:
    """Config for automation tests with short timeouts.

    Enables ``allow_js_evaluation`` so EvaluateInput-based tests work.
    Production callers must opt in to this flag explicitly (see SafetyConfig).
    """
    return AppConfig(
        log_level="WARNING",
        browser={
            "headless": True,
            "default_timeout": 15000,
            "navigation_timeout": 20000,
            "max_contexts": 2,
        },
        automation={
            "default_action_timeout": 8000,
            "screenshot_dir": str(tmp_path / "screenshots"),
            "stop_on_error": True,
        },
        safety={
            "allow_js_evaluation": True,  # Tests use EvaluateInput
        },
    )


class TestClickAction:
    @pytest.mark.asyncio
    async def test_click_nonexistent_selector(self, auto_config: AppConfig) -> None:
        """Clicking a missing element should return TIMEOUT or FAILED."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://example.com",
                [ClickInput(selector="#does-not-exist", timeout=2000)],
            )
        assert result.actions_failed == 1
        assert result.results[0].status in (ActionStatus.FAILED, ActionStatus.TIMEOUT)


class TestFillAction:
    @pytest.mark.asyncio
    async def test_fill_input(self, auto_config: AppConfig) -> None:
        """Fill an input on httpbin.org/forms/post."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://httpbin.org/forms/post",
                [
                    WaitInput(target=WaitTarget.SELECTOR, value="input[name='custname']"),
                    FillInput(selector="input[name='custname']", value="Test User"),
                    EvaluateInput(
                        expression="document.querySelector('input[name=\"custname\"]').value"
                    ),
                ],
            )
        assert result.actions_succeeded == 3
        assert result.results[2].data["result"] == "Test User"


class TestNavigateAction:
    @pytest.mark.asyncio
    async def test_navigate_back_forward(self, auto_config: AppConfig) -> None:
        """Navigate to two pages, go back, verify URL."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://example.com",
                [
                    NavigateInput(
                        navigate_action=NavigateDirection.GOTO,
                        url="https://httpbin.org/html",
                    ),
                    NavigateInput(navigate_action=NavigateDirection.BACK),
                    EvaluateInput(expression="window.location.href"),
                ],
            )
        assert result.actions_succeeded == 3
        # After going back, should be at example.com
        assert "example.com" in result.results[2].data["result"]


class TestScreenshotAction:
    @pytest.mark.asyncio
    async def test_viewport_screenshot(self, auto_config: AppConfig) -> None:
        """Take a viewport screenshot."""
        async with Agent(auto_config) as agent:
            result = await agent.screenshot("https://example.com")

        assert result.status == ActionStatus.SUCCESS
        assert result.size_bytes > 0
        assert Path(result.path).exists()

    @pytest.mark.asyncio
    async def test_full_page_screenshot(self, auto_config: AppConfig) -> None:
        """Full-page screenshot should be at least as large as viewport."""
        async with Agent(auto_config) as agent:
            viewport = await agent.screenshot("https://example.com")
            full = await agent.screenshot(
                "https://example.com", full_page=True
            )

        assert full.status == ActionStatus.SUCCESS
        assert full.size_bytes > 0

    @pytest.mark.asyncio
    async def test_screenshot_in_sequence(self, auto_config: AppConfig) -> None:
        """Screenshot action within a sequence."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://example.com",
                [ScreenshotInput(full_page=False)],
            )
        assert result.actions_succeeded == 1
        ss_data = result.results[0].data
        assert "path" in ss_data
        assert ss_data["size_bytes"] > 0


class TestWaitAction:
    @pytest.mark.asyncio
    async def test_wait_for_selector_found(self, auto_config: AppConfig) -> None:
        """Wait for a known selector that exists."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://example.com",
                [WaitInput(target=WaitTarget.SELECTOR, value="h1")],
            )
        assert result.actions_succeeded == 1

    @pytest.mark.asyncio
    async def test_wait_for_selector_timeout(self, auto_config: AppConfig) -> None:
        """Wait for a missing selector should time out."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://example.com",
                [
                    WaitInput(
                        target=WaitTarget.SELECTOR,
                        value="#nonexistent",
                        timeout=2000,
                    )
                ],
            )
        assert result.actions_failed == 1
        assert result.results[0].status in (ActionStatus.TIMEOUT, ActionStatus.FAILED)


class TestEvaluateAction:
    @pytest.mark.asyncio
    async def test_evaluate_arithmetic(self, auto_config: AppConfig) -> None:
        """Evaluate simple JS expression."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://example.com",
                [EvaluateInput(expression="1 + 1")],
            )
        assert result.actions_succeeded == 1
        assert result.results[0].data["result"] == 2

    @pytest.mark.asyncio
    async def test_evaluate_dom(self, auto_config: AppConfig) -> None:
        """Evaluate DOM query."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://example.com",
                [EvaluateInput(expression="document.title")],
            )
        assert result.actions_succeeded == 1
        assert "Example" in result.results[0].data["result"]


class TestScrollAction:
    @pytest.mark.asyncio
    async def test_scroll_down(self, auto_config: AppConfig) -> None:
        """Scroll down and verify scroll position changed."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://example.com",
                [
                    ScrollInput(direction="down", amount=3),
                    EvaluateInput(expression="window.scrollY"),
                ],
            )
        assert result.actions_succeeded == 2


class TestKeyboardAction:
    @pytest.mark.asyncio
    async def test_keyboard_in_input(self, auto_config: AppConfig) -> None:
        """Type via keyboard into a focused input."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://httpbin.org/forms/post",
                [
                    ClickInput(selector="input[name='custname']"),
                    KeyboardInput(key="H"),
                    KeyboardInput(key="i"),
                    EvaluateInput(
                        expression="document.querySelector('input[name=\"custname\"]').value"
                    ),
                ],
            )
        assert result.actions_succeeded == 4
        assert result.results[3].data["result"] == "Hi"


class TestHoverAction:
    @pytest.mark.asyncio
    async def test_hover_element(self, auto_config: AppConfig) -> None:
        """Hover over an element without error."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://example.com",
                [HoverInput(selector="h1")],
            )
        assert result.actions_succeeded == 1


class TestSequenceExecution:
    @pytest.mark.asyncio
    async def test_full_sequence(self, auto_config: AppConfig) -> None:
        """Run a multi-step sequence: wait, evaluate, screenshot."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://example.com",
                [
                    WaitInput(target=WaitTarget.SELECTOR, value="h1"),
                    EvaluateInput(expression="document.title"),
                    ScreenshotInput(full_page=False),
                ],
            )
        assert result.actions_total == 3
        assert result.actions_succeeded == 3
        assert result.actions_failed == 0
        assert result.total_time_ms > 0

    @pytest.mark.asyncio
    async def test_stop_on_error_true(self, auto_config: AppConfig) -> None:
        """With stop_on_error=True, remaining actions should be SKIPPED."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://example.com",
                [
                    EvaluateInput(expression="1 + 1"),
                    ClickInput(selector="#bad-selector", timeout=2000),
                    EvaluateInput(expression="2 + 2"),  # should be skipped
                ],
                stop_on_error=True,
            )
        assert result.actions_succeeded == 1
        assert result.actions_failed == 1
        assert result.results[2].status == ActionStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_stop_on_error_false(self, auto_config: AppConfig) -> None:
        """With stop_on_error=False, all actions execute despite failures."""
        async with Agent(auto_config) as agent:
            result = await agent.interact(
                "https://example.com",
                [
                    EvaluateInput(expression="1 + 1"),
                    ClickInput(selector="#bad-selector", timeout=2000),
                    EvaluateInput(expression="2 + 2"),  # should still run
                ],
                stop_on_error=False,
            )
        assert result.actions_succeeded == 2
        assert result.actions_failed == 1
        assert result.results[2].status == ActionStatus.SUCCESS
        assert result.results[2].data["result"] == 4
