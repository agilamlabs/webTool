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
import re
import time
from pathlib import Path
from typing import Any, ClassVar, Optional
from urllib.parse import urlparse
from weakref import WeakKeyDictionary, WeakSet

from loguru import logger
from playwright.async_api import Dialog, Locator, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout
from pydantic import ValidationError

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
    ClickXYInput,
    DialogInput,
    DialogResponse,
    DragAndDropInput,
    EvaluateInput,
    FillInput,
    HoverInput,
    IframeClickInput,
    InteractiveElement,
    KeyboardInput,
    LocatorSpec,
    NavigateDirection,
    NavigateInput,
    ObserveResult,
    PressKeyInput,
    ScreenshotFormat,
    ScreenshotInput,
    ScreenshotResult,
    ScrollDirection,
    ScrollInput,
    SelectInput,
    SelectorLike,
    ShadowDomClickInput,
    TypeInput,
    TypeTextInput,
    UploadFileInput,
    WaitInput,
    WaitTarget,
)
from .session_manager import SessionManager
from .utils import _is_cross_platform_absolute, check_domain_allowed, safe_join_path

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


# v1.6.9: coordinate-click safety. Pattern matches submit/login/destructive
# verbs in element text or aria-label; used by _looks_like_destructive_at_point
# to gate click_xy when allow_form_submit=False.
_DESTRUCTIVE_TEXT_PATTERN = re.compile(
    r"\b("
    r"submit|send|save|"
    r"log\s*in|login|sign\s*in|signin|"
    r"register|sign\s*up|signup|create\s*account|"
    r"continue|next|proceed|confirm|"
    r"delete|remove|"
    r"pay|buy|purchase|checkout|order|"
    r"accept|agree|consent|allow|enable"
    r")\b",
    re.IGNORECASE,
)


# JS snippet: collect up to 5 ancestors (inner -> outer) of the element at
# (x, y). Returns [] if no element is hit (offscreen / outside viewport).
# Used by _do_click_xy when allow_form_submit=False to feed the
# destructive-control heuristic without requiring a selector.
_ELEMENT_FROM_POINT_JS = """
({x, y}) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return [];
  const items = [];
  let n = el;
  while (n && items.length < 5) {
    items.push({
      tag: (n.tagName || '').toLowerCase(),
      type: n.getAttribute && n.getAttribute('type'),
      role: n.getAttribute && n.getAttribute('role'),
      text: ((n.innerText || n.value || '') + '').slice(0, 120),
      aria: n.getAttribute && n.getAttribute('aria-label'),
      in_form: !!(n.closest && n.closest('form')),
    });
    n = n.parentElement;
  }
  return items;
}
"""


# v1.7.0 Wave 4C: a set-of-marks element ref is always ``e`` + one or more
# digits (minted by the enumeration JS below). Used to validate
# ``LocatorSpec.ref`` before it is interpolated into a ``[data-webtool-ref]``
# attribute selector -- a malformed value must never reach page.locator.
_REF_PATTERN = re.compile(r"e[0-9]+")

# JS snippet (set-of-marks): walk the DOM ONCE and return a bounded, numbered
# list of the genuinely actionable elements that are VISIBLE in the viewport.
# First-party tool instrumentation (NOT caller-supplied JS) -- gated like the
# rest of observe()'s snapshot evaluates, i.e. allowed as internal capture, not
# subject to safety.allow_js_evaluation (which fences EvaluateInput/wait_for
# JS the *caller* supplies).
#
# Determinism: a single page.evaluate, no async, no network. ``max`` bounds the
# returned list; ``tag`` controls whether a data-webtool-ref attribute is
# written (set-of-marks mechanism (b)). Stale refs from a PRIOR observe are
# cleared first so refs never accumulate or collide across calls.
_ENUMERATE_INTERACTIVE_JS = r"""
({ max, tag }) => {
  const SEL = [
    'a[href]', 'button', 'input', 'select', 'textarea',
    '[role=button]', '[role=link]', '[role=tab]', '[role=menuitem]',
    '[role=checkbox]', '[role=radio]', '[role=combobox]', '[role=textbox]',
    '[role=switch]', '[role=option]', '[onclick]', '[contenteditable=""]',
    '[contenteditable=true]',
  ].join(',');

  // Clear refs from a previous observe so eN never collides across calls.
  if (tag) {
    for (const old of document.querySelectorAll('[data-webtool-ref]')) {
      old.removeAttribute('data-webtool-ref');
    }
  }

  const vw = window.innerWidth || 0;
  const vh = window.innerHeight || 0;
  const seen = new Set();
  const out = [];
  let truncated = false;
  let n = 0;

  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit.toLowerCase();
    const t = el.tagName.toLowerCase();
    if (t === 'a') return 'link';
    if (t === 'button') return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') {
      const it = (el.getAttribute('type') || 'text').toLowerCase();
      if (it === 'checkbox') return 'checkbox';
      if (it === 'radio') return 'radio';
      if (it === 'button' || it === 'submit' || it === 'reset' || it === 'image')
        return 'button';
      return 'textbox';
    }
    return t;
  };

  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const ph = el.getAttribute('placeholder');
    const txt = (el.innerText || el.textContent || '').trim();
    if (txt) return txt;
    if (ph && ph.trim()) return ph.trim();
    const val = (el.value || '').trim ? (el.value || '').trim() : '';
    if (val) return val;
    const title = el.getAttribute('title');
    if (title && title.trim()) return title.trim();
    return '';
  };

  for (const el of document.querySelectorAll(SEL)) {
    if (seen.has(el)) continue;
    seen.add(el);

    const rect = el.getBoundingClientRect();
    // Visible-first: skip zero-area and fully-offscreen elements.
    if (rect.width <= 0 || rect.height <= 0) continue;
    if (rect.bottom <= 0 || rect.top >= vh) continue;
    if (rect.right <= 0 || rect.left >= vw) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    if (parseFloat(style.opacity || '1') === 0) continue;

    if (out.length >= max) { truncated = true; break; }

    n += 1;
    const ref = 'e' + n;
    if (tag) el.setAttribute('data-webtool-ref', ref);

    const disabled = !!(el.disabled) ||
      el.getAttribute('aria-disabled') === 'true';

    out.push({
      ref,
      role: roleOf(el),
      name: nameOf(el).slice(0, 200),
      tag: el.tagName.toLowerCase(),
      enabled: !disabled,
      visible: true,
      bbox: [
        Math.round(rect.x * 100) / 100,
        Math.round(rect.y * 100) / 100,
        Math.round(rect.width * 100) / 100,
        Math.round(rect.height * 100) / 100,
      ],
      selector: tag ? ('[data-webtool-ref="' + ref + '"]') : null,
    });
  }

  return { elements: out, truncated };
}
"""


def _looks_like_submit(selector: SelectorLike | None) -> bool:
    """Best-effort, *advisory* heuristic: does this selector look like a
    submit button?

    For a CSS selector string, checks for ``button[type=submit]`` patterns.
    For a LocatorSpec, checks every text-bearing field (role_name, text,
    label, placeholder) for submit-like keywords (submit, send, save,
    log in, sign in, register, create account, continue).

    BR-9: this is a string-pattern guess, not a guarantee. It is trivially
    bypassable -- a submit can be triggered by clicking a ``<div>``, an
    icon-only button, a non-English / synonym label, JS-attached handlers,
    or pressing Enter in a field. So a False result does NOT prove a click
    is non-submitting. The heuristic exists only to *flag the obvious cases*
    when the operator has already opted out of form submission. The real
    control is the ``safety.allow_form_submit`` config flag (default True):
    when False, callers should keep submit-style interactions out of the
    sequence entirely. ``allow_form_submit=True`` performs no such check at
    all. Treat this function as a tripwire, never a sandbox.
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
        ref > role > test_id > label > placeholder > text > selector

    The ``ref`` path (v1.7.0 Wave 4C set-of-marks) resolves the live
    ``[data-webtool-ref="eN"]`` attribute that the most recent ``observe()``
    stamped on the page. It re-resolves against the CURRENT DOM, so a stale
    ref (element removed / page re-rendered) yields a zero-match Locator that
    fails through the normal SelectorNotFoundError / ActionStatus.FAILED path.

    Raises:
        SelectorNotFoundError: If ``spec`` is an empty LocatorSpec, or carries
            a ``ref`` that is not a well-formed ``eN`` token.
    """
    from .exceptions import SelectorNotFoundError

    if isinstance(spec, str):
        return page.locator(spec)

    if spec.ref:
        # Validate the ref shape before interpolating it into a CSS selector.
        # observe() only ever mints ``e<digits>`` refs; anything else is a
        # caller / prompt-injection error and must NOT reach page.locator,
        # where a crafted value could break out of the attribute selector.
        if not _REF_PATTERN.fullmatch(spec.ref):
            raise SelectorNotFoundError(
                f"LocatorSpec.ref {spec.ref!r} is malformed; expected an "
                "observe() element ref like 'e5'.",
                action="resolve_locator",
            )
        return page.locator(f'[data-webtool-ref="{spec.ref}"]')

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

    # v1.6.16 deep-review fix: ``role_name`` is only an accessible-name filter
    # applied together with ``role`` (handled in the ``if spec.role`` branch
    # above). A role_name-only spec is unusable -- give a PRECISE error instead
    # of the misleading "LocatorSpec is empty" (a field IS set, just not a
    # standalone locator).
    if spec.role_name:
        raise SelectorNotFoundError(
            "LocatorSpec.role_name is set without role; role_name is only an "
            "accessible-name filter applied together with role.",
            action="resolve_locator",
        )

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


