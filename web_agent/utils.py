"""Retry decorator, retry policies, user-agent rotation, domain checks, budget tracking, and helpers."""

from __future__ import annotations

import asyncio
import random
import time
from enum import Enum
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, TypeVar
from urllib.parse import urlparse

from loguru import logger

# Import to ensure loguru patcher is installed when utils is imported
from . import correlation as _correlation  # noqa: F401

if TYPE_CHECKING:
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
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) "
    "Gecko/20100101 Firefox/132.0",
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
    """Decorator that retries an async function with exponential backoff + jitter."""

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


class RetryableHTTPError(Exception):
    """HTTP error that may succeed on retry (e.g. 500, 502, 503)."""

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(f"HTTP {status_code} for {url}")


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
            f"Unknown retry policy: {name!r}. Choose from: "
            f"{[p.value for p in RetryPolicy]}"
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


def check_domain_allowed(url: str, safety: SafetyConfig) -> bool:
    """Return True if ``url``'s host is permitted by safety allow/deny lists.

    Rules:
        - Empty allow-list => all hosts allowed (subject to deny-list).
        - Deny-list takes precedence over allow-list.
        - Pattern matching uses suffix semantics
          (``example.com`` matches ``api.example.com``).

    Args:
        url: The URL to check.
        safety: SafetyConfig containing allow/deny patterns.

    Returns:
        True if allowed, False if blocked.
    """
    host = _normalize_host(url)
    if not host:
        return False

    for pattern in safety.denied_domains:
        if _matches_domain(host, pattern):
            return False

    if not safety.allowed_domains:
        return True

    return any(_matches_domain(host, p) for p in safety.allowed_domains)


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
