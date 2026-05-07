"""High-level composite recipes wrapping the search/fetch/extract/download primitives.

Recipes are stateless wrappers over the existing primitives. They live as
methods on :class:`Agent` (and as MCP tools) so AI agents can express
common goals (research a topic, find and download a file, open the best
result for a query) in a single call instead of orchestrating multiple
low-level calls.

Available recipes:
- :meth:`Recipes.search_and_open_best_result` -- search, rank results, fetch+extract top hit
- :meth:`Recipes.find_and_download_file` -- search, locate first file URL of given types, download
- :meth:`Recipes.web_research` -- search, parallel fetch+extract top N, return citations
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Optional
from urllib.parse import urlparse

from loguru import logger

from .browser_manager import BrowserManager
from .config import AppConfig
from .content_extractor import ContentExtractor
from .correlation import get_correlation_id
from .downloader import Downloader
from .models import (
    Citation,
    DownloadResult,
    ExtractionResult,
    FetchDiagnostic,
    FetchStatus,
    FormFilterSpec,
    ResearchResult,
    SearchResultItem,
)
from .search_engine import SearchEngine
from .session_manager import SessionManager
from .utils import BudgetTracker, check_domain_allowed
from .web_fetcher import WebFetcher, _is_download_url

# Domains that get a small relevance bonus in the default ranker
_WELL_KNOWN_DOMAINS = (
    "wikipedia.org",
    "github.com",
    "stackoverflow.com",
    "arxiv.org",
    "python.org",
    "mozilla.org",
    "nature.com",
    "nih.gov",
    "edu",
    "gov",
)


# Reusable named ranking profiles. Each profile is a tuple of host hints
# that get merged with caller-supplied ``prefer_domains`` and applied as
# a +0.40 ranking bonus. Designed so callers don't have to hard-code
# host lists for common research scenarios.
RANKING_PROFILES: dict[str, tuple[str, ...]] = {
    "official_sources": (
        "ec.europa.eu",
        "esma.europa.eu",
        "eba.europa.eu",
        "sec.gov",
        "treasury.gov",
        "federalreserve.gov",
        "bis.org",
        "imf.org",
        "worldbank.org",
        "oecd.org",
        "un.org",
        "europa.eu",
        "gov",
        "gov.uk",
        "gov.in",
    ),
    "docs": (
        "docs.python.org",
        "developer.mozilla.org",
        "tldp.org",
        "readthedocs.io",
        "readthedocs.org",
        "pkg.go.dev",
        "rust-lang.org",
        "kubernetes.io",
        "docs.aws.amazon.com",
        "cloud.google.com",
        "learn.microsoft.com",
    ),
    "research": (
        "arxiv.org",
        "ssrn.com",
        "ncbi.nlm.nih.gov",
        "pubmed.ncbi.nlm.nih.gov",
        "nature.com",
        "science.org",
        "acm.org",
        "ieee.org",
        "researchgate.net",
        "papers.ssrn.com",
        "openreview.net",
    ),
    "news": (
        "reuters.com",
        "apnews.com",
        "bbc.com",
        "bbc.co.uk",
        "ft.com",
        "wsj.com",
        "bloomberg.com",
        "nytimes.com",
        "economist.com",
        "axios.com",
        "theguardian.com",
    ),
    "files": (  # commonly hosts canonical PDFs / datasets
        "sec.gov",
        "ec.europa.eu",
        "esma.europa.eu",
        "data.gov",
        "data.gov.uk",
        "github.com",
        "githubusercontent.com",
        "huggingface.co",
        "kaggle.com",
        "zenodo.org",
        "figshare.com",
    ),
}


def _resolve_domain_hints(
    prefer_domains: Optional[list[str]],
    domain_profile: Optional[str],
) -> tuple[str, ...]:
    """Combine an optional named profile with caller-supplied domain hints.

    Profile domains come first (so the caller's explicit domains can also
    benefit from rare-domain weighting if any). Unknown profile names are
    silently ignored after a debug log.
    """
    profile_hints: tuple[str, ...] = ()
    if domain_profile:
        if domain_profile in RANKING_PROFILES:
            profile_hints = RANKING_PROFILES[domain_profile]
        else:
            logger.debug(
                "Unknown ranking profile {p!r}; ignoring",
                p=domain_profile,
            )
    user_hints = tuple(prefer_domains or ())
    return profile_hints + user_hints


class Recipes:
    """Composite high-level workflows over the existing web_agent primitives.

    Args:
        search: SearchEngine for query execution.
        fetcher: WebFetcher for page fetching.
        extractor: ContentExtractor for content extraction.
        downloader: Downloader for file downloads.
        config: AppConfig for budget/safety configuration.
        browser_manager: Optional BrowserManager for direct page control
            (required by :meth:`fill_form_and_extract`).
        sessions: Optional SessionManager for session-aware page acquisition.
    """

    def __init__(
        self,
        search: SearchEngine,
        fetcher: WebFetcher,
        extractor: ContentExtractor,
        downloader: Downloader,
        config: AppConfig,
        browser_manager: Optional[BrowserManager] = None,
        sessions: Optional[SessionManager] = None,
    ) -> None:
        self._search = search
        self._fetcher = fetcher
        self._extractor = extractor
        self._downloader = downloader
        self._config = config
        self._bm = browser_manager
        self._sessions = sessions
        # Merge built-in RANKING_PROFILES with user-defined profiles from
        # AppConfig.ranking_profiles. User-defined wins on collision so a
        # caller can redefine 'docs' for an internal portal.
        self._profiles: dict[str, tuple[str, ...]] = {**RANKING_PROFILES}
        for name, hosts in (config.ranking_profiles or {}).items():
            self._profiles[name] = tuple(hosts)

    def _resolve_hints(
        self,
        prefer_domains: Optional[list[str]],
        domain_profile: Optional[str],
    ) -> tuple[str, ...]:
        """Combine profile + caller hints, consulting the merged profile dict.

        Built-in profiles can be overridden by user-defined ones via
        ``AppConfig.ranking_profiles``. Unknown profile names log a debug
        message and are otherwise ignored.
        """
        profile_hints: tuple[str, ...] = ()
        if domain_profile:
            if domain_profile in self._profiles:
                profile_hints = self._profiles[domain_profile]
            else:
                logger.debug(
                    "Unknown ranking profile {p!r} (known: {known}); ignoring",
                    p=domain_profile,
                    known=sorted(self._profiles.keys()),
                )
        user_hints = tuple(prefer_domains or ())
        return profile_hints + user_hints

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Lower-case word tokens with length >= 2."""
        return {tok for tok in re.findall(r"[a-zA-Z0-9]+", text.lower()) if len(tok) >= 2}

    @staticmethod
    def _rank(
        query: str,
        item: SearchResultItem,
        scheme: str = "default",
        prefer_domains: tuple[str, ...] = (),
    ) -> float:
        """Score a search result.

        Schemes:
            ``default``: query-token overlap + HTTPS bonus + well-known
                domain bonus + caller-supplied prefer_domains bonus +
                position tiebreaker
            ``overlap``: only token overlap
            ``position``: only inverse position

        Args:
            prefer_domains: Caller-supplied host hints (e.g. ``("ec.europa.eu",
                "esma.europa.eu")``). Each result whose host matches any
                hint (exact or as a parent suffix) gets a +0.40 bonus,
                large enough to dominate the well-known bonus. Only
                applied for the ``default`` scheme.
        """
        if scheme == "position":
            return 1.0 / max(1, item.position)

        q_toks = Recipes._tokenize(query)
        r_toks = Recipes._tokenize(f"{item.title} {item.snippet} {item.displayed_url}")
        overlap = len(q_toks & r_toks) / max(1, len(q_toks)) if q_toks else 0.0
        if scheme == "overlap":
            return overlap

        # default
        score = overlap
        try:
            parsed = urlparse(item.url)
            if parsed.scheme == "https":
                score += 0.30
            host = (parsed.hostname or "").lower()
            for known in _WELL_KNOWN_DOMAINS:
                if host == known or host.endswith("." + known):
                    score += 0.20
                    break
            # Caller-supplied domain hints get a much larger bonus so they
            # outrank token overlap and well-known domains.
            for pref in prefer_domains:
                pref_l = pref.lower().lstrip(".")
                if host == pref_l or host.endswith("." + pref_l):
                    score += 0.40
                    break
            # Small penalty for very deep subdomains
            subdomain_depth = host.count(".")
            if subdomain_depth > 2:
                score -= 0.10
        except Exception:
            pass

        # Position tiebreaker
        score += 0.10 / max(1, item.position)
        return score

    # ------------------------------------------------------------------
    # Recipe 1: search_and_open_best_result
    # ------------------------------------------------------------------

    async def search_and_open_best_result(
        self,
        query: str,
        ranking: str = "default",
        session_id: Optional[str] = None,
        prefer_domains: Optional[list[str]] = None,
        domain_profile: Optional[str] = None,
    ) -> ExtractionResult:
        """Search for ``query``, rank results, fetch+extract the top hit.

        Args:
            query: The search query.
            ranking: Ranking scheme (``default`` | ``overlap`` | ``position``).
            session_id: Optional persistent browser session for the fetch.
            prefer_domains: Optional caller-supplied host hints (e.g.
                ``["ec.europa.eu", "esma.europa.eu"]``). Results from
                these hosts get a strong ranking bonus.
            domain_profile: Optional named ranking profile that contributes
                a curated host list on top of ``prefer_domains``. Available:
                ``"official_sources" | "docs" | "research" | "news" | "files"``.

        Returns:
            ExtractionResult for the top-ranked URL. If all URLs are blocked
            or fail, returns an empty ExtractionResult with ``extraction_method="none"``.
        """
        logger.info("Recipe: search_and_open_best_result for {q}", q=query)
        search_resp = await self._search.search(query, max_results=10)

        # Filter denied domains BEFORE ranking
        allowed = [
            r for r in search_resp.results if check_domain_allowed(r.url, self._config.safety)
        ]

        if not allowed:
            return ExtractionResult(
                url="",
                extraction_method="none",
                correlation_id=get_correlation_id(),
            )

        prefs = self._resolve_hints(prefer_domains, domain_profile)
        ranked = sorted(
            allowed,
            key=lambda r: self._rank(query, r, ranking, prefer_domains=prefs),
            reverse=True,
        )

        # Fetch top hit
        top = ranked[0]
        fetch_result = await self._fetcher.fetch(top.url, session_id=session_id)
        extracted = self._extractor.extract(fetch_result)
        # Inherit correlation_id from current scope
        extracted.correlation_id = get_correlation_id()
        return extracted

    # ------------------------------------------------------------------
    # Recipe 2: find_and_download_file
    # ------------------------------------------------------------------

    async def find_and_download_file(
        self,
        query: str,
        file_types: Optional[list[str]] = None,
        session_id: Optional[str] = None,
    ) -> DownloadResult:
        """Search for ``query``, find first matching file URL, download it.

        Looks for direct file URLs in the search results whose extension
        matches one of ``file_types``. Falls through to the first download-
        looking URL if no exact match is found.

        Args:
            query: The search query.
            file_types: Allowed extensions (e.g. ``["pdf", "xlsx"]``). Default ``["pdf"]``.
            session_id: Optional persistent browser session for the download.

        Returns:
            DownloadResult. If no file URL is found, returns an error result
            with ``status=NETWORK_ERROR`` and a clear message.
        """
        if file_types is None:
            file_types = ["pdf"]
        # Normalize extensions: ensure leading dot, lowercase
        normalized = {f".{ft.lstrip('.').lower()}" for ft in file_types}

        logger.info(
            "Recipe: find_and_download_file query={q} types={t}",
            q=query,
            t=sorted(normalized),
        )

        search_resp = await self._search.search(query, max_results=20)

        # Collect candidate URLs
        candidates: list[str] = []
        for r in search_resp.results:
            url = r.url
            if not check_domain_allowed(url, self._config.safety):
                continue
            ext = self._url_extension(url)
            if ext in normalized:
                candidates.append(url)

        if not candidates:
            # Fallback: any download-looking URL (any of our known download extensions)
            candidates = [
                r.url
                for r in search_resp.results
                if _is_download_url(r.url) and check_domain_allowed(r.url, self._config.safety)
            ]

        if not candidates:
            return DownloadResult(
                url="",
                filepath="",
                filename="",
                status=FetchStatus.NETWORK_ERROR,
                error_message=(
                    f"No file URL matching {sorted(normalized)} found in "
                    f"search results for {query!r}"
                ),
                correlation_id=get_correlation_id(),
            )

        return await self._downloader.download(candidates[0], session_id=session_id)

    @staticmethod
    def _url_extension(url: str) -> str:
        """Return the URL path's file extension (lowercase, with dot)."""
        try:
            path = urlparse(url).path.lower()
            last = path.rsplit("/", 1)[-1] if "/" in path else path
            if "." in last:
                return "." + last.rsplit(".", 1)[-1]
        except Exception:
            pass
        return ""

    # ------------------------------------------------------------------
    # Recipe 3: web_research
    # ------------------------------------------------------------------

    async def web_research(
        self,
        query: str,
        depth: int = 1,
        max_pages: int = 5,
        session_id: Optional[str] = None,
        prefer_domains: Optional[list[str]] = None,
        domain_profile: Optional[str] = None,
    ) -> ResearchResult:
        """Search and extract content from the top N pages, returning structured citations.

        Args:
            query: Research question / topic.
            depth: Reserved for future expansion. v1 supports depth=1 only.
            max_pages: Maximum number of pages to fetch and extract.
            session_id: Optional persistent browser session.
            prefer_domains: Optional caller-supplied host hints; matching
                results get a strong ranking bonus.
            domain_profile: Optional named ranking profile -- one of
                ``"official_sources" | "docs" | "research" | "news" | "files"``.

        Returns:
            ResearchResult with citations, summary pages, budget telemetry,
            warnings, download_candidates, and per-URL diagnostics.
        """
        from .agent import _MessageBag

        start = time.perf_counter()
        bag = _MessageBag()
        download_candidates: list[SearchResultItem] = []
        diagnostics: list[FetchDiagnostic] = []
        budget = BudgetTracker(self._config.safety)

        if depth != 1:
            logger.warning(
                "web_research depth={d} requested but only depth=1 supported in v1",
                d=depth,
            )

        logger.info("Recipe: web_research query={q} max_pages={n}", q=query, n=max_pages)

        # Pull more results than needed so ranking + filtering have headroom
        search_resp = await self._search.search(query, max_results=max(max_pages * 2, 10))

        # Filter denied domains, skip download URLs (research is about reading pages)
        allowed: list[SearchResultItem] = []
        for r in search_resp.results:
            if not check_domain_allowed(r.url, self._config.safety):
                bag.warn("domain_blocked", f"Domain denied: {r.url}", url=r.url)
                diagnostics.append(
                    FetchDiagnostic(
                        url=r.url,
                        status=FetchStatus.BLOCKED,
                        provider=r.provider,
                        block_reason="domain_blocked",
                    )
                )
                continue
            if _is_download_url(r.url):
                download_candidates.append(r)
                diagnostics.append(
                    FetchDiagnostic(
                        url=r.url,
                        status=FetchStatus.SUCCESS,
                        provider=r.provider,
                        block_reason="download_skipped",
                    )
                )
                continue
            allowed.append(r)

        if not allowed:
            bag.err("no_allowed_pages", "No allowed pages in search results")
            return ResearchResult(
                query=query,
                errors=bag.errors,
                warnings=bag.warnings,
                download_candidates=download_candidates,
                diagnostics=diagnostics,
                structured_warnings=bag.structured_warnings,
                structured_errors=bag.structured_errors,
                correlation_id=get_correlation_id(),
                total_time_ms=(time.perf_counter() - start) * 1000,
            )

        prefs = self._resolve_hints(prefer_domains, domain_profile)
        # Cache scores so we don't re-tokenize per item during sort + citation build
        scores: dict[str, float] = {
            r.url: self._rank(query, r, prefer_domains=prefs) for r in allowed
        }
        ranked = sorted(allowed, key=lambda r: scores[r.url], reverse=True)
        targets = ranked[:max_pages]

        # Fetch in parallel (bounded by BrowserManager semaphore inside fetcher)
        fetch_tasks = [self._fetcher.fetch(r.url, session_id=session_id) for r in targets]
        fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        citations: list[Citation] = []
        summary_pages: list[ExtractionResult] = []

        for item, fr in zip(targets, fetch_results, strict=True):
            if isinstance(fr, BaseException):
                bag.warn(
                    "fetch_exception",
                    f"Fetch raised for {item.url}: {fr}",
                    url=item.url,
                )
                diagnostics.append(
                    FetchDiagnostic(
                        url=item.url,
                        status=FetchStatus.NETWORK_ERROR,
                        provider=item.provider,
                        block_reason="network_error",
                    )
                )
                continue
            try:
                budget.check_time()
                budget.add_page()
            except Exception as exc:
                bag.err("budget_exceeded", str(exc))
                break

            if fr.status != FetchStatus.SUCCESS or not fr.html:
                bag.warn(
                    "fetch_failed",
                    f"Failed to fetch {item.url}: {fr.error_message}",
                    url=item.url,
                )
                # local import to avoid leaking the helper into module top
                from .agent import _block_reason_for

                diagnostics.append(
                    FetchDiagnostic(
                        url=item.url,
                        final_url=fr.final_url,
                        status=fr.status,
                        status_code=fr.status_code,
                        provider=item.provider,
                        block_reason=_block_reason_for(fr),
                        response_time_ms=fr.response_time_ms,
                        from_cache=fr.from_cache,
                    )
                )
                continue

            extracted = self._extractor.extract(fr)
            extracted.correlation_id = get_correlation_id()

            try:
                budget.add_chars(extracted.content_length)
            except Exception as exc:
                bag.err("budget_exceeded", str(exc))
                summary_pages.append(extracted)
                diagnostics.append(
                    FetchDiagnostic(
                        url=item.url,
                        final_url=fr.final_url,
                        status=fr.status,
                        status_code=fr.status_code,
                        provider=item.provider,
                        content_length=extracted.content_length,
                        response_time_ms=fr.response_time_ms,
                        from_cache=fr.from_cache,
                    )
                )
                citations.append(
                    Citation(
                        url=item.url,
                        title=extracted.title or item.title,
                        snippet=item.snippet,
                        extraction_method=extracted.extraction_method,
                        relevance_score=scores[item.url],
                    )
                )
                break

            summary_pages.append(extracted)
            diagnostics.append(
                FetchDiagnostic(
                    url=item.url,
                    final_url=fr.final_url,
                    status=fr.status,
                    status_code=fr.status_code,
                    provider=item.provider,
                    content_length=extracted.content_length,
                    response_time_ms=fr.response_time_ms,
                    from_cache=fr.from_cache,
                )
            )
            citations.append(
                Citation(
                    url=item.url,
                    title=extracted.title or item.title,
                    snippet=item.snippet,
                    extraction_method=extracted.extraction_method,
                    relevance_score=scores[item.url],
                )
            )

        # Promote "no usable pages at all" from warnings to a fatal error.
        if not summary_pages and not bag.errors:
            bag.err(
                "all_fetches_failed",
                "All page fetches failed; see warnings/diagnostics for detail",
            )

        elapsed = (time.perf_counter() - start) * 1000
        return ResearchResult(
            query=query,
            citations=citations,
            summary_pages=summary_pages,
            pages_visited=budget.pages_used,
            chars_extracted=budget.chars_used,
            errors=bag.errors,
            warnings=bag.warnings,
            download_candidates=download_candidates,
            diagnostics=diagnostics,
            structured_warnings=bag.structured_warnings,
            structured_errors=bag.structured_errors,
            correlation_id=get_correlation_id(),
            total_time_ms=elapsed,
        )

    # ------------------------------------------------------------------
    # Recipe 4: fill_form_and_extract (Phase 7 / v1.6.1)
    # ------------------------------------------------------------------

    async def fill_form_and_extract(
        self,
        url: str,
        spec: FormFilterSpec,
        session_id: Optional[str] = None,
    ) -> ExtractionResult:
        """Open a URL, fill a search/filter form, then extract post-submit content.

        Targets dynamic calendar / regulator-filings / event-listing pages
        where content is gated behind a search box and/or filter controls.
        Caller supplies semantic locators in ``spec``; the recipe executes
        the actions and returns the extracted post-submit content.

        Steps:
          1. Open ``url`` (using a persistent session when ``session_id`` is set).
          2. If ``spec.query_selector`` and ``spec.query_value`` are both set,
             fill the search box.
          3. For each ``(locator, value)`` in ``spec.filters``, fill the value
             (auto-detecting <select> vs <input> via element role).
          4. Submit: click ``spec.submit_selector`` if set, else press Enter
             on the query input.
          5. Wait for ``spec.wait_for`` (or ``networkidle``) before reading.
          6. Run :meth:`ContentExtractor.extract` on the resulting HTML.

        Returns:
            ExtractionResult. On failure (timeout, locator not found, blocked
            domain) returns an ExtractionResult with ``extraction_method="none"``.
        """
        if self._bm is None:
            raise RuntimeError(
                "Recipes.fill_form_and_extract requires a BrowserManager; "
                "construct Recipes via Agent (which wires it for you)."
            )
        if not check_domain_allowed(url, self._config.safety):
            logger.warning("fill_form_and_extract: domain blocked: {url}", url=url)
            return ExtractionResult(
                url=url, extraction_method="none", correlation_id=get_correlation_id()
            )

        # Late import to avoid a circular dep through agent.py.
        from playwright.async_api import Page
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        from .browser_actions import _resolve_locator
        from .models import FetchResult, FetchStatus

        timeout_ms = spec.wait_timeout_ms

        async def _drive(page: Page) -> ExtractionResult:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except PlaywrightTimeout:
                logger.warning("fill_form_and_extract: navigation timed out for {url}", url=url)
                return ExtractionResult(
                    url=url, extraction_method="none", correlation_id=get_correlation_id()
                )

            # Step 2: fill query box
            if spec.query_selector is not None and spec.query_value is not None:
                try:
                    loc = _resolve_locator(page, spec.query_selector)
                    await loc.fill(spec.query_value, timeout=timeout_ms)
                except Exception as exc:
                    logger.warning("query fill failed: {e}", e=exc)
                    return ExtractionResult(
                        url=url,
                        extraction_method="none",
                        correlation_id=get_correlation_id(),
                    )

            # Step 3: filters (auto-detect <select> vs input)
            for selector, value in spec.filters:
                try:
                    loc = _resolve_locator(page, selector)
                    tag = (await loc.evaluate("el => el.tagName")).lower()
                    if tag == "select":
                        await loc.select_option(value=value, timeout=timeout_ms)
                    else:
                        await loc.fill(value, timeout=timeout_ms)
                except Exception as exc:
                    logger.warning("filter fill failed for {s}: {e}", s=selector, e=exc)
                    return ExtractionResult(
                        url=url,
                        extraction_method="none",
                        correlation_id=get_correlation_id(),
                    )

            # Step 4: submit (click button OR press Enter on query input)
            try:
                if spec.submit_selector is not None:
                    submit_loc = _resolve_locator(page, spec.submit_selector)
                    await submit_loc.click(timeout=timeout_ms)
                elif spec.query_selector is not None:
                    qloc = _resolve_locator(page, spec.query_selector)
                    await qloc.press("Enter", timeout=timeout_ms)
                # else: caller already submitted via filters or expects auto-search
            except Exception as exc:
                logger.warning("submit failed: {e}", e=exc)
                return ExtractionResult(
                    url=url, extraction_method="none", correlation_id=get_correlation_id()
                )

            # Step 5: wait for results
            try:
                if spec.wait_for is not None:
                    wait_loc = _resolve_locator(page, spec.wait_for)
                    await wait_loc.wait_for(state="visible", timeout=timeout_ms)
                else:
                    await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PlaywrightTimeout:
                logger.warning("fill_form_and_extract: wait_for timed out")
                return ExtractionResult(
                    url=url, extraction_method="none", correlation_id=get_correlation_id()
                )

            # Step 6: extract
            html = await page.content()
            final_url = page.url
            fr = FetchResult(
                url=url,
                final_url=final_url,
                status=FetchStatus.SUCCESS,
                html=html,
                correlation_id=get_correlation_id(),
            )
            extracted = self._extractor.extract(fr)
            extracted.correlation_id = get_correlation_id()
            return extracted

        if session_id and self._sessions is not None:
            ctx = self._sessions.get(session_id)
            self._sessions.touch(session_id)
            page = await ctx.new_page()
            try:
                return await _drive(page)
            finally:
                await page.close()
        else:
            async with self._bm.new_page(block_resources=False) as page:
                return await _drive(page)
