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
        # v1.6.12: tally of 429 events seen per host. Internal-only for
        # now; hooks for a future adaptive-rps policy. No getter exposed
        # until that policy ships.
        self._429_counts: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def acquire(self, host: str) -> None:
        """Block until a token is available for ``host``. No-op if disabled.

        v1.6.12: re-reads ``_next_allowed`` after each sleep so a
        :meth:`notify_429` call that extends the deadline DURING the
        sleep is honoured. Pre-v1.6.12, ``acquire`` snapshotted
        ``next_ok`` before the sleep and wrote ``max(now, next_ok) +
        interval`` afterwards -- losing any concurrent extension.
        """
        if not self._enabled or not host:
            return
        host = host.lower()
        async with self._locks[host]:
            # Loop because ``notify_429`` may extend ``_next_allowed``
            # while we're sleeping; re-read each iteration until the
            # current value is in the past.
            while True:
                now = time.monotonic()
                next_ok = self._next_allowed.get(host, 0.0)
                if now >= next_ok:
                    break
                await asyncio.sleep(next_ok - now)
            # Schedule the next request slot from when we actually
            # got through the gate.
            self._next_allowed[host] = now + self._interval

    def notify_429(
        self,
        host: str,
        retry_after_seconds: float | None = None,
        *,
        fallback_factor: float = 2.0,
    ) -> None:
        """v1.6.12: signal that *host* returned HTTP 429.

        Extends the host's next-allowed time to
        ``now + max(retry_after_seconds, interval * fallback_factor)`` --
        so the next :meth:`acquire` call waits long enough.

        When ``retry_after_seconds`` is ``None`` (server omitted the
        ``Retry-After`` header), the fallback is ``interval *
        fallback_factor`` -- doubles the per-host interval by default so
        callers still back off.

        Stacks naturally with the retry decorator: a 429 retry incurs
        the decorator's exponential-jitter sleep PLUS this extension via
        the next ``acquire(host)`` call. The 429 incident is tallied
        internally in ``self._429_counts`` for future adaptive-rps work.

        No-op when the limiter is disabled (``rps_per_host <= 0``) or
        the host string is empty.

        Args:
            host: Hostname (case-insensitive).
            retry_after_seconds: Parsed ``Retry-After`` value in seconds,
                or ``None`` if the header was absent or unparseable.
                Negative values are clamped to ``0`` by the caller via
                :func:`web_agent.utils.parse_retry_after`.
            fallback_factor: Multiplier on ``self._interval`` used when
                ``retry_after_seconds`` is ``None``. Default ``2.0``.
        """
        if not self._enabled or not host:
            return
        host = host.lower()
        # Fallback = interval * factor (at least one full interval delay
        # when the server doesn't tell us). max() with retry_after means
        # an explicit short Retry-After can't shorten our own minimum
        # backoff; we always wait at least the fallback.
        delay = max(retry_after_seconds or 0.0, self._interval * fallback_factor)
        now = time.monotonic()
        current = self._next_allowed.get(host, 0.0)
        # Take the later of current next_allowed and (now + delay).
        # Prevents a 429-after-a-success from shortening an in-flight
        # interval gate.
        self._next_allowed[host] = max(current, now + delay)
        self._429_counts[host] = self._429_counts.get(host, 0) + 1
