"""Search engine with Google primary and DuckDuckGo fallback."""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlencode, urlparse

from loguru import logger
from playwright.async_api import Page

from .browser_manager import BrowserManager
from .config import AppConfig
from .models import SearchResponse, SearchResultItem


class SearchEngine:
    """Performs web searches with automatic fallback.

    Tries Google first. If Google blocks with a CAPTCHA or returns no results,
    falls back to DuckDuckGo HTML which is more scraping-friendly.
    """

    def __init__(self, browser_manager: BrowserManager, config: AppConfig) -> None:
        self._bm = browser_manager
        self._config = config

    async def search(
        self,
        query: str,
        max_results: int | None = None,
        *,
        strict: bool = False,
    ) -> SearchResponse:
        """Execute a web search, trying Google then DuckDuckGo as fallback.

        Args:
            query: Search query string.
            max_results: Maximum number of results.
            strict: If True, raise :class:`SearchError` when both engines
                return zero results (instead of returning an empty SearchResponse).

        Raises:
            SearchError: If ``strict=True`` and both Google and DuckDuckGo fail.
        """
        max_r = max_results or self._config.search.max_results

        # Try Google first
        result = await self._search_google(query, max_r)
        if result.results:
            return result

        # Fall back to DuckDuckGo
        logger.info("Google returned no results, falling back to DuckDuckGo")
        result = await self._search_duckduckgo(query, max_r)
        if result.results:
            return result

        # Both engines failed -- caller may want an exception
        if strict:
            from .exceptions import SearchError

            raise SearchError(
                f"Both Google and DuckDuckGo returned no results for {query!r}. "
                "This usually means the search engines blocked the request "
                "(CAPTCHA / rate-limit) or the query has truly no matches."
            )
        return result

    # ------------------------------------------------------------------
    # Google
    # ------------------------------------------------------------------

    async def _search_google(self, query: str, max_results: int) -> SearchResponse:
        """Attempt a Google search."""
        params: dict[str, str | int] = {
            "q": query,
            "hl": self._config.search.language,
            "gl": self._config.search.region,
            "num": max_results,
        }
        if self._config.search.safe_search:
            params["safe"] = "active"

        url = f"{self._config.search.search_url}?{urlencode(params)}"
        logger.info("Searching Google: {q}", q=query)

        try:
            async with self._bm.new_page() as page:
                await page.goto(url, wait_until="domcontentloaded")

                # Check for CAPTCHA / block page
                if await self._is_blocked(page):
                    logger.warning("Google blocked the request (CAPTCHA detected)")
                    return SearchResponse(query=query)

                # Handle consent dialog
                await self._handle_google_consent(page)

                # Wait for results container
                try:
                    await page.wait_for_selector("div#search, div#rso", timeout=15000)
                except Exception:
                    logger.warning("Google SERP selectors not found")
                    return SearchResponse(query=query)

                results = await self._parse_google_results(page, max_results)

            return SearchResponse(query=query, total_results=len(results), results=results)
        except Exception as e:
            logger.warning("Google search failed: {e}", e=e)
            return SearchResponse(query=query)

    async def _is_blocked(self, page: Page) -> bool:
        """Detect if Google is showing a CAPTCHA or block page."""
        blocked_indicators = [
            "form#captcha-form",
            "div#recaptcha",
            "iframe[src*='recaptcha']",
        ]
        url = page.url
        if "/sorry/" in url or "captcha" in url.lower():
            return True
        for selector in blocked_indicators:
            if await page.query_selector(selector):
                return True
        return False

    async def _handle_google_consent(self, page: Page) -> None:
        """Dismiss Google's cookie consent dialog if it appears."""
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
        except Exception as e:
            logger.debug("Consent handling skipped: {e}", e=e)

    async def _parse_google_results(self, page: Page, max_results: int) -> list[SearchResultItem]:
        """Parse organic results from the Google SERP DOM."""
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
                        )
                    )
            except Exception as e:
                logger.debug("Skipping Google result {idx}: {e}", idx=idx, e=e)
                continue

        logger.info("Parsed {n} Google results", n=len(items))
        return items

    # ------------------------------------------------------------------
    # DuckDuckGo (fallback)
    # ------------------------------------------------------------------

    async def _search_duckduckgo(self, query: str, max_results: int) -> SearchResponse:
        """Search using DuckDuckGo HTML version (no JS required, scraping-friendly)."""
        params = {"q": query}
        if self._config.search.safe_search:
            params["kp"] = "1"

        url = f"https://html.duckduckgo.com/html/?{urlencode(params)}"
        logger.info("Searching DuckDuckGo: {q}", q=query)

        try:
            async with self._bm.new_page() as page:
                await page.goto(url, wait_until="domcontentloaded")

                # Wait for results
                try:
                    await page.wait_for_selector("div.results div.result", timeout=15000)
                except Exception:
                    # Try alternative: links container
                    try:
                        await page.wait_for_selector("div.results a.result__a", timeout=10000)
                    except Exception:
                        logger.warning("DuckDuckGo returned no results")
                        return SearchResponse(query=query)

                results = await self._parse_duckduckgo_results(page, max_results)

            return SearchResponse(query=query, total_results=len(results), results=results)
        except Exception as e:
            logger.error("DuckDuckGo search failed: {e}", e=e)
            return SearchResponse(query=query)

    @staticmethod
    def _extract_ddg_url(redirect_href: str) -> str:
        """Extract the real URL from a DuckDuckGo redirect link.

        DDG HTML wraps results as: //duckduckgo.com/l/?uddg=<encoded_url>&rut=...
        """
        if not redirect_href:
            return ""
        # Normalize protocol-relative URLs
        if redirect_href.startswith("//"):
            redirect_href = "https:" + redirect_href

        parsed = urlparse(redirect_href)
        qs = parse_qs(parsed.query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
        # Already a direct URL
        if redirect_href.startswith("http"):
            return redirect_href
        return ""

    async def _parse_duckduckgo_results(
        self, page: Page, max_results: int
    ) -> list[SearchResultItem]:
        """Parse results from DuckDuckGo HTML page."""
        items: list[SearchResultItem] = []

        # DuckDuckGo HTML structure:
        #   div.result > h2.result__title > a.result__a (title + redirect URL)
        #   div.result > a.result__snippet (snippet)
        #   div.result > ...  a.result__url (displayed URL)
        result_elements = await page.query_selector_all("div.results div.result")

        for idx, element in enumerate(result_elements):
            if idx >= max_results:
                break
            try:
                # Title and redirect URL
                link_el = await element.query_selector("a.result__a")
                if not link_el:
                    continue

                title = await link_el.inner_text()
                raw_href = await link_el.get_attribute("href") or ""
                real_url = self._extract_ddg_url(raw_href)

                # Displayed URL (a.result__url, not span)
                url_el = await element.query_selector("a.result__url")
                displayed_url = ""
                if url_el:
                    displayed_url = (await url_el.inner_text()).strip()

                # Snippet
                snippet_el = await element.query_selector("a.result__snippet")
                snippet = await snippet_el.inner_text() if snippet_el else ""

                # Reject non-http(s) schemes (javascript:, data:, file:)
                # that DDG redirect targets could theoretically contain.
                if title and real_url and real_url.lower().startswith(("http://", "https://")):
                    items.append(
                        SearchResultItem(
                            position=idx + 1,
                            title=title.strip(),
                            url=real_url,
                            displayed_url=displayed_url,
                            snippet=snippet.strip(),
                        )
                    )
            except Exception as e:
                logger.debug("Skipping DDG result {idx}: {e}", idx=idx, e=e)
                continue

        logger.info("Parsed {n} DuckDuckGo results", n=len(items))
        return items
