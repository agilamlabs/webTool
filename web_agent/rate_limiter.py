"""Per-host async rate limiter.

Implements a simple per-host minimum-interval gate so a single host
never receives more than ``rps_per_host`` requests per second from a
single :class:`Agent`. Critical for being a good citizen of the web --
without this, parallel fetches against the same host can trip server-
side rate limits and get the IP / User-Agent banned.

Per-host locks mean different hosts proceed concurrently, only the
same-host stream is serialized at the configured rate.

Example::

    limiter = RateLimiter(rps_per_host=2.0)
    await limiter.acquire("api.example.com")
    # ... do request ...
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class RateLimiter:
    """Async per-host rate gate using minimum-interval scheduling.

    Args:
        rps_per_host: Requests-per-second budget for each host.
            ``0`` or negative disables the limiter entirely.

    Notes:
        Uses :func:`time.monotonic` so it is unaffected by wall-clock
        adjustments. State is in-memory and not shared across processes.
    """

    def __init__(self, rps_per_host: float = 2.0) -> None:
        self._enabled = rps_per_host > 0
        self._interval = 1.0 / rps_per_host if self._enabled else 0.0
        self._next_allowed: dict[str, float] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def acquire(self, host: str) -> None:
        """Block until a token is available for ``host``. No-op if disabled."""
        if not self._enabled or not host:
            return
        host = host.lower()
        async with self._locks[host]:
            now = time.monotonic()
            next_ok = self._next_allowed.get(host, 0.0)
            if now < next_ok:
                await asyncio.sleep(next_ok - now)
                now = time.monotonic()
            self._next_allowed[host] = max(now, next_ok) + self._interval
