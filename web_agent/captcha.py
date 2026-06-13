"""Pluggable CAPTCHA / bot-challenge resolver hook (v1.7.0).

``web_agent`` DETECTS bot walls structurally (see
:mod:`web_agent.challenge`) and, by default, surfaces an unbeaten wall
honestly as :class:`~web_agent.models.FetchStatus.BLOCKED`. It does NOT
ship a CAPTCHA-solving service: bundling a third-party solver (2captcha,
Anti-Captcha, CapSolver, ...) would bake in a paid dependency, a stream
of ToS / abuse questions, and a moving target. Instead this module
defines a clean, well-typed EXTENSION POINT so the operator can supply
their own resolution strategy -- a human-in-the-loop handoff, a headed
browser hand-off, a paid solver API, an audio-CAPTCHA transcriber, etc.

How it plugs in:

    from web_agent import Agent, CaptchaContext, CaptchaResolution

    async def resolve(ctx: CaptchaContext) -> CaptchaResolution:
        # ctx.page is the LIVE Playwright page sitting on the wall;
        # ctx.challenge tells you the vendor / kind. Interact with the
        # page to clear it (inject a token, click the checkbox, wait for
        # a human), then report what you did.
        token = await my_solver(ctx.challenge.vendor, ctx.url)
        await ctx.page.evaluate(_inject_token_js, token)
        await ctx.page.click("button[type=submit]")
        return CaptchaResolution(resolved=True, method="2captcha")

    agent = Agent(captcha_resolver=resolve)        # or agent.captcha_resolver = resolve

Honesty guarantee: the resolver's own verdict is ADVISORY. After the
hook runs, :class:`web_agent.web_fetcher.WebFetcher` re-runs
:func:`web_agent.challenge.detect_challenge` against the live page and
only treats the wall as cleared when detection itself comes back clean.
A resolver that returns ``resolved=True`` but leaves the interstitial
standing does not turn a BLOCKED into a SUCCESS.

The hook is a Python callable, so -- like the Wave 6 ``llm_extractor`` --
it is NEVER accepted over the MCP wire; it is configured in-process by
whoever constructs the :class:`~web_agent.agent.Agent`.

This module stays import-light (stdlib + ``models`` only); the live
``Page`` type is referenced only under ``TYPE_CHECKING`` so the hook
contract can be imported and unit-tested without pulling in Playwright.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Union

from .models import ChallengeInfo

if TYPE_CHECKING:  # pragma: no cover -- typing only; no runtime Playwright dep
    from playwright.async_api import Page


@dataclass(frozen=True)
class CaptchaContext:
    """Everything a resolver hook needs to attempt one clearing pass.

    Passed to the :data:`CaptchaResolver` callable each attempt. Holds a
    LIVE Playwright :class:`~playwright.async_api.Page` parked on the
    interstitial -- the resolver drives it directly (fill a token, click
    the checkbox, wait for a human) -- plus the structural
    :class:`~web_agent.models.ChallengeInfo` that identified the wall.
    """

    page: Page
    """The live page sitting on the challenge. The resolver interacts with
    it to clear the wall; web_agent re-reads it afterwards to verify."""

    challenge: ChallengeInfo
    """The detected wall (vendor / kind / confidence / evidence). Re-detected
    fresh each attempt, so on a 2nd attempt this reflects the current state."""

    url: str
    """The originally requested URL (pre-redirect)."""

    final_url: str
    """The page's current URL (``page.url``) at the start of this attempt --
    challenge endpoints often differ from the requested URL."""

    attempt: int
    """1-based attempt counter, bounded by ``FetchConfig.captcha_max_attempts``."""

    max_attempts: int
    """The total attempt budget for this wall (``captcha_max_attempts``)."""

    correlation_id: Optional[str] = None
    """The active correlation id, for stitching resolver logs into the trace."""


@dataclass(frozen=True)
class CaptchaResolution:
    """A resolver hook's report on one clearing attempt.

    ``resolved`` is the resolver's own belief about whether it cleared the
    wall -- it is ADVISORY. web_agent re-runs structural detection against
    the live page and that re-detection, not this flag, decides whether the
    fetch proceeds. ``resolved=False`` additionally signals "I give up on
    this wall", so web_agent stops early instead of burning the remaining
    attempt budget.
    """

    resolved: bool
    """The resolver's belief that it cleared the wall this attempt. Advisory:
    re-detection is authoritative. False also means "stop, I cannot solve it"."""

    detail: Optional[str] = None
    """Optional human-readable note (e.g. ``"audio captcha transcribed"``),
    surfaced in logs for explainability."""

    method: Optional[str] = None
    """Optional short tag for the technique used (e.g. ``"2captcha"``,
    ``"human-handoff"``), surfaced in logs for explainability."""


# A resolver hook: given a CaptchaContext, attempt to clear the wall and
# report the outcome. May return a CaptchaResolution, a bare bool
# (True == "I cleared it"), or None (treated as not-resolved).
#
# PREFER ``async def``. An async hook is bounded by
# ``FetchConfig.captcha_attempt_timeout_s`` and yields the event loop while it
# waits. A SYNCHRONOUS hook BLOCKS the entire event loop for its whole
# duration and cannot be timed out -- so any hook that waits on I/O, a human,
# or a remote solver MUST be async, or it will stall all other concurrent work.
CaptchaResolver = Callable[
    [CaptchaContext],
    Union[bool, CaptchaResolution, None, Awaitable[Union[bool, CaptchaResolution, None]]],
]


def normalize_resolution(value: object) -> CaptchaResolution:
    """Coerce a resolver's return value into a :class:`CaptchaResolution`.

    Lenient by design so simple hooks can just ``return True`` / ``return
    False`` / ``return None``:

    - ``None`` -> ``CaptchaResolution(resolved=False)``
    - ``bool`` -> ``CaptchaResolution(resolved=<bool>)``
    - a :class:`CaptchaResolution` -> returned unchanged
    - any object exposing a ``bool`` ``.resolved`` attribute -> adopted
      (with ``.detail`` / ``.method`` when they are strings)
    - anything else -> ``CaptchaResolution(resolved=bool(value))``
    """
    if value is None:
        return CaptchaResolution(resolved=False)
    if isinstance(value, CaptchaResolution):
        return value
    if isinstance(value, bool):
        return CaptchaResolution(resolved=value)
    resolved_attr = getattr(value, "resolved", None)
    if isinstance(resolved_attr, bool):
        detail = getattr(value, "detail", None)
        method = getattr(value, "method", None)
        return CaptchaResolution(
            resolved=resolved_attr,
            detail=detail if isinstance(detail, str) else None,
            method=method if isinstance(method, str) else None,
        )
    return CaptchaResolution(resolved=bool(value))
