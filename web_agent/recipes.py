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
- :meth:`Recipes.collect_across_pages` -- walk a paginated / infinite-scroll listing,
  assembling extracted content across pages (v1.7.0 Wave 3B)
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from loguru import logger

from .browser_actions import BrowserActions
from .browser_manager import BrowserManager
from .config import AppConfig
from .content_extractor import ContentExtractor
from .correlation import get_correlation_id
from .downloader import Downloader
from .models import (
    ActionStatus,
    Citation,
    CollectedPage,
    CollectionResult,
    DownloadResult,
    ExtractionResult,
    FetchDiagnostic,
    FetchResult,
    FetchStatus,
    FormFilterSpec,
    ResearchResult,
    SearchResultItem,
)
from .search_engine import SearchEngine
from .session_manager import SessionManager
from .utils import BudgetTracker, check_domain_allowed, safe_page_content
from .web_fetcher import (
    _EXT_TO_KIND,
    WebFetcher,
    _is_download_url,
    _response_peer_is_private,
    _url_ext_classification,
    is_binary_kind,
    is_extractable_binary_kind,
)

if TYPE_CHECKING:
    from .agent import _MessageBag as _MessageBagT

# Callable that builds the terminal CollectionResult from a stopped_reason.
_FinishT = Callable[[str], CollectionResult]

# Ordering for the advisory injection-risk rollup on CollectionResult
# (none < low < medium < high). Used to compute ``max_injection_risk``
# across collected pages.
_INJECTION_RISK_ORDER: dict[str, int] = {"none": 0, "low": 1, "medium": 2, "high": 3}


