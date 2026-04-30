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

from .config import AppConfig
from .content_extractor import ContentExtractor
from .correlation import correlation_scope, get_correlation_id
from .downloader import Downloader
from .models import (
    Citation,
    DownloadResult,
    ExtractionResult,
    FetchStatus,
    ResearchResult,
    SearchResultItem,
)
from .search_engine import SearchEngine
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


class Recipes:
    """Composite high-level workflows over the existing web_agent primitives.

    Args:
        search: SearchEngine for query execution.
        fetcher: WebFetcher for page fetching.
        extractor: ContentExtractor for content extraction.
        downloader: Downloader for file downloads.
        config: AppConfig for budget/safety configuration.
    """

    def __init__(
        self,
        search: SearchEngine,
        fetcher: WebFetcher,
        extractor: ContentExtractor,
        downloader: Downloader,
        config: AppConfig,
    ) -> None:
        self._search = search
        self._fetcher = fetcher
        self._extractor = extractor
        self._downloader = downloader
        self._config = config

    # ------------------------------------------------------------------
    # Ranking
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Lower-case word tokens with length >= 2."""
        return {tok for tok in re.findall(r"[a-zA-Z0-9]+", text.lower()) if len(tok) >= 2}

    @staticmethod
    def _rank(query: str, item: SearchResultItem, scheme: str = "default") -> float:
        """Score a search result.

        Schemes:
            ``default``: query-token overlap + HTTPS bonus + well-known domain bonus + position tiebreaker
            ``overlap``: only token overlap
            ``position``: only inverse position
        """
        if scheme == "position":
            return 1.0 / max(1, item.position)

        q_toks = Recipes._tokenize(query)
        r_toks = Recipes._tokenize(f"{item.title} {item.snippet} {item.displayed_url}")
        overlap = (
            len(q_toks & r_toks) / max(1, len(q_toks)) if q_toks else 0.0
        )
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
            # Small penalty for very deep subdomains
            subdomain_depth = host.count(".")
            if subdomain_depth > 2:
                score -= 0.10
        except Exception:  # noqa: BLE001
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
    ) -> ExtractionResult:
        """Search for ``query``, rank results, fetch+extract the top hit.

        Args:
            query: The search query.
            ranking: Ranking scheme (``default`` | ``overlap`` | ``position``).
            session_id: Optional persistent browser session for the fetch.

        Returns:
            ExtractionResult for the top-ranked URL. If all URLs are blocked
            or fail, returns an empty ExtractionResult with ``extraction_method="none"``.
        """
        logger.info("Recipe: search_and_open_best_result for {q}", q=query)
        search_resp = await self._search.search(query, max_results=10)

        # Filter denied domains BEFORE ranking
        allowed = [
            r
            for r in search_resp.results
            if check_domain_allowed(r.url, self._config.safety)
        ]

        if not allowed:
            return ExtractionResult(
                url="",
                extraction_method="none",
                correlation_id=get_correlation_id(),
            )

        ranked = sorted(
            allowed,
            key=lambda r: self._rank(query, r, ranking),
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
                if _is_download_url(r.url)
                and check_domain_allowed(r.url, self._config.safety)
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
            if "/" in path:
                last = path.rsplit("/", 1)[-1]
            else:
                last = path
            if "." in last:
                return "." + last.rsplit(".", 1)[-1]
        except Exception:  # noqa: BLE001
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
    ) -> ResearchResult:
        """Search and extract content from the top N pages, returning structured citations.

        Args:
            query: Research question / topic.
            depth: Reserved for future expansion. v1 supports depth=1 only.
            max_pages: Maximum number of pages to fetch and extract.
            session_id: Optional persistent browser session.

        Returns:
            ResearchResult with citations, summary pages, budget telemetry, and errors.
        """
        start = time.perf_counter()
        errors: list[str] = []
        budget = BudgetTracker(self._config.safety)

        if depth != 1:
            logger.warning(
                "web_research depth={d} requested but only depth=1 supported in v1",
                d=depth,
            )

        logger.info(
            "Recipe: web_research query={q} max_pages={n}", q=query, n=max_pages
        )

        # Pull more results than needed so ranking + filtering have headroom
        search_resp = await self._search.search(
            query, max_results=max(max_pages * 2, 10)
        )

        # Filter denied domains, skip download URLs (research is about reading pages)
        allowed: list[SearchResultItem] = []
        for r in search_resp.results:
            if not check_domain_allowed(r.url, self._config.safety):
                errors.append(f"Domain denied: {r.url}")
                continue
            if _is_download_url(r.url):
                continue
            allowed.append(r)

        if not allowed:
            return ResearchResult(
                query=query,
                errors=errors or ["No allowed pages in search results"],
                correlation_id=get_correlation_id(),
                total_time_ms=(time.perf_counter() - start) * 1000,
            )

        # Cache scores so we don't re-tokenize per item during sort + citation build
        scores: dict[str, float] = {
            r.url: self._rank(query, r) for r in allowed
        }
        ranked = sorted(allowed, key=lambda r: scores[r.url], reverse=True)
        targets = ranked[:max_pages]

        # Fetch in parallel (bounded by BrowserManager semaphore inside fetcher)
        fetch_tasks = [self._fetcher.fetch(r.url, session_id=session_id) for r in targets]
        fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        citations: list[Citation] = []
        summary_pages: list[ExtractionResult] = []

        for item, fr in zip(targets, fetch_results):
            if isinstance(fr, BaseException):
                errors.append(f"Fetch raised for {item.url}: {fr}")
                continue
            try:
                budget.check_time()
                budget.add_page()
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                break

            if fr.status != FetchStatus.SUCCESS or not fr.html:
                errors.append(f"Failed to fetch {item.url}: {fr.error_message}")
                continue

            extracted = self._extractor.extract(fr)
            extracted.correlation_id = get_correlation_id()

            try:
                budget.add_chars(extracted.content_length)
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                summary_pages.append(extracted)
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
            citations.append(
                Citation(
                    url=item.url,
                    title=extracted.title or item.title,
                    snippet=item.snippet,
                    extraction_method=extracted.extraction_method,
                    relevance_score=scores[item.url],
                )
            )

        elapsed = (time.perf_counter() - start) * 1000
        return ResearchResult(
            query=query,
            citations=citations,
            summary_pages=summary_pages,
            pages_visited=budget.pages_used,
            chars_extracted=budget.chars_used,
            errors=errors,
            correlation_id=get_correlation_id(),
            total_time_ms=elapsed,
        )
