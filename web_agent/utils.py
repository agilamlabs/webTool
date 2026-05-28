"""Retry decorator, retry policies, user-agent rotation, domain checks, budget tracking, and helpers."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import ipaddress
import random
import re
import socket
import time
from collections.abc import Callable
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import urlparse

from loguru import logger

# Import to ensure loguru patcher is installed when utils is imported
from . import correlation as _correlation  # noqa: F401
from .models import HtmlCaptureSource

if TYPE_CHECKING:
    from playwright.async_api import Page

    from .config import SafetyConfig

T = TypeVar("T")

# ---------------------------------------------------------------------------
# User Agent Pool -- real, recent browser strings across OS/browser combos
# ---------------------------------------------------------------------------
USER_AGENTS: list[str] = [
    # Chrome 131 - Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 131 - macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 131 - Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 130 - Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Firefox 132 - Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    # Safari 18 - macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    # Edge 131 - Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]


def get_random_user_agent() -> str:
    """Return a random user-agent string from the pool."""
    return random.choice(USER_AGENTS)


# ---------------------------------------------------------------------------
# Async Retry Decorator
# ---------------------------------------------------------------------------
def async_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    non_retryable_exceptions: tuple[type[Exception], ...] = (),
) -> Callable:
    """Decorator that retries an async function with exponential backoff + jitter.

    Raises:
        ValueError: If ``max_retries < 1``. Without at least one attempt
            the decorator's loop body never runs and the trailing
            ``raise last_exception`` would raise a bogus
            ``TypeError("exceptions must derive from BaseException")``
            because ``last_exception`` would be None.
    """
    if max_retries < 1:
        raise ValueError(f"max_retries must be >= 1, got {max_retries}")

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except non_retryable_exceptions as e:
                    logger.warning(
                        "Non-retryable error in {fn}: {e}",
                        fn=func.__name__,
                        e=e,
                    )
                    raise
                except Exception as e:
                    last_exception = e
                    if attempt == max_retries:
                        logger.error(
                            "{fn} failed after {n} attempts: {e}",
                            fn=func.__name__,
                            n=max_retries,
                            e=e,
                        )
                        raise
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    jitter = delay * random.uniform(0.5, 1.0)
                    logger.warning(
                        "{fn} attempt {a}/{n} failed: {e}. Retrying in {d:.1f}s",
                        fn=func.__name__,
                        a=attempt,
                        n=max_retries,
                        e=e,
                        d=jitter,
                    )
                    await asyncio.sleep(jitter)
            raise last_exception  # type: ignore[misc]  # unreachable

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Timing Helper
# ---------------------------------------------------------------------------
class Timer:
    """Simple context manager for measuring elapsed wall-clock time."""

    def __init__(self) -> None:
        self._start: float = 0.0
        self._end: float = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        self._end = time.perf_counter()

    @property
    def elapsed_ms(self) -> float:
        return (self._end - self._start) * 1000


# ---------------------------------------------------------------------------
# HTTP Error Classification
# ---------------------------------------------------------------------------
class NonRetryableHTTPError(Exception):
    """HTTP error that should not be retried (e.g. 404, 403)."""

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(f"HTTP {status_code} for {url}")


# Retryable 5xx errors are raised as plain ``Exception`` from the
# fetcher and handled by the ``async_retry`` decorator's catch-all
# branch -- there's no value in a dedicated class today, since no
# caller distinguishes "retryable HTTP" from other transient
# exceptions. If that changes, add ``RetryableHTTPError`` back here.


def parse_retry_after(header_value: str | None) -> float | None:
    """Parse an HTTP ``Retry-After`` header into seconds-from-now.

    RFC 9110 §10.2.3: the header is EITHER an integer (delta-seconds)
    OR an HTTP-date. Both forms are handled here; the result is the
    number of seconds the client should wait before retrying.

    v1.6.12: introduced so :meth:`RateLimiter.notify_429` and
    :class:`WebFetcher` can back off the right amount on a 429
    response. Exported from ``web_agent`` so callers writing custom
    backoff logic can reuse it.

    Args:
        header_value: Raw header value or ``None`` (e.g. from
            ``response.headers.get("retry-after")``).

    Returns:
        Float seconds from now (``>= 0.0``), or ``None`` if the header
        is absent or unparseable. Negative deltas (past dates) clamp
        to ``0.0``.
    """
    if not header_value:
        return None
    val = header_value.strip()
    # Try integer seconds first (most common server form).
    try:
        return max(0.0, float(int(val)))
    except (ValueError, TypeError):
        pass
    # Fall back to HTTP-date. ``parsedate_to_datetime`` raises on
    # malformed input (typeshed types it as returning a ``datetime``,
    # not ``Optional[datetime]``) so the try/except is the right gate.
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(val)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except (TypeError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Page Content Capture (v1.6.13)
# ---------------------------------------------------------------------------
# Marker substrings in Playwright's "Unable to retrieve content because the
# page is navigating and changing the content" error. Both substrings cover
# variations seen across Playwright 1.40-1.50; matching on either prevents
# us from chasing the exact message format if upstream rewords it.
_NAVIGATION_RACE_MARKERS = ("navigating and changing", "page is navigating")


def _is_navigation_race(exc: BaseException) -> bool:
    """Return True if ``exc`` looks like Playwright's mid-navigation race.

    Detected via message-substring match because the typed Playwright
    error class (``playwright._impl._errors.Error``) is not part of the
    documented public API and the message itself is the stable signal.
    """
    msg = str(exc)
    return any(marker in msg for marker in _NAVIGATION_RACE_MARKERS)


async def safe_page_content(
    page: Page,
    *,
    retries: int = 3,
    settle_ms: int = 250,
    use_cdp_fallback: bool = True,
    cdp_timeout_ms: int = 5000,
) -> tuple[str, HtmlCaptureSource]:
    """Capture ``page.content()`` resiliently across in-flight navigations.

    Playwright raises ``Error: Unable to retrieve content because the page
    is navigating and changing the content`` when ``page.content()`` is
    invoked exactly when the document is being torn down or replaced
    (client-side redirects, SPA route swaps, meta-refresh, Cloudflare
    interstitials, hydration mid-flight). The race is **transient** -- the
    page is fine, the snapshot moment was wrong -- so the right response
    is "wait a beat and retry," not "fail the fetch."

    This helper implements three fallback tiers:

    1. **``page.content()`` with bounded retry.** Up to ``retries``
       attempts. Between attempts the helper does
       ``wait_for_load_state('domcontentloaded', timeout=2000)``
       (best-effort, exceptions swallowed) plus a ``settle_ms`` sleep,
       but ONLY between attempts -- the settle is skipped after the
       final tier-1 attempt because the next step is tier-2 which runs
       in-page via ``page.evaluate`` and does not depend on DCL having
       fired. (v1.6.13 review-pass I-2: the prior version wasted up to
       2.25s on the last attempt before falling through.) Only the
       specific navigation-race error is retried; every other exception
       re-raises immediately so the outer ``async_retry`` decorator can
       do its normal work.

    2. **``page.evaluate('document.documentElement.outerHTML')``.** Runs
       inside the page context and tolerates some races that the
       remote-protocol ``page.content()`` rejects. Returns whatever the
       page-side DOM has right now.

    3. **CDP ``DOM.getOuterHTML``.** Reads the browser's internal DOM
       tree directly, bypassing the JS-side navigation checks both prior
       tiers honour. Only attempted when ``use_cdp_fallback=True``
       (default) and the page is on a CDP-capable backend (Chromium).
       The session is detached in a ``finally`` block so we never leak
       it. Failure (non-Chromium browser, detached page, CDP timeout)
       falls through to the final ``""`` return.

    Args:
        page: The Playwright ``Page`` to capture.
        retries: Maximum tier-1 attempts. Default 3 (one initial try +
            two retries).
        settle_ms: Milliseconds to sleep between tier-1 retries after
            ``wait_for_load_state``. Default 250.
        use_cdp_fallback: When True (default), tier 3 is attempted if
            tiers 1 and 2 both fail. Pass False to skip CDP (e.g. for
            non-Chromium backends where the extra round-trip is wasted).
        cdp_timeout_ms: Per-command timeout (in milliseconds) for the
            tier-3 CDP calls. v1.6.13 review-pass M-2: wired via
            ``asyncio.wait_for`` around each ``cdp.send`` so a hung CDP
            session can never block the helper indefinitely. On
            timeout the outer ``except Exception`` falls through to
            the final ``("", "navigating")`` return. Default 5000.

    Returns:
        A ``(html, source)`` tuple where ``source`` is one of:

        - ``"content"``: tier 1 succeeded.
        - ``"evaluate"``: tier 2 succeeded.
        - ``"cdp"``: tier 3 succeeded.
        - ``"navigating"``: all tiers failed. ``html`` is ``""``. The
          caller should treat the fetch as degraded -- e.g. set
          ``FetchResult.html_capture_source="navigating"`` so downstream
          telemetry / extractors can branch on it.

    Notes:
        Designed to never raise on the navigation-race path -- callers
        can rely on always getting a tuple back. Non-race exceptions
        propagate (so the outer ``async_retry`` still owns generic
        failure handling like network drops, timeouts, etc.).
    """
    last_err: Exception | None = None
    total_attempts = max(1, retries)

    # Tier 1: page.content() with bounded retry on the specific race.
    for attempt in range(total_attempts):
        try:
            html = await page.content()
            return html, "content"
        except Exception as exc:
            if not _is_navigation_race(exc):
                raise
            last_err = exc
            # v1.6.13 review-pass I-2: skip the settle on the LAST
            # attempt -- tier-2 (page.evaluate) runs in-page context
            # and doesn't depend on domcontentloaded having fired, so
            # the wait + sleep on the final iteration is pure waste
            # (up to 2.25s of latency on the already-degraded path).
            if attempt < total_attempts - 1:
                # Best-effort settle: domcontentloaded usually fires
                # quickly after the in-progress navigation resolves;
                # the 2s cap stops us from blocking on pages that never
                # reach DCL (long-poll, streamed responses).
                with contextlib.suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=2000)
                if settle_ms > 0:
                    await asyncio.sleep(settle_ms / 1000)

    # Tier 2: page.evaluate runs in the page context and tolerates some
    # races that the remote-protocol page.content() rejects.
    try:
        evaluated = await page.evaluate(
            "() => (document && document.documentElement "
            "&& document.documentElement.outerHTML) || ''"
        )
        if isinstance(evaluated, str) and evaluated:
            logger.debug(
                "safe_page_content tier-2 (evaluate) succeeded after race: {e}",
                e=last_err,
            )
            return evaluated, "evaluate"
    except Exception as exc:
        logger.debug("safe_page_content tier-2 (evaluate) failed: {e}", e=exc)

    # Tier 3: CDP DOM.getOuterHTML reads the browser's internal DOM tree
    # and bypasses most JS-side navigation checks. v1.6.13 review-pass
    # M-2: each cdp.send is wrapped in asyncio.wait_for so a hung CDP
    # session can't block the helper indefinitely; on TimeoutError the
    # outer except falls through to the final ("", "navigating") path.
    if use_cdp_fallback:
        cdp = None
        cdp_timeout = max(0.001, cdp_timeout_ms / 1000)
        try:
            cdp = await page.context.new_cdp_session(page)
            doc = await asyncio.wait_for(
                cdp.send("DOM.getDocument", {"depth": -1, "pierce": True}),
                timeout=cdp_timeout,
            )
            root_id = doc.get("root", {}).get("nodeId")
            if root_id is not None:
                outer = await asyncio.wait_for(
                    cdp.send("DOM.getOuterHTML", {"nodeId": root_id}),
                    timeout=cdp_timeout,
                )
                html = outer.get("outerHTML", "") if isinstance(outer, dict) else ""
                if isinstance(html, str) and html:
                    logger.debug(
                        "safe_page_content tier-3 (CDP) succeeded after race: {e}",
                        e=last_err,
                    )
                    return html, "cdp"
        except Exception as exc:
            logger.debug("safe_page_content tier-3 (CDP) failed: {e}", e=exc)
        finally:
            if cdp is not None:
                with contextlib.suppress(Exception):
                    await cdp.detach()

    # All tiers failed. Caller gets ("", "navigating") and can mark the
    # FetchResult degraded rather than crash the whole pipeline.
    logger.warning(
        "safe_page_content abandoned after all tiers (last error: {e})",
        e=last_err,
    )
    return "", "navigating"


# ---------------------------------------------------------------------------
# Retry Policy Profiles
# ---------------------------------------------------------------------------
class RetryPolicy(str, Enum):
    """Named retry profiles for declarative configuration.

    - ``FAST``: 1 retry, 0.5s base, 5s max. For latency-sensitive flows where
      a quick failure is preferred over recovery.
    - ``BALANCED``: 3 retries, 1s base, 30s max (current default).
    - ``PARANOID``: 5 retries, 2s base, 60s max. For flaky targets where
      eventual success matters more than speed.
    """

    FAST = "fast"
    BALANCED = "balanced"
    PARANOID = "paranoid"


_POLICY_KWARGS: dict[RetryPolicy, dict[str, float]] = {
    RetryPolicy.FAST: {"max_retries": 1, "base_delay": 0.5, "max_delay": 5.0},
    RetryPolicy.BALANCED: {"max_retries": 3, "base_delay": 1.0, "max_delay": 30.0},
    RetryPolicy.PARANOID: {"max_retries": 5, "base_delay": 2.0, "max_delay": 60.0},
}


def get_retry_policy(name: str | RetryPolicy) -> dict[str, float]:
    """Return a ``dict`` of kwargs (``max_retries``, ``base_delay``, ``max_delay``)
    suitable for passing to :func:`async_retry`.

    Args:
        name: Policy name (``fast``, ``balanced``, ``paranoid``) or a
            ``RetryPolicy`` enum member.

    Returns:
        Dict of retry kwargs. Raises ``ValueError`` if name is unknown.
    """
    try:
        policy = RetryPolicy(name) if not isinstance(name, RetryPolicy) else name
    except ValueError as exc:
        raise ValueError(
            f"Unknown retry policy: {name!r}. Choose from: {[p.value for p in RetryPolicy]}"
        ) from exc
    return dict(_POLICY_KWARGS[policy])


# ---------------------------------------------------------------------------
# Domain Allow / Deny Helpers
# ---------------------------------------------------------------------------
def _normalize_host(url: str) -> str:
    """Return the lowercase hostname (without port) from a URL, or empty string."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().strip()
        return host
    except Exception:
        return ""


