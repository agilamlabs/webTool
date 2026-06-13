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

**v1.7.0 (Wave 2E) resilience.** Search is the agent's entry point to
the web and structurally fragile (DDGS gets CAPTCHA'd above ~1 req/s,
Google SERP scraping rots). Two hardening features live here:

- **Provider health memory / circuit breaker** -- a provider that just
  got blocked or raised is skipped for a short, bounded cooldown
  (:class:`_ProviderCircuitBreaker`) and then probed again, instead of
  being hammered on every call. In-memory, per-engine, monotonic-clock
  based with the clock injectable for tests.
- **Blocked-vs-empty distinction** -- :meth:`search_with_outcome`
  returns a :class:`SearchOutcome` that tells the caller *why* a search
  came back empty: all providers blocked (CAPTCHA / rate-limit / circuit
  open) vs. providers answered with genuinely zero hits. :meth:`search`
  preserves the legacy ``SearchResponse`` return shape.

**Links-only.** :meth:`search` (and :meth:`search_with_outcome`) are
LINKS-ONLY: they return SERP items (title / url / snippet) from the
providers and perform NO page fetch or extraction. The expensive
fetch+extract step lives in :class:`~web_agent.agent.Agent` /
:class:`~web_agent.recipes.Recipes`, never here -- so wiring a cheap
search-only entry point is just exposing this engine.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from loguru import logger

from .browser_manager import BrowserManager
from .cache import Cache
from .config import AppConfig
from .models import SearchResponse
from .rate_limiter import RateLimiter
from .search_providers import (
    DDGSProvider,
    PlaywrightProvider,
    ProviderBlockedError,
    SearchProvider,
    SearXNGProvider,
)

# Default circuit-breaker cooldown (seconds). A provider that blocks or
# raises is skipped for this window, then probed again. Deliberately short
# and bounded so a transient block does not knock a provider out for long.
# Not a config.py knob (SearchConfig is frozen this wave); override via the
# ``circuit_cooldown_s`` constructor arg.
DEFAULT_CIRCUIT_COOLDOWN_S = 60.0


def _result_host_is_private(url: str) -> bool:
    """True if ``url``'s host is a LITERAL private/loopback/link-local IP.

    v1.6.16 SP-1: a literal internal IP is never a legitimate public search
    hit; a malicious/compromised provider could return one to lure the agent
    into fetching an internal address. Applied uniformly to EVERY provider's
    results at the choke point (SearXNG already filtered inline since v1.6.14
    C-2; DDGS/Playwright did not). Literal-only -- no DNS in the search hot
    path; hostname results that resolve internally are still caught by the
    downstream fetch gate.
    """
    host = urlparse(url).hostname or ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


@dataclass
class SearchOutcome:
    """Structured result of a chain walk -- the blocked-vs-empty signal.

    v1.7.0 (Wave 2E): :class:`SearchResponse` (models.py) cannot tell a
    caller *why* a search returned nothing. This dataclass carries that
    context so the integrator (``Agent.search``) can surface it. The
    ``response`` is always a valid :class:`SearchResponse` (possibly empty);
    the remaining fields explain it.

    Attributes:
        response: The winning provider's :class:`SearchResponse`, or an
            empty one when the chain produced no hits.
        blocked: True when the chain produced zero results AND at least one
            provider was actively blocked (CAPTCHA / rate-limit / anomaly)
            or skipped because its circuit was open. Distinguishes "every
            provider was refused" from "providers answered, zero hits".
            Always False when ``response.results`` is non-empty.
        providers_tried: Names of providers actually invoked this call (in
            order). Excludes providers skipped because they were
            unavailable or had an open circuit.
        providers_skipped_cooldown: Names of providers skipped because their
            circuit breaker was open (recently blocked / failed).
        provider_errors: ``{provider_name: reason}`` for every provider that
            blocked or raised this call. ``reason`` is a short string
            (e.g. ``"ratelimit"``, ``"captcha"``, the exception text).
    """

    response: SearchResponse
    blocked: bool = False
    providers_tried: list[str] = field(default_factory=list)
    providers_skipped_cooldown: list[str] = field(default_factory=list)
    provider_errors: dict[str, str] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        """True when the empty result is due to provider trouble, not zero hits.

        Broader than :attr:`blocked`: True whenever the chain produced no
        results AND at least one provider was blocked, in cooldown, OR raised
        a generic error. ``blocked`` is the narrower "actively refused
        (CAPTCHA / rate-limit / cooldown)" signal; a lone transient provider
        error is ``degraded`` but not ``blocked``. Use this to decide whether
        an empty answer is trustworthy ("genuinely nothing out there") or
        should be retried / surfaced as a soft failure.
        """
        if self.response.results:
            return False
        return self.blocked or bool(self.provider_errors) or bool(self.providers_skipped_cooldown)


