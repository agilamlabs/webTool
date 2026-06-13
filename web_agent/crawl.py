"""Bounded, same-site breadth-first crawl with optional sitemap seeding.

:class:`SiteCrawler` walks a single site starting from one URL, fetching
every page through the injected :class:`~web_agent.web_fetcher.WebFetcher`
(so all v1.7.0 safety gates -- robots obedience, per-host rate limiting,
bot-challenge detection, injection sanitize, and SSRF re-gating -- apply to
every navigation) and extracting content through the injected
:class:`~web_agent.content_extractor.ContentExtractor`. It MIRRORS
:meth:`web_agent.recipes.Recipes.collect_across_pages`: same extractor call
(``extract_async``), same advisory injection rollup ordering
(none < low < medium < high), same per-URL :class:`FetchDiagnostic` shape.

The crawl is bounded on every axis -- ``max_pages``, ``max_depth``, an
optional wall-clock ``time_budget_s``, a per-page link cap, and scope (same
host, or same registrable domain) -- so it terminates on adversarial link
graphs (cycles, fan-out bombs, infinite calendars). URLs are deduplicated,
so a cyclic graph never double-fetches.

:func:`extract_links` is a pure, side-effect-free helper that resolves and
scopes the ``<a href>`` targets in a page; it is exported separately so it
can be unit-tested without a browser.
"""

from __future__ import annotations

import collections
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

from loguru import logger

from .config import AppConfig
from .content_extractor import ContentExtractor
from .correlation import get_correlation_id
from .models import (
    CrawledPage,
    CrawlResult,
    ExtractionResult,
    FetchDiagnostic,
    FetchResult,
    FetchStatus,
)
from .sitemap import parse_sitemap
from .utils import check_domain_allowed
from .web_fetcher import WebFetcher

# Ordering for the advisory injection-risk rollup on CrawlResult
# (none < low < medium < high). Mirrors ``Recipes._INJECTION_RISK_ORDER`` so
# the crawl-level ``max_injection_risk`` is computed identically.
_INJECTION_RISK_ORDER: dict[str, int] = {"none": 0, "low": 1, "medium": 2, "high": 3}

# Map a non-success FetchStatus onto a coarse diagnostic ``block_reason``.
# Mirrors ``agent._BLOCK_REASON_BY_STATUS`` / ``_block_reason_for`` but is kept
# local so this module does not import agent.py (a heavy, browser-coupled
# module the integrator edits concurrently).
_BLOCK_REASON_BY_STATUS: dict[FetchStatus, str] = {
    FetchStatus.TIMEOUT: "timeout",
    FetchStatus.HTTP_ERROR: "http_error",
    FetchStatus.NETWORK_ERROR: "network_error",
    FetchStatus.BLOCKED: "domain_blocked",
}

# Bound on how many CHILD sitemaps an index is allowed to fan out to, so a
# hostile ``<sitemapindex>`` listing thousands of children cannot turn seeding
# into an unbounded fetch storm. The overall URL count is independently
# bounded by ``sitemap_max_urls``.
_MAX_CHILD_SITEMAPS = 10

# Schemes we never follow from an ``<a href>`` (not navigable page content).
_NON_HTTP_SCHEMES = ("mailto:", "javascript:", "tel:", "data:")

