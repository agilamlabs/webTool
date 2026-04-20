"""Three-tier content extraction: trafilatura -> BeautifulSoup4 -> raw text."""

from __future__ import annotations

from typing import Optional

import trafilatura
from bs4 import BeautifulSoup
from loguru import logger

from .config import AppConfig
from .models import ExtractionResult, FetchResult, FetchStatus


class ContentExtractor:
    """Extracts structured content from raw HTML using a layered fallback strategy."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def extract(self, fetch_result: FetchResult) -> ExtractionResult:
        """Extract structured content from a FetchResult.

        Fallback chain:
          1. trafilatura (best quality, F1 ~0.958)
          2. BeautifulSoup4 structural extraction
          3. Raw text stripping (last resort)
        """
        if fetch_result.status != FetchStatus.SUCCESS or not fetch_result.html:
            return ExtractionResult(url=fetch_result.url, extraction_method="none")

        html = fetch_result.html
        url = fetch_result.final_url
        min_len = self._config.extraction.min_content_length

        # Layer 1: trafilatura
        result = self._extract_trafilatura(html, url)
        if result and result.content and len(result.content) >= min_len:
            return result
        logger.debug("Trafilatura insufficient for {url}, trying BS4", url=url)

        # Layer 2: BeautifulSoup
        result = self._extract_bs4(html, url)
        if result and result.content and len(result.content) >= min_len:
            return result
        logger.debug("BS4 insufficient for {url}, falling back to raw", url=url)

        # Layer 3: raw text
        return self._extract_raw(html, url)

    def _extract_trafilatura(
        self, html: str, url: str
    ) -> Optional[ExtractionResult]:
        """Primary extractor using trafilatura with metadata."""
        try:
            doc = trafilatura.bare_extraction(
                html,
                url=url,
                favor_precision=self._config.extraction.favor_precision,
                favor_recall=self._config.extraction.favor_recall,
                include_tables=self._config.extraction.include_tables,
                include_links=self._config.extraction.include_links,
                include_comments=self._config.extraction.include_comments,
                with_metadata=True,
            )
            if doc is None:
                return None

            # bare_extraction returns a Document object; access attributes directly
            text = getattr(doc, "text", None)
            if not text:
                return None

            return ExtractionResult(
                url=url,
                title=getattr(doc, "title", None),
                description=getattr(doc, "description", None),
                author=getattr(doc, "author", None),
                date=getattr(doc, "date", None),
                sitename=getattr(doc, "sitename", None),
                content=text,
                language=getattr(doc, "language", None),
                extraction_method="trafilatura",
                content_length=len(text),
            )
        except Exception as e:
            logger.warning("Trafilatura failed for {url}: {e}", url=url, e=e)
            return None

    def _extract_bs4(self, html: str, url: str) -> Optional[ExtractionResult]:
        """Fallback extractor using BeautifulSoup structural heuristics."""
        try:
            soup = BeautifulSoup(html, "lxml")

            # Title
            title = None
            title_tag = soup.find("title")
            if title_tag:
                title = title_tag.get_text(strip=True)

            # Meta description
            description = None
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc:
                description = meta_desc.get("content", "")  # type: ignore[arg-type]

            # Author
            author = None
            meta_author = soup.find("meta", attrs={"name": "author"})
            if meta_author:
                author = meta_author.get("content", "")  # type: ignore[arg-type]

            # Main content: try semantic tags first, then common class/id patterns
            content_tag = (
                soup.find("article")
                or soup.find("main")
                or soup.find("div", {"role": "main"})
                or soup.find("div", class_="content")
                or soup.find("div", id="content")
                or soup.body
            )

            # Strip non-content elements
            if content_tag:
                for unwanted in content_tag.find_all(
                    ["nav", "header", "footer", "aside", "script", "style", "noscript"]
                ):
                    unwanted.decompose()
                text = content_tag.get_text(separator="\n", strip=True)
            else:
                text = ""

            if not text:
                return None

            return ExtractionResult(
                url=url,
                title=title,
                description=description,
                author=author,
                content=text,
                extraction_method="bs4",
                content_length=len(text),
            )
        except Exception as e:
            logger.warning("BS4 extraction failed for {url}: {e}", url=url, e=e)
            return None

    def _extract_raw(self, html: str, url: str) -> ExtractionResult:
        """Last resort: strip all tags and return body text."""
        try:
            soup = BeautifulSoup(html, "lxml")
            for tag in soup.find_all(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
        except Exception:
            text = ""

        return ExtractionResult(
            url=url,
            content=text if text else None,
            extraction_method="raw",
            content_length=len(text) if text else 0,
        )
