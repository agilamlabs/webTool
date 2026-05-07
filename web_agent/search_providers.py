"""Search providers: SearXNG (primary), DDGS (fallback), Playwright (browser fallback).

Each provider is a single search source behind a uniform interface. The
:class:`web_agent.search_engine.SearchEngine` chains a configured list
of providers in priority order, falling through to the next on empty
results or errors. The first non-empty response wins.

Default chain (configurable via :class:`SearchConfig.providers`):

1. :class:`SearXNGProvider` -- queries a self-hosted SearXNG instance
   over its JSON API. Privacy-respecting metasearch aggregator. Silently
   skipped when ``searxng_base_url`` is not configured.
2. :class:`DDGSProvider` -- uses the ``ddgs`` package
   (formerly ``duckduckgo-search``) to query DuckDuckGo without a real
   browser. Silently skipped when the optional dependency is missing.
3. :class:`PlaywrightProvider` -- launches Chromium and scrapes Google's
   SERP (with consent + CAPTCHA detection) then DDG HTML. Slow but
   doesn't depend on third-party search APIs.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import httpx
from loguru import logger
from playwright.async_api import Page

from .browser_manager import BrowserManager
from .config import AppConfig
from .models import SearchResponse, SearchResultItem
from .rate_limiter import RateLimiter


class SearchProvider(ABC):
    """Abstract base for one search backend.

    Implementations should:
    - Set ``name`` (used in logs + error messages).
    - Override ``is_available`` if the provider has preconditions
      (config keys, optional dependencies). Default returns True.
    - Implement ``search`` to return a :class:`SearchResponse`. May
      return an empty response (chain falls through) or raise on
      transient errors (chain logs and falls through).
    """

    name: ClassVar[str] = "<unset>"

    @property
    def is_available(self) -> bool:
        """Whether this provider is usable in the current environment."""
        return True

    @abstractmethod
    async def search(self, query: str, max_results: int) -> SearchResponse: ...


# ---------------------------------------------------------------------------
# SearXNG (self-hosted JSON API)
# ---------------------------------------------------------------------------


class SearXNGProvider(SearchProvider):
    """Query a self-hosted SearXNG instance via its JSON API.

    SearXNG is a privacy-respecting metasearch engine that aggregates
    results from many backends. This provider hits ``<base_url>/search``
    with ``format=json`` and parses the result list.

    Args:
        base_url: SearXNG instance URL (e.g. ``http://localhost:8888``).
            ``None`` disables this provider (skipped in the chain).
        timeout: HTTP timeout in seconds for the JSON request.
        rate_limiter: Optional per-host rate gate, applied to the
            SearXNG host.
    """

    name: ClassVar[str] = "searxng"

    def __init__(
        self,
        base_url: str | None,
        timeout: float = 10.0,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/") if base_url else None
        self._timeout = timeout
        self._rate_limiter = rate_limiter

    @property
    def is_available(self) -> bool:
        return self._base_url is not None

    async def search(self, query: str, max_results: int) -> SearchResponse:
        if not self._base_url:
            return SearchResponse(query=query)

        host = urlparse(self._base_url).hostname or ""
        if self._rate_limiter is not None and host:
            await self._rate_limiter.acquire(host)

        params = {"q": query, "format": "json", "safesearch": "0"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                resp = await client.get(f"{self._base_url}/search", params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("SearXNG @ {u} failed: {e}", u=self._base_url, e=exc)
            return SearchResponse(query=query)

        items: list[SearchResultItem] = []
        for idx, item in enumerate(data.get("results", [])[:max_results]):
            url = item.get("url", "")
            if not url or not url.startswith(("http://", "https://")):
                continue
            items.append(
                SearchResultItem(
                    position=idx + 1,
                    title=(item.get("title") or "").strip(),
                    url=url,
                    displayed_url=urlparse(url).hostname or "",
                    snippet=(item.get("content") or "").strip(),
                    provider=self.name,
                )
            )
        logger.info("SearXNG returned {n} results", n=len(items))
        return SearchResponse(query=query, total_results=len(items), results=items)


# ---------------------------------------------------------------------------
# DDGS (ddgs / duckduckgo-search package)
# ---------------------------------------------------------------------------


class DDGSProvider(SearchProvider):
    """Search DuckDuckGo via the ``ddgs`` Python package.

    Faster than the Playwright fallback because it uses DuckDuckGo's
    public HTML/JSON endpoints directly with httpx -- no browser
    required. The library is unofficial and may break on DDG-side
    changes; we treat any exception as "fall through to next provider".

    The package import is lazy so the toolkit still works if ``ddgs``
    is not installed (this provider is silently skipped).

    Args:
        rate_limiter: Optional per-host rate gate.
    """

    name: ClassVar[str] = "ddgs"

    def __init__(self, rate_limiter: RateLimiter | None = None) -> None:
        self._rate_limiter = rate_limiter
        self._available: bool | None = None  # lazy probe cache

    @property
    def is_available(self) -> bool:
        if self._available is None:
            try:
                import ddgs  # noqa: F401

                self._available = True
            except ImportError:
                logger.debug("ddgs package not installed; DDGSProvider disabled")
                self._available = False
        return self._available

    async def search(self, query: str, max_results: int) -> SearchResponse:
        if not self.is_available:
            return SearchResponse(query=query)

        if self._rate_limiter is not None:
            await self._rate_limiter.acquire("duckduckgo.com")

        from ddgs import DDGS

        def _do_search() -> list[dict]:
            with DDGS() as client:
                return list(client.text(query, max_results=max_results))

        try:
            raw = await asyncio.to_thread(_do_search)
        except Exception as exc:
            logger.warning("DDGS failed: {e}", e=exc)
            return SearchResponse(query=query)

        items: list[SearchResultItem] = []
        for idx, item in enumerate(raw):
            url = item.get("href") or ""
            if not url.startswith(("http://", "https://")):
                continue
            items.append(
                SearchResultItem(
                    position=idx + 1,
                    title=(item.get("title") or "").strip(),
                    url=url,
                    displayed_url=urlparse(url).hostname or "",
                    snippet=(item.get("body") or "").strip(),
                    provider=self.name,
                )
            )
        logger.info("DDGS returned {n} results", n=len(items))
        return SearchResponse(query=query, total_results=len(items), results=items)


# ---------------------------------------------------------------------------
# Playwright (Google + DDG HTML, browser-driven)
# ---------------------------------------------------------------------------


class PlaywrightProvider(SearchProvider):
    """Browser-driven search: tries Google then DuckDuckGo HTML.

    Last-resort fallback when SearXNG and DDGS are unavailable / empty.
    Uses a real Chromium with stealth so it can survive most bot-
    detection that plain httpx requests would trip. Slow (~5-15s per
    search) compared to API-based providers.

    Internally:
    1. Tries the Google SERP. Detects CAPTCHA / sorry pages and bails
       early. Handles the cookie consent dialog.
    2. On empty results or block, falls through to ``html.duckduckgo.com``
       which is JS-free and scraping-friendly.
    """

    name: ClassVar[str] = "playwright"

    def __init__(
        self,
        browser_manager: BrowserManager,
        config: AppConfig,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._bm = browser_manager
        self._config = config
        self._rate_limiter = rate_limiter

    async def search(self, query: str, max_results: int) -> SearchResponse:
        # Try Google SERP first
        if self._rate_limiter is not None:
            await self._rate_limiter.acquire("www.google.com")
        result = await self._search_google(query, max_results)
        if result.results:
            return result

        logger.info("Playwright Google empty, falling back to DDG HTML")
        if self._rate_limiter is not None:
            await self._rate_limiter.acquire("html.duckduckgo.com")
        return await self._search_duckduckgo(query, max_results)

    # ------------------------------------------------------------------
    # Google SERP
    # ------------------------------------------------------------------

    async def _search_google(self, query: str, max_results: int) -> SearchResponse:
        params: dict[str, str | int] = {
            "q": query,
            "hl": self._config.search.language,
            "gl": self._config.search.region,
            "num": max_results,
        }
        if self._config.search.safe_search:
            params["safe"] = "active"

        url = f"{self._config.search.search_url}?{urlencode(params)}"
        logger.info("Searching Google (Playwright): {q}", q=query)

        try:
            async with self._bm.new_page() as page:
                await page.goto(url, wait_until="domcontentloaded")

                if await self._is_blocked(page):
                    logger.warning("Google blocked the request (CAPTCHA detected)")
                    return SearchResponse(query=query)

                await self._handle_google_consent(page)

                try:
                    await page.wait_for_selector("div#search, div#rso", timeout=15000)
                except Exception:
                    logger.warning("Google SERP selectors not found")
                    return SearchResponse(query=query)

                items = await self._parse_google_results(page, max_results)

            return SearchResponse(query=query, total_results=len(items), results=items)
        except Exception as exc:
            logger.warning("Google search failed: {e}", e=exc)
            return SearchResponse(query=query)

    async def _is_blocked(self, page: Page) -> bool:
        blocked_indicators = [
            "form#captcha-form",
            "div#recaptcha",
            "iframe[src*='recaptcha']",
        ]
        if "/sorry/" in page.url or "captcha" in page.url.lower():
            return True
        for selector in blocked_indicators:
            if await page.query_selector(selector):
                return True
        return False

    async def _handle_google_consent(self, page: Page) -> None:
        try:
            consent_btn = await page.query_selector("button#L2AGLb")
            if consent_btn:
                await consent_btn.click()
                await page.wait_for_load_state("domcontentloaded")
                logger.debug("Dismissed Google consent dialog")
                return

            consent_form = await page.query_selector("form[action*='consent']")
            if consent_form:
                accept_btn = await consent_form.query_selector("button[type='submit']")
                if accept_btn:
                    await accept_btn.click()
                    await page.wait_for_load_state("domcontentloaded")
                    logger.debug("Dismissed consent form")
        except Exception as exc:
            logger.debug("Consent handling skipped: {e}", e=exc)

    async def _parse_google_results(self, page: Page, max_results: int) -> list[SearchResultItem]:
        items: list[SearchResultItem] = []

        result_elements = await page.query_selector_all("div#rso div.g")
        if not result_elements:
            result_elements = await page.query_selector_all("div#rso > div[data-hveid]")

        for idx, element in enumerate(result_elements):
            if idx >= max_results:
                break
            try:
                title_el = await element.query_selector("h3")
                title = await title_el.inner_text() if title_el else ""

                link_el = await element.query_selector("a[href]")
                href = await link_el.get_attribute("href") if link_el else ""

                cite_el = await element.query_selector("cite")
                if not cite_el:
                    cite_el = await element.query_selector("span.VuuXrf")
                displayed_url = await cite_el.inner_text() if cite_el else ""

                snippet_el = (
                    await element.query_selector("div[data-sncf]")
                    or await element.query_selector("div.VwiC3b")
                    or await element.query_selector("span.aCOpRe")
                    or await element.query_selector("[data-content-feature='1']")
                )
                snippet = await snippet_el.inner_text() if snippet_el else ""

                if title and href and href.startswith("http"):
                    items.append(
                        SearchResultItem(
                            position=idx + 1,
                            title=title.strip(),
                            url=href,
                            displayed_url=displayed_url.strip(),
                            snippet=snippet.strip(),
                            provider=self.name,
                        )
                    )
            except Exception as exc:
                logger.debug("Skipping Google result {idx}: {e}", idx=idx, e=exc)
                continue

        logger.info("Parsed {n} Google results", n=len(items))
        return items

    # ------------------------------------------------------------------
    # DuckDuckGo HTML (no JS, scraping-friendly)
    # ------------------------------------------------------------------

    async def _search_duckduckgo(self, query: str, max_results: int) -> SearchResponse:
        params = {"q": query}
        if self._config.search.safe_search:
            params["kp"] = "1"

        url = f"https://html.duckduckgo.com/html/?{urlencode(params)}"
        logger.info("Searching DuckDuckGo HTML (Playwright): {q}", q=query)

        try:
            async with self._bm.new_page() as page:
                await page.goto(url, wait_until="domcontentloaded")

                try:
                    await page.wait_for_selector("div.results div.result", timeout=15000)
                except Exception:
                    try:
                        await page.wait_for_selector("div.results a.result__a", timeout=10000)
                    except Exception:
                        logger.warning("DuckDuckGo HTML returned no results")
                        return SearchResponse(query=query)

                items = await self._parse_duckduckgo_results(page, max_results)

            return SearchResponse(query=query, total_results=len(items), results=items)
        except Exception as exc:
            logger.error("DuckDuckGo HTML search failed: {e}", e=exc)
            return SearchResponse(query=query)

    @staticmethod
    def _extract_ddg_url(redirect_href: str) -> str:
        """Unwrap a DDG redirect URL: ``//duckduckgo.com/l/?uddg=<encoded>``."""
        if not redirect_href:
            return ""
        if redirect_href.startswith("//"):
            redirect_href = "https:" + redirect_href

        parsed = urlparse(redirect_href)
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
        if redirect_href.startswith("http"):
            return redirect_href
        return ""

    async def _parse_duckduckgo_results(
        self, page: Page, max_results: int
    ) -> list[SearchResultItem]:
        items: list[SearchResultItem] = []
        result_elements = await page.query_selector_all("div.results div.result")

        for idx, element in enumerate(result_elements):
            if idx >= max_results:
                break
            try:
                link_el = await element.query_selector("a.result__a")
                if not link_el:
                    continue

                title = await link_el.inner_text()
                raw_href = await link_el.get_attribute("href") or ""
                real_url = self._extract_ddg_url(raw_href)

                url_el = await element.query_selector("a.result__url")
                displayed_url = (await url_el.inner_text()).strip() if url_el else ""

                snippet_el = await element.query_selector("a.result__snippet")
                snippet = await snippet_el.inner_text() if snippet_el else ""

                if title and real_url and real_url.lower().startswith(("http://", "https://")):
                    items.append(
                        SearchResultItem(
                            position=idx + 1,
                            title=title.strip(),
                            url=real_url,
                            displayed_url=displayed_url,
                            snippet=snippet.strip(),
                            provider=self.name,
                        )
                    )
            except Exception as exc:
                logger.debug("Skipping DDG result {idx}: {e}", idx=idx, e=exc)
                continue

        logger.info("Parsed {n} DuckDuckGo HTML results", n=len(items))
        return items