# Find ``<a href="...">`` / ``<a href='...'>`` targets. Mirrors the simple
# regex the prompt sanctions; the extractor's own bs4 path is heavier than we
# need for link harvesting.
_HREF_RE = re.compile(r"href\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


def _block_reason_for(fr: FetchResult) -> Optional[str]:
    """Coarse diagnostic ``block_reason`` for a non-success FetchResult.

    Returns None on success. A fetch stopped by a bot-mitigation wall carries
    a :class:`~web_agent.models.ChallengeInfo`; surface the specific
    ``'bot_challenge'`` reason for it, otherwise map by status. Mirrors
    :func:`web_agent.agent._block_reason_for` without importing agent.py.
    """
    if fr.status == FetchStatus.SUCCESS:
        return None
    if fr.challenge is not None:
        return "bot_challenge"
    return _BLOCK_REASON_BY_STATUS.get(fr.status)


def _registrable_domain(host: str) -> str:
    """Approximate the registrable domain as the LAST TWO dot-labels of ``host``.

    e.g. ``www.example.com`` and ``shop.example.com`` both reduce to
    ``example.com``. This is a deliberately simple last-2-labels HEURISTIC: it
    is WRONG for multi-part public suffixes such as ``co.uk`` / ``com.au``
    (``a.example.co.uk`` reduces to ``co.uk`` here, over-broadening scope), and
    for single-label hosts it returns the host unchanged. A fully correct
    implementation needs the Public Suffix List, which is out of scope for this
    bounded same-site crawler; the scope is further constrained by
    ``check_domain_allowed`` and the page/depth caps, so the worst case of the
    heuristic is a slightly wider-but-still-bounded crawl.
    """
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    return ".".join(labels[-2:])


def _in_scope(
    host: str,
    *,
    scope_host: str,
    same_registrable_domain: bool,
) -> bool:
    """Is ``host`` within the crawl scope defined by ``scope_host``?

    Scope is an exact host match when ``same_registrable_domain`` is False; when
    True, it is a match on the last-2-labels registrable-domain heuristic (so
    subdomains of the same site are in scope). Empty hosts are out of scope.
    """
    if not host:
        return False
    if not same_registrable_domain:
        return host == scope_host
    return _registrable_domain(host) == _registrable_domain(scope_host)


def extract_links(
    html: str,
    base_url: str,
    *,
    scope_host: str,
    same_registrable_domain: bool,
    cap: int,
    include: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> list[str]:
    """Resolve, scope, filter, dedupe, and cap the ``<a href>`` links in ``html``.

    Pure function (no I/O). Steps, in order:

    1. Find every ``href`` via regex and resolve it against ``base_url`` with
       :func:`urllib.parse.urljoin`.
    2. Drop fragments (strip ``#...``) and non-http(s) schemes
       (``mailto:`` / ``javascript:`` / ``tel:`` / ``data:``).
    3. SCOPE filter: keep a URL only if its host is in scope (same host, or --
       when ``same_registrable_domain`` -- same last-2-labels registrable
       domain; see :func:`_registrable_domain` for the heuristic's limits).
    4. Apply ``include`` (keep only URLs matching ANY include regex, when
       given) then ``exclude`` (drop URLs matching ANY exclude regex), both via
       :func:`re.search`.
    5. Dedupe preserving first-seen order; cap to ``cap``.

    Args:
        html: The page HTML to harvest links from.
        base_url: URL the page was fetched from, for relative-href resolution.
        scope_host: The host that defines crawl scope.
        same_registrable_domain: Widen scope from exact-host to
            same-registrable-domain.
        cap: Maximum number of links to return (>= 0).
        include: Optional regexes; when given, a URL is kept only if it matches
            at least one.
        exclude: Optional regexes; a URL is dropped if it matches at least one.

    Returns:
        The in-scope, filtered, de-duplicated, capped list of absolute URLs.
    """
    if cap <= 0 or not html:
        return []

    include_res = [re.compile(p) for p in include] if include else []
    exclude_res = [re.compile(p) for p in exclude] if exclude else []

    out: list[str] = []
    seen: set[str] = set()
    for match in _HREF_RE.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        # A pure-fragment href (``#section``) is a same-page anchor, never a
        # crawl target -- drop it BEFORE urljoin (which would otherwise resolve
        # it to the base page itself). Mirrors recipes._find_next_link.
        if raw.startswith("#"):
            continue
        # Drop obviously-non-navigable schemes before resolving (a relative
        # href has no scheme and falls through to urljoin).
        lowered = raw.lower()
        if lowered.startswith(_NON_HTTP_SCHEMES):
            continue
        resolved = urljoin(base_url, raw)
        # Strip any trailing fragment from an otherwise-valid URL.
        resolved = resolved.split("#", 1)[0]
        if not resolved:
            continue
        parsed = urlparse(resolved)
        if parsed.scheme not in ("http", "https"):
            continue
        if not _in_scope(
            (parsed.hostname or "").lower(),
            scope_host=scope_host,
            same_registrable_domain=same_registrable_domain,
        ):
            continue
        if include_res and not any(r.search(resolved) for r in include_res):
            continue
        if exclude_res and any(r.search(resolved) for r in exclude_res):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
        if len(out) >= cap:
            break
    return out


class SiteCrawler:
    """Bounded breadth-first same-site crawler over the injected primitives.

    Every page is fetched via :meth:`WebFetcher.fetch` (full v1.7.0 safety
    gate set) and extracted via :meth:`ContentExtractor.extract_async`,
    mirroring :meth:`Recipes.collect_across_pages`. Construct one per crawl;
    :meth:`crawl` carries no instance state between calls.

    Args:
        fetcher: WebFetcher for page fetching.
        extractor: ContentExtractor for content extraction.
        config: AppConfig for safety configuration (domain allow/deny).
    """

    def __init__(
        self,
        fetcher: WebFetcher,
        extractor: ContentExtractor,
        config: AppConfig,
    ) -> None:
        self._fetcher = fetcher
        self._extractor = extractor
        self._config = config

    # ------------------------------------------------------------------
    # Injection rollup (mirrors Recipes._injection_rollup)
    # ------------------------------------------------------------------

    @staticmethod
    def _injection_rollup(pages: list[CrawledPage]) -> tuple[Optional[str], int]:
        """Roll per-page injection reports up to a crawl-level summary.

        Returns ``(max_injection_risk, pages_with_injection)`` where the max is
        the highest ``risk`` across pages carrying a report (ordered
        none < low < medium < high), or ``None`` when no page carried one
        (detection disabled). ``pages_with_injection`` counts pages scoring
        above ``'none'``. Identical semantics to
        :meth:`Recipes._injection_rollup`.
        """
        max_rank = -1
        max_risk: Optional[str] = None
        n_with_injection = 0
        for page in pages:
            report = page.injection
            if report is None:
                continue
            rank = _INJECTION_RISK_ORDER.get(report.risk, 0)
            if rank > max_rank:
                max_rank = rank
                max_risk = report.risk
            if rank > 0:
                n_with_injection += 1
        return max_risk, n_with_injection

    # ------------------------------------------------------------------
    # Page-record / diagnostic builders (mirror Recipes helpers)
    # ------------------------------------------------------------------

    @staticmethod
    def _success_page(
        url: str,
        depth: int,
        fr: FetchResult,
        extracted: ExtractionResult,
        links_found: int,
    ) -> CrawledPage:
        """Build a CrawledPage for a successfully fetched+extracted page."""
        return CrawledPage(
            url=url,
            final_url=fr.final_url if fr.final_url != url else None,
            depth=depth,
            status=fr.status.value,
            title=extracted.title,
            content=extracted.content or "",
            content_length=extracted.content_length,
            extraction_method=extracted.extraction_method,
            links_found=links_found,
            injection=extracted.injection,
        )

    @staticmethod
    def _failed_page(url: str, depth: int, fr: FetchResult) -> CrawledPage:
        """Build a CrawledPage for a non-success fetch (no extraction ran)."""
        return CrawledPage(
            url=url,
            final_url=fr.final_url if fr.final_url != url else None,
            depth=depth,
            status=fr.status.value,
            error_message=fr.error_message,
        )

    @staticmethod
    def _diagnostic(
        url: str,
        fr: FetchResult,
        *,
        content_length: int = 0,
        block_reason: Optional[str] = None,
    ) -> FetchDiagnostic:
        """Build a per-URL FetchDiagnostic (mirrors Recipes._page_diagnostic)."""
        return FetchDiagnostic(
            url=url,
            final_url=fr.final_url,
            status=fr.status,
            status_code=fr.status_code,
            block_reason=block_reason,
            content_length=content_length,
            response_time_ms=fr.response_time_ms,
            from_cache=fr.from_cache,
        )

    # ------------------------------------------------------------------
    # Sitemap seeding
    # ------------------------------------------------------------------

    async def _seed_from_sitemap(
        self,
        *,
        origin: str,
        scope_host: str,
        same_registrable_domain: bool,
        sitemap_max_urls: int,
        session_id: Optional[str],
    ) -> list[str]:
        """Best-effort fetch+parse of ``<origin>/sitemap.xml`` -> in-scope URLs.

        Fetches the conventional ``/sitemap.xml`` for the start origin. When it
        is a sitemap INDEX, fetches up to ``_MAX_CHILD_SITEMAPS`` child
        sitemaps and merges their URLs. The merged URL list is filtered to the
        crawl scope and bounded to ``sitemap_max_urls``. Any
        missing/blocked/garbage sitemap is silently skipped -- seeding is
        best-effort and NEVER fatal (all failures are swallowed and logged).

        Returns the list of in-scope seed URLs (possibly empty).
        """
        candidate: list[str] = []
        try:
            root_url = f"{origin}/sitemap.xml"
            fr = await self._fetcher.fetch(root_url, session_id=session_id)
            if fr.status != FetchStatus.SUCCESS or not fr.html:
                logger.debug("Sitemap not available at {u} (status={s})", u=root_url, s=fr.status)
                return []
            parsed = parse_sitemap(fr.html, max_urls=sitemap_max_urls)

            if parsed.is_index:
                # Fan out to child sitemaps, bounded on both child count and
                # the overall URL total.
                for child_url in parsed.urls[:_MAX_CHILD_SITEMAPS]:
                    if len(candidate) >= sitemap_max_urls:
                        break
                    child_fr = await self._fetcher.fetch(child_url, session_id=session_id)
                    if child_fr.status != FetchStatus.SUCCESS or not child_fr.html:
                        continue
                    child_parsed = parse_sitemap(child_fr.html, max_urls=sitemap_max_urls)
                    # A child that is itself an index is NOT recursed further
                    # (one level of fan-out only) -- keep seeding bounded.
                    candidate.extend(child_parsed.urls)
            else:
                candidate.extend(parsed.urls)
        except Exception as exc:  # best-effort: a bad sitemap never aborts the crawl
            logger.debug("Sitemap seeding failed for {o}: {e}", o=origin, e=exc)
            return []

        # Scope-filter and bound.
        seeds: list[str] = []
        for url in candidate:
            host = (urlparse(url).hostname or "").lower()
            if _in_scope(
                host, scope_host=scope_host, same_registrable_domain=same_registrable_domain
            ):
                seeds.append(url)
                if len(seeds) >= sitemap_max_urls:
                    break
        return seeds

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------

    async def crawl(
        self,
        start_url: str,
        *,
        max_pages: int,
        max_depth: int,
        same_registrable_domain: bool,
        use_sitemap: bool,
        sitemap_max_urls: int,
        per_page_link_cap: int,
        include: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        session_id: Optional[str] = None,
        time_budget_s: Optional[float] = None,
    ) -> CrawlResult:
        """Breadth-first crawl ``start_url`` within scope, bounded on every axis.

        Args:
            start_url: The URL to start from (depth 0).
            max_pages: Stop after this many pages are fetched.
            max_depth: Deepest link depth to fetch (start URL is depth 0).
            same_registrable_domain: Scope by registrable domain (subdomains
                included) instead of exact host.
            use_sitemap: Seed the frontier from ``<origin>/sitemap.xml`` when
                available (best-effort).
            sitemap_max_urls: Bound on sitemap-derived seed URLs.
            per_page_link_cap: Max NEW links harvested from a single page.
            include: Optional include regexes for harvested links (keep only
                matches).
            exclude: Optional exclude regexes for harvested links (drop
                matches).
            session_id: Optional persistent browser session for every fetch.
            time_budget_s: Optional wall-clock budget; the crawl stops with
                ``stopped_reason='budget'`` once elapsed exceeds it.

        Returns:
            A :class:`CrawlResult` with per-page records, counts, the injection
            rollup, per-URL diagnostics, ``stopped_reason``, and timing. A
            no-host / domain-blocked start URL yields ``stopped_reason='blocked'``
            with an error and no pages.
        """
        start_time = time.perf_counter()

        def _elapsed_ms() -> float:
            return (time.perf_counter() - start_time) * 1000

        scope_host = (urlparse(start_url).hostname or "").lower()

        pages: list[CrawledPage] = []
        diagnostics: list[FetchDiagnostic] = []
        errors: list[str] = []
        warnings: list[str] = []
        skipped_offsite = 0
        skipped_disallowed = 0
        sitemap_used = False
        sitemap_urls_seeded = 0
        max_depth_reached = 0

        def _build(stopped_reason: str) -> CrawlResult:
            max_risk, n_with_injection = self._injection_rollup(pages)
            return CrawlResult(
                start_url=start_url,
                pages=pages,
                pages_crawled=len(pages),
                urls_discovered=len(discovered),
                max_depth_reached=max_depth_reached,
                total_content_length=sum(p.content_length for p in pages),
                sitemap_used=sitemap_used,
                sitemap_urls_seeded=sitemap_urls_seeded,
                skipped_offsite=skipped_offsite,
                skipped_disallowed=skipped_disallowed,
                max_injection_risk=max_risk,
                pages_with_injection=n_with_injection,
                stopped_reason=stopped_reason,
                errors=errors,
                warnings=warnings,
                diagnostics=diagnostics,
                correlation_id=get_correlation_id(),
                total_time_ms=_elapsed_ms(),
            )

        # Pre-gate: a start URL with no host, or whose domain is denied, never
        # crawls. ``discovered`` must exist for ``_build`` even on this path.
        discovered: set[str] = {start_url}
        if not scope_host or not check_domain_allowed(start_url, self._config.safety):
            errors.append(
                f"Start URL is not crawlable (no host or domain blocked): {start_url}"
            )
            diagnostics.append(
                FetchDiagnostic(
                    url=start_url, status=FetchStatus.BLOCKED, block_reason="domain_blocked"
                )
            )
            logger.warning("crawl: start URL blocked/host-less: {u}", u=start_url)
            return _build("blocked")

        logger.info(
            "crawl: start={u} max_pages={mp} max_depth={md} scope={scope}",
            u=start_url,
            mp=max_pages,
            md=max_depth,
            scope="registrable_domain" if same_registrable_domain else scope_host,
        )

        frontier: collections.deque[tuple[str, int]] = collections.deque([(start_url, 0)])
        visited: set[str] = set()

        # --- Sitemap seeding (best-effort; never fatal) ---
        if use_sitemap:
            parsed = urlparse(start_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            seeds = await self._seed_from_sitemap(
                origin=origin,
                scope_host=scope_host,
                same_registrable_domain=same_registrable_domain,
                sitemap_max_urls=sitemap_max_urls,
                session_id=session_id,
            )
            if seeds:
                sitemap_used = True
                for seed in seeds:
                    if seed not in discovered:
                        discovered.add(seed)
                        frontier.append((seed, 1))
                        sitemap_urls_seeded += 1
                logger.info("crawl: seeded {n} URL(s) from sitemap", n=sitemap_urls_seeded)

        # --- BFS main loop, wrapped so one bad page never kills the crawl ---
        stopped_reason = "frontier_empty"
        try:
            while frontier and len(pages) < max_pages:
                # Wall-clock budget check (before popping the next page).
                if time_budget_s is not None and (
                    time.perf_counter() - start_time
                ) >= time_budget_s:
                    stopped_reason = "budget"
                    break

                url, depth = frontier.popleft()
                if url in visited:
                    continue
                visited.add(url)

                # Past the depth ceiling: never fetch (defensive -- we also
                # avoid enqueueing beyond max_depth below).
                if depth > max_depth:
                    continue

                try:
                    fr = await self._fetcher.fetch(url, session_id=session_id)
                except Exception as exc:
                    # One page's unexpected failure is recorded and the crawl
                    # continues -- never let a single bad page abort the walk.
                    warnings.append(f"Fetch raised for {url}: {exc}")
                    diagnostics.append(
                        FetchDiagnostic(
                            url=url,
                            status=FetchStatus.NETWORK_ERROR,
                            block_reason="network_error",
                        )
                    )
                    logger.warning("crawl: fetch raised for {u}: {e}", u=url, e=exc)
                    continue

                if depth > max_depth_reached:
                    max_depth_reached = depth

                if fr.status != FetchStatus.SUCCESS:
                    # Non-success: still record the page + diagnostic, but do
                    # not extract or harvest links from it.
                    if fr.status == FetchStatus.BLOCKED:
                        skipped_disallowed += 1
                    else:
                        errors.append(f"Fetch failed for {url}: {fr.error_message}")
                    warnings.append(f"Non-success fetch ({fr.status.value}) for {url}")
                    pages.append(self._failed_page(url, depth, fr))
                    diagnostics.append(
                        self._diagnostic(url, fr, block_reason=_block_reason_for(fr))
                    )
                    continue

                # SUCCESS: extract content (mirror collect_across_pages).
                extracted = await self._extractor.extract_async(fr)

                # Harvest links only when we may go deeper.
                links_found = 0
                if depth < max_depth and fr.html:
                    harvested = extract_links(
                        fr.html,
                        fr.final_url or url,
                        scope_host=scope_host,
                        same_registrable_domain=same_registrable_domain,
                        cap=per_page_link_cap,
                        include=include,
                        exclude=exclude,
                    )
                    # Side-count offsite drops: links the page referenced that
                    # fell outside scope. Recomputed cheaply from the raw hrefs
                    # so the count reflects what we SAW but rejected for scope.
                    skipped_offsite += self._count_offsite(
                        fr.html, fr.final_url or url, same_registrable_domain, scope_host
                    )
                    for link in harvested:
                        if link not in discovered:
                            discovered.add(link)
                            frontier.append((link, depth + 1))
                            links_found += 1

                pages.append(
                    self._success_page(url, depth, fr, extracted, links_found)
                )
                diagnostics.append(
                    self._diagnostic(url, fr, content_length=extracted.content_length)
                )

            else:
                # Loop exited via the while-condition, not a break: either the
                # frontier drained or the page cap was hit.
                stopped_reason = "max_pages" if len(pages) >= max_pages else "frontier_empty"
        except Exception as exc:  # catastrophic failure ends the crawl
            errors.append(f"Crawl aborted by unexpected error: {exc}")
            logger.exception("crawl: catastrophic failure: {e}", e=exc)
            return _build("error")

        return _build(stopped_reason)

    @staticmethod
    def _count_offsite(
        html: str,
        base_url: str,
        same_registrable_domain: bool,
        scope_host: str,
    ) -> int:
        """Count distinct http(s) links on the page that fell OUTSIDE scope.

        A side count for ``skipped_offsite``: resolves each ``<a href>``, drops
        fragments / non-http(s) schemes, and counts the distinct in-page URLs
        whose host is out of scope. Pure; mirrors :func:`extract_links`
        resolution so the two agree on what "in scope" means.
        """
        seen_offsite: set[str] = set()
        for match in _HREF_RE.finditer(html):
            raw = match.group(1).strip()
            if not raw:
                continue
            if raw.startswith("#"):
                continue
            if raw.lower().startswith(_NON_HTTP_SCHEMES):
                continue
            resolved = urljoin(base_url, raw).split("#", 1)[0]
            if not resolved:
                continue
            parsed = urlparse(resolved)
            if parsed.scheme not in ("http", "https"):
                continue
            host = (parsed.hostname or "").lower()
            if not _in_scope(
                host, scope_host=scope_host, same_registrable_domain=same_registrable_domain
            ):
                seen_offsite.add(resolved)
        return len(seen_offsite)
