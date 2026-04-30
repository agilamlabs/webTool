"""Multi-provider search orchestrator.

Builds a chain of :class:`SearchProvider` instances from
:class:`SearchConfig.providers` and executes them in order, falling
through to the next on empty results or transient errors. The first
non-empty response wins.

Default chain (configurable):
    ``["searxng", "ddgs", "playwright"]``

- **SearXNG** (privacy-respecting metasearch, self-hosted) -- skipped
  silently when ``searxng_base_url`` is not set.
- **DDGS** (DuckDuckGo via the ``ddgs`` package) -- skipped silently
  when the optional dependency is missing.
- **Playwright** (browser-driven Google then DDG HTML scraping) --
  always available; the slow but reliable fallback.

To opt out of one or more providers, pass a custom ``providers`` list,
e.g. ``providers=["playwright"]`` to use only browser-based search.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from .browser_manager import BrowserManager
from .cache import Cache
from .config import AppConfig
from .models import SearchResponse
from .rate_limiter import RateLimiter
from .search_providers import (
    DDGSProvider,
    PlaywrightProvider,
    SearchProvider,
    SearXNGProvider,
)


class SearchEngine:
    """Chain orchestrator over a list of :class:`SearchProvider` instances.

    The constructor builds the full provider catalog from configuration
    and selects the subset listed in :attr:`SearchConfig.providers`,
    in priority order. :meth:`search` walks the chain until a provider
    returns at least one result, or all are exhausted.

    Args:
        browser_manager: Shared browser lifecycle manager (used by
            ``PlaywrightProvider``).
        config: Application configuration. ``config.search.providers``
            controls which providers run and in what order.
        rate_limiter: Optional per-host rate gate, applied uniformly
            inside every provider that performs network I/O.
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        config: AppConfig,
        rate_limiter: Optional[RateLimiter] = None,
        cache: Optional[Cache] = None,
    ) -> None:
        self._config = config
        self._cache = cache

        # Build the full catalog. Only providers listed in
        # config.search.providers (in that order) actually run.
        catalog: dict[str, SearchProvider] = {
            "searxng": SearXNGProvider(
                base_url=config.search.searxng_base_url,
                timeout=config.search.searxng_timeout,
                rate_limiter=rate_limiter,
            ),
            "ddgs": DDGSProvider(rate_limiter=rate_limiter),
            "playwright": PlaywrightProvider(browser_manager, config, rate_limiter=rate_limiter),
        }
        self._providers: list[SearchProvider] = [
            catalog[name] for name in config.search.providers if name in catalog
        ]

    @property
    def providers(self) -> list[SearchProvider]:
        """Read-only snapshot of the configured provider chain (in order)."""
        return list(self._providers)

    async def search(
        self,
        query: str,
        max_results: int | None = None,
        *,
        strict: bool = False,
    ) -> SearchResponse:
        """Walk the provider chain until one returns results.

        Args:
            query: Search query string.
            max_results: Maximum results per provider. ``None`` reads
                from ``config.search.max_results``.
            strict: If True and ALL providers return empty / fail,
                raise :class:`SearchError`. Default False (return empty
                ``SearchResponse``).

        Raises:
            SearchError: Only when ``strict=True`` and the entire chain
                exhausted without producing any results.
        """
        max_r = max_results or self._config.search.max_results

        # Cache lookup -- key includes max_results so different result
        # counts for the same query don't collide.
        cache_key = f"search:{query}:{max_r}"
        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                logger.debug("Cache hit for search: {q}", q=query)
                cached["from_cache"] = True
                # Reset searched_at to "now" so callers can't be misled
                # into thinking the data is fresh when it's actually
                # an hours-old cache hit. Pair this with from_cache=True
                # for full transparency.
                cached["searched_at"] = datetime.now(timezone.utc).isoformat()
                return SearchResponse(**cached)

        last_response = SearchResponse(query=query)
        for provider in self._providers:
            if not provider.is_available:
                logger.debug("Skipping unavailable provider: {p}", p=provider.name)
                continue
            try:
                response = await provider.search(query, max_r)
            except Exception as exc:
                logger.warning("Provider {p} raised: {e}", p=provider.name, e=exc)
                continue

            if response.results:
                logger.info(
                    "Search succeeded via {p} ({n} results)",
                    p=provider.name,
                    n=response.total_results,
                )
                # Cache non-empty responses so repeat searches skip the
                # entire chain. Empty responses are NOT cached -- a real
                # "no results" lock-in is more annoying than re-querying.
                if self._cache is not None:
                    await self._cache.set(cache_key, response.model_dump(mode="json"))
                return response
            last_response = response

        if strict:
            from .exceptions import SearchError

            attempted = [p.name for p in self._providers if p.is_available]
            raise SearchError(
                f"All search providers exhausted ({attempted}) returned no "
                f"results for {query!r}. Possible causes: "
                "missing searxng_base_url, ddgs package not installed, "
                "search engines blocking the request (CAPTCHA / rate-limit), "
                "or no network reachability."
            )
        return last_response
