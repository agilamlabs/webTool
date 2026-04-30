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
import contextlib
import json
import time
from pathlib import Path
from typing import Any, ClassVar, Optional
from urllib.parse import urlparse

from loguru import logger
from playwright.async_api import Dialog, Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .browser_manager import BrowserManager
from .config import AppConfig
from .correlation import get_correlation_id
from .debug import DebugCapture
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
    LocatorSpec,
    NavigateDirection,
    NavigateInput,
    ScreenshotFormat,
    ScreenshotInput,
    ScreenshotResult,
    ScrollDirection,
    ScrollInput,
    SelectInput,
    SelectorLike,
    TypeInput,
    WaitInput,
    WaitTarget,
)
from .session_manager import SessionManager
from .utils import check_domain_allowed, safe_join_path

# Heuristic CSS patterns that look like submit buttons (used by safe_mode)
_SUBMIT_BUTTON_HINTS = (
    "button[type=submit]",
    "button[type='submit']",
    'button[type="submit"]',
    "input[type=submit]",
    "input[type='submit']",
    'input[type="submit"]',
)


_SUBMIT_TEXT_KEYWORDS = (
    "submit",
    "send",
    "save",
    "log in",
    "sign in",
    "register",
    "create account",
    "continue",
)


def _looks_like_submit(selector: SelectorLike | None) -> bool:
    """Best-effort heuristic: does this selector look like a submit button?

    For a CSS selector string, checks for ``button[type=submit]`` patterns.
    For a LocatorSpec, checks every text-bearing field (role_name, text,
    label, placeholder) for submit-like keywords (submit, send, save,
    log in, sign in, register, create account, continue).
    """
    if selector is None:
        return False
    if isinstance(selector, LocatorSpec):
        # Check ALL text-bearing fields, not just role_name
        for text_field in (
            selector.role_name,
            selector.text,
            selector.label,
            selector.placeholder,
        ):
            if text_field and any(kw in text_field.lower() for kw in _SUBMIT_TEXT_KEYWORDS):
                return True
        sel_str = (selector.selector or "").lower()
    else:
        sel_str = selector.lower()
    return any(hint in sel_str for hint in _SUBMIT_BUTTON_HINTS)


def _selector_repr(selector: SelectorLike | None) -> Optional[str]:
    """Render a selector value for inclusion in ActionResult.selector (a str field)."""
    if selector is None:
        return None
    if isinstance(selector, LocatorSpec):
        return selector.model_dump_json(exclude_none=True)
    return selector


