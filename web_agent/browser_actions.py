"""Browser automation actions: click, type, fill, scroll, screenshot, navigate, and more.

Provides a ``BrowserActions`` class that wraps Playwright's page API into
structured, composable actions. Each action returns an ``ActionResult``
with status, timing, and optional return data.

Actions can be composed into sequences and executed via
:meth:`BrowserActions.execute_sequence` or individually via
:meth:`BrowserActions.execute_action`.

Example::

    from web_agent import Agent
    from web_agent.models import ClickInput, FillInput, ScreenshotInput

    async with Agent() as agent:
        result = await agent.interact("https://example.com", [
            FillInput(selector="#search", value="query"),
            ClickInput(selector="button[type=submit]"),
            ScreenshotInput(full_page=True),
        ])
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from playwright.async_api import Dialog, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .browser_manager import BrowserManager
from .config import AppConfig
from .models import (
    Action,
    ActionResult,
    ActionSequenceResult,
    ActionStatus,
    ActionType,
    ClickInput,
    DialogInput,
    DialogResponse,
    EvaluateInput,
    FillInput,
    HoverInput,
    KeyboardInput,
    NavigateDirection,
    NavigateInput,
    ScreenshotFormat,
    ScreenshotInput,
    ScreenshotResult,
    ScrollDirection,
    ScrollInput,
    SelectInput,
    TypeInput,
    WaitInput,
    WaitTarget,
)


class _DialogState:
    """Thread-safe dialog handler state scoped to a single sequence execution."""

    def __init__(self) -> None:
        self.response: DialogResponse = DialogResponse.DISMISS
        self.prompt_text: str | None = None

    async def handle(self, dialog: Dialog) -> None:
        """Handle a browser dialog using the current configuration."""
        logger.debug("Dialog appeared: {type} - {msg}", type=dialog.type, msg=dialog.message)
        if self.response == DialogResponse.ACCEPT:
            if self.prompt_text is not None:
                await dialog.accept(self.prompt_text)
            else:
                await dialog.accept()
        else:
            await dialog.dismiss()


class BrowserActions:
    """Executes browser automation actions on Playwright pages.

    Each action handler wraps a Playwright API call, catches errors,
    and returns a structured :class:`ActionResult`. Actions can run
    individually or as ordered sequences.

    Args:
        browser_manager: The shared browser lifecycle manager.
        config: Application configuration.
    """

    def __init__(self, browser_manager: BrowserManager, config: AppConfig) -> None:
        self._bm = browser_manager
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_sequence(
        self,
        url: str,
        actions: list[Action],
        stop_on_error: bool | None = None,
    ) -> ActionSequenceResult:
        """Navigate to a URL and execute an ordered list of browser actions.

        Args:
            url: Starting URL to navigate to before executing actions.
            actions: Ordered list of action inputs to execute.
            stop_on_error: If ``True``, halt on first failure and mark remaining
                actions as SKIPPED. ``None`` reads from config.

        Returns:
            ActionSequenceResult with per-action results and aggregate counts.
        """
        should_stop = (
            stop_on_error
            if stop_on_error is not None
            else self._config.automation.stop_on_error
        )
        start = time.perf_counter()
        results: list[ActionResult] = []
        succeeded = 0
        failed = 0

        # Per-sequence dialog state (thread-safe - not shared across sequences)
        dialog_state = _DialogState()

        async with self._bm.new_page(block_resources=False) as page:
            await page.goto(url, wait_until="domcontentloaded")
            page.on("dialog", dialog_state.handle)
            # Store dialog_state on page for _do_dialog to access
            page._web_agent_dialog_state = dialog_state  # type: ignore[attr-defined]

            for action_input in actions:
                # Optional delay between actions
                if self._config.automation.slow_mo_actions > 0:
                    await asyncio.sleep(
                        self._config.automation.slow_mo_actions / 1000
                    )

                result = await self.execute_action(page, action_input)
                results.append(result)

                if result.status in (ActionStatus.SUCCESS,):
                    succeeded += 1
                else:
                    failed += 1
                    if should_stop:
                        # Mark remaining actions as skipped
                        for remaining in actions[len(results) :]:
                            results.append(
                                ActionResult(
                                    action=ActionType(remaining.action),
                                    status=ActionStatus.SKIPPED,
                                    selector=getattr(remaining, "selector", None),
                                )
                            )
                        break

        elapsed = (time.perf_counter() - start) * 1000
        return ActionSequenceResult(
            url=url,
            actions_total=len(actions),
            actions_succeeded=succeeded,
            actions_failed=failed,
            results=results,
            total_time_ms=elapsed,
        )

    async def execute_action(self, page: Page, action_input: Action) -> ActionResult:
        """Execute a single action on a page. Catches errors and returns structured result."""
        action_type = ActionType(action_input.action)
        handler = self._dispatch.get(action_type)
        if not handler:
            return ActionResult(
                action=action_type,
                status=ActionStatus.FAILED,
                error_message=f"Unknown action type: {action_type}",
            )

        start = time.perf_counter()
        try:
            result = await handler(self, page, action_input)
            result.duration_ms = (time.perf_counter() - start) * 1000
            return result
        except PlaywrightTimeout as e:
            return ActionResult(
                action=action_type,
                status=ActionStatus.TIMEOUT,
                selector=getattr(action_input, "selector", None),
                duration_ms=(time.perf_counter() - start) * 1000,
                error_message=str(e),
            )
        except Exception as e:
            return ActionResult(
                action=action_type,
                status=ActionStatus.FAILED,
                selector=getattr(action_input, "selector", None),
                duration_ms=(time.perf_counter() - start) * 1000,
                error_message=str(e),
            )

    async def take_screenshot(
        self,
        url: str,
        path: str | None = None,
        full_page: bool = False,
        format: ScreenshotFormat = ScreenshotFormat.PNG,
        quality: int | None = None,
    ) -> ScreenshotResult:
        """Convenience: navigate to URL and take a screenshot."""
        ss_dir = Path(self._config.automation.screenshot_dir)
        ss_dir.mkdir(parents=True, exist_ok=True)

        if not path:
            ext = "png" if format == ScreenshotFormat.PNG else "jpg"
            safe_url = "".join(c if c.isalnum() else "_" for c in url)[:50]
            path = str(ss_dir / f"{safe_url}.{ext}")

        async with self._bm.new_page(block_resources=False) as page:
            await page.goto(url, wait_until="networkidle")
            ss_kwargs: dict[str, Any] = {
                "path": path,
                "full_page": full_page,
                "type": format.value,
            }
            if format == ScreenshotFormat.JPEG and quality is not None:
                ss_kwargs["quality"] = quality
            await page.screenshot(**ss_kwargs)

        size = Path(path).stat().st_size
        logger.info("Screenshot saved: {path} ({size} bytes)", path=path, size=size)
        return ScreenshotResult(
            url=url,
            path=path,
            format=format,
            size_bytes=size,
            status=ActionStatus.SUCCESS,
        )

    # ------------------------------------------------------------------
    # Action Handlers
    # ------------------------------------------------------------------

    def _resolve_timeout(self, action_timeout: int | None) -> int:
        return action_timeout or self._config.automation.default_action_timeout

    async def _do_click(self, page: Page, action: ClickInput) -> ActionResult:
        timeout = self._resolve_timeout(action.timeout)
        click_count = 2 if action.double_click else 1
        await page.click(
            action.selector,
            button=action.button.value,
            click_count=click_count,
            modifiers=action.modifiers if action.modifiers else None,
            timeout=timeout,
        )
        logger.debug("Clicked {sel}", sel=action.selector)
        return ActionResult(
            action=ActionType.CLICK,
            status=ActionStatus.SUCCESS,
            selector=action.selector,
        )

    async def _do_type(self, page: Page, action: TypeInput) -> ActionResult:
        timeout = self._resolve_timeout(action.timeout)
        if action.clear_first:
            await page.fill(action.selector, "", timeout=timeout)
        await page.type(action.selector, action.text, delay=action.delay, timeout=timeout)
        logger.debug("Typed into {sel}", sel=action.selector)
        return ActionResult(
            action=ActionType.TYPE,
            status=ActionStatus.SUCCESS,
            selector=action.selector,
            data={"text_length": len(action.text)},
        )

    async def _do_fill(self, page: Page, action: FillInput) -> ActionResult:
        timeout = self._resolve_timeout(action.timeout)
        await page.fill(action.selector, action.value, timeout=timeout)
        logger.debug("Filled {sel}", sel=action.selector)
        return ActionResult(
            action=ActionType.FILL,
            status=ActionStatus.SUCCESS,
            selector=action.selector,
        )

    async def _do_scroll(self, page: Page, action: ScrollInput) -> ActionResult:
        timeout = self._resolve_timeout(action.timeout)

        # Mode 1: Scroll element into view
        if action.selector:
            loc = page.locator(action.selector)
            await loc.scroll_into_view_if_needed(timeout=timeout)
            logger.debug("Scrolled {sel} into view", sel=action.selector)
            return ActionResult(
                action=ActionType.SCROLL,
                status=ActionStatus.SUCCESS,
                selector=action.selector,
            )

        # Mode 2: Infinite scroll
        if action.infinite_scroll:
            iterations = 0
            for _ in range(action.infinite_scroll_max):
                prev_height = await page.evaluate("document.body.scrollHeight")
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(action.infinite_scroll_delay_ms / 1000)
                new_height = await page.evaluate("document.body.scrollHeight")
                iterations += 1
                if new_height == prev_height:
                    break
            logger.debug("Infinite scroll: {n} iterations", n=iterations)
            return ActionResult(
                action=ActionType.SCROLL,
                status=ActionStatus.SUCCESS,
                data={"iterations": iterations},
            )

        # Mode 3: Normal scroll by direction and amount
        dx, dy = 0, 0
        pixels = action.amount * 100  # each tick ≈ 100px
        if action.direction == ScrollDirection.DOWN:
            dy = pixels
        elif action.direction == ScrollDirection.UP:
            dy = -pixels
        elif action.direction == ScrollDirection.RIGHT:
            dx = pixels
        elif action.direction == ScrollDirection.LEFT:
            dx = -pixels
        await page.mouse.wheel(dx, dy)
        logger.debug("Scrolled {dir} by {amt}", dir=action.direction.value, amt=action.amount)
        return ActionResult(
            action=ActionType.SCROLL,
            status=ActionStatus.SUCCESS,
            data={"direction": action.direction.value, "amount": action.amount},
        )

    async def _do_screenshot(self, page: Page, action: ScreenshotInput) -> ActionResult:
        ss_dir = Path(self._config.automation.screenshot_dir)
        ss_dir.mkdir(parents=True, exist_ok=True)

        path = action.path
        if not path:
            ext = "png" if action.format == ScreenshotFormat.PNG else "jpg"
            path = str(ss_dir / f"screenshot_{int(time.time())}.{ext}")

        ss_kwargs: dict[str, Any] = {
            "path": path,
            "type": action.format.value,
        }
        if action.format == ScreenshotFormat.JPEG and action.quality is not None:
            ss_kwargs["quality"] = action.quality

        if action.selector:
            loc = page.locator(action.selector)
            await loc.screenshot(**ss_kwargs)
        else:
            ss_kwargs["full_page"] = action.full_page
            await page.screenshot(**ss_kwargs)

        size = Path(path).stat().st_size
        logger.debug("Screenshot saved: {path}", path=path)
        return ActionResult(
            action=ActionType.SCREENSHOT,
            status=ActionStatus.SUCCESS,
            selector=action.selector,
            data={"path": path, "size_bytes": size},
        )

    async def _do_navigate(self, page: Page, action: NavigateInput) -> ActionResult:
        if action.navigate_action == NavigateDirection.GOTO:
            if not action.url:
                return ActionResult(
                    action=ActionType.NAVIGATE,
                    status=ActionStatus.FAILED,
                    error_message="URL required for goto navigation",
                )
            await page.goto(action.url, wait_until=action.wait_until)
        elif action.navigate_action == NavigateDirection.BACK:
            await page.go_back()
        elif action.navigate_action == NavigateDirection.FORWARD:
            await page.go_forward()
        elif action.navigate_action == NavigateDirection.RELOAD:
            await page.reload()

        logger.debug("Navigated: {act} -> {url}", act=action.navigate_action.value, url=page.url)
        return ActionResult(
            action=ActionType.NAVIGATE,
            status=ActionStatus.SUCCESS,
            data={"url": page.url},
        )

    async def _do_dialog(self, page: Page, action: DialogInput) -> ActionResult:
        dialog_state: _DialogState = getattr(page, "_web_agent_dialog_state", _DialogState())
        dialog_state.response = action.dialog_action
        dialog_state.prompt_text = action.prompt_text
        logger.debug(
            "Dialog handler set: {act} (prompt={p})",
            act=action.dialog_action.value,
            p=action.prompt_text,
        )
        return ActionResult(
            action=ActionType.DIALOG,
            status=ActionStatus.SUCCESS,
            data={"dialog_action": action.dialog_action.value},
        )

    async def _do_hover(self, page: Page, action: HoverInput) -> ActionResult:
        timeout = self._resolve_timeout(action.timeout)
        await page.hover(action.selector, timeout=timeout)
        logger.debug("Hovered {sel}", sel=action.selector)
        return ActionResult(
            action=ActionType.HOVER,
            status=ActionStatus.SUCCESS,
            selector=action.selector,
        )

    async def _do_select(self, page: Page, action: SelectInput) -> ActionResult:
        timeout = self._resolve_timeout(action.timeout)
        kwargs: dict[str, Any] = {"timeout": timeout}
        if action.value is not None:
            kwargs["value"] = action.value
        elif action.label is not None:
            kwargs["label"] = action.label
        elif action.index is not None:
            kwargs["index"] = action.index
        else:
            return ActionResult(
                action=ActionType.SELECT,
                status=ActionStatus.FAILED,
                selector=action.selector,
                error_message="Must specify value, label, or index",
            )
        await page.select_option(action.selector, **kwargs)
        logger.debug("Selected option in {sel}", sel=action.selector)
        return ActionResult(
            action=ActionType.SELECT,
            status=ActionStatus.SUCCESS,
            selector=action.selector,
        )

    async def _do_keyboard(self, page: Page, action: KeyboardInput) -> ActionResult:
        for _ in range(action.repeat):
            await page.keyboard.press(action.key)
        logger.debug("Pressed key {key} x{n}", key=action.key, n=action.repeat)
        return ActionResult(
            action=ActionType.KEYBOARD,
            status=ActionStatus.SUCCESS,
            data={"key": action.key, "repeat": action.repeat},
        )

    async def _do_wait(self, page: Page, action: WaitInput) -> ActionResult:
        timeout = self._resolve_timeout(action.timeout)

        if action.target == WaitTarget.SELECTOR:
            if not action.value:
                return ActionResult(
                    action=ActionType.WAIT,
                    status=ActionStatus.FAILED,
                    error_message="Selector value required for selector wait",
                )
            await page.wait_for_selector(
                action.value, state=action.state, timeout=timeout
            )
        elif action.target == WaitTarget.URL:
            if not action.value:
                return ActionResult(
                    action=ActionType.WAIT,
                    status=ActionStatus.FAILED,
                    error_message="URL pattern required for URL wait",
                )
            await page.wait_for_url(f"**{action.value}**", timeout=timeout)
        elif action.target == WaitTarget.NETWORK_IDLE:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        elif action.target == WaitTarget.LOAD_STATE:
            state = action.value or "load"
            await page.wait_for_load_state(state, timeout=timeout)
        elif action.target == WaitTarget.TEXT:
            if not action.value:
                return ActionResult(
                    action=ActionType.WAIT,
                    status=ActionStatus.FAILED,
                    error_message="Text value required for text wait",
                )
            await page.locator(f"text={action.value}").wait_for(timeout=timeout)
        elif action.target == WaitTarget.FUNCTION:
            if not action.value:
                return ActionResult(
                    action=ActionType.WAIT,
                    status=ActionStatus.FAILED,
                    error_message="Function body required for function wait",
                )
            await page.wait_for_function(action.value, timeout=timeout)

        logger.debug("Wait completed: {target}", target=action.target.value)
        return ActionResult(
            action=ActionType.WAIT,
            status=ActionStatus.SUCCESS,
            data={"target": action.target.value},
        )

    async def _do_evaluate(self, page: Page, action: EvaluateInput) -> ActionResult:
        result = await page.evaluate(action.expression)
        # Ensure the result is JSON-serializable
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            result = str(result)
        logger.debug("Evaluated JS expression")
        return ActionResult(
            action=ActionType.EVALUATE,
            status=ActionStatus.SUCCESS,
            data={"result": result},
        )

    # ------------------------------------------------------------------
    # Dispatch table
    # ------------------------------------------------------------------

    _dispatch: dict = {
        ActionType.CLICK: _do_click,
        ActionType.TYPE: _do_type,
        ActionType.FILL: _do_fill,
        ActionType.SCROLL: _do_scroll,
        ActionType.SCREENSHOT: _do_screenshot,
        ActionType.NAVIGATE: _do_navigate,
        ActionType.DIALOG: _do_dialog,
        ActionType.HOVER: _do_hover,
        ActionType.SELECT: _do_select,
        ActionType.KEYBOARD: _do_keyboard,
        ActionType.WAIT: _do_wait,
        ActionType.EVALUATE: _do_evaluate,
    }