def _matches_domain(host: str, pattern: str) -> bool:
    """Return True if ``host`` equals ``pattern`` or is a subdomain of it.

    Examples:
        _matches_domain("www.example.com", "example.com") -> True
        _matches_domain("api.example.com", "example.com") -> True
        _matches_domain("notexample.com", "example.com") -> False
    """
    host = host.lower().strip()
    pattern = pattern.lower().strip().lstrip(".")
    if not host or not pattern:
        return False
    return host == pattern or host.endswith("." + pattern)


def _is_private_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if ``ip`` is a true SSRF risk address.

    Covers RFC 1918 / RFC 4193 private ranges, loopback, link-local
    (including AWS IMDS at 169.254.169.254), and the unspecified address.

    Deliberately excludes ``is_reserved`` (which over-matches NAT64 and
    other public-traffic mechanisms) and ``is_multicast`` (rarely an SSRF
    target).
    """
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified)


@functools.lru_cache(maxsize=2048)
def _resolve_host_addresses(host: str) -> tuple[str, ...]:
    """Resolve ``host`` to all addresses, cached for the process lifetime.

    Cached because ``check_domain_allowed`` calls :func:`is_private_address`
    for every URL when ``block_private_ips=True`` (default), and DNS
    resolution is otherwise the dominant per-request cost. DNS rarely
    changes within a single agent's lifetime, and even when it does the
    LRU eviction (2048-entry cap) bounds staleness for long-running
    processes.

    Returns an empty tuple on resolution failure -- callers treat that as
    "unknown / cannot prove private" and fall through.

    Note: this is a sync function called from sync code paths
    (:func:`check_domain_allowed`); a future async refactor would
    propagate :class:`asyncio` through every URL gate. See the v1.7
    roadmap for that work.
    """
    try:
        # info[4] is the sockaddr tuple; index 0 is the IP literal
        # (str for both IPv4 and IPv6). Stubs type it as Any, so we
        # narrow with str() to keep mypy strict-mode happy.
        return tuple(str(info[4][0]) for info in socket.getaddrinfo(host, None))
    except (socket.gaierror, OSError):
        return ()


def is_private_address(host: str) -> bool:
    """Return True if ``host`` resolves to a private/loopback/link-local IP.

    Covers RFC1918 (10/8, 172.16/12, 192.168/16), loopback (127/8 and ::1),
    link-local (169.254/16 incl. AWS IMDS at 169.254.169.254, fe80::/10),
    and the unspecified address (0.0.0.0, ::).

    If ``host`` is a hostname (not a literal IP), this function consults
    a per-process DNS cache (see :func:`_resolve_host_addresses`) and
    checks every resolved address. On resolution failure it returns
    False (we don't know -- caller must decide).

    Args:
        host: Hostname or IP literal.

    Returns:
        True if the host resolves to a private/restricted address.
    """
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
        return _is_private_ip(ip)
    except ValueError:
        # Not a literal IP -- try cached DNS resolution
        pass

    for addr_str in _resolve_host_addresses(host):
        try:
            if _is_private_ip(ipaddress.ip_address(addr_str)):
                return True
        except ValueError:
            continue
    return False


def check_domain_allowed(url: str, safety: SafetyConfig, *, strict: bool = False) -> bool:
    """Return True if ``url``'s host is permitted by safety allow/deny lists.

    Rules (in order):
        1. URL must have a parseable host.
        2. If ``safety.block_private_ips`` is True, reject private/loopback/
           link-local hosts (including AWS IMDS at 169.254.169.254).
        3. Deny-list match -> reject.
        4. Empty allow-list -> allow (subject to above).
        5. Allow-list match (suffix semantics, e.g. ``example.com``
           matches ``api.example.com``) -> allow.

    Args:
        url: The URL to check.
        safety: SafetyConfig containing allow/deny patterns.
        strict: If True, raise :class:`DomainNotAllowedError` instead of
            returning False on rejection. Default False (return bool).

    Returns:
        True if allowed, False if blocked.

    Raises:
        DomainNotAllowedError: If ``strict=True`` and the URL is rejected.
    """
    from .exceptions import DomainNotAllowedError

    host = _normalize_host(url)

    def _reject(reason: str) -> bool:
        if strict:
            raise DomainNotAllowedError(f"Domain rejected: {reason}", url=url, host=host)
        return False

    if not host:
        return _reject("no host")

    # Private-IP / SSRF protection (when enabled in config).
    block_private = getattr(safety, "block_private_ips", False)
    if block_private and is_private_address(host):
        return _reject(f"private/loopback/link-local IP: {host}")

    for pattern in safety.denied_domains:
        if _matches_domain(host, pattern):
            return _reject(f"matches deny-list pattern {pattern!r}")

    if not safety.allowed_domains:
        return True

    if any(_matches_domain(host, p) for p in safety.allowed_domains):
        return True
    return _reject("not in allow-list")


# Cross-platform absolute-path detection patterns.
# - POSIX absolute: starts with "/"
# - Windows drive-rooted: "C:\foo" or "C:/foo" (any letter)
# - Windows root-only:    "\foo" or "/foo" (already covered by POSIX rule)
# - UNC path:             "\\server\share" or "//server/share"
# We detect ALL of these regardless of the host OS, because:
#   - pathlib.PurePosixPath("C:\\Windows").is_absolute() returns False on Linux
#     (Linux treats "C:\\Windows" as a relative file named literally "C:\Windows"),
#     so a Linux container would silently accept a Windows-rooted path that the
#     security helper is supposed to reject.
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_PATTERN = re.compile(r"^[\\/]{2}[^\\/]")


def _is_cross_platform_absolute(user_path: str) -> bool:
    """True if ``user_path`` is absolute on ANY major platform.

    Catches POSIX absolute, Windows drive-rooted, Windows root-only, and
    UNC paths regardless of the host OS the check runs on.
    """
    if not user_path:
        return False
    if user_path.startswith(("/", "\\")):
        return True
    if _WINDOWS_DRIVE_PATTERN.match(user_path):
        return True
    if _UNC_PATTERN.match(user_path):
        return True
    # Fallback to pathlib (catches any platform-specific case the regexes miss)
    return Path(user_path).is_absolute()


def safe_join_path(base: Path | str, user_path: str) -> Path:
    """Resolve ``base / user_path`` and ensure it does NOT escape ``base``.

    Defends against path-traversal attacks where ``user_path`` contains
    ``..`` components, absolute paths, or symlinks pointing outside ``base``.

    Cross-platform absolute-path detection: rejects POSIX absolute paths
    (``/etc/passwd``), Windows drive-rooted paths (``C:\\Windows\\System32``),
    and UNC paths (``\\\\server\\share``) regardless of the OS where the
    check runs. This matters because ``pathlib.PurePosixPath`` does NOT
    treat ``C:\\Windows`` as absolute on Linux, so a Linux container would
    otherwise silently accept Windows-rooted user input.

    Args:
        base: Base directory (must be a real directory).
        user_path: Relative path supplied by an external caller.

    Returns:
        The resolved absolute path, guaranteed to be inside ``base``.

    Raises:
        ValueError: If the resolved path escapes ``base``, or if ``user_path``
            is empty or absolute on any major platform.
    """
    if not user_path:
        raise ValueError("Empty filename / path")

    if _is_cross_platform_absolute(user_path):
        raise ValueError(f"Absolute paths are not allowed: {user_path!r}")

    user_p = Path(user_path)

    base_resolved = Path(base).resolve()
    candidate = (base_resolved / user_p).resolve()

    try:
        candidate.relative_to(base_resolved)
    except ValueError:
        raise ValueError(f"Path escapes base directory: {user_path!r} -> {candidate}") from None

    return candidate


# ---------------------------------------------------------------------------
# Budget Tracker
# ---------------------------------------------------------------------------
class BudgetTracker:
    """Track per-call budget for pages, characters, and wall-clock time.

    All limits read from :class:`SafetyConfig`. Calling ``add_page()``,
    ``add_chars(n)``, or ``check_time()`` raises
    :class:`BudgetExceededError` when the corresponding limit is hit.

    Example::

        tracker = BudgetTracker(config.safety)
        for url in urls:
            try:
                tracker.check_time()
                tracker.add_page()
                content = await fetch(url)
                tracker.add_chars(len(content))
            except BudgetExceededError as exc:
                errors.append(str(exc))
                break
    """

    def __init__(self, safety: SafetyConfig) -> None:
        self._safety = safety
        self._pages = 0
        self._chars = 0
        self._start = time.perf_counter()

    def add_page(self) -> None:
        """Increment page counter; raise if max_pages_per_call exceeded."""
        from .exceptions import BudgetExceededError

        self._pages += 1
        if self._pages > self._safety.max_pages_per_call:
            raise BudgetExceededError(
                f"Page budget exhausted ({self._pages}/{self._safety.max_pages_per_call})",
                budget_type="pages",
                limit=float(self._safety.max_pages_per_call),
            )

    def add_chars(self, n: int) -> None:
        """Add ``n`` to char counter; raise if max_chars_per_call exceeded."""
        from .exceptions import BudgetExceededError

        self._chars += max(0, n)
        if self._chars > self._safety.max_chars_per_call:
            raise BudgetExceededError(
                f"Character budget exhausted ({self._chars}/{self._safety.max_chars_per_call})",
                budget_type="chars",
                limit=float(self._safety.max_chars_per_call),
            )

    def check_time(self) -> None:
        """Raise if wall-clock budget exceeded."""
        from .exceptions import BudgetExceededError

        elapsed = time.perf_counter() - self._start
        if elapsed > self._safety.max_time_per_call_seconds:
            raise BudgetExceededError(
                f"Time budget exhausted ({elapsed:.1f}s/{self._safety.max_time_per_call_seconds}s)",
                budget_type="time",
                limit=self._safety.max_time_per_call_seconds,
            )

    @property
    def remaining(self) -> dict[str, float]:
        """Return remaining budget across all dimensions."""
        elapsed = time.perf_counter() - self._start
        return {
            "pages": float(self._safety.max_pages_per_call - self._pages),
            "chars": float(self._safety.max_chars_per_call - self._chars),
            "seconds": max(0.0, self._safety.max_time_per_call_seconds - elapsed),
        }

    @property
    def pages_used(self) -> int:
        return self._pages

    @property
    def chars_used(self) -> int:
        return self._chars