def _resolve_locator(page: Page, spec: SelectorLike) -> Locator:
    """Convert a CSS selector string or LocatorSpec into a Playwright Locator.

    Resolution priority for LocatorSpec (first non-None wins):
        role > test_id > label > placeholder > text > selector

    Raises:
        SelectorNotFoundError: If ``spec`` is an empty LocatorSpec.
    """
    from .exceptions import SelectorNotFoundError

    if isinstance(spec, str):
        return page.locator(spec)

    if spec.role:
        # Playwright types role as a Literal of ARIA roles; we accept any
        # str at the API boundary and let Playwright validate at runtime.
        if spec.role_name:
            return page.get_by_role(spec.role, name=spec.role_name)  # type: ignore[arg-type]
        return page.get_by_role(spec.role)  # type: ignore[arg-type]
    if spec.test_id:
        return page.get_by_test_id(spec.test_id)
    if spec.label:
        return page.get_by_label(spec.label)
    if spec.placeholder:
        return page.get_by_placeholder(spec.placeholder)
    if spec.text:
        return page.get_by_text(spec.text)
    if spec.selector:
        return page.locator(spec.selector)

    raise SelectorNotFoundError(
        "LocatorSpec is empty (no selector field set)",
        action="resolve_locator",
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
        sessions: Optional SessionManager for persistent browser sessions.
        debug: Optional DebugCapture for failure artifact capture.
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        config: AppConfig,
        sessions: Optional[SessionManager] = None,
        debug: Optional[DebugCapture] = None,
    ) -> None:
        self._bm = browser_manager
        self._config = config
        self._sessions = sessions
        self._debug = debug or DebugCapture(config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_sequence(
        self,
        url: str,
        actions: list[Action],
        stop_on_error: bool | None = None,
        session_id: Optional[str] = None,
    ) -> ActionSequenceResult:
        """Navigate to a URL and execute an ordered list of browser actions.

        Args:
            url: Starting URL to navigate to before executing actions.
            actions: Ordered list of action inputs to execute.
            stop_on_error: If ``True``, halt on first failure and mark remaining
                actions as SKIPPED. ``None`` reads from config.
            session_id: Optional persistent browser session for the entire sequence.

        Returns:
            ActionSequenceResult with per-action results and aggregate counts.
        """
        cid = get_correlation_id()

        # Domain allow/deny gate on the starting URL
        if not check_domain_allowed(url, self._config.safety):
            host = urlparse(url).hostname or ""
            return ActionSequenceResult(
                url=url,
                actions_total=len(actions),
                actions_failed=len(actions),
                results=[
                    ActionResult(
                        action=ActionType(a.action),
                        status=ActionStatus.SKIPPED,
                        selector=_selector_repr(getattr(a, "selector", None)),
                        error_message=f"Domain not allowed: {host}",
                    )
                    for a in actions
                ],
                correlation_id=cid,
            )

        # Pre-flight granular safety checks
        safety = self._config.safety

        def _block_all(reason: str) -> ActionSequenceResult:
            return ActionSequenceResult(
                url=url,
                actions_total=len(actions),
                actions_failed=len(actions),
                results=[
                    ActionResult(
                        action=ActionType(act.action),
                        status=ActionStatus.SKIPPED,
                        selector=_selector_repr(getattr(act, "selector", None)),
                        error_message=reason,
                    )
                    for act in actions
                ],
                correlation_id=cid,
            )

        for a in actions:
            if isinstance(a, EvaluateInput) and not safety.allow_js_evaluation:
                return _block_all(
                    "EvaluateInput blocked: safety.allow_js_evaluation=False "
                    "(set safety.allow_js_evaluation=True to opt in)"
                )
            if (
                isinstance(a, ClickInput)
                and not safety.allow_form_submit
                and _looks_like_submit(a.selector)
            ):
                return _block_all(
                    "Submit-button click blocked: safety.allow_form_submit=False "
                    "(form submission heuristic; set allow_form_submit=True to opt in)"
                )

        should_stop = (
            stop_on_error if stop_on_error is not None else self._config.automation.stop_on_error
        )
        start = time.perf_counter()
        results: list[ActionResult] = []
        succeeded = 0
        failed = 0
        all_artifacts: list[str] = []

        # Per-sequence dialog state (thread-safe - not shared across sequences)
        dialog_state = _DialogState()

        # Initialize cleanup state BEFORE the branch so the finally block is
        # safe even if page-acquisition raises (e.g. unknown session_id, or
        # ctx.new_page() fails on a closed browser context).
        page = None
        ctx_mgr = None
        owner = "ephemeral"

        try:
            if session_id and self._sessions is not None:
                owner = "session"
                ctx = self._sessions.get(session_id)
                self._sessions.touch(session_id)
                page = await ctx.new_page()
            else:
                ctx_mgr = self._bm.new_page(block_resources=False)
                page = await ctx_mgr.__aenter__()

            await page.goto(url, wait_until="domcontentloaded")
            page.on("dialog", dialog_state.handle)
            page._web_agent_dialog_state = dialog_state  # type: ignore[attr-defined]

            for action_input in actions:
                if self._config.automation.slow_mo_actions > 0:
                    await asyncio.sleep(self._config.automation.slow_mo_actions / 1000)

                result = await self.execute_action(page, action_input)
                if result.debug_artifacts:
                    all_artifacts.extend(result.debug_artifacts)
                results.append(result)

                if result.status == ActionStatus.SUCCESS:
                    succeeded += 1
                else:
                    failed += 1
                    if should_stop:
                        for remaining in actions[len(results) :]:
                            results.append(
                                ActionResult(
                                    action=ActionType(remaining.action),
                                    status=ActionStatus.SKIPPED,
                                    selector=_selector_repr(getattr(remaining, "selector", None)),
                                )
                            )
                        break
        except Exception as exc:
            # Acquisition or sequence error -- record it and let cleanup run.
            failed += 1
            results.append(
                ActionResult(
                    action=ActionType.NAVIGATE,
                    status=ActionStatus.FAILED,
                    error_message=f"Sequence aborted: {exc}",
                )
            )
            for remaining in actions[len(results) :]:
                results.append(
                    ActionResult(
                        action=ActionType(remaining.action),
                        status=ActionStatus.SKIPPED,
                        selector=_selector_repr(getattr(remaining, "selector", None)),
                    )
                )
        finally:
            if page is not None and owner == "session":
                with contextlib.suppress(Exception):
                    await page.close()
            elif ctx_mgr is not None:
                with contextlib.suppress(Exception):
                    await ctx_mgr.__aexit__(None, None, None)

        elapsed = (time.perf_counter() - start) * 1000
        return ActionSequenceResult(
            url=url,
            actions_total=len(actions),
            actions_succeeded=succeeded,
            actions_failed=failed,
            results=results,
            total_time_ms=elapsed,
            correlation_id=cid,
            debug_artifacts=all_artifacts,
        )

    async def execute_action(self, page: Page, action_input: Action) -> ActionResult:
        """Execute a single action on a page. Catches errors and returns structured result.

        Internal: action handlers may raise :class:`ActionError`,
        :class:`ActionTimeoutError`, or :class:`SelectorNotFoundError`.
        These are caught here and converted to structured ActionResult so
        the caller (typically :meth:`execute_sequence`) sees consistent
        result-based control flow regardless of failure mode.
        """
        from .exceptions import ActionError, ActionTimeoutError, SelectorNotFoundError

        action_type = ActionType(action_input.action)
        handler = self._dispatch.get(action_type)
        sel_repr = _selector_repr(getattr(action_input, "selector", None))
        if not handler:
            return ActionResult(
                action=action_type,
                status=ActionStatus.FAILED,
                error_message=f"Unknown action type: {action_type}",
            )

        start = time.perf_counter()
        try:
            result: ActionResult = await handler(self, page, action_input)
            result.duration_ms = (time.perf_counter() - start) * 1000
            return result
        except (PlaywrightTimeout, ActionTimeoutError) as e:
            artifacts: list[str] = []
            if self._debug.enabled:
                artifacts = await self._debug.capture_page(
                    page, e, action_type.value, context={"selector": sel_repr}
                )
            return ActionResult(
                action=action_type,
                status=ActionStatus.TIMEOUT,
                selector=sel_repr,
                duration_ms=(time.perf_counter() - start) * 1000,
                error_message=str(e),
                debug_artifacts=artifacts,
            )
        except (ActionError, SelectorNotFoundError, Exception) as e:
            artifacts = []
            if self._debug.enabled:
                artifacts = await self._debug.capture_page(
                    page, e, action_type.value, context={"selector": sel_repr}
                )
            return ActionResult(
                action=action_type,
                status=ActionStatus.FAILED,
                selector=sel_repr,
                duration_ms=(time.perf_counter() - start) * 1000,
                error_message=str(e),
                debug_artifacts=artifacts,
            )

    async def take_screenshot(
        self,
        url: str,
        path: str | None = None,
        full_page: bool = False,
        format: ScreenshotFormat = ScreenshotFormat.PNG,
        quality: int | None = None,
        session_id: Optional[str] = None,
    ) -> ScreenshotResult:
        """Convenience: navigate to URL and take a screenshot."""
        cid = get_correlation_id()

        if not check_domain_allowed(url, self._config.safety):
            return ScreenshotResult(
                url=url,
                path="",
                format=format,
                status=ActionStatus.FAILED,
                correlation_id=cid,
            )

        ss_dir = Path(self._config.automation.screenshot_dir)
        ss_dir.mkdir(parents=True, exist_ok=True)

        if not path:
            ext = "png" if format == ScreenshotFormat.PNG else "jpg"
            safe_url = "".join(c if c.isalnum() else "_" for c in url)[:50]
            path = str(ss_dir / f"{safe_url}.{ext}")
        else:
            # Caller-supplied path -- defend against path traversal.
            try:
                path = str(safe_join_path(ss_dir, path))
            except ValueError as exc:
                logger.warning("Rejected screenshot path: {e}", e=exc)
                return ScreenshotResult(
                    url=url,
                    path="",
                    format=format,
                    status=ActionStatus.FAILED,
                    correlation_id=cid,
                )

        async def _capture(page: Page) -> None:
            await page.goto(url, wait_until="networkidle")
            ss_kwargs: dict[str, Any] = {
                "path": path,
                "full_page": full_page,
                "type": format.value,
            }
            if format == ScreenshotFormat.JPEG and quality is not None:
                ss_kwargs["quality"] = quality
            await page.screenshot(**ss_kwargs)

        if session_id and self._sessions is not None:
            ctx = self._sessions.get(session_id)
            self._sessions.touch(session_id)
            page = await ctx.new_page()
            try:
                await _capture(page)
            finally:
                await page.close()
        else:
            async with self._bm.new_page(block_resources=False) as page:
                await _capture(page)

        size = Path(path).stat().st_size
        logger.info("Screenshot saved: {path} ({size} bytes)", path=path, size=size)
        return ScreenshotResult(
            url=url,
            path=path,
            format=format,
            size_bytes=size,
            status=ActionStatus.SUCCESS,
            correlation_id=cid,
        )

    # ------------------------------------------------------------------
    # Action Handlers
    # ------------------------------------------------------------------

    def _resolve_timeout(self, action_timeout: int | None) -> int:
        return action_timeout or self._config.automation.default_action_timeout

    async def _do_click(self, page: Page, action: ClickInput) -> ActionResult:
        timeout = self._resolve_timeout(action.timeout)
        click_count = 2 if action.double_click else 1
        loc = _resolve_locator(page, action.selector)
        await loc.click(
            button=action.button.value,
            click_count=click_count,
            modifiers=action.modifiers if action.modifiers else None,  # type: ignore[arg-type]
            timeout=timeout,
        )
        sel_repr = _selector_repr(action.selector)
        logger.debug("Clicked {sel}", sel=sel_repr)
        return ActionResult(
            action=ActionType.CLICK,
            status=ActionStatus.SUCCESS,
            selector=sel_repr,
        )

    async def _do_type(self, page: Page, action: TypeInput) -> ActionResult:
        timeout = self._resolve_timeout(action.timeout)
        loc = _resolve_locator(page, action.selector)
        if action.clear_first:
            await loc.fill("", timeout=timeout)
        # Locator API: fill() then type() doesn't exist as a single combo;
        # for type-with-keystrokes use locator.press_sequentially when available,
        # else fall back to keyboard.type after focus.
        if hasattr(loc, "press_sequentially"):
            await loc.press_sequentially(action.text, delay=action.delay, timeout=timeout)
        else:
            await loc.click(timeout=timeout)
            await page.keyboard.type(action.text, delay=action.delay)
        sel_repr = _selector_repr(action.selector)
        logger.debug("Typed into {sel}", sel=sel_repr)
        return ActionResult(
            action=ActionType.TYPE,
            status=ActionStatus.SUCCESS,
            selector=sel_repr,
            data={"text_length": len(action.text)},
        )

    async def _do_fill(self, page: Page, action: FillInput) -> ActionResult:
        timeout = self._resolve_timeout(action.timeout)
        loc = _resolve_locator(page, action.selector)
        await loc.fill(action.value, timeout=timeout)
        sel_repr = _selector_repr(action.selector)
        logger.debug("Filled {sel}", sel=sel_repr)
        return ActionResult(
            action=ActionType.FILL,
            status=ActionStatus.SUCCESS,
            selector=sel_repr,
        )

    async def _do_scroll(self, page: Page, action: ScrollInput) -> ActionResult:
        timeout = self._resolve_timeout(action.timeout)

        # Mode 1: Scroll element into view
        if action.selector:
            loc = _resolve_locator(page, action.selector)
            await loc.scroll_into_view_if_needed(timeout=timeout)
            sel_repr = _selector_repr(action.selector)
            logger.debug("Scrolled {sel} into view", sel=sel_repr)
            return ActionResult(
                action=ActionType.SCROLL,
                status=ActionStatus.SUCCESS,
                selector=sel_repr,
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
        else:
            # Defend against path-traversal in caller-supplied path.
            try:
                path = str(safe_join_path(ss_dir, path))
            except ValueError as exc:
                return ActionResult(
                    action=ActionType.SCREENSHOT,
                    status=ActionStatus.FAILED,
                    selector=_selector_repr(action.selector),
                    error_message=f"Invalid screenshot path: {exc}",
                )

        ss_kwargs: dict[str, Any] = {
            "path": path,
            "type": action.format.value,
        }
        if action.format == ScreenshotFormat.JPEG and action.quality is not None:
            ss_kwargs["quality"] = action.quality

        if action.selector:
            loc = _resolve_locator(page, action.selector)
            await loc.screenshot(**ss_kwargs)
        else:
            ss_kwargs["full_page"] = action.full_page
            await page.screenshot(**ss_kwargs)

        size = Path(path).stat().st_size
        logger.debug("Screenshot saved: {path}", path=path)
        return ActionResult(
            action=ActionType.SCREENSHOT,
            status=ActionStatus.SUCCESS,
            selector=_selector_repr(action.selector),
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
            await page.goto(action.url, wait_until=action.wait_until)  # type: ignore[arg-type]
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
        loc = _resolve_locator(page, action.selector)
        await loc.hover(timeout=timeout)
        sel_repr = _selector_repr(action.selector)
        logger.debug("Hovered {sel}", sel=sel_repr)
        return ActionResult(
            action=ActionType.HOVER,
            status=ActionStatus.SUCCESS,
            selector=sel_repr,
        )

    async def _do_select(self, page: Page, action: SelectInput) -> ActionResult:
        from .exceptions import ActionError

        timeout = self._resolve_timeout(action.timeout)
        sel_repr = _selector_repr(action.selector)
        kwargs: dict[str, Any] = {"timeout": timeout}
        if action.value is not None:
            kwargs["value"] = action.value
        elif action.label is not None:
            kwargs["label"] = action.label
        elif action.index is not None:
            kwargs["index"] = action.index
        else:
            raise ActionError(
                "Select action must specify one of: value, label, or index",
                action="select",
                selector=sel_repr,
            )
        loc = _resolve_locator(page, action.selector)
        await loc.select_option(**kwargs)
        logger.debug("Selected option in {sel}", sel=sel_repr)
        return ActionResult(
            action=ActionType.SELECT,
            status=ActionStatus.SUCCESS,
            selector=sel_repr,
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
                action.value,
                state=action.state,  # type: ignore[arg-type]
                timeout=timeout,
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
            await page.wait_for_load_state(state, timeout=timeout)  # type: ignore[arg-type]
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

    # Class-level lookup table: ActionType -> handler function (unbound).
    # Annotated as ClassVar so it's clearly shared state, not a per-instance dict.
    _dispatch: ClassVar[dict[ActionType, Any]] = {
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