class _ProviderCircuitBreaker:
    """Per-provider failure memory with a bounded cooldown.

    Mirrors ``SessionManager``'s injectable monotonic-clock pattern so tests
    drive cooldown expiry with a fake clock (no sleeps). A provider that
    blocks or raises is "tripped": :meth:`is_open` returns True for it until
    ``cooldown`` seconds of monotonic time elapse, after which the provider
    is probed again (half-open -- the next call is allowed through, and a
    success clears the trip while another failure re-arms it).

    Circuits are keyed by provider *instance identity* (``id(provider)``),
    not by ``name``: the engine's catalog gives every live provider a unique
    name, but keying by identity is the correct, drift-proof choice (a
    provider's health is about that object, and two distinct objects -- even
    if they happened to share a name -- have independent circuits). The
    ``name`` is used only for human-readable log lines.

    State is in-memory and per-:class:`SearchEngine` instance.
    """

    def __init__(
        self,
        cooldown: float = DEFAULT_CIRCUIT_COOLDOWN_S,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._cooldown = max(0.0, float(cooldown))
        self._clock: Callable[[], float] = clock or time.monotonic
        # provider identity -> monotonic timestamp at which it was tripped.
        self._tripped_at: dict[int, float] = {}

    def is_open(self, provider: SearchProvider) -> bool:
        """True if ``provider``'s circuit is open (it should be skipped).

        Once ``cooldown`` seconds elapse the circuit moves to half-open and
        this returns False (a re-probe is allowed). The trip marker is left
        in place so a fresh failure re-trips cleanly; :meth:`record_success`
        clears it once the provider proves healthy again.
        """
        tripped = self._tripped_at.get(id(provider))
        if tripped is None or self._cooldown <= 0:
            return False
        return self._clock() - tripped < self._cooldown

    def trip(self, provider: SearchProvider, reason: str = "blocked") -> None:
        """Record a failure for ``provider`` and (re)start its cooldown."""
        key = id(provider)
        was_open = key in self._tripped_at
        self._tripped_at[key] = self._clock()
        if not was_open:
            logger.info(
                "Search provider {p} circuit tripped ({reason}); cooling down {c:.0f}s",
                p=provider.name,
                reason=reason,
                c=self._cooldown,
            )
        else:
            logger.debug(
                "Search provider {p} re-tripped ({reason})", p=provider.name, reason=reason
            )

    def record_success(self, provider: SearchProvider) -> None:
        """Clear ``provider``'s trip marker after a healthy call."""
        if self._tripped_at.pop(id(provider), None) is not None:
            logger.info("Search provider {p} circuit recovered", p=provider.name)


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
        cache: Optional result cache. Non-empty responses are cached so a
            repeat query skips the whole chain.
        circuit_cooldown_s: Seconds a provider stays "tripped" (skipped)
            after it blocks or raises, before being probed again. Defaults
            to :data:`DEFAULT_CIRCUIT_COOLDOWN_S`. ``0`` disables the
            breaker. Not read from config (SearchConfig is frozen this
            wave); the integrator may add a SearchConfig field later and
            thread it here.
        clock: Injectable monotonic clock for the circuit breaker. Tests
            pass a fake clock to exercise cooldown expiry without sleeping;
            production uses ``time.monotonic``.
    """

    def __init__(
        self,
        browser_manager: BrowserManager,
        config: AppConfig,
        rate_limiter: Optional[RateLimiter] = None,
        cache: Optional[Cache] = None,
        *,
        circuit_cooldown_s: float = DEFAULT_CIRCUIT_COOLDOWN_S,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._config = config
        self._cache = cache
        self._breaker = _ProviderCircuitBreaker(circuit_cooldown_s, clock=clock)

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
        """Walk the provider chain until one returns results (LINKS-ONLY).

        Returns SERP items only -- title / url / snippet -- and performs NO
        page fetch or extraction. This is the cheap search-only path; the
        expensive fetch+extract step lives in ``Agent`` / ``Recipes``.

        Args:
            query: Search query string.
            max_results: Maximum results per provider. ``None`` reads
                from ``config.search.max_results``.
            strict: If True and ALL providers return empty / fail / are
                blocked, raise :class:`SearchError`. Default False (return
                empty ``SearchResponse``).

        Raises:
            SearchError: Only when ``strict=True`` and the entire chain
                exhausted without producing any results.
        """
        outcome = await self.search_with_outcome(query, max_results, strict=strict)
        return outcome.response

    async def search_with_outcome(
        self,
        query: str,
        max_results: int | None = None,
        *,
        strict: bool = False,
    ) -> SearchOutcome:
        """Like :meth:`search`, but returns the blocked-vs-empty context.

        The returned :class:`SearchOutcome` carries the winning
        ``SearchResponse`` plus *why* it is shaped that way: which providers
        were tried, which were skipped for cooldown, which blocked/errored,
        and a single ``blocked`` flag that is True only when zero results
        came back AND at least one provider was actively refused. The
        integrator (``Agent.search``) maps this onto the public result.

        LINKS-ONLY: no page is fetched or extracted (see :meth:`search`).
        """
        max_r = max_results or self._config.search.max_results
        # v1.6.16 SP-2: clamp at the choke point so an unbounded max_results
        # can't flow into Google's ``num=`` or a provider slice. The MCP layer
        # clamps too, but a direct API caller bypasses that.
        max_r = max(1, min(int(max_r), 100))

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
                # v1.6.16 SE-1: copy before mutating -- an in-memory cache
                # backend may hand back a reference to its stored dict, so
                # mutating ``cached`` in place would corrupt the cached entry.
                response = SearchResponse(**{**cached, "from_cache": True})
                return SearchOutcome(response=response, providers_tried=[])

        last_response = SearchResponse(query=query)
        tried: list[str] = []
        skipped_cooldown: list[str] = []
        errors: dict[str, str] = {}
        any_blocked = False

        for provider in self._providers:
            if not provider.is_available:
                # Unavailable providers are reported once at construction
                # (see __init__); skip silently here so an optional provider
                # that isn't set up (e.g. SearXNG with no base_url) doesn't
                # spam DEBUG on every search.
                continue

            # v1.7.0 (Wave 2E): provider health memory. A provider that
            # recently blocked / raised is in cooldown -- skip it silently
            # (don't spam logs every call) and remember it for the
            # blocked-vs-empty signal. It is re-probed once the cooldown
            # elapses (is_open flips to False).
            if self._breaker.is_open(provider):
                skipped_cooldown.append(provider.name)
                any_blocked = True
                continue

            tried.append(provider.name)
            try:
                response = await provider.search(query, max_r)
            except ProviderBlockedError as exc:
                # Active block (CAPTCHA / rate-limit / anomaly). Trip the
                # breaker and record it so the caller can tell blocked from
                # empty. Then fall through to the next provider.
                logger.warning("Provider {p} blocked: {r}", p=provider.name, r=exc.reason)
                self._breaker.trip(provider, exc.reason)
                errors[provider.name] = exc.reason
                any_blocked = True
                continue
            except Exception as exc:
                # Transient/unknown failure. Treat as a circuit-tripping
                # signal too (a provider throwing arbitrary errors is unhealthy
                # for the cooldown window), but it does NOT by itself mean
                # "blocked" for the caller-facing flag unless nothing succeeds.
                logger.warning("Provider {p} raised: {e}", p=provider.name, e=exc)
                self._breaker.trip(provider, "error")
                errors[provider.name] = str(exc) or exc.__class__.__name__
                continue

            # v1.6.16 SP-1: drop any result whose host is a literal private IP,
            # for EVERY provider (defense-in-depth at the choke point). An
            # all-private result set then falls through as "empty".
            if response.results:
                filtered = [r for r in response.results if not _result_host_is_private(r.url)]
                if len(filtered) != len(response.results):
                    response.results = filtered
                    response.total_results = len(filtered)

            if response.results:
                logger.info(
                    "Search succeeded via {p} ({n} results)",
                    p=provider.name,
                    n=response.total_results,
                )
                # Provider answered cleanly -- clear any prior trip.
                self._breaker.record_success(provider)
                # Cache non-empty responses so repeat searches skip the
                # entire chain. Empty responses are NOT cached -- a real
                # "no results" lock-in is more annoying than re-querying.
                if self._cache is not None:
                    await self._cache.set(cache_key, response.model_dump(mode="json"))
                return SearchOutcome(
                    response=response,
                    blocked=False,
                    providers_tried=tried,
                    providers_skipped_cooldown=skipped_cooldown,
                    provider_errors=errors,
                )

            # Genuine zero-results answer from a reachable provider: it is
            # healthy, so clear any stale trip and remember the empty response.
            self._breaker.record_success(provider)
            last_response = response

        # Chain exhausted with no results. ``blocked`` is True only if at
        # least one provider was actively refused (blocked or in cooldown);
        # a clean pass of providers all answering "zero hits" is empty-not-
        # blocked.
        if any_blocked:
            logger.warning(
                "Search for {q!r} returned no results -- all reachable providers "
                "blocked/in-cooldown (tried={t}, cooldown={c}, errors={e})",
                q=query,
                t=tried,
                c=skipped_cooldown,
                e=errors,
            )
        else:
            logger.info(
                "Search for {q!r} returned no results -- providers reachable, zero hits "
                "(tried={t})",
                q=query,
                t=tried,
            )

        if strict:
            from .exceptions import SearchError

            attempted = [p.name for p in self._providers if p.is_available]
            cause = (
                "search engines blocking the request (CAPTCHA / rate-limit) or all "
                "providers in cooldown"
                if any_blocked
                else "providers reachable but returned zero results"
            )
            raise SearchError(
                f"All search providers exhausted ({attempted}) returned no "
                f"results for {query!r}. Most likely: {cause}. Other possible causes: "
                "missing searxng_base_url, ddgs package not installed, "
                "or no network reachability."
            )
        return SearchOutcome(
            response=last_response,
            blocked=any_blocked,
            providers_tried=tried,
            providers_skipped_cooldown=skipped_cooldown,
            provider_errors=errors,
        )
