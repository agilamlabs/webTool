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
    """

    def __init__(
        self,
        user_agent: str = "web-agent-toolkit",
        ttl_seconds: float = 3600.0,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._user_agent = user_agent
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        # host -> (fetched_at, parser_or_None)
        self._cache: dict[str, tuple[float, RobotFileParser | None]] = {}
        self._lock = asyncio.Lock()

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
        async with self._lock:
            cached_at, rp = self._cache.get(host, (0.0, None))
            if (time.monotonic() - cached_at) > self._ttl:
                rp = await self._fetch_and_parse(scheme, host)
                self._cache[host] = (time.monotonic(), rp)

        if rp is None:
            # robots.txt missing / fetch failed -> default allow
            return True
        return rp.can_fetch(self._user_agent, url)

    async def _fetch_and_parse(self, scheme: str, host: str) -> RobotFileParser | None:
        """Fetch and parse robots.txt for the given host.

        Returns ``None`` on any failure (network error, non-200 status,
        timeout, parse error). Callers treat ``None`` as allow-all.
        """
        url = f"{scheme}://{host}/robots.txt"
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
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
