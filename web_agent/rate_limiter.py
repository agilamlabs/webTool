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

# v1.6.16 RL-1: bound the per-host state dicts. RateLimiter is constructed
# once per Agent and lives for the whole process (e.g. the MCP server holds
# one Agent and fetches arbitrary caller/LLM-chosen hosts), so otherwise
# ``_next_allowed`` / ``_locks`` / ``_429_counts`` accumulate one permanent
# entry per distinct host forever. Mirrors the bounded DNS cache in utils
# (_DNS_CACHE_MAXSIZE) and ``RobotsChecker._evict_if_needed``: FIFO eviction
# of the oldest-inserted host, never evicting a host whose lock is currently
# held. A few hundred bytes per host means this only triggers in an
# extremely long-lived, high-host-cardinality process; it is a slow-leak
# guard, not a hot path.
_RATE_LIMITER_MAXSIZE: int = 2048


class RateLimiter:
    """Async per-host rate gate using minimum-interval scheduling.

    Args:
        rps_per_host: Requests-per-second budget for each host.
            ``0`` or negative disables the limiter entirely.

    Notes:
        Uses :func:`time.monotonic` so it is unaffected by wall-clock
        adjustments. State is in-memory and not shared across processes.
    """

    # v1.6.14 C-1: maximum sleep induced by a single ``Retry-After`` value.
    # A misbehaving (or hostile) server can return ``Retry-After: 99999999``
    # which, parsed verbatim by :func:`parse_retry_after`, would block the
    # next ``acquire(host)`` for ~1157 days. We cap the per-event extension
    # at 5 minutes so a single bad header can hurt throughput but cannot
    # wedge the agent for hours/days. 5 minutes balances "long enough to
    # genuinely back off a rate-limited host" against "short enough that
    # the agent recovers on the next retry cycle" -- much longer and the
    # async_retry wrapper above this would itself give up first.
    MAX_RETRY_AFTER_SECONDS: float = 300.0

    def __init__(self, rps_per_host: float = 2.0) -> None:
        self._enabled = rps_per_host > 0
        self._interval = 1.0 / rps_per_host if self._enabled else 0.0
        self._next_allowed: dict[str, float] = {}
        # v1.6.16 RL-1: a plain dict with explicit get-or-create (was a
        # ``defaultdict``) so we can FIFO-evict before inserting a brand-new
        # host and keep the per-host maps bounded.
        self._locks: dict[str, asyncio.Lock] = {}
        # v1.6.12: tally of 429 events seen per host. Internal-only for
        # now; hooks for a future adaptive-rps policy. No getter exposed
        # until that policy ships.
        self._429_counts: dict[str, int] = {}

    def _evict_if_needed(self, incoming_host: str) -> None:
        """Bound the per-host state dicts via FIFO eviction (RL-1).

        When ``_locks`` is at capacity, drop the oldest-inserted host
        (dict insertion order) -- but never the host we're about to use,
        and never a host whose lock is currently held (a request is in
        flight against it). The host's ``_next_allowed`` and ``_429_counts``
        entries are evicted together so the three maps can't drift. Mirrors
        ``RobotsChecker._evict_if_needed``.
        """
        while len(self._locks) >= _RATE_LIMITER_MAXSIZE:
            victim: str | None = None
            for candidate in self._locks:
                if candidate == incoming_host:
                    continue
                lk = self._locks.get(candidate)
                if lk is not None and lk.locked():
                    continue
                victim = candidate
                break
            if victim is None:
                # Every entry is the incoming host or has an in-flight
                # acquire -- nothing safe to evict right now. Bail rather
                # than spin; the next call will retry.
                break
            self._locks.pop(victim, None)
            self._next_allowed.pop(victim, None)
            self._429_counts.pop(victim, None)

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
        # v1.6.16 RL-1: evict before inserting a brand-new host so the
        # per-host dicts stay bounded over a long-lived process. ``setdefault``
        # is safe without an outer lock because lock creation is sync (no
        # ``await`` between get-or-create), so two coroutines for the same
        # host still converge on the same Lock object (mirrors RobotsChecker).
        if host not in self._locks:
            self._evict_if_needed(host)
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
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

        v1.6.14 C-1: the computed delay is clamped to
        :attr:`MAX_RETRY_AFTER_SECONDS` (5 minutes) to prevent a hostile
        or misconfigured server from wedging the agent for days via a
        ``Retry-After: 99999999`` header. Without the cap, a single
        extreme value would block subsequent :meth:`acquire` calls for
        the cap value's wall-clock duration -- a denial-of-service from
        any HTTP endpoint that returns 429s.

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
        # v1.6.16 RL-1: keep the per-host maps bounded here too. In normal
        # flow ``acquire(host)`` ran first and already created the lock
        # entry, but guard the brand-new-host case so a 429 for a host that
        # somehow skipped acquire() can't grow ``_next_allowed`` /
        # ``_429_counts`` past the bound.
        if host not in self._locks:
            self._evict_if_needed(host)
            self._locks.setdefault(host, asyncio.Lock())
        # Fallback = interval * factor (at least one full interval delay
        # when the server doesn't tell us). max() with retry_after means
        # an explicit short Retry-After can't shorten our own minimum
        # backoff; we always wait at least the fallback.
        delay = max(retry_after_seconds or 0.0, self._interval * fallback_factor)
        # v1.6.14 C-1: cap the delay so a hostile/misbehaving server
        # returning an absurd Retry-After (e.g. 99999999s ~= 1157 days)
        # cannot wedge the agent for hours/days. See class constant
        # docstring for the rationale on the 5-minute ceiling.
        delay = min(delay, self.MAX_RETRY_AFTER_SECONDS)
        now = time.monotonic()
        current = self._next_allowed.get(host, 0.0)
        # Take the later of current next_allowed and (now + delay).
        # Prevents a 429-after-a-success from shortening an in-flight
        # interval gate.
        self._next_allowed[host] = max(current, now + delay)
        self._429_counts[host] = self._429_counts.get(host, 0) + 1
