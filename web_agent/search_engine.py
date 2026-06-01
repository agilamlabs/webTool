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
        # v1.6.15: report configured-but-unavailable providers ONCE here, at
        # construction, instead of re-logging on every ``search()`` call. The
        # common case is SearXNG sitting in the default chain with no
        # ``search.searxng_base_url`` set -- that previously emitted
        # "Skipping unavailable provider: searxng" on EVERY search (loguru
        # surfaces DEBUG by default), which read as a recurring error rather
        # than the benign "SearXNG isn't configured" that it is. The search
        # loop now skips unavailable providers silently.
        unavailable = [p.name for p in self._providers if not p.is_available]
        if unavailable:
            hint = (
                " (SearXNG needs search.searxng_base_url, e.g. http://localhost:8888)"
                if "searxng" in unavailable
                else ""
            )
            logger.debug(
                "Search providers configured but unavailable, skipped: {u}{hint}",
                u=unavailable,
                hint=hint,
            )

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
        #
        # v1.6.14 C-3: also fold in ``safe_search`` so two differently
        # configured engines sharing a cache backend can't serve each
        # other's (differently-filtered) results. Search results are not
        # per-session/authenticated, so -- unlike the fetch cache -- no
        # session identity is needed in the key here.
        cache_key = f"search:{int(self._config.search.safe_search)}:{query}:{max_r}"
        if self._cache is not None:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                logger.debug("Cache hit for search: {q}", q=query)
                # Mark from_cache so callers know the result was reused.
                # We deliberately preserve the original ``searched_at``
                # so callers doing time-diff math see an honest
                # timestamp; ``from_cache=True`` is the source of truth
                # for staleness, not the timestamp.
                cached["from_cache"] = True
                return SearchResponse(**cached)

        last_response = SearchResponse(query=query)
        for provider in self._providers:
            if not provider.is_available:
                # Unavailable providers are reported once at construction
                # (see __init__); skip silently here so an optional provider
                # that isn't set up (e.g. SearXNG with no base_url) doesn't
                # spam DEBUG on every search.
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
