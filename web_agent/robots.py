"""robots.txt fetching, parsing, and TTL caching.

Uses Python's stdlib :class:`urllib.robotparser.RobotFileParser` for
parsing. ``robots.txt`` is fetched on first encounter for each host,
parsed, and cached for ``ttl_seconds``. Subsequent calls reuse the
parsed result without network I/O.

A missing or unreachable ``robots.txt`` is treated as **allow-all** --
this matches the convention of most well-behaved crawlers and avoids
locking the agent out of correctly-configured but firewalled hosts.

Example::

    rc = RobotsChecker(user_agent="my-bot")
    if await rc.is_allowed("https://example.com/page"):
        # proceed
        ...
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from loguru import logger

from .utils import is_private_address

# v1.6.16 ROBOTS-1: bound the per-host cache / lock dicts. RobotsChecker
# lives for the whole process (e.g. the MCP server holds one Agent), so a
# run that touches many distinct hosts otherwise accumulates a
# (float, RobotFileParser) tuple plus an asyncio.Lock per host forever.
# Mirrors the bounded DNS cache in utils (_DNS_CACHE_MAXSIZE). FIFO
# eviction of the oldest-inserted host keeps it minimal and correct.
_ROBOTS_CACHE_MAXSIZE: int = 2048


class RobotsChecker:
    """Per-host robots.txt cache.

    Args:
        user_agent: User-Agent string sent when fetching robots.txt and
            checked against rule groups via ``can_fetch``.
        ttl_seconds: How long a parsed robots.txt is reused before
            re-fetching. Default 1 hour.
        timeout_seconds: Per-request timeout for fetching robots.txt.
            Default 5 seconds; we want this short so a slow robots.txt
            doesn't block the actual fetch.
        block_private_ips: When True (default), skip fetching robots.txt
            for a host that resolves to a private/loopback/link-local
            address (treating it as allow-all, returning ``None``).
            Defense-in-depth against blind SSRF via DNS rebinding -- the
            robots fetch uses its own httpx client that re-resolves the
            host independently of the caller's SSRF gate (ROBOTS-2).
    """

    def __init__(
        self,
        user_agent: str = "web-agent-toolkit",
        ttl_seconds: float = 3600.0,
        timeout_seconds: float = 5.0,
        block_private_ips: bool = True,
    ) -> None:
        self._user_agent = user_agent
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._block_private_ips = block_private_ips
        # host -> (fetched_at, parser_or_None)
        self._cache: dict[str, tuple[float, RobotFileParser | None]] = {}
        # v1.6.14 C-10: per-host locks instead of one global lock. A
        # single shared lock serialised the robots.txt fetch for EVERY
        # host -- a slow robots.txt on host A blocked checks for unrelated
        # hosts B, C, ... Per-host locks preserve the "fetch each host's
        # robots.txt at most once" guarantee while letting different hosts
        # proceed concurrently. Locks are created lazily on first use.
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def user_agent(self) -> str:
        return self._user_agent

    async def is_allowed(self, url: str) -> bool:
        """Return True if ``url`` may be fetched per its host's robots.txt.

        Returns ``True`` (allow) if:
        - URL has no host (e.g. ``file://``)
        - robots.txt is missing or unreachable
        - robots.txt explicitly permits the path

        Returns ``False`` only if a successfully-fetched robots.txt
        explicitly disallows the path for our user-agent.
        """
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            return True

        scheme = parsed.scheme or "https"
        # v1.6.16 ROBOTS-1: evict before inserting a new host so the
        # per-host dicts stay bounded over a long-lived process.
        if host not in self._locks:
            self._evict_if_needed(host)
        # v1.6.14 C-10: lock per-host so a slow robots.txt fetch on one
        # host doesn't serialise checks for every other host. ``setdefault``
        # is safe without an outer lock because lock creation is sync
        # (no ``await`` between the get-or-create), so two coroutines for
        # the same host still converge on the same Lock object.
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            cached = self._cache.get(host)
            # v1.6.16 ROBOTS-3: treat "not cached" as an explicit miss.
            # The previous ``(0.0, None)`` sentinel combined with the
            # ``(monotonic() - cached_at) > ttl`` staleness test never fired
            # on the first lookup while ``time.monotonic()`` (seconds since
            # boot) was still below ``ttl``: for the first hour of machine
            # uptime robots.txt was NEVER fetched and every path was silently
            # treated as allow-all. Check membership explicitly so the first
            # lookup for a host always fetches, independent of uptime.
            if cached is None or (time.monotonic() - cached[0]) > self._ttl:
                rp = await self._fetch_and_parse(scheme, host)
                self._cache[host] = (time.monotonic(), rp)
            else:
                rp = cached[1]

        if rp is None:
            # robots.txt missing / fetch failed -> default allow
            return True
        return rp.can_fetch(self._user_agent, url)

    def _evict_if_needed(self, incoming_host: str) -> None:
        """Bound the cache / lock dicts via FIFO eviction (ROBOTS-1).

        When the dicts are at capacity, drop the oldest-inserted host
        (dict insertion order) -- but never the host we're about to use,
        and never a host whose lock is currently held (its fetch is in
        flight). The lock is evicted together with its cache entry so the
        two maps can't drift. A few hundred bytes per host means this only
        ever triggers in an extremely long-lived, high-host-cardinality
        process; it is a slow-leak guard, not a hot path.
        """
        while len(self._locks) >= _ROBOTS_CACHE_MAXSIZE:
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
                # fetch -- nothing safe to evict right now. Bail rather
                # than spin; the next call will retry.
                break
            self._locks.pop(victim, None)
            self._cache.pop(victim, None)

    async def _fetch_and_parse(self, scheme: str, host: str) -> RobotFileParser | None:
        """Fetch and parse robots.txt for the given host.

        Returns ``None`` on any failure (network error, non-200 status,
        timeout, parse error). Callers treat ``None`` as allow-all.
        """
        # v1.6.16 ROBOTS-2: defense-in-depth SSRF guard. The robots fetch
        # uses its own httpx client that re-resolves the host independently
        # of the caller's SSRF gate (check_domain_allowed), so a host that
        # resolved public when the caller gated it can rebind to an internal
        # address before this fetch -- a blind-SSRF lever. When private-IP
        # blocking is on, skip the fetch for a private/loopback/link-local
        # host and treat it as allow-all (return None), matching the
        # module's existing fail-open-to-allow policy without changing the
        # public return contract.
        if self._block_private_ips and is_private_address(host):
            logger.debug(
                "robots.txt fetch skipped for private/internal host {h}; "
                "treating as allow-all (SSRF defense-in-depth)",
                h=host,
            )
            return None

        url = f"{scheme}://{host}/robots.txt"
        try:
            # v1.6.14 C-5: do NOT follow redirects when fetching robots.txt.
            # robots.txt is a fixed well-known path; a cross-host 3xx is a
            # SSRF lever (a redirect to an internal host would otherwise be
            # fetched). Any non-200 -- including a 3xx -- is treated as "no
            # robots / allow-all" per the existing semantics below.
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                resp = await client.get(url, headers={"User-Agent": self._user_agent})
            if resp.status_code != 200:
                logger.debug(
                    "robots.txt for {h}: HTTP {c}, treating as allow-all",
                    h=host,
                    c=resp.status_code,
                )
                return None
            rp = RobotFileParser()
            rp.parse(resp.text.splitlines())
            return rp
        except Exception as exc:
            logger.debug(
                "robots.txt fetch failed for {h}: {e}, treating as allow-all",
                h=host,
                e=exc,
            )
            return None
