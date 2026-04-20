"""Retry decorator, user-agent rotation pool, and helpers."""

from __future__ import annotations

import asyncio
import random
import time
from functools import wraps
from typing import Any, Callable, TypeVar

from loguru import logger

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