# Per-Page dialog state, weakly keyed so closing the Page (Playwright
# lifecycle) drops the entry automatically. Replaces v1.6.4's hack of
# stuffing the state onto the Page via ``page._web_agent_dialog_state``,
# which would break if Playwright ever introduced ``__slots__``.
_PAGE_DIALOG_STATES: WeakKeyDictionary[Page, _DialogState] = WeakKeyDictionary()

# v1.6.16 BR-2 fix: a SEPARATE in-flight marker for the concurrent-sequence
# guard. The guard previously keyed on ``page in _PAGE_DIALOG_STATES`` as a
# proxy for "a sequence is running" -- but the single-action dialog path
# (``Agent.handle_dialog`` -> ``execute_single_on_session`` -> ``_do_dialog``)
# also writes that slot WITHOUT starting a sequence and never pops it, so the
# next ``interact()`` falsely tripped the guard and aborted the whole sequence
# with a misleading "already running" error. Tracking live sequences in their
# own WeakSet decouples "a sequence is in flight" from "a dialog-state slot
# exists". Only ``execute_sequence`` populates this set; it is removed in the
# sequence's ``finally``.
_PAGE_ACTIVE_SEQUENCES: WeakSet[Page] = WeakSet()


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
        network_collector: Optional[Any] = None,
        trace_recorder: Optional[Any] = None,
    ) -> None:
        self._bm = browser_manager
        self._config = config
        self._sessions = sessions
        self._debug = debug or DebugCapture(config)
        # v1.6.8: shared NetworkCollector. When set and capture is on,
        # execute_sequence and take_screenshot copy the per-Page events
        # onto their results. None when no Agent provided one (older test
        # scaffolding).
        self._network_collector = network_collector
        # v1.6.8: shared SessionTraceRecorder. When set and trace_enabled,
        # execute_sequence appends a JSONL entry per action under
        # diagnostics.trace_dir, keyed by session_id.
        self._trace_recorder = trace_recorder

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
            # v1.6.14 C-2: WaitInput(target=FUNCTION) calls
            # page.wait_for_function(action.value), which executes
            # arbitrary JS in the page context -- a parallel JS-eval
            # path that EvaluateInput's gate alone does not cover. Without
            # this check an LLM-controlled sequence can bypass
            # allow_js_evaluation=False by emitting a wait-function with
            # malicious JS (e.g. cookie exfiltration). Gate it here at
            # the same chokepoint as EvaluateInput.
            #
            # BR-10: the *authoritative* gate now lives in ``_do_wait``
            # (v1.6.14 A-1), which re-checks ``allow_js_evaluation`` on every
            # entry point. This pre-flight is an intentional early-exit, NOT
            # redundant defence: ``_block_all`` rejects the whole sequence
            # atomically *before any action runs*, whereas the handler gate
            # raises only once the loop reaches the offending wait (after
            # earlier actions have already executed). Keep both -- removing
            # this one would change the all-or-nothing semantics callers rely
            # on for blocked sequences.
            if (
                isinstance(a, WaitInput)
                and a.target == WaitTarget.FUNCTION
                and not safety.allow_js_evaluation
            ):
                return _block_all(
                    "WaitInput(target=FUNCTION) blocked: "
                    "safety.allow_js_evaluation=False "
                    "(wait_for_function executes arbitrary JS; "
                    "set safety.allow_js_evaluation=True to opt in)"
                )
            # BR-9: advisory tripwire only. ``_looks_like_submit`` is a
            # string-pattern guess that catches obvious submit clicks; it is
            # bypassable (icon buttons, div handlers, Enter key, synonyms).
            # The authoritative gate is the operator setting
            # ``allow_form_submit=False`` AND keeping submit-style actions out
            # of the sequence -- this check does not, and cannot, guarantee no
            # form is submitted.
            if (
                isinstance(a, ClickInput)
                and not safety.allow_form_submit
                and _looks_like_submit(a.selector)
            ):
                return _block_all(
                    "Submit-button click blocked: safety.allow_form_submit=False "
                    "(advisory form-submission heuristic; set allow_form_submit=True to opt in)"
                )

        should_stop = (
            stop_on_error if stop_on_error is not None else self._config.automation.stop_on_error
        )
        start = time.perf_counter()
        results: list[ActionResult] = []
        succeeded = 0
        failed = 0
        all_artifacts: list[str] = []
        # v1.6.8: optional per-action verification screenshots.
        verification_screenshots: list[str] = []

        # Per-sequence dialog state (thread-safe - not shared across sequences)
        dialog_state = _DialogState()

        # Initialize cleanup state BEFORE the branch so the finally block is
        # safe even if page-acquisition raises (e.g. unknown session_id, or
        # ctx.new_page() fails on a closed browser context).
        #
        # v1.6.6 ownership semantics:
        #   "ephemeral"          -- no session_id; ctx_mgr owns lifecycle.
        #   "session_persistent" -- reused the session's current tab.
        #                            DO NOT close at sequence end -- the
        #                            tab outlives the sequence so subsequent
        #                            interact() calls share state.
        #   "session_ephemeral"  -- legacy v1.6.5 behavior under
        #                            automation.fresh_tab_per_call=True OR
        #                            the session has no current tab. We
        #                            opened the page; we close it at the end.
        page = None
        ctx_mgr = None
        owner = "ephemeral"
        # v1.6.14 A-2: bound dialog handler reference, set once registered
        # inside the try. Bound early to None so the finally can remove it
        # unconditionally even if page-acquisition raises before registration.
        dialog_handler: Any = None
        # v1.6.16 BR-2: True once THIS sequence has registered itself in
        # _PAGE_ACTIVE_SEQUENCES, so the finally only clears the marker it
        # owns -- a sequence refused by the concurrency guard (which never
        # added itself) must not clear the running sequence's marker.
        marked_active = False
        # v1.6.8: bind early so the except path / final return doesn't
        # see an UnboundLocalError when an exception fires before the
        # success-snapshot line inside the try.
        net_events: list = []
        api_cands: list[str] = []
        dl_intents: list[str] = []

        try:
            if session_id and self._sessions is not None:
                ctx = self._sessions.get(session_id)
                self._sessions.touch(session_id)
                # v1.6.6: prefer the session's current tab unless the operator
                # explicitly opted into fresh-page-per-call behavior.
                fresh = self._config.automation.fresh_tab_per_call
                tab_mgr = None
                with contextlib.suppress(KeyError):
                    tab_mgr = self._sessions.get_tab_manager(session_id)

                reused = False
                if not fresh and tab_mgr is not None:
                    current = tab_mgr.current()
                    if current is not None and not current.is_closed():
                        page = current
                        owner = "session_persistent"
                        reused = True

                if not reused:
                    # Either fresh_tab_per_call=True, no TabManager (shouldn't
                    # happen in v1.6.6 but defensive), or the current page
                    # was closed externally. Fall back to opening a new page.
                    page = await ctx.new_page()
                    owner = "session_ephemeral"
                    # v1.6.8: attach network capture to the fallback page.
                    # The persistent tab path went through TabManager which
                    # already attached on register_initial_page; this branch
                    # bypasses TabManager so we must attach manually.
                    if self._network_collector is not None:
                        self._network_collector.attach(page)
            else:
                ctx_mgr = self._bm.new_page(block_resources=False)
                page = await ctx_mgr.__aenter__()
                # BrowserManager.new_page() already attaches the collector
                # before yielding.

            # All branches above set ``page``; mypy can't follow the
            # cross-branch reassignment so we narrow here.
            assert page is not None
            await page.goto(url, wait_until="domcontentloaded")

            # Post-navigation re-check: the initial goto may have followed a
            # redirect to a denied / private-IP host. Defense-in-depth.
            if not check_domain_allowed(page.url, safety):
                host = urlparse(page.url).hostname or ""
                return _block_all(f"Initial navigation redirected to disallowed domain: {host}")

            # v1.6.16 BR-2: a session-persistent tab is shared across calls,
            # so two execute_sequence coroutines awaited concurrently against
            # the SAME session_id both resolve to this one Page. Each would
            # otherwise register its own "dialog" listener and overwrite the
            # single-slot ``_PAGE_DIALOG_STATES[page]`` entry -- corrupting
            # dialog routing (sequence B clobbers A's DialogResponse) and
            # tripping Playwright's "Dialog is already handled" when two live
            # listeners fire. Concurrent automation against one shared tab is
            # a contraindicated, undocumented usage, so we refuse the second
            # concurrent sequence with a clear error. The in-flight state is
            # tracked in its OWN set (``_PAGE_ACTIVE_SEQUENCES``), NOT via
            # dialog-slot presence: the single-action ``handle_dialog`` path
            # writes a dialog slot without starting a sequence, so keying the
            # guard on the slot made a later interact() falsely abort here. We
            # register AFTER passing the check and de-register in ``finally``
            # (guarded by ``marked_active`` so a refused sequence can never
            # clear the running sequence's marker).
            if page in _PAGE_ACTIVE_SEQUENCES:
                from .exceptions import ActionError

                raise ActionError(
                    "A sequence is already running on this session's tab; "
                    "concurrent execute_sequence calls against the same "
                    "session_id are not supported (run them sequentially)."
                )
            _PAGE_ACTIVE_SEQUENCES.add(page)
            marked_active = True
            # v1.6.14 A-2: keep the exact handler reference we register so
            # the finally block can remove_listener it. dialog_state.handle
            # is a bound method -- accessing it twice yields two distinct
            # objects, so capturing it once is required for the later
            # remove_listener to match.
            dialog_handler = dialog_state.handle
            page.on("dialog", dialog_handler)
            _PAGE_DIALOG_STATES[page] = dialog_state

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

                # v1.6.8: post-action verification screenshot. Best-effort
                # only -- failures log at DEBUG and never fail the sequence.
                if (
                    self._config.diagnostics.screenshot_after_action
                    and result.status == ActionStatus.SUCCESS
                ):
                    vshot = await self._capture_verification_screenshot(
                        page, action_index=len(results) - 1, cid=cid
                    )
                    if vshot:
                        verification_screenshots.append(vshot)

                # v1.6.8: per-action trace recording. Off by default; only
                # active when DiagnosticsConfig.trace_enabled and a
                # session_id is known (replay requires session context).
                if session_id and self._trace_recorder is not None and self._trace_recorder.enabled:
                    with contextlib.suppress(Exception):
                        await self._trace_recorder.record(
                            session_id=session_id,
                            method=f"action.{action_input.action}",
                            args=action_input.model_dump(exclude_none=True, exclude={"tab_id"}),
                            status=result.status.value,
                            elapsed_ms=result.duration_ms,
                            url=url,
                        )

                # Per-action URL-drift check: detect when an innocuous-looking
                # action (link click, form submit, JS-driven nav) lands on a
                # disallowed domain or a private IP. Catches redirect chains
                # that bypass the explicit GOTO check in _do_navigate.
                if not check_domain_allowed(page.url, safety):
                    host = urlparse(page.url).hostname or ""
                    drift_msg = f"Page drifted to disallowed domain after action: {host}"
                    # If the action itself was reported as SUCCESS but the
                    # page ended up somewhere it shouldn't, downgrade.
                    if result.status == ActionStatus.SUCCESS:
                        result.status = ActionStatus.FAILED
                        result.error_message = drift_msg
                        succeeded -= 1
                        failed += 1
                    # Force-stop the sequence regardless of stop_on_error --
                    # we don't want subsequent actions running on a page we
                    # shouldn't be on.
                    for remaining in actions[len(results) :]:
                        results.append(
                            ActionResult(
                                action=ActionType(remaining.action),
                                status=ActionStatus.SKIPPED,
                                selector=_selector_repr(getattr(remaining, "selector", None)),
                                error_message=drift_msg,
                            )
                        )
                    break

                if result.status != ActionStatus.SUCCESS and should_stop:
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
            # v1.6.8 (review C-1 fix): snapshot network events BEFORE the
            # page is closed. Must run on BOTH the success and exception
            # paths -- the original placement inside ``except`` left every
            # successful sequence's network_events / api_candidates /
            # download_candidates empty. ``finally`` fires unconditionally
            # and ``page`` is still alive here (close happens below).
            if self._network_collector is not None and page is not None:
                with contextlib.suppress(Exception):
                    # v1.6.14 A-4: when response-body capture is on, drain
                    # in-flight body reads BEFORE snapshotting events (and
                    # before the page is closed below). Otherwise body_text
                    # never populates and the orphaned tasks race a closed
                    # page. Mirrors web_fetcher.py's pre-snapshot drain;
                    # wait_for_pending_bodies has a bounded default timeout.
                    if self._config.diagnostics.capture_response_bodies:
                        await self._network_collector.wait_for_pending_bodies()
                    net_events = self._network_collector.events_for(page)
                    api_cands = self._network_collector.api_candidates_for(page)
                    dl_intents = self._network_collector.download_intents_for(page)
            # v1.6.14 A-2: remove the dialog listener for ALL pages, not just
            # ephemeral ones. The previous code only closed (and thereby
            # detached) ephemeral pages; session-persistent tabs survive the
            # sequence, so each execute_sequence stacked another listener and
            # eventually tripped Playwright's "Dialog already handled". The
            # handler reference is the exact bound method we registered.
            if page is not None and dialog_handler is not None:
                with contextlib.suppress(Exception):
                    page.remove_listener("dialog", dialog_handler)
                # v1.6.16 BR-2: also drop this page's dialog-state slot.
                # _PAGE_DIALOG_STATES is a WeakKeyDictionary keyed by the
                # Page, but a session-persistent tab is never closed, so the
                # slot would otherwise survive this sequence and make the NEXT
                # sequential execute_sequence on the same session falsely trip
                # the concurrent-sequence guard above. Gated on
                # ``dialog_handler is not None`` so a sequence the guard
                # REFUSED (it never registered a handler) can't pop the owning
                # sequence's slot.
                _PAGE_DIALOG_STATES.pop(page, None)
            # v1.6.16 BR-2: drop THIS sequence's in-flight marker so the next
            # sequence on the same persistent tab isn't falsely refused.
            # Guarded by ``marked_active`` so a sequence the concurrency guard
            # refused (it never added itself) can't clear the running
            # sequence's marker.
            if page is not None and marked_active:
                _PAGE_ACTIVE_SEQUENCES.discard(page)
            # v1.6.6: only close pages WE created. Persistent session tabs
            # outlive sequences so subsequent interact() calls share state.
            if page is not None and owner == "session_ephemeral":
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
            network_events=net_events,
            api_candidates=api_cands,
            download_candidates=dl_intents,
            verification_screenshots=verification_screenshots,
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
        # v1.6.16 deep-review fix: also catch the builtin/asyncio TimeoutError.
        # ``asyncio.wait_for`` (used to bound the infinite-scroll page.evaluate
        # calls in _do_scroll) raises the BUILTIN TimeoutError, which is neither
        # PlaywrightTimeout nor ActionTimeoutError -- so a hung infinite-scroll
        # previously fell through to the generic handler below and was reported
        # as FAILED instead of the TIMEOUT the inline comment there promises.
        # (asyncio.TimeoutError IS the builtin TimeoutError on Python 3.11+.)
        except (PlaywrightTimeout, ActionTimeoutError, TimeoutError) as e:
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

    async def _capture_verification_screenshot(
        self,
        page: Page,
        action_index: int,
        cid: Optional[str],
    ) -> Optional[str]:
        """v1.6.8: take a best-effort PNG screenshot after a successful action.

        File name: ``verify-<correlation_id>-<index>.png`` under
        ``automation.screenshot_dir``. Failure logs at DEBUG and returns
        None -- this method NEVER raises and never fails the sequence.

        Path goes through ``safe_join_path`` so even though we construct
        the filename ourselves, a malicious correlation_id (shouldn't be
        possible -- they're UUIDs -- but defensive) cannot escape the
        screenshot dir.
        """
        try:
            ss_dir = Path(self._config.automation.screenshot_dir)
            ss_dir.mkdir(parents=True, exist_ok=True)
            safe_cid = (cid or "no-cid").replace("/", "_").replace("\\", "_")
            fname = f"verify-{safe_cid}-{action_index:03d}.png"
            try:
                resolved = safe_join_path(ss_dir, fname)
            except ValueError as exc:
                logger.warning("Rejected verification screenshot path: {e}", e=exc)
                return None
            await page.screenshot(path=str(resolved), full_page=False, type="png")
            return str(resolved)
        except Exception as exc:
            logger.debug("Verification screenshot failed: {e}", e=exc)
            return None

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

        async def _capture(page: Page) -> Optional[str]:
            """Capture a screenshot. Returns None on success, or an
            error message string if the post-redirect URL violates the
            safety policy (in which case no file is written).

            Uses ``FetchConfig.wait_until`` (default 'domcontentloaded')
            instead of hardcoding 'networkidle': screenshots are exactly
            the case where networkidle hangs indefinitely on pages with
            analytics polling, and we already chose 'domcontentloaded'
            as the default for the fetch path in v1.6.2.
            """
            wait_until = self._config.fetch.wait_until
            await page.goto(url, wait_until=wait_until)
            # Post-redirect re-check: a whitelisted host can redirect
            # to a private IP / denied host. Block before screenshotting.
            if not check_domain_allowed(page.url, self._config.safety):
                host = urlparse(page.url).hostname or ""
                return f"Screenshot navigation redirected to disallowed domain: {host}"
            ss_kwargs: dict[str, Any] = {
                "path": path,
                "full_page": full_page,
                "type": format.value,
            }
            if format == ScreenshotFormat.JPEG and quality is not None:
                ss_kwargs["quality"] = quality
            await page.screenshot(**ss_kwargs)
            return None

        capture_error: Optional[str] = None
        if session_id and self._sessions is not None:
            ctx = self._sessions.get(session_id)
            self._sessions.touch(session_id)
            page = await ctx.new_page()
            try:
                capture_error = await _capture(page)
            finally:
                await page.close()
        else:
            async with self._bm.new_page(block_resources=False) as page:
                capture_error = await _capture(page)

        if capture_error is not None:
            return ScreenshotResult(
                url=url,
                path="",
                format=format,
                status=ActionStatus.FAILED,
                error_message=capture_error,
                correlation_id=cid,
            )

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
            # v1.6.14 A-6: bound each evaluate call so a hung page can't
            # stall for up to max_iterations x an unbounded wait. Playwright's
            # Page.evaluate (1.58) takes NO timeout kwarg, so we wrap each call
            # in asyncio.wait_for to impose a per-call deadline. A few-second
            # cap keeps the bounded loop bounded in wall-clock time too; reuse
            # the resolved action/config timeout but clamp it so a single
            # iteration can't run away. A timed-out evaluate raises
            # TimeoutError, which execute_action surfaces as a TIMEOUT result.
            scroll_eval_timeout = min(timeout, 5000) / 1000
            iterations = 0
            for _ in range(action.infinite_scroll_max):
                prev_height = await asyncio.wait_for(
                    page.evaluate("document.body.scrollHeight"),
                    timeout=scroll_eval_timeout,
                )
                await asyncio.wait_for(
                    page.evaluate("window.scrollBy(0, window.innerHeight)"),
                    timeout=scroll_eval_timeout,
                )
                await asyncio.sleep(action.infinite_scroll_delay_ms / 1000)
                new_height = await asyncio.wait_for(
                    page.evaluate("document.body.scrollHeight"),
                    timeout=scroll_eval_timeout,
                )
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
        # GOTO carries a caller-supplied URL -- validate it BEFORE the
        # network call. Without this check, an LLM-supplied automation
        # script can navigate the browser to a private IP (SSRF) or
        # denied host, bypassing the safety policy that gates fetch and
        # download.
        if action.navigate_action == NavigateDirection.GOTO:
            if not action.url:
                return ActionResult(
                    action=ActionType.NAVIGATE,
                    status=ActionStatus.FAILED,
                    error_message="URL required for goto navigation",
                )
            if not check_domain_allowed(action.url, self._config.safety):
                host = urlparse(action.url).hostname or ""
                return ActionResult(
                    action=ActionType.NAVIGATE,
                    status=ActionStatus.FAILED,
                    error_message=(
                        f"NavigateInput.url not allowed by SafetyConfig: "
                        f"{host} (caller-supplied URL blocked before network)"
                    ),
                )
            await page.goto(action.url, wait_until=action.wait_until)  # type: ignore[arg-type]
        elif action.navigate_action == NavigateDirection.BACK:
            await page.go_back()
        elif action.navigate_action == NavigateDirection.FORWARD:
            await page.go_forward()
        elif action.navigate_action == NavigateDirection.RELOAD:
            await page.reload()

        # Post-navigation re-check: every navigation direction can
        # land on a different URL than expected (redirect from GOTO,
        # history entry from BACK/FORWARD, server-side redirect on
        # RELOAD). Re-validate ``page.url`` so a whitelisted host can
        # never bounce us to AWS IMDS / RFC1918 / a denied domain.
        if not check_domain_allowed(page.url, self._config.safety):
            host = urlparse(page.url).hostname or ""
            return ActionResult(
                action=ActionType.NAVIGATE,
                status=ActionStatus.FAILED,
                error_message=(f"Navigation landed on disallowed domain after redirect: {host}"),
                data={"url": page.url},
            )

        logger.debug("Navigated: {act} -> {url}", act=action.navigate_action.value, url=page.url)
        return ActionResult(
            action=ActionType.NAVIGATE,
            status=ActionStatus.SUCCESS,
            data={"url": page.url},
        )

    async def _do_dialog(self, page: Page, action: DialogInput) -> ActionResult:
        dialog_state = _PAGE_DIALOG_STATES.get(page)
        if dialog_state is None:
            dialog_state = _DialogState()
            _PAGE_DIALOG_STATES[page] = dialog_state
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
            # v1.6.14 A-1: self-enforce the JS-eval gate. wait_for_function
            # executes arbitrary JS in the page context, so it is a parallel
            # JS-eval path. execute_sequence's pre-flight gates it, but the
            # public entry points execute_action / execute_single_on_session
            # dispatch here directly and bypass that pre-flight. Re-check the
            # same SafetyConfig the pre-flight uses so the gate holds at every
            # entry point.
            if not self._config.safety.allow_js_evaluation:
                from .exceptions import ActionError

                raise ActionError(
                    "WaitInput(target=FUNCTION) blocked: "
                    "safety.allow_js_evaluation=False "
                    "(wait_for_function executes arbitrary JS; "
                    "set safety.allow_js_evaluation=True to opt in)",
                    action="wait",
                )
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
        # v1.6.14 A-1: self-enforce the JS-eval gate at the handler so the
        # public entry points execute_action / execute_single_on_session --
        # which dispatch here directly, bypassing execute_sequence's
        # pre-flight -- cannot run arbitrary JS when the operator has opted
        # out. Uses the same SafetyConfig the pre-flight reads.
        if not self._config.safety.allow_js_evaluation:
            from .exceptions import ActionError

            raise ActionError(
                "EvaluateInput blocked: safety.allow_js_evaluation=False "
                "(set safety.allow_js_evaluation=True to opt in)",
                action="evaluate",
            )
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
    # v1.6.6 Feature 4: coordinate-level fallback handlers
    # ------------------------------------------------------------------

    async def _inspect_element_at_point(
        self, page: Page, x: float, y: float
    ) -> list[dict[str, Any]]:
        """v1.6.9: introspect the element stack at viewport (x, y).

        Runs ``document.elementFromPoint(x, y)`` and walks up to 5
        ancestors, returning structural attributes used by
        ``_looks_like_destructive_at_point``. Returns an empty list when
        nothing is hit (point outside the document / over a closed
        shadow root) or when the evaluation fails.

        v1.6.10: an empty list is no longer universally "allow" -- when
        :attr:`SafetyConfig.coordinate_click_unknown_policy` is ``"block"``,
        the ``_do_click_xy`` caller rejects the click on empty inspection.
        The fallback "allow" semantics still apply under the default
        ``"allow"`` policy.
        """
        try:
            result = await page.evaluate(_ELEMENT_FROM_POINT_JS, {"x": x, "y": y})
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("elementFromPoint inspection failed: {e}", e=exc)
            return []
        if not isinstance(result, list):
            return []
        return [el for el in result if isinstance(el, dict)]

    def _looks_like_destructive_at_point(self, elements: list[dict[str, Any]]) -> bool:
        """v1.6.9: True if the inspected element stack looks like a
        submit/login/destructive control.

        Decision rules (any match wins, examined inner -> outer):
          * ``button[type=submit]``
          * ``input[type=submit|image]``
          * ``role=button`` whose accessible text matches the destructive verb pattern
          * Any ``button``/``a``/``input`` whose text+aria+value matches the pattern
        Empty input -> False (cannot tell -> allow).
        """
        if not elements:
            return False
        for el in elements:
            tag = (el.get("tag") or "").lower()
            typ = (el.get("type") or "").lower()
            role = (el.get("role") or "").lower()
            # v1.6.9 review (I-1): elementFromPoint JS collects `n.value`
            # into the `text` field for input/textarea, so destructive text
            # in `<input type="button" value="Delete">` is already part of
            # text_blob -- we just need to extend the tag check below to
            # include ``input``.
            text_blob = f"{el.get('text') or ''} {el.get('aria') or ''}"
            if tag == "button" and typ == "submit":
                return True
            if tag == "input" and typ in {"submit", "image"}:
                return True
            if role == "button" and _DESTRUCTIVE_TEXT_PATTERN.search(text_blob):
                return True
            if tag in {"button", "a", "input"} and _DESTRUCTIVE_TEXT_PATTERN.search(text_blob):
                return True
        return False

    async def _do_click_xy(self, page: Page, action: ClickXYInput) -> ActionResult:
        """Click at viewport coordinates (CSS pixels). No selector resolution.

        v1.6.9 safety changes:
          * Honors ``safety.allow_coordinate_clicks`` (default True; forced
            False by ``safe_mode=True``). When False, returns a failed
            ActionResult without clicking.
          * When ``allow_form_submit=False``, runs
            ``document.elementFromPoint`` to inspect the target stack and
            blocks the click if it looks like a submit/login/destructive
            control.

        v1.6.10 addition:
          * Honors ``safety.coordinate_click_unknown_policy``. When the
            policy is "block" AND elementFromPoint returned an empty list
            (point outside any element / inspection raised), the click is
            rejected. The v1.6.9 default ("allow") keeps the permissive
            behaviour so existing callers are unaffected. ``safe_mode``
            forces "block" via ``_apply_safe_mode``.
        """
        safety = self._config.safety
        if not safety.allow_coordinate_clicks:
            reason = "safe_mode" if safety.safe_mode else "allow_coordinate_clicks=False"
            logger.info("click_xy rejected: {r}", r=reason)
            return ActionResult(
                action=ActionType.CLICK_XY,
                status=ActionStatus.FAILED,
                error_message=f"Coordinate clicks are disabled by safety config ({reason}).",
            )
        # v1.6.10 review C-1 fix: the destructive-check and the
        # unknown-policy check are independent. Either being active
        # requires elementFromPoint inspection. Prior to this fix the
        # whole block was gated by ``not allow_form_submit``, which
        # made ``coordinate_click_unknown_policy='block'`` unreachable
        # whenever a caller kept the default ``allow_form_submit=True``.
        needs_destructive_check = not safety.allow_form_submit
        needs_unknown_check = safety.coordinate_click_unknown_policy == "block"
        if needs_destructive_check or needs_unknown_check:
            elements = await self._inspect_element_at_point(page, action.x, action.y)
            if needs_destructive_check and self._looks_like_destructive_at_point(elements):
                top = elements[0] if elements else None
                logger.info(
                    "click_xy blocked: target at ({x}, {y}) looks destructive: {top}",
                    x=action.x,
                    y=action.y,
                    top=top,
                )
                return ActionResult(
                    action=ActionType.CLICK_XY,
                    status=ActionStatus.FAILED,
                    error_message=(
                        "Coordinate click blocked: target looks like a submit/destructive "
                        f"control (allow_form_submit=False). Inspected top element: {top!r}"
                    ),
                )
            # Unknown-policy gate. ``_inspect_element_at_point`` already
            # swallows exceptions and returns [], so this single check
            # covers both "point outside any element" and "JS evaluate
            # raised". Fires regardless of ``allow_form_submit``:
            # callers running with form submits ALLOWED can still opt
            # into "block-on-unknown" by setting the policy to "block".
            if needs_unknown_check and not elements:
                logger.info(
                    "click_xy blocked: elementFromPoint at ({x}, {y}) returned "
                    "no target (coordinate_click_unknown_policy='block')",
                    x=action.x,
                    y=action.y,
                )
                return ActionResult(
                    action=ActionType.CLICK_XY,
                    status=ActionStatus.FAILED,
                    error_message=(
                        "Coordinate click blocked: target unknown "
                        "(empty elementFromPoint result; "
                        "coordinate_click_unknown_policy='block')."
                    ),
                )
        await page.mouse.click(
            action.x,
            action.y,
            button=action.button.value,
            click_count=action.clicks,
            delay=action.delay,
        )
        logger.debug("Coordinate click at ({x}, {y})", x=action.x, y=action.y)
        return ActionResult(
            action=ActionType.CLICK_XY,
            status=ActionStatus.SUCCESS,
            data={"x": action.x, "y": action.y, "button": action.button.value},
        )

    async def _do_type_text(self, page: Page, action: TypeTextInput) -> ActionResult:
        """Type ``text`` into whatever currently has keyboard focus.

        No selector resolution -- pair with a preceding click_xy or click
        to direct keystrokes at a specific element.
        """
        await page.keyboard.type(action.text, delay=action.delay)
        logger.debug("Typed {n} chars into current focus target", n=len(action.text))
        return ActionResult(
            action=ActionType.TYPE_TEXT,
            status=ActionStatus.SUCCESS,
            data={"length": len(action.text)},
        )

    async def _do_press_key(self, page: Page, action: PressKeyInput) -> ActionResult:
        """Press ``key`` (with optional ``modifiers``) at page level.

        Modifiers are sent as a Playwright key combo string:
        ``["Control", "Shift"]`` + ``"a"`` -> ``"Control+Shift+a"``.
        """
        combo = "+".join([*action.modifiers, action.key]) if action.modifiers else action.key
        await page.keyboard.press(combo)
        logger.debug("Pressed key: {k}", k=combo)
        return ActionResult(
            action=ActionType.PRESS_KEY,
            status=ActionStatus.SUCCESS,
            data={"key": combo},
        )

    # ------------------------------------------------------------------
    # v1.6.7 Interaction Library handlers (Feature 5)
    # ------------------------------------------------------------------

    def _validate_upload_path(self, raw_path: str) -> str:
        """Resolve and validate an upload path against SafetyConfig.

        By default upload_file accepts only paths under
        ``download.download_dir`` to prevent prompt-injection
        exfiltration of arbitrary local files (e.g. ``~/.ssh/id_rsa``).
        Set ``safety.allow_upload_outside_download_dir=True`` to widen.

        Uses ``_is_cross_platform_absolute`` (v1.6.4 helper) so behavior
        is consistent on Windows and POSIX -- ``Path.is_absolute()`` is
        OS-dependent and rejects ``/foo`` on Windows.

        Returns the resolved absolute path string. Raises
        SafeModeBlockedError on any safety violation; the caller
        surfaces this as ActionResult FAILED rather than crashing.
        """
        from .exceptions import SafeModeBlockedError

        if _is_cross_platform_absolute(raw_path):
            resolved = Path(raw_path).resolve()
        else:
            base = Path(self._config.download.download_dir).resolve()
            try:
                resolved = safe_join_path(base, raw_path)
            except ValueError as exc:
                raise SafeModeBlockedError(
                    f"upload_file: invalid relative path {raw_path!r}: {exc}",
                    operation="upload_file",
                ) from exc

        # Containment check BEFORE existence to close the
        # file-existence oracle: without this ordering, a caller can
        # tell whether ``/etc/passwd`` exists by comparing the
        # "does not exist" vs "outside download_dir" error messages.
        # Any out-of-scope path now gets the same "outside" error
        # regardless of existence.
        if not self._config.safety.allow_upload_outside_download_dir:
            base = Path(self._config.download.download_dir).resolve()
            try:
                resolved.relative_to(base)
            except ValueError as exc:
                raise SafeModeBlockedError(
                    f"upload_file: path is outside download_dir {base}. "
                    f"Set safety.allow_upload_outside_download_dir=True "
                    f"to permit arbitrary local files.",
                    operation="upload_file",
                ) from exc
        else:
            # v1.6.14 A-7: the escape hatch is enabled, so an out-of-dir
            # upload is permitted -- but log it at WARNING so the audit
            # trail records that a file reached outside download_dir. A
            # prompt-injection exfil attempt (e.g. uploading ~/.ssh/id_rsa)
            # surfaces here rather than passing silently.
            base = Path(self._config.download.download_dir).resolve()
            try:
                resolved.relative_to(base)
            except ValueError:
                logger.warning(
                    "upload_file: path {p} is OUTSIDE download_dir {b} "
                    "(permitted because allow_upload_outside_download_dir=True)",
                    p=resolved,
                    b=base,
                )

        if not resolved.exists():
            raise SafeModeBlockedError(
                f"upload_file: path does not exist: {resolved}",
                operation="upload_file",
            )

        return str(resolved)

    async def _do_upload_file(self, page: Page, action: UploadFileInput) -> ActionResult:
        """Upload one or more files to a file input element.

        Path safety: each path is validated against
        ``download.download_dir`` unless the upload-outside-download-dir
        flag is enabled (see :meth:`_validate_upload_path`).
        """
        from .exceptions import SafeModeBlockedError

        try:
            resolved_paths = [self._validate_upload_path(p) for p in action.paths]
        except SafeModeBlockedError as exc:
            return ActionResult(
                action=ActionType.UPLOAD_FILE,
                status=ActionStatus.FAILED,
                selector=_selector_repr(action.selector),
                error_message=str(exc),
            )

        loc = _resolve_locator(page, action.selector)
        await loc.set_input_files(resolved_paths)
        logger.debug("Uploaded {n} file(s) to {sel}", n=len(resolved_paths), sel=action.selector)
        return ActionResult(
            action=ActionType.UPLOAD_FILE,
            status=ActionStatus.SUCCESS,
            selector=_selector_repr(action.selector),
            # v1.6.14 A-3: surface only basenames, never the resolved absolute
            # paths. Echoing absolute filesystem paths back through the
            # ActionResult (and thus to an MCP/LLM caller) leaks the server's
            # directory layout and acts as a path oracle. The upload itself
            # still used the full resolved paths above.
            data={"paths": [Path(p).name for p in resolved_paths], "count": len(resolved_paths)},
        )

    async def _do_iframe_click(self, page: Page, action: IframeClickInput) -> ActionResult:
        """Click a target inside an iframe via Playwright's frame_locator.

        ``frame_locator`` returns a chainable scoped to the iframe;
        subsequent ``locator(...).click()`` calls operate inside it.
        Works for same-origin iframes. Cross-origin iframes raise --
        coord-click is the fallback for those.
        """
        frame = page.frame_locator(action.iframe_selector)
        target = frame.locator(action.inner_selector)
        if action.timeout is not None:
            await target.click(timeout=action.timeout)
        else:
            await target.click()
        logger.debug(
            "Clicked {inner} inside iframe {ifr}",
            inner=action.inner_selector,
            ifr=action.iframe_selector,
        )
        return ActionResult(
            action=ActionType.IFRAME_CLICK,
            status=ActionStatus.SUCCESS,
            selector=f"{action.iframe_selector} >> {action.inner_selector}",
            data={
                "iframe_selector": action.iframe_selector,
                "inner_selector": action.inner_selector,
            },
        )

    async def _do_shadow_dom_click(self, page: Page, action: ShadowDomClickInput) -> ActionResult:
        """Click an element inside a shadow DOM tree.

        Playwright auto-pierces shadow DOM for CSS selectors. The ``>>``
        combinator chains a parent and a descendant locator -- we use
        it here with ``host_selector >> inner_selector`` so callers can
        compose pierce queries explicitly.
        """
        combined = f"{action.host_selector} >> {action.inner_selector}"
        loc = page.locator(combined)
        if action.timeout is not None:
            await loc.click(timeout=action.timeout)
        else:
            await loc.click()
        logger.debug("Shadow-DOM click via {sel}", sel=combined)
        return ActionResult(
            action=ActionType.SHADOW_DOM_CLICK,
            status=ActionStatus.SUCCESS,
            selector=combined,
            data={
                "host_selector": action.host_selector,
                "inner_selector": action.inner_selector,
            },
        )

    async def _do_drag_and_drop(self, page: Page, action: DragAndDropInput) -> ActionResult:
        """Drag from one selector and drop on another.

        Resolves both via _resolve_locator to honor semantic LocatorSpec
        AND CSS-string selectors uniformly. Playwright's
        ``page.drag_and_drop`` accepts only string selectors, so we use
        the lower-level ``source.drag_to(target)`` API for LocatorSpec
        compatibility.
        """
        source_loc = _resolve_locator(page, action.source)
        target_loc = _resolve_locator(page, action.target)
        if action.timeout is not None:
            await source_loc.drag_to(target_loc, timeout=action.timeout)
        else:
            await source_loc.drag_to(target_loc)
        logger.debug("Dragged {src} -> {tgt}", src=action.source, tgt=action.target)
        return ActionResult(
            action=ActionType.DRAG_AND_DROP,
            status=ActionStatus.SUCCESS,
            data={
                "source": _selector_repr(action.source),
                "target": _selector_repr(action.target),
            },
        )

    # ------------------------------------------------------------------
    # v1.6.7 Top-level scroll_until_text / print_page_as_pdf
    # ------------------------------------------------------------------

    async def scroll_until_text(
        self,
        text: str,
        *,
        session_id: str,
        tab_id: Optional[str] = None,
        max_scrolls: int = 10,
        scroll_step: int = 800,
    ) -> ActionResult:
        """Scroll the page in `scroll_step`-px increments until ``text``
        is present in document.body.innerText, or ``max_scrolls`` is
        exhausted.

        Useful for infinite-scroll feeds where the target row only
        materializes after enough scrolling.
        """
        # v1.6.16 deep-review fix: bound max_scrolls. This path does NOT go
        # through a pydantic action model, so ScrollInput.infinite_scroll_max's
        # le=1000 bound does not apply; each iteration costs a mouse.wheel + a
        # ~2s wait_for_load_state + a page.evaluate, so an LLM / prompt-injection
        # value like 10**8 made total wall-clock attacker-controlled (months)
        # and pinned the session tab. Clamp to the same 1000 ceiling (covers the
        # MCP tool and the direct Python API in one place).
        max_scrolls = max(0, min(max_scrolls, 1000))
        if self._sessions is None:
            raise RuntimeError("scroll_until_text requires a SessionManager")
        tab_mgr = self._sessions.get_tab_manager(session_id)
        page = tab_mgr.get_or_current(tab_id)
        if page is None:
            raise ValueError(f"Session {session_id!r} has no current tab. Open one first.")
        self._sessions.touch(session_id)

        # Re-gate the session tab's current URL before reading its content.
        # A prior navigation may have parked this tab on a denied/private
        # host (e.g. a redirect a post-nav check flagged while the tab still
        # sits there); reading document.body.innerText off such a page would
        # be a deny-list bypass / content-exfil oracle.
        if not check_domain_allowed(page.url, self._config.safety):
            host = urlparse(page.url).hostname or ""
            return ActionResult(
                action=ActionType.SCROLL,
                status=ActionStatus.FAILED,
                error_message=f"scroll_until_text: current page URL is on a disallowed domain: {host}",
            )

        from .exceptions import ActionError

        # Already on the page? Quick win. A failure here is non-fatal: we
        # simply fall through to the scroll loop, which re-reads the body.
        # (BR-8) Log at DEBUG rather than swallowing silently.
        try:
            body = await page.evaluate("() => document.body ? document.body.innerText : ''")
            if isinstance(body, str) and text in body:
                return ActionResult(
                    action=ActionType.SCROLL,
                    status=ActionStatus.SUCCESS,
                    data={"text": text, "scrolls_used": 0, "found": True},
                )
        except Exception as exc:
            logger.debug("scroll_until_text initial body read failed: {e}", e=exc)

        for i in range(max_scrolls):
            # R1 (concurrency): keep the idle clock warm for the WHOLE scroll.
            # The entry touch() above only stamps last_used once; a long walk
            # (up to 1000 rounds, each waiting ~2s) otherwise freezes the idle
            # clock for the entire duration, so a concurrent create()/list()
            # whose _reap_idle fires (busy-blind: SessionManager._idle_expired
            # selects purely on now - last_used > session_idle_ttl_s) can
            # close() this live tab out from under the active scroll. Re-touch
            # once per round -- a cheap dict write -- so an actively-scrolling
            # session is never seen as idle. self._sessions is non-None here
            # (guarded at entry); touch() no-ops on an unknown id.
            self._sessions.touch(session_id)
            # BR-8: a closed page can never yield more text -- surface it as a
            # genuine error instead of silently spinning to a misleading
            # "text not found after N scrolls".
            if page.is_closed():
                raise ActionError("scroll_until_text: page was closed mid-scroll", action="scroll")
            try:
                await page.mouse.wheel(0, scroll_step)
                await page.wait_for_load_state("domcontentloaded", timeout=2000)
            except PlaywrightTimeout as exc:
                # Benign: an infinite-scroll feed often never reaches a quiet
                # load state within the 2s budget. Keep scrolling.
                logger.debug("scroll_until_text load-state wait timed out: {e}", e=exc)
            try:
                body = await page.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception as exc:
                # If the page closed, this is fatal -- re-raise. Otherwise it
                # is a transient eval error during navigation; ride over it.
                if page.is_closed():
                    raise ActionError(
                        "scroll_until_text: page was closed mid-scroll", action="scroll"
                    ) from exc
                logger.debug("scroll_until_text body read failed (transient): {e}", e=exc)
                continue
            if isinstance(body, str) and text in body:
                return ActionResult(
                    action=ActionType.SCROLL,
                    status=ActionStatus.SUCCESS,
                    data={"text": text, "scrolls_used": i + 1, "found": True},
                )

        return ActionResult(
            action=ActionType.SCROLL,
            status=ActionStatus.FAILED,
            data={"text": text, "scrolls_used": max_scrolls, "found": False},
            error_message=f"Text {text!r} not found after {max_scrolls} scrolls",
        )

    async def scroll_to_bottom(
        self,
        *,
        session_id: str,
        tab_id: Optional[str] = None,
        max_scrolls: Optional[int] = None,
        settle_ms: Optional[int] = None,
        stable_rounds: Optional[int] = None,
    ) -> ActionResult:
        """v1.7.0 Wave 3B: scroll to the bottom repeatedly until lazy content stops loading.

        Repeatedly scroll the session tab to the bottom, waiting
        ``settle_ms`` after each scroll for lazy / infinite-scroll content
        to load, until the document ``scrollHeight`` stops growing for
        ``stable_rounds`` consecutive rounds (a stable bottom) OR
        ``max_scrolls`` is hit (the cap). This materializes the FULL
        assembled DOM so a subsequent ``observe`` / extract on the same tab
        sees every below-the-fold item -- the recurring failure mode where
        scroll-triggered content never enters an agent's observation.

        Thin, bounded browser action -- it does NOT extract; pair it with a
        fetch/observe on the same tab.

        Args:
            session_id: Persistent browser session whose tab to scroll.
            tab_id: Tab within the session (defaults to the current tab).
            max_scrolls: Hard cap on scroll rounds. Defaults to
                ``automation.infinite_scroll_max``. Clamped to ``[0, 1000]``
                (same ceiling as ``scroll_until_text`` -- this path does not
                go through a pydantic action model, so a hostile value is
                clamped here).
            settle_ms: Milliseconds to wait after each scroll for lazy
                content. Defaults to ``automation.scroll_settle_ms``.
            stable_rounds: Consecutive unchanged-height rounds required to
                declare a stable bottom. Defaults to
                ``automation.scroll_stable_rounds``. Clamped to ``>= 1``.

        Returns:
            ActionResult with ``data={"scrolls_used": int, "reached_bottom":
            bool, "final_height": int}``. ``reached_bottom`` is True when the
            height stabilized, False when ``max_scrolls`` was hit while the
            page was still growing.
        """
        automation = self._config.automation
        if max_scrolls is None:
            # Reuse the existing infinite-scroll ceiling rather than a new knob.
            max_scrolls = ScrollInput.model_fields["infinite_scroll_max"].default
        if settle_ms is None:
            settle_ms = automation.scroll_settle_ms
        if stable_rounds is None:
            stable_rounds = automation.scroll_stable_rounds
        # Same 1000-round wall-clock clamp as scroll_until_text: each round
        # costs an evaluate + a settle sleep, so an LLM / prompt-injection
        # value like 10**8 would make total wall-clock attacker-controlled.
        max_scrolls = max(0, min(int(max_scrolls), 1000))
        settle_ms = max(0, int(settle_ms))
        stable_rounds = max(1, int(stable_rounds))

        if self._sessions is None:
            raise RuntimeError("scroll_to_bottom requires a SessionManager")
        tab_mgr = self._sessions.get_tab_manager(session_id)
        page = tab_mgr.get_or_current(tab_id)
        if page is None:
            raise ValueError(f"Session {session_id!r} has no current tab. Open one first.")
        self._sessions.touch(session_id)

        # Re-gate the session tab's current URL before driving / reading it.
        # A prior navigation may have parked this tab on a denied/private
        # host; scrolling it (and surfacing its assembled content to a later
        # extract) would be a deny-list bypass / content-exfil oracle.
        if not check_domain_allowed(page.url, self._config.safety):
            host = urlparse(page.url).hostname or ""
            return ActionResult(
                action=ActionType.SCROLL,
                status=ActionStatus.FAILED,
                error_message=(
                    f"scroll_to_bottom: current page URL is on a disallowed domain: {host}"
                ),
            )

        from .exceptions import ActionError

        # v1.6.14 A-6 pattern: bound each evaluate so a hung page can't stall
        # for up to max_scrolls x an unbounded wait. Page.evaluate takes no
        # timeout kwarg, so wrap each call in asyncio.wait_for. A timed-out
        # evaluate raises the builtin TimeoutError, which execute_action's
        # caller surfaces as TIMEOUT; here we propagate it the same way the
        # other session-driving actions do (the Agent wrapper runs inside
        # _call_scope and the public surface returns result-based, but a
        # timeout on a hung page is a genuine error worth surfacing).
        eval_timeout = min(automation.default_action_timeout or 10000, 5000) / 1000

        async def _scroll_height() -> int:
            value = await asyncio.wait_for(
                page.evaluate("document.documentElement.scrollHeight"),
                timeout=eval_timeout,
            )
            return int(value) if isinstance(value, (int, float)) else 0

        try:
            last_height = await _scroll_height()
        except Exception as exc:
            if page.is_closed():
                raise ActionError(
                    "scroll_to_bottom: page was closed before scrolling", action="scroll"
                ) from exc
            logger.debug("scroll_to_bottom initial height read failed: {e}", e=exc)
            last_height = 0

        scrolls_used = 0
        stable = 0
        reached_bottom = False
        for _ in range(max_scrolls):
            # R1 (concurrency): keep the idle clock warm for the WHOLE scroll.
            # The entry touch() above only stamps last_used once; a long walk
            # (up to 1000 rounds, each waiting settle_ms) otherwise freezes the
            # idle clock for the entire duration, so a concurrent create()/list()
            # whose _reap_idle fires (busy-blind: SessionManager._idle_expired
            # selects purely on now - last_used > session_idle_ttl_s) can
            # close() this live tab out from under the active scroll. Re-touch
            # once per round -- a cheap dict write -- so an actively-scrolling
            # session is never seen as idle. self._sessions is non-None here
            # (guarded at entry); touch() no-ops on an unknown id.
            self._sessions.touch(session_id)
            if page.is_closed():
                raise ActionError("scroll_to_bottom: page was closed mid-scroll", action="scroll")
            try:
                await asyncio.wait_for(
                    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)"),
                    timeout=eval_timeout,
                )
            except Exception as exc:
                if page.is_closed():
                    raise ActionError(
                        "scroll_to_bottom: page was closed mid-scroll", action="scroll"
                    ) from exc
                logger.debug("scroll_to_bottom scroll step failed (transient): {e}", e=exc)
            scrolls_used += 1
            if settle_ms:
                await asyncio.sleep(settle_ms / 1000)
            try:
                new_height = await _scroll_height()
            except Exception as exc:
                if page.is_closed():
                    raise ActionError(
                        "scroll_to_bottom: page was closed mid-scroll", action="scroll"
                    ) from exc
                logger.debug("scroll_to_bottom height read failed (transient): {e}", e=exc)
                continue
            if new_height <= last_height:
                stable += 1
                if stable >= stable_rounds:
                    reached_bottom = True
                    last_height = new_height
                    break
            else:
                stable = 0
            last_height = new_height

        logger.debug(
            "scroll_to_bottom: {n} scrolls, reached_bottom={b}, height={h}",
            n=scrolls_used,
            b=reached_bottom,
            h=last_height,
        )
        return ActionResult(
            action=ActionType.SCROLL,
            status=ActionStatus.SUCCESS,
            data={
                "scrolls_used": scrolls_used,
                "reached_bottom": reached_bottom,
                "final_height": last_height,
            },
        )

    async def print_page_as_pdf(
        self,
        url: Optional[str] = None,
        output_path: Optional[str] = None,
        *,
        session_id: Optional[str] = None,
        tab_id: Optional[str] = None,
    ) -> ScreenshotResult:
        """Render a page to PDF using Chromium's headless ``page.pdf()``.

        Output path goes through ``safe_join_path`` against
        ``automation.screenshot_dir`` -- PDFs land alongside screenshots.
        Returns the same ``ScreenshotResult`` shape (path, dimensions
        carry zero for PDF) for consistency.
        """
        cid = get_correlation_id()
        safety = self._config.safety

        # Resolve target page (session or ephemeral with url)
        page: Optional[Page] = None
        ctx_mgr_local = None
        owner_mode = "ephemeral"
        if session_id and self._sessions is not None:
            tab_mgr = self._sessions.get_tab_manager(session_id)
            page = tab_mgr.get_or_current(tab_id)
            if page is None:
                raise ValueError(
                    f"Session {session_id!r} has no current tab. "
                    f"Open one with agent.new_tab(url, session_id=...)."
                )
            owner_mode = "session_persistent"
            self._sessions.touch(session_id)

        try:
            if page is None:
                if not url:
                    raise ValueError("print_page_as_pdf() requires either session_id or url.")
                if not check_domain_allowed(url, safety):
                    host = urlparse(url).hostname or ""
                    raise ValueError(f"Domain not allowed: {host}")
                ctx_mgr_local = self._bm.new_page(block_resources=False)
                page = await ctx_mgr_local.__aenter__()

            assert page is not None
            if url:
                await page.goto(url, wait_until=self._config.fetch.wait_until)
                if not check_domain_allowed(page.url, safety):
                    host = urlparse(page.url).hostname or ""
                    raise ValueError(f"Page redirected to disallowed domain: {host}")

            # Output path
            shot_dir = Path(self._config.automation.screenshot_dir)
            shot_dir.mkdir(parents=True, exist_ok=True)
            if output_path is None:
                filename = f"page_{cid or 'anon'}_{int(time.time() * 1000)}.pdf"
                resolved = safe_join_path(shot_dir, filename)
            else:
                # _is_cross_platform_absolute mirrors v1.6.4's project
                # convention -- ``Path.is_absolute()`` is OS-dependent
                # and would treat ``C:\\...`` as relative on POSIX.
                resolved = (
                    Path(output_path).resolve()
                    if _is_cross_platform_absolute(output_path)
                    else safe_join_path(shot_dir, output_path)
                )

            await page.pdf(path=str(resolved))
            # PDFs reuse ScreenshotResult; format is PNG (the closest enum
            # we have today) -- callers identify PDFs via the .pdf suffix
            # on ``path``. status=SUCCESS. We do stat the file for size --
            # the cost is negligible vs. the chromium PDF render that
            # just completed, and audit logs / size-budgets downstream
            # would otherwise under-count.
            try:
                size = resolved.stat().st_size
            except OSError:
                size = 0
            return ScreenshotResult(
                url=page.url,
                path=str(resolved),
                format=ScreenshotFormat.PNG,
                size_bytes=size,
                status=ActionStatus.SUCCESS,
                correlation_id=cid,
            )
        finally:
            if owner_mode == "ephemeral" and ctx_mgr_local is not None:
                with contextlib.suppress(Exception):
                    await ctx_mgr_local.__aexit__(None, None, None)

    # ------------------------------------------------------------------
    # v1.6.6 Feature 5: observe mode
    # ------------------------------------------------------------------

    async def _enumerate_interactive_elements(
        self, page: Page
    ) -> tuple[list[InteractiveElement], bool]:
        """v1.7.0 Wave 4C set-of-marks: enumerate viewport-visible actionable
        elements via a single ``page.evaluate`` DOM walk.

        Bounded by ``automation.observe_max_elements`` and (when
        ``automation.observe_tag_refs``) stamps a ``data-webtool-ref="eN"``
        attribute on each so a later action can target it via
        ``LocatorSpec(ref="eN")``. Returns ``(elements, truncated)``. Best
        effort: a failed evaluate yields ``([], False)`` and logs at DEBUG --
        observe()'s other fields still return.
        """
        automation = self._config.automation
        max_elements = automation.observe_max_elements
        tag_refs = automation.observe_tag_refs
        try:
            raw = await page.evaluate(
                _ENUMERATE_INTERACTIVE_JS,
                {"max": max_elements, "tag": tag_refs},
            )
        except Exception as exc:
            logger.debug("observe element enumeration failed: {e}", e=exc)
            return [], False

        if not isinstance(raw, dict):
            return [], False
        raw_elements = raw.get("elements")
        truncated = bool(raw.get("truncated"))
        if not isinstance(raw_elements, list):
            return [], truncated

        elements: list[InteractiveElement] = []
        for item in raw_elements:
            if not isinstance(item, dict):
                continue
            try:
                elements.append(InteractiveElement.model_validate(item))
            except ValidationError as exc:  # pragma: no cover - defensive
                logger.debug("observe element coercion skipped one entry: {e}", e=exc)

        if truncated:
            logger.warning(
                "observe enumerated {n} interactive elements; truncated at "
                "automation.observe_max_elements={cap}. Raise the cap or "
                "narrow the viewport to see the rest.",
                n=len(elements),
                cap=max_elements,
            )
        return elements, truncated

    async def observe(
        self,
        url: Optional[str] = None,
        *,
        session_id: Optional[str] = None,
        tab_id: Optional[str] = None,
        include_text: bool = True,
        include_aria: bool = False,
        include_elements: bool = True,
    ) -> ObserveResult:
        """Capture a page's visual and structural state for observe-act-verify loops.

        Resolution order for the target page:
          1. If ``session_id`` is set, use the session's TabManager:
             ``tab_id`` (if given) or the session's current tab.
          2. Else, if ``url`` is given, open an ephemeral page and navigate.
          3. Else raise.

        Always returns a screenshot path plus viewport / page / scroll /
        DPR. ``include_text`` (default True) captures
        ``document.body.innerText`` truncated to
        ``safety.max_chars_per_call``. ``include_aria`` (default False)
        runs ``page.accessibility.snapshot()`` -- off by default because
        snapshots can be megabytes on complex pages.

        ``include_elements`` (default True) runs the v1.7.0 Wave 4C
        set-of-marks pass: one ``page.evaluate`` walks the DOM and returns a
        BOUNDED, numbered list of the actionable elements visible in the
        viewport on ``ObserveResult.elements`` (each an
        :class:`InteractiveElement` with ref / role / name / tag / enabled /
        visible / bbox / selector). Bounded by
        ``automation.observe_max_elements``; ``ObserveResult.elements_truncated``
        flags (and a WARNING logs) when the page exceeds the cap. When
        ``automation.observe_tag_refs`` is True (default) each element is
        stamped with a ``data-webtool-ref="eN"`` attribute so a later action
        can target it via ``LocatorSpec(ref="eN")`` -- the observe -> act loop.
        The ref re-resolves against the live tab and fails cleanly (the
        established not-found path) if the element is gone.
        """
        cid = get_correlation_id()
        safety = self._config.safety

        # Resolve target page
        page: Optional[Page] = None
        ctx_mgr_local = None
        used_tab_id: Optional[str] = None
        owner_mode = "ephemeral"

        if session_id and self._sessions is not None:
            tab_mgr = self._sessions.get_tab_manager(session_id)
            page = tab_mgr.get_or_current(tab_id)
            if page is None:
                raise ValueError(
                    f"Session {session_id!r} has no current tab. "
                    f"Open one with agent.new_tab(url, session_id=...)."
                )
            used_tab_id = tab_id if tab_id is not None else tab_mgr.current_tab_id()
            owner_mode = "session_persistent"
            self._sessions.touch(session_id)

        try:
            if page is None:
                if not url:
                    raise ValueError(
                        "observe() requires either session_id (to use the session's "
                        "current tab) or url (to open an ephemeral page)."
                    )
                if not check_domain_allowed(url, safety):
                    host = urlparse(url).hostname or ""
                    raise ValueError(f"Domain not allowed: {host}")
                ctx_mgr_local = self._bm.new_page(block_resources=False)
                page = await ctx_mgr_local.__aenter__()

            # All branches above set ``page``; narrow for mypy.
            assert page is not None

            # Navigate if URL given. For session+tab mode without a URL,
            # we observe the current state in place.
            if url:
                # Pre-navigation gate on the INPUT url. The session path
                # resolves ``page`` from the session tab above, skipping the
                # ephemeral branch's identical check -- without this gate the
                # goto below would fire an outbound request to a denied/private
                # host before the post-redirect check could see it (blind SSRF
                # on an LLM-controlled URL).
                if not check_domain_allowed(url, safety):
                    host = urlparse(url).hostname or ""
                    raise ValueError(f"Domain not allowed: {host}")
                # Snapshot the session tab's previous URL so we can roll
                # back if the goto lands on a denied host. Without this,
                # a thwarted observe() permanently navigates the session's
                # main tab away from whatever the user was on.
                prev_url = page.url if owner_mode == "session_persistent" else None
                await page.goto(url, wait_until=self._config.fetch.wait_until)
                # Post-redirect re-check
                if not check_domain_allowed(page.url, safety):
                    host = urlparse(page.url).hostname or ""
                    # BR-7: re-validate prev_url against the same safety
                    # policy before rolling back. prev_url is the tab's
                    # last-known URL, but the policy may have changed since
                    # (or the prior page itself sat on a now-denied host),
                    # and navigating back must not become a second way onto a
                    # disallowed domain. Skip the rollback if prev_url no
                    # longer passes; leaving the tab where it is (then
                    # raising) is the fail-closed choice.
                    if (
                        prev_url
                        and prev_url != "about:blank"
                        and check_domain_allowed(prev_url, safety)
                    ):
                        with contextlib.suppress(Exception):
                            await page.goto(prev_url)
                    raise ValueError(f"Page redirected to disallowed domain: {host}")

            # Screenshot to screenshot_dir
            screenshot_dir = Path(self._config.automation.screenshot_dir)
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            filename = f"observe_{cid or 'anon'}_{int(time.time() * 1000)}.png"
            shot_path = safe_join_path(screenshot_dir, filename)
            await page.screenshot(path=str(shot_path), full_page=False)

            # Collect viewport / page / scroll / DPR in one IPC hop
            dims = await page.evaluate(
                "() => ({"
                " vw: window.innerWidth,"
                " vh: window.innerHeight,"
                " pw: document.documentElement.scrollWidth,"
                " ph: document.documentElement.scrollHeight,"
                " sx: window.scrollX,"
                " sy: window.scrollY,"
                " dpr: window.devicePixelRatio"
                " })"
            )

            title: Optional[str]
            try:
                title = await page.title()
            except Exception:
                title = None

            visible_text: Optional[str] = None
            if include_text:
                try:
                    raw = await page.evaluate("() => document.body ? document.body.innerText : ''")
                    if raw and isinstance(raw, str):
                        cap = safety.max_chars_per_call
                        visible_text = raw[:cap] if cap and len(raw) > cap else raw
                except Exception as exc:
                    logger.debug("observe innerText capture failed: {e}", e=exc)

            aria_snapshot: Optional[dict[str, Any]] = None
            if include_aria:
                try:
                    # Playwright's stubs hide page.accessibility from public API
                    # but the runtime attribute exists across versions.
                    aria_snapshot = await page.accessibility.snapshot()  # type: ignore[attr-defined]
                except Exception as exc:
                    logger.debug("observe aria snapshot failed: {e}", e=exc)

            elements: list[InteractiveElement] = []
            elements_truncated = False
            if include_elements:
                elements, elements_truncated = await self._enumerate_interactive_elements(page)

            return ObserveResult(
                url=page.url,
                title=title,
                screenshot_path=str(shot_path),
                viewport_width=int(dims["vw"]),
                viewport_height=int(dims["vh"]),
                page_width=int(dims["pw"]),
                page_height=int(dims["ph"]),
                scroll_x=int(dims["sx"]),
                scroll_y=int(dims["sy"]),
                device_pixel_ratio=float(dims["dpr"]),
                visible_text=visible_text,
                aria_snapshot=aria_snapshot,
                elements=elements,
                elements_truncated=elements_truncated,
                tab_id=used_tab_id,
                session_id=session_id,
                correlation_id=cid,
            )
        finally:
            # Persistent session tabs outlive observe() -- only close
            # ephemeral contexts we opened ourselves.
            if owner_mode == "ephemeral" and ctx_mgr_local is not None:
                with contextlib.suppress(Exception):
                    await ctx_mgr_local.__aexit__(None, None, None)

    # ------------------------------------------------------------------
    # v1.6.6 Feature 4: top-level execute_single_on_session helper
    # ------------------------------------------------------------------

    async def execute_single_on_session(
        self,
        action: Action,
        *,
        session_id: str,
        tab_id: Optional[str] = None,
    ) -> ActionResult:
        """Execute a single action against a session's tab without
        navigating first. Used by top-level Agent.click_xy / type_text /
        press_key which target a live page, not a URL.

        Raises KeyError if session_id is unknown or the session has no tab.
        """
        if self._sessions is None:
            raise RuntimeError(
                "Agent has no SessionManager wired up; cannot run session-targeted single actions."
            )
        tab_mgr = self._sessions.get_tab_manager(session_id)
        page = tab_mgr.get_or_current(tab_id)
        if page is None:
            raise ValueError(
                f"Session {session_id!r} has no current tab. Open one with agent.new_tab(...)."
            )
        self._sessions.touch(session_id)
        return await self.execute_action(page, action)

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
        # v1.6.6 coordinate-level fallbacks (Feature 4)
        ActionType.CLICK_XY: _do_click_xy,
        ActionType.TYPE_TEXT: _do_type_text,
        ActionType.PRESS_KEY: _do_press_key,
        # v1.6.7 interaction-skill library (Feature 5)
        ActionType.UPLOAD_FILE: _do_upload_file,
        ActionType.IFRAME_CLICK: _do_iframe_click,
        ActionType.SHADOW_DOM_CLICK: _do_shadow_dom_click,
        ActionType.DRAG_AND_DROP: _do_drag_and_drop,
    }