def _selector_desc(sel: object) -> str:
    """v1.7.0: short human-readable description of a SelectorLike.

    Used only in ``fill_form_and_extract`` failure messages so the caller
    learns WHICH locator failed (CSS string verbatim; LocatorSpec as its
    non-None fields).
    """
    if isinstance(sel, str):
        return repr(sel)
    dump = getattr(sel, "model_dump", None)
    if callable(dump):
        try:
            fields = {k: v for k, v in dump().items() if v is not None}
            return repr(fields)
        except (TypeError, ValueError):  # pragma: no cover -- defensive
            pass
    return repr(sel)


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
        actions: Optional BrowserActions for the ``"scroll"`` strategy of
            :meth:`collect_across_pages` (scroll-to-exhaustion needs a
            session tab). The ``"next_link"`` / ``"page_param"`` strategies
            go through the injected WebFetcher and do not need it.
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
        actions: Optional[BrowserActions] = None,
    ) -> None:
        self._search = search
        self._fetcher = fetcher
        self._extractor = extractor
        self._downloader = downloader
        self._config = config
        self._bm = browser_manager
        self._sessions = sessions
        self._actions = actions
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

        # Fetch top hit -- v1.6.9: route through fetch_smart so an
        # extensionless binary URL (PDF served as Content-Type:application/pdf)
        # gets fetch_binary'd instead of dumped into the HTML extractor.
        top = ranked[0]
        fetch_result = await self._fetcher.fetch_smart(top.url, session_id=session_id)
        extracted = await self._extractor.extract_async(fetch_result)
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

        Tier 1: direct extension match against ``file_types``.
        Fallback (v1.6.5, refined v1.6.10): HEAD-probe extensionless URLs
        and accept the first whose detected binary kind matches
        ``file_types``.

        v1.6.11: the prior "any download-looking URL" fallback is removed.
        A caller asking for ``file_types=["pdf"]`` over results containing
        only ``.xlsx`` URLs now gets ``NETWORK_ERROR``, not an XLSX. To
        accept multiple kinds, widen ``file_types`` explicitly.

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
        # REC-3: the canonical *kinds* the requested extensions map to, for
        # matching against ``classify_url`` (which returns a kind, not an
        # extension -- ``.doc``/``.xls`` collapse to ``docx``/``xlsx``).
        # Extensions with no kind mapping (e.g. ``.mp4``) contribute nothing,
        # so an extensionless probe is only accepted as one of the kinds the
        # caller actually asked for.
        requested_kinds = {_EXT_TO_KIND[ext] for ext in normalized if ext in _EXT_TO_KIND}

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

        if not candidates and self._config.safety.probe_binary_urls:
            # Fallback (v1.6.5, refined v1.6.10, sole fallback v1.6.11):
            # HEAD-probe extensionless URLs. Recovers regulator dashboards
            # / asset-portal URLs whose path lacks an extension but whose
            # Content-Type indicates a binary document. Only one HEAD per
            # result; we stop on the first acceptable match.
            #
            # v1.6.10: ``classify_url`` returns a granular kind. Reject
            # binary URLs whose detected kind does not match the caller's
            # ``file_types`` -- a v1.6.9 caller asking for ['pdf'] would
            # previously accept an extensionless XLSX/ZIP because the
            # classifier collapsed everything to ``"binary"``.
            #
            # v1.6.11: the prior "Fallback 1" (any download-looking URL
            # regardless of extension match) is removed; this HEAD-probe
            # path is the sole fallback.
            for r in search_resp.results:
                if not check_domain_allowed(r.url, self._config.safety):
                    continue
                if self._url_extension(r.url):
                    continue  # already considered in Tier 1/2
                classification = await self._fetcher.classify_url(r.url, session_id=session_id)
                if not is_binary_kind(classification):
                    continue
                # REC-3: match the detected *kind* against the kinds the
                # caller's ``file_types`` map to. ``classify_url`` returns a
                # canonical kind (``classify_url(".doc") -> "docx"``,
                # ``".xls" -> "xlsx"``), so comparing the kind against the raw
                # dotted extensions (``".doc" in {".doc"}``) never matched --
                # a caller asking for ['doc'] could never accept an
                # extensionless DOC. Map through ``_EXT_TO_KIND`` so the
                # comparison is kind-vs-kind.
                #
                # ``binary_other`` is opaque (HEAD said attachment but we
                # cannot pin the kind). Accept it only when the caller did
                # not pin specific types (``requested_kinds`` empty); skip it
                # otherwise rather than guess.
                if classification == "binary_other":
                    if requested_kinds:
                        continue
                elif classification not in requested_kinds:
                    continue
                logger.info(
                    "Extensionless URL routed to download via HEAD probe: {url} (kind={kind})",
                    url=r.url,
                    kind=classification,
                )
                candidates.append(r.url)
                break

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
        extract_files: bool = False,
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
            extract_files: v1.6.10. When True, route URLs whose extension
                points to a downloadable file (PDF/XLSX/DOCX/CSV/...)
                through :meth:`WebFetcher.fetch_smart` + the binary
                extractor instead of routing them to ``download_candidates``.
                Default False preserves the v1.6.9 read-pages-only behaviour.
                Mirrors :meth:`Agent.search_and_extract` ``extract_files``.

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
                if extract_files:
                    # v1.6.11: only route extractable kinds (PDF/XLSX/DOCX/CSV)
                    # through fetch_smart + extractor. Skip videos / installers /
                    # ISOs / archives before the fetch -- the v1.6.10 I-1 guard
                    # still catches HEAD-probed extensionless binaries downstream.
                    kind = _url_ext_classification(r.url)
                    if is_extractable_binary_kind(kind):
                        allowed.append(r)
                    else:
                        download_candidates.append(r)
                        diagnostics.append(
                            FetchDiagnostic(
                                url=r.url,
                                status=FetchStatus.SUCCESS,
                                provider=r.provider,
                                block_reason="not_extractable_kind",
                            )
                        )
                else:
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

        # Fetch in parallel. v1.6.9: route through fetch_smart so
        # extensionless binary URLs (regulator dashboards etc.) get
        # fetch_binary'd instead of HTML-extracted.
        #
        # v1.6.16 REC-2: bound the fan-out with the SAME per-session
        # semaphore ``fetch_many`` added in v1.6.14 C-4. On the session
        # path, ``fetch_smart -> fetch -> _do_fetch`` creates pages via
        # ``ctx.new_page()`` directly (web_fetcher.py), bypassing the
        # BrowserManager context semaphore -- so an unbounded
        # ``asyncio.gather`` over a large ``max_pages`` opens 20+
        # concurrent navigations on one BrowserContext and reproducibly
        # crashes Chromium's renderer. Gate concurrency to
        # ``BrowserConfig.max_pages_per_session_fetch`` (the same bound
        # ``fetch_many`` uses); the ephemeral path (no ``session_id``)
        # stays bounded by ``max_contexts`` via ``BrowserManager.new_page``,
        # exactly as ``fetch_many`` leaves it. ``return_exceptions=True``
        # is preserved so a single page failure never aborts the run, and
        # task order matches ``targets`` for the ``strict=True`` zip below.
        if session_id is not None:
            sem = asyncio.Semaphore(self._config.browser.max_pages_per_session_fetch)

            async def _gated_fetch(target_url: str) -> FetchResult:
                async with sem:
                    return await self._fetcher.fetch_smart(target_url, session_id=session_id)

            fetch_tasks = [_gated_fetch(r.url) for r in targets]
        else:
            fetch_tasks = [self._fetcher.fetch_smart(r.url, session_id=session_id) for r in targets]
        fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        citations: list[Citation] = []
        summary_pages: list[ExtractionResult] = []

        for item, fr in zip(targets, fetch_results, strict=True):
            if isinstance(fr, asyncio.CancelledError):
                # v1.6.14 E-4: never swallow cancellation. gather(
                # return_exceptions=True) hands back a CancelledError as a
                # result object; treating it as a generic "fetch error"
                # would mask a real task cancellation and let web_research
                # keep running (and holding browser resources) after the
                # caller cancelled. Re-raise so cancellation propagates.
                raise fr
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

            # v1.6.10: ``fetch_smart`` can return a successful binary
            # FetchResult (extensionless PDF, regulator dashboard, ...);
            # gating on ``fr.html`` alone dropped those silently as
            # fetch_failed. Accept either an HTML or binary payload.
            if fr.status != FetchStatus.SUCCESS or not (fr.html or fr.binary):
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

            extracted = await self._extractor.extract_async(fr)
            extracted.correlation_id = get_correlation_id()

            # v1.6.10 review I-1 fix: a binary FetchResult of an
            # unrecognized kind (PPTX, ZIP, octet-stream) makes
            # ``ContentExtractor.extract`` return
            # ``extraction_method='none' / content_length=0``. Without
            # this guard we'd silently append a contentless Citation
            # and pass the budget check (0 chars). Surface a
            # diagnostic + skip instead so the caller can see why an
            # otherwise-successful fetch produced no content.
            if (
                fr.binary is not None
                and extracted.extraction_method == "none"
                and extracted.content_length == 0
            ):
                bag.warn(
                    "binary_not_extracted",
                    (
                        f"Binary fetch succeeded but no extractor recognized "
                        f"the content ({fr.content_type or 'no content-type'}): "
                        f"{item.url}"
                    ),
                    url=item.url,
                )
                diagnostics.append(
                    FetchDiagnostic(
                        url=item.url,
                        final_url=fr.final_url,
                        status=fr.status,
                        status_code=fr.status_code,
                        provider=item.provider,
                        block_reason="binary_not_extracted",
                        content_length=0,
                        response_time_ms=fr.response_time_ms,
                        from_cache=fr.from_cache,
                    )
                )
                continue

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
            v1.7.0: every failure exit also populates ``failure_stage``
            ('navigation' | 'query_fill' | 'filter_fill' | 'submit' |
            'wait_for' | 'ssrf_redirect' | 'capture') and an actionable
            ``error_message`` naming the failed selector/stage, so callers
            can self-correct instead of guessing at opaque emptiness.
        """
        if self._bm is None:
            raise RuntimeError(
                "Recipes.fill_form_and_extract requires a BrowserManager; "
                "construct Recipes via Agent (which wires it for you)."
            )
        if not check_domain_allowed(url, self._config.safety):
            logger.warning("fill_form_and_extract: domain blocked: {url}", url=url)
            return ExtractionResult(
                url=url,
                extraction_method="none",
                fetch_status="blocked",
                failure_stage="navigation",
                error_message=(
                    f"domain of {url} is blocked by the SafetyConfig allow/deny "
                    "rules; do not retry this URL -- choose a source on an "
                    "allowed domain"
                ),
                correlation_id=get_correlation_id(),
            )

        # Late import to avoid a circular dep through agent.py.
        from playwright.async_api import Page
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        from .browser_actions import _resolve_locator

        timeout_ms = spec.wait_timeout_ms

        async def _drive(page: Page) -> ExtractionResult:
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except PlaywrightTimeout:
                logger.warning("fill_form_and_extract: navigation timed out for {url}", url=url)
                return ExtractionResult(
                    url=url,
                    extraction_method="none",
                    fetch_status="timeout",
                    failure_stage="navigation",
                    error_message=(
                        f"navigation to {url} timed out after {timeout_ms}ms; "
                        "the site may be slow or blocking automation -- retry "
                        "with a larger spec.wait_timeout_ms, or verify the URL "
                        "loads via web_fetch first"
                    ),
                    correlation_id=get_correlation_id(),
                )

            # v1.6.16 REC-1: this recipe drives a raw Playwright page, so the
            # SSRF re-checks that ``WebFetcher._navigate_and_extract`` performs
            # after ``goto`` (web_fetcher.py) must be repeated here -- the
            # up-front ``check_domain_allowed(url)`` gate above cannot see a
            # post-navigation 3xx / meta-refresh redirect to an internal host
            # or a DNS rebind to a private peer (cloud metadata, RFC1918,
            # loopback). Re-validate BOTH the client-side final URL and the
            # navigation Response URL, then re-check the ACTUAL connected
            # peer IP. On any violation, fail the same way a blocked domain
            # does (extraction_method="none") so internal content is never
            # filled into / extracted from.
            final_url = page.url
            response_url = response.url if response is not None else None
            for candidate in (final_url, response_url):
                if (
                    isinstance(candidate, str)
                    and candidate
                    and candidate != url
                    and not check_domain_allowed(candidate, self._config.safety)
                ):
                    logger.warning(
                        "fill_form_and_extract: navigation redirected to "
                        "disallowed URL {c} (from {url})",
                        c=candidate,
                        url=url,
                    )
                    return ExtractionResult(
                        url=url,
                        extraction_method="none",
                        fetch_status="blocked",
                        failure_stage="ssrf_redirect",
                        error_message=(
                            f"navigation redirected to a disallowed URL "
                            f"({candidate}); the redirect target is outside the "
                            "allowed domains -- do not retry this URL; choose a "
                            "different source"
                        ),
                        correlation_id=get_correlation_id(),
                    )
            if getattr(self._config.safety, "block_private_ips", False) and (
                await _response_peer_is_private(response)
            ):
                logger.warning(
                    "fill_form_and_extract: navigation connected to a private/"
                    "loopback/link-local peer for {url} (post-connect "
                    "DNS-rebinding guard)",
                    url=url,
                )
                return ExtractionResult(
                    url=url,
                    extraction_method="none",
                    fetch_status="blocked",
                    failure_stage="ssrf_redirect",
                    error_message=(
                        "navigation connected to a private/loopback/link-local "
                        "address (SSRF guard); do not retry this URL"
                    ),
                    correlation_id=get_correlation_id(),
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
                        failure_stage="query_fill",
                        error_message=(
                            f"query selector {_selector_desc(spec.query_selector)} "
                            f"could not be filled within {timeout_ms}ms "
                            f"({exc}); inspect the page with web_observe and "
                            "retry with a corrected selector"
                        ),
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
                        failure_stage="filter_fill",
                        error_message=(
                            f"filter selector {_selector_desc(selector)} failed "
                            f"for value {value!r} ({exc}); verify the control "
                            "exists and is visible with web_observe, then retry "
                            "with a corrected selector or value"
                        ),
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
                submit_desc = (
                    f"submit selector {_selector_desc(spec.submit_selector)} could not be clicked"
                    if spec.submit_selector is not None
                    else (
                        f"pressing Enter on query selector "
                        f"{_selector_desc(spec.query_selector)} failed"
                    )
                )
                return ExtractionResult(
                    url=url,
                    extraction_method="none",
                    failure_stage="submit",
                    error_message=(
                        f"{submit_desc} within {timeout_ms}ms ({exc}); inspect "
                        "the page with web_observe and retry with a corrected "
                        "submit_selector"
                    ),
                    correlation_id=get_correlation_id(),
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
                wait_desc = (
                    f"wait_for selector {_selector_desc(spec.wait_for)} did not "
                    f"become visible within {timeout_ms}ms; the form may not "
                    "have produced results -- verify the selector with "
                    "web_observe or increase spec.wait_timeout_ms"
                    if spec.wait_for is not None
                    else (
                        f"page did not reach network-idle within {timeout_ms}ms "
                        "after submit; results may still have loaded -- retry "
                        "with an explicit spec.wait_for selector for the "
                        "results container"
                    )
                )
                return ExtractionResult(
                    url=url,
                    extraction_method="none",
                    failure_stage="wait_for",
                    error_message=wait_desc,
                    correlation_id=get_correlation_id(),
                )

            # v1.6.16 H1: re-gate the POST-SUBMIT URL before extraction.
            # The form submission in step 4 is itself a navigation that can
            # 302 / JS-redirect to an internal host (SSO/SSO-rebind flows,
            # attacker forms that POST elsewhere). The initial-nav re-check
            # above (around the first goto) does NOT cover this later
            # navigation, so without this gate content from a denied/private
            # host would be captured by safe_page_content below. ``check_
            # domain_allowed(page.url, ...)`` with ``block_private_ips=True``
            # already rejects a ``page.url`` that is a private/loopback/
            # link-local IP literal (e.g. http://169.254.169.254/), closing
            # the main exposure. A full post-connect peer-IP re-check on the
            # SUBMIT navigation is intentionally out of scope: it would
            # require wrapping the submit in ``page.expect_navigation()``,
            # which breaks in-place (SPA) forms that never navigate.
            post_submit_url = page.url
            if not check_domain_allowed(post_submit_url, self._config.safety):
                logger.warning(
                    "fill_form_and_extract: post-submit navigation to disallowed URL {u}",
                    u=post_submit_url,
                )
                return ExtractionResult(
                    url=url,
                    extraction_method="none",
                    fetch_status="blocked",
                    failure_stage="ssrf_redirect",
                    error_message=(
                        f"form submission navigated to a disallowed URL "
                        f"({post_submit_url}); content was not extracted -- the "
                        "form posts outside the allowed domains; do not retry "
                        "this URL"
                    ),
                    correlation_id=get_correlation_id(),
                )

            # Step 6: extract
            # v1.6.13: 3-tier safe capture so a mid-navigation race
            # (very common on form-submit flows that trigger a redirect)
            # doesn't blow up extraction. Source is propagated to the
            # FetchResult so the downstream extractor + telemetry can see
            # whether we hit the degraded path.
            html, html_source = await safe_page_content(page)
            final_url = page.url
            if html_source == "navigating":
                # v1.6.14 C-6: all 3 capture tiers failed -- this is a
                # transport-level capture failure (matches downloader.py
                # NETWORK_ERROR pattern in ``_do_save_page``), NOT a
                # successful fetch with empty content. Returning a
                # FetchResult(status=SUCCESS, html="") would lie to the
                # caller: the downstream extractor sees ``not fr.html``
                # and emits ``extraction_method="none"`` anyway, but the
                # SUCCESS wrapper hides the fact that this was a capture
                # failure (form may have succeeded, but the post-submit
                # page never settled). Short-circuit with
                # ``extraction_method="none"`` and ``content_length=0`` so
                # the caller can distinguish "form worked, page has no
                # content" from "navigation race killed extraction".
                logger.warning(
                    "fill_form_and_extract: page.content() abandoned after all "
                    "tiers for {url}; returning extraction_method='none'",
                    url=url,
                )
                return ExtractionResult(
                    url=url,
                    extraction_method="none",
                    content_length=0,
                    failure_stage="capture",
                    error_message=(
                        "post-submit page content could not be captured (page "
                        "kept navigating through all capture tiers); the form "
                        "may have submitted successfully -- retry once, or add "
                        "a spec.wait_for selector for a stable results element"
                    ),
                    correlation_id=get_correlation_id(),
                )
            fr = FetchResult(
                url=url,
                final_url=final_url,
                status=FetchStatus.SUCCESS,
                html=html,
                html_capture_source=html_source,
                correlation_id=get_correlation_id(),
            )
            extracted = await self._extractor.extract_async(fr)
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

    # ------------------------------------------------------------------
    # Recipe 5: collect_across_pages (v1.7.0 Wave 3B)
    # ------------------------------------------------------------------

    @staticmethod
    def _content_hash(text: str) -> str:
        """Stable hash of extracted text for content-level dedup."""
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def _bump_page_param(url: str) -> Optional[str]:
        """Increment a ``?page=`` / ``?p=`` query param; None if absent.

        Only the FIRST recognized param is bumped. Returns None when neither
        ``page`` nor ``p`` is present (the caller stops the walk), so we
        never guess a starting page for a URL that doesn't paginate by
        query string.
        """
        parsed = urlparse(url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        target = None
        for i, (k, v) in enumerate(pairs):
            if k.lower() in ("page", "p") and v.isdigit():
                target = i
                break
        if target is None:
            return None
        k, v = pairs[target]
        pairs[target] = (k, str(int(v) + 1))
        new_query = urlencode(pairs)
        return urlunparse(parsed._replace(query=new_query))

    async def collect_across_pages(
        self,
        url: str,
        *,
        strategy: str = "next_link",
        max_pages: Optional[int] = None,
        session_id: Optional[str] = None,
        next_texts: Optional[list[str]] = None,
        settle_ms: Optional[int] = None,
        stable_rounds: Optional[int] = None,
        max_scrolls: Optional[int] = None,
    ) -> CollectionResult:
        """Walk a multi-page listing and assemble the extracted content.

        Bounded by ``max_pages`` (clamped to
        ``automation.pagination_max_pages``) and the per-call budget
        (``SafetyConfig.max_pages_per_call`` / ``max_chars_per_call`` /
        ``max_time_per_call_seconds``). Pages are DEDUPLICATED by URL and
        by content hash, so a "next" control that loops never double-counts.

        SAFETY GATES DIFFER BY STRATEGY. The ``"next_link"`` and
        ``"page_param"`` strategies fetch every page through the injected
        ``WebFetcher.fetch``, so v1.7.0 challenge detection,
        injection-sanitize, **robots obedience, per-host rate limiting**, and
        SSRF re-gating all apply to every navigation. The ``"scroll"``
        strategy does NOT: it drives a raw ``page.goto`` on the session tab
        (the scroll assembly needs a live tab, not a fetch), so it applies
        only an SSRF re-gate (post-navigation ``check_domain_allowed`` +
        deny-list) and the injection sanitize/scan baked into
        ``ContentExtractor`` -- **robots.txt and rate limiting are NOT
        consulted** for the scroll navigation. Use ``"scroll"`` only on a URL
        you are already authorized to crawl interactively.

        Strategies:
          - ``"scroll"``: single URL. Uses scroll-to-exhaustion
            (:meth:`BrowserActions.scroll_to_bottom`) on a session tab to
            assemble all lazy/infinite-scroll content, then extracts the
            page once. Raw navigation (SSRF + injection only -- see the
            safety-gates note above). Requires ``session_id`` and an injected
            ``BrowserActions``.
          - ``"next_link"``: extract a page, find the "next" control (a
            ``rel=next`` link, an ``aria-label*=next`` link, or an ``<a>``
            whose text/aria matches ``next_texts``), navigate to it, repeat.
            Stops on no next control, a revisited URL (cycle guard),
            ``max_pages``, or budget exhaustion. A page emptied by
            ``injection_action='block'`` is flagged (``CollectedPage.
            blocked_reason='injection_blocked'``) and the walk CONTINUES --
            it is not treated as end-of-listing.
          - ``"page_param"``: increment a ``?page=`` / ``?p=`` query param
            until an empty or duplicate page, ``max_pages``, or budget. A page
            emptied by ``injection_action='block'`` is flagged and skipped
            WITHOUT halting (only a genuinely empty page terminates).

        Args:
            url: The listing URL to start from.
            strategy: ``"scroll"`` | ``"next_link"`` | ``"page_param"``.
            max_pages: Page cap (clamped to ``automation.pagination_max_pages``).
            session_id: Persistent session. Required for ``"scroll"``;
                threaded through ``fetch`` for the link/param strategies.
            next_texts: Override the ``next_link`` control vocabulary
                (defaults to ``automation.pagination_next_texts``).
            settle_ms / stable_rounds / max_scrolls: Forwarded to
                ``scroll_to_bottom`` for the ``"scroll"`` strategy.

        Returns:
            CollectionResult with per-page content, counts, a
            ``stopped_reason``, diagnostics, warnings, and budget telemetry.
        """
        from .agent import _block_reason_for, _MessageBag

        start = time.perf_counter()
        bag = _MessageBag()
        diagnostics: list[FetchDiagnostic] = []
        pages: list[CollectedPage] = []
        budget = BudgetTracker(self._config.safety)

        page_cap = self._config.automation.pagination_max_pages
        if max_pages is None:
            max_pages = page_cap
        # Clamp to the configured ceiling so a large per-call value can't
        # make the walk unbounded.
        max_pages = max(1, min(int(max_pages), page_cap))

        logger.info(
            "Recipe: collect_across_pages url={u} strategy={s} max_pages={n}",
            u=url,
            s=strategy,
            n=max_pages,
        )

        def _finish(stopped_reason: str) -> CollectionResult:
            max_risk, n_with_injection = self._injection_rollup(pages)
            return CollectionResult(
                start_url=url,
                strategy=strategy,
                pages=pages,
                pages_collected=len(pages),
                total_content_length=sum(p.content_length for p in pages),
                max_injection_risk=max_risk,
                pages_with_injection=n_with_injection,
                stopped_reason=stopped_reason,
                errors=bag.errors,
                warnings=bag.warnings,
                diagnostics=diagnostics,
                structured_warnings=bag.structured_warnings,
                structured_errors=bag.structured_errors,
                correlation_id=get_correlation_id(),
                total_time_ms=(time.perf_counter() - start) * 1000,
            )

        if not check_domain_allowed(url, self._config.safety):
            bag.err("domain_blocked", f"Start URL domain is blocked: {url}", url=url)
            diagnostics.append(
                FetchDiagnostic(url=url, status=FetchStatus.BLOCKED, block_reason="domain_blocked")
            )
            return _finish("blocked")

        if strategy == "scroll":
            return await self._collect_scroll(
                url,
                session_id=session_id,
                settle_ms=settle_ms,
                stable_rounds=stable_rounds,
                max_scrolls=max_scrolls,
                bag=bag,
                diagnostics=diagnostics,
                pages=pages,
                budget=budget,
                finish=_finish,
            )
        if strategy not in ("next_link", "page_param"):
            bag.err(
                "unknown_strategy",
                f"Unknown collection strategy {strategy!r}; "
                "expected 'scroll' | 'next_link' | 'page_param'",
            )
            return _finish("error")

        # --- next_link / page_param: a paginated fetch-and-extract walk ---
        visited_urls: set[str] = set()
        seen_hashes: set[str] = set()
        vocabulary = next_texts or self._config.automation.pagination_next_texts
        current: Optional[str] = url
        stopped_reason = "no_next"

        while current is not None:
            if len(pages) >= max_pages:
                stopped_reason = "max_pages"
                break
            # Cycle guard: a next control that loops back to a visited URL
            # (or a page_param that wraps) must not re-collect.
            if current in visited_urls:
                stopped_reason = "cycle"
                break
            # Re-gate every navigation target (defense-in-depth on top of the
            # gate inside fetch()).
            if not check_domain_allowed(current, self._config.safety):
                bag.warn("domain_blocked", f"Pagination target blocked: {current}", url=current)
                diagnostics.append(
                    FetchDiagnostic(
                        url=current, status=FetchStatus.BLOCKED, block_reason="domain_blocked"
                    )
                )
                stopped_reason = "blocked"
                break
            try:
                budget.check_time()
                budget.add_page()
            except Exception as exc:
                bag.err("budget_exceeded", str(exc))
                stopped_reason = "budget"
                break

            visited_urls.add(current)
            # Go through fetch() (NOT a raw goto) so robots, rate limiting,
            # challenge detection, injection-sanitize, and SSRF re-gating all
            # apply to every page automatically.
            fr = await self._fetcher.fetch(current, session_id=session_id)
            if fr.status != FetchStatus.SUCCESS or not fr.html:
                bag.warn(
                    "fetch_failed",
                    f"Failed to fetch {current}: {fr.error_message}",
                    url=current,
                )
                diagnostics.append(
                    FetchDiagnostic(
                        url=current,
                        final_url=fr.final_url,
                        status=fr.status,
                        status_code=fr.status_code,
                        block_reason=_block_reason_for(fr),
                        response_time_ms=fr.response_time_ms,
                        from_cache=fr.from_cache,
                    )
                )
                stopped_reason = "blocked" if fr.status == FetchStatus.BLOCKED else "error"
                break

            extracted = await self._extractor.extract_async(fr)
            content = extracted.content or ""

            # B4: a page emptied by injection_action='block' (HIGH-risk scan)
            # is NOT end-of-listing -- its content is withheld BY POLICY, not
            # because the listing ran out. Detect it BEFORE the empty/dup
            # checks below (which would otherwise read its empty content as a
            # page_param 'empty_page' terminator or a duplicate). Record a
            # warning + diagnostic, append the page flagged (carrying the
            # injection report from B3 so the caller sees WHY it's blank), and
            # keep walking: page_param advances to the next page; next_link
            # still follows the page's "next" control.
            if self._is_injection_blocked(extracted):
                bag.warn(
                    "injection_blocked",
                    (
                        f"Page content withheld by injection_action='block' "
                        f"(HIGH-risk prompt-injection scan): {current}; the "
                        "page was flagged, not treated as end-of-listing"
                    ),
                    url=current,
                )
                self._append_page(
                    pages, current, fr, extracted, blocked_reason="injection_blocked"
                )
                diagnostics.append(
                    self._page_diagnostic(
                        current, fr, extracted, block_reason="injection_blocked"
                    )
                )
                if strategy == "page_param":
                    current = self._bump_page_param(current)
                    if current is None:
                        stopped_reason = "no_next"
                else:
                    current = self._find_next_link(fr, list(vocabulary))
                    if current is None:
                        stopped_reason = "no_next"
                continue

            chash = self._content_hash(content)
            # Content-level dedup: a different URL that renders identical
            # content (e.g. a "next" that silently clamps at the last page)
            # is not collected twice; for page_param this is the empty/dup
            # stop signal.
            if content and chash in seen_hashes:
                if strategy == "page_param":
                    stopped_reason = "empty_page"
                else:
                    bag.warn(
                        "duplicate_page",
                        f"Skipped duplicate page content: {current}",
                        url=current,
                    )
                    stopped_reason = "cycle"
                break
            # page_param: an empty page is the natural end of the listing.
            if strategy == "page_param" and not content:
                diagnostics.append(
                    FetchDiagnostic(
                        url=current,
                        final_url=fr.final_url,
                        status=fr.status,
                        status_code=fr.status_code,
                        content_length=0,
                        response_time_ms=fr.response_time_ms,
                        from_cache=fr.from_cache,
                    )
                )
                stopped_reason = "empty_page"
                break

            if content:
                seen_hashes.add(chash)
            try:
                budget.add_chars(extracted.content_length)
            except Exception as exc:
                bag.err("budget_exceeded", str(exc))
                self._append_page(pages, current, fr, extracted)
                diagnostics.append(
                    self._page_diagnostic(current, fr, extracted)
                )
                stopped_reason = "budget"
                break

            self._append_page(pages, current, fr, extracted)
            diagnostics.append(self._page_diagnostic(current, fr, extracted))

            # Find the next target.
            if strategy == "page_param":
                current = self._bump_page_param(current)
                if current is None:
                    stopped_reason = "no_next"
            else:
                current = self._find_next_link(fr, list(vocabulary))
                if current is None:
                    stopped_reason = "no_next"

        return _finish(stopped_reason)

    @staticmethod
    def _is_injection_blocked(extracted: ExtractionResult) -> bool:
        """B4: did ``injection_action='block'`` empty this page's content?

        The content extractor stamps ``failure_stage='injection_blocked'`` and
        empties ``content`` when a HIGH-risk injection scan fires under
        ``SafetyConfig.injection_action='block'``. We treat that as the
        authoritative signal, with a defensive fallback to (HIGH report +
        empty content) in case the stage marker is ever absent. This is
        distinct from a genuinely empty page (no report / non-HIGH risk),
        which must still terminate a ``page_param`` walk.
        """
        if extracted.failure_stage == "injection_blocked":
            return True
        report = extracted.injection
        return (
            report is not None
            and report.risk == "high"
            and not (extracted.content or "")
        )

    @staticmethod
    def _injection_rollup(pages: list[CollectedPage]) -> tuple[Optional[str], int]:
        """Roll the per-page injection reports up to a collection-level summary.

        Returns ``(max_injection_risk, pages_with_injection)`` where the max
        is the highest ``risk`` across pages that carry a report (ordered
        none < low < medium < high), or ``None`` when no page carried one
        (detection disabled). ``pages_with_injection`` counts pages scoring
        above 'none'.
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

    @staticmethod
    def _append_page(
        pages: list[CollectedPage],
        url: str,
        fr: FetchResult,
        extracted: ExtractionResult,
        *,
        blocked_reason: Optional[str] = None,
    ) -> None:
        pages.append(
            CollectedPage(
                url=url,
                final_url=fr.final_url if fr.final_url != url else None,
                title=extracted.title,
                content=extracted.content or "",
                content_length=extracted.content_length,
                extraction_method=extracted.extraction_method,
                injection=extracted.injection,
                blocked_reason=blocked_reason,
            )
        )

    @staticmethod
    def _page_diagnostic(
        url: str,
        fr: FetchResult,
        extracted: ExtractionResult,
        *,
        block_reason: Optional[str] = None,
    ) -> FetchDiagnostic:
        return FetchDiagnostic(
            url=url,
            final_url=fr.final_url,
            status=fr.status,
            status_code=fr.status_code,
            block_reason=block_reason,
            content_length=extracted.content_length,
            response_time_ms=fr.response_time_ms,
            from_cache=fr.from_cache,
        )

    @staticmethod
    def _find_next_link(fr: FetchResult, vocabulary: list[str]) -> Optional[str]:
        """Resolve the 'next' control's href from the just-fetched page.

        Parses the fetched HTML with BeautifulSoup (no second navigation):
        rel=next, then aria-label*=next/older, then a text/aria substring
        match against ``vocabulary``. Relative hrefs are resolved against
        the page's final URL. Only same-result-shape hrefs (http/https) are
        returned; javascript:/#-only anchors are skipped.
        """
        from urllib.parse import urljoin

        from bs4 import BeautifulSoup, Tag

        if not fr.html:
            return None
        base = fr.final_url or fr.url
        soup = BeautifulSoup(fr.html, "html.parser")
        wanted = [w.strip().lower() for w in vocabulary if w.strip()]

        def _href(tag: Tag) -> Optional[str]:
            raw = tag.get("href")
            if not isinstance(raw, str) or not raw:
                return None
            if raw.startswith(("javascript:", "#", "mailto:")):
                return None
            resolved = urljoin(base, raw)
            scheme = urlparse(resolved).scheme
            return resolved if scheme in ("http", "https") else None

        # 1. rel=next
        for a in soup.find_all("a", rel=True):
            if not isinstance(a, Tag):
                continue
            rel = a.get("rel")
            rel_tokens = rel if isinstance(rel, list) else [rel]
            if any(isinstance(t, str) and t.lower() == "next" for t in rel_tokens):
                href = _href(a)
                if href:
                    return href

        # 2. + 3. aria-label / text substring match
        for a in soup.find_all("a"):
            if not isinstance(a, Tag):
                continue
            aria = a.get("aria-label")
            label = (aria if isinstance(aria, str) else "") + " " + a.get_text()
            label_l = label.lower()
            if any(w in label_l for w in wanted):
                href = _href(a)
                if href:
                    return href
        return None

    async def _collect_scroll(
        self,
        url: str,
        *,
        session_id: Optional[str],
        settle_ms: Optional[int],
        stable_rounds: Optional[int],
        max_scrolls: Optional[int],
        bag: _MessageBagT,
        diagnostics: list[FetchDiagnostic],
        pages: list[CollectedPage],
        budget: BudgetTracker,
        finish: _FinishT,
    ) -> CollectionResult:
        """``"scroll"`` strategy: scroll-to-exhaustion then extract once.

        Navigates the session's current tab to ``url`` via a RAW
        ``page.goto`` (the scroll assembly needs a live tab, not a fetch),
        drives :meth:`BrowserActions.scroll_to_bottom` so the full assembled
        DOM (all lazy / infinite-scroll content) materializes, then captures
        and extracts the page once. Requires ``session_id`` (the scroll drives
        a persistent tab) and an injected ``BrowserActions``.

        SAFETY GATES: because this is a raw navigation rather than a
        ``WebFetcher.fetch``, only the SSRF re-gate below (post-navigation
        ``check_domain_allowed`` + deny-list) and the injection
        sanitize/scan inside ``ContentExtractor`` apply. **robots.txt
        obedience and per-host rate limiting are NOT consulted here** --
        ``Recipes`` has no handle on the ``RobotsChecker`` / ``RateLimiter``
        (both are encapsulated inside ``WebFetcher``), so the scroll path
        cannot acquire them without reaching across a module boundary. The
        ``"next_link"`` / ``"page_param"`` strategies, which DO route through
        ``fetch``, get the full gate set. This is documented honestly rather
        than silently implied.
        """
        if session_id is None or self._sessions is None:
            bag.err(
                "scroll_requires_session",
                "strategy='scroll' requires a session_id (scroll-to-exhaustion "
                "drives a persistent session tab)",
                url=url,
            )
            return finish("error")
        if self._actions is None:
            bag.err(
                "scroll_unavailable",
                "strategy='scroll' requires an injected BrowserActions "
                "(construct Recipes via Agent, which wires it)",
                url=url,
            )
            return finish("error")

        try:
            budget.check_time()
            budget.add_page()
        except Exception as exc:
            bag.err("budget_exceeded", str(exc))
            return finish("budget")

        # The session already has a current tab (SessionManager.create
        # registers one). Navigate it to the target URL, then scroll. The
        # up-front check_domain_allowed(url) in collect_across_pages already
        # gated the start URL; we re-gate the POST-navigation URL below.
        self._sessions.touch(session_id)
        tab_mgr = self._sessions.get_tab_manager(session_id)
        page = tab_mgr.get_or_current(None)
        if page is None:
            ctx = self._sessions.get(session_id)
            page = await ctx.new_page()
            await tab_mgr.register_initial_page(page)

        await page.goto(url, wait_until="domcontentloaded")
        # Post-nav SSRF re-gate (the goto may have redirected to an internal
        # host); never scroll/extract content off a disallowed page.
        if not check_domain_allowed(page.url, self._config.safety):
            bag.warn("ssrf_redirect", f"Scroll nav redirected off-domain: {page.url}", url=url)
            diagnostics.append(
                FetchDiagnostic(
                    url=url,
                    final_url=page.url,
                    status=FetchStatus.BLOCKED,
                    block_reason="domain_blocked",
                )
            )
            return finish("blocked")

        scroll_res = await self._actions.scroll_to_bottom(
            session_id=session_id,
            settle_ms=settle_ms,
            stable_rounds=stable_rounds,
            max_scrolls=max_scrolls,
        )
        if scroll_res.status != ActionStatus.SUCCESS:
            bag.warn(
                "scroll_failed",
                f"scroll_to_bottom did not succeed: {scroll_res.error_message}",
                url=url,
            )

        # Capture the assembled DOM and extract. safe_page_content is the
        # 3-tier mid-navigation-safe capture used elsewhere in this module.
        html, html_source = await safe_page_content(page)
        if html_source == "navigating" or not html:
            bag.err(
                "capture_failed",
                "assembled page content could not be captured after scrolling",
                url=url,
            )
            diagnostics.append(
                FetchDiagnostic(url=url, final_url=page.url, status=FetchStatus.NETWORK_ERROR)
            )
            return finish("error")

        fr = FetchResult(
            url=url,
            final_url=page.url,
            status=FetchStatus.SUCCESS,
            html=html,
            html_capture_source=html_source,
            correlation_id=get_correlation_id(),
        )
        extracted = await self._extractor.extract_async(fr)
        try:
            budget.add_chars(extracted.content_length)
        except Exception as exc:
            bag.err("budget_exceeded", str(exc))
        self._append_page(pages, url, fr, extracted)
        diagnostics.append(self._page_diagnostic(url, fr, extracted))
        return finish("scroll_complete")
