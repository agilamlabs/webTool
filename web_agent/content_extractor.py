"""Three-tier content extraction: trafilatura -> BeautifulSoup4 -> raw text.

Also supports binary extraction for PDF (pypdf) and XLSX (openpyxl) when
the optional ``[binary]`` extra is installed. Without those libraries,
binary extraction returns ``ExtractionResult(extraction_method="none")``
with a clear install hint -- it never crashes the pipeline.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

import trafilatura
from bs4 import BeautifulSoup
from loguru import logger

from .config import AppConfig
from .models import ExtractionResult, FetchResult, FetchStatus


def _is_pdf(fr: FetchResult) -> bool:
    ct = (fr.content_type or "").lower()
    if "application/pdf" in ct:
        return True
    return urlparse(fr.final_url or fr.url).path.lower().endswith(".pdf")


def _is_xlsx(fr: FetchResult) -> bool:
    ct = (fr.content_type or "").lower()
    if "spreadsheetml" in ct or "openxmlformats-officedocument.spreadsheet" in ct:
        return True
    return urlparse(fr.final_url or fr.url).path.lower().endswith(".xlsx")


def _is_docx(fr: FetchResult) -> bool:
    ct = (fr.content_type or "").lower()
    if "wordprocessingml" in ct or "officedocument.wordprocessing" in ct:
        return True
    return urlparse(fr.final_url or fr.url).path.lower().endswith(".docx")


def _is_csv(fr: FetchResult) -> bool:
    ct = (fr.content_type or "").lower()
    if ct.startswith(("text/csv", "text/tab-separated-values")):
        return True
    return urlparse(fr.final_url or fr.url).path.lower().endswith((".csv", ".tsv"))


class ContentExtractor:
    """Extracts structured content from raw HTML using a layered fallback strategy."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def extract(self, fetch_result: FetchResult, *, strict: bool = False) -> ExtractionResult:
        """Extract structured content from a FetchResult.

        Dispatches on FetchResult contents:
          - ``binary`` populated -> PDF or XLSX branch (requires ``[binary]`` extra)
          - ``html`` populated -> three-tier HTML fallback chain

        HTML fallback chain:
          1. trafilatura (best quality, F1 ~0.958)
          2. BeautifulSoup4 structural extraction
          3. Raw text stripping (last resort)

        Args:
            fetch_result: The FetchResult to extract from.
            strict: If True, raise :class:`ExtractionError` when all three
                layers fail to produce content (very rare, since raw is
                a last-resort always-success path). When False, returns
                an ExtractionResult with extraction_method="none".

        Raises:
            ExtractionError: If ``strict=True`` and all layers fail.
        """
        if fetch_result.status != FetchStatus.SUCCESS:
            if strict:
                from .exceptions import ExtractionError

                raise ExtractionError(
                    f"Cannot extract from non-success FetchResult: "
                    f"status={fetch_result.status}, url={fetch_result.url}"
                )
            return ExtractionResult(url=fetch_result.url, extraction_method="none")

        url = fetch_result.final_url

        # Binary branch: PDF / XLSX / DOCX / CSV. Dispatched on
        # content_type or URL extension.
        if fetch_result.binary is not None:
            if _is_pdf(fetch_result):
                return self._extract_pdf(fetch_result.binary, url)
            if _is_xlsx(fetch_result):
                return self._extract_xlsx(fetch_result.binary, url)
            if _is_docx(fetch_result):
                return self._extract_docx(fetch_result.binary, url)
            if _is_csv(fetch_result):
                return self._extract_csv(fetch_result.binary, url)
            # Unrecognized binary -- not extractable
            return ExtractionResult(
                url=url,
                extraction_method="none",
                content=None,
            )

        if not fetch_result.html:
            if strict:
                from .exceptions import ExtractionError

                raise ExtractionError(f"FetchResult has neither html nor binary: url={url}")
            return ExtractionResult(url=url, extraction_method="none")

        html = fetch_result.html
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

        # Layer 3: raw text (always-success unless catastrophic)
        raw_result = self._extract_raw(html, url)
        if strict and (not raw_result.content or len(raw_result.content) < min_len):
            from .exceptions import ExtractionError

            raise ExtractionError(
                f"All three extraction layers failed for {url} "
                f"(content_length={raw_result.content_length})"
            )
        return raw_result

    # ------------------------------------------------------------------
    # Binary branches: PDF + XLSX
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_pdf(blob: bytes, url: str) -> ExtractionResult:
        """Extract text from PDF bytes using pypdf.

        Returns an ExtractionResult with extraction_method='pdf' on success
        or 'none' on missing-library / encrypted / malformed PDF.
        """
        try:
            from pypdf import PdfReader
        except ImportError:
            logger.warning(
                "pypdf not installed; PDF extraction skipped. "
                "Install with: pip install 'web-agent-toolkit[binary]'"
            )
            return ExtractionResult(
                url=url,
                extraction_method="none",
                content=None,
            )

        try:
            from io import BytesIO

            reader = PdfReader(BytesIO(blob))
            if reader.is_encrypted:
                logger.info("PDF is encrypted, skipping: {url}", url=url)
                return ExtractionResult(
                    url=url,
                    extraction_method="none",
                    content=None,
                )
            parts: list[str] = []
            for page in reader.pages:
                try:
                    parts.append(page.extract_text() or "")
                except Exception as page_exc:
                    logger.debug("PDF page extract failed: {e}", e=page_exc)
            text = "\n\n".join(p for p in parts if p).strip()
            if not text:
                return ExtractionResult(
                    url=url,
                    extraction_method="none",
                    content=None,
                )
            # Pull /Title from the document info dict if available
            title: Optional[str] = None
            try:
                info = reader.metadata
                if info is not None and getattr(info, "title", None):
                    title = str(info.title)
            except Exception:
                pass
            return ExtractionResult(
                url=url,
                title=title,
                content=text,
                extraction_method="pdf",
                content_length=len(text),
            )
        except Exception as exc:
            logger.warning("PDF extraction failed for {url}: {e}", url=url, e=exc)
            return ExtractionResult(url=url, extraction_method="none", content=None)

    @staticmethod
    def _extract_xlsx(blob: bytes, url: str) -> ExtractionResult:
        """Extract text from XLSX bytes using openpyxl (TSV-style per sheet)."""
        try:
            from openpyxl import load_workbook
        except ImportError:
            logger.warning(
                "openpyxl not installed; XLSX extraction skipped. "
                "Install with: pip install 'web-agent-toolkit[binary]'"
            )
            return ExtractionResult(
                url=url,
                extraction_method="none",
                content=None,
            )

        try:
            from io import BytesIO

            wb = load_workbook(BytesIO(blob), read_only=True, data_only=True)
            sheet_dumps: list[str] = []
            for sheet in wb.worksheets:
                rows: list[str] = [f"# Sheet: {sheet.title}"]
                for row in sheet.iter_rows(values_only=True):
                    cells = ["" if cell is None else str(cell) for cell in row]
                    if any(cells):
                        rows.append("\t".join(cells))
                if len(rows) > 1:
                    sheet_dumps.append("\n".join(rows))
            wb.close()
            text = "\n\n".join(sheet_dumps).strip()
            if not text:
                return ExtractionResult(
                    url=url,
                    extraction_method="none",
                    content=None,
                )
            return ExtractionResult(
                url=url,
                content=text,
                extraction_method="xlsx",
                content_length=len(text),
            )
        except Exception as exc:
            logger.warning("XLSX extraction failed for {url}: {e}", url=url, e=exc)
            return ExtractionResult(url=url, extraction_method="none", content=None)

    @staticmethod
    def _extract_docx(blob: bytes, url: str) -> ExtractionResult:
        """Extract text from DOCX bytes using python-docx (paragraph-by-paragraph)."""
        try:
            import docx as python_docx  # python-docx package
        except ImportError:
            logger.warning(
                "python-docx not installed; DOCX extraction skipped. "
                "Install with: pip install 'web-agent-toolkit[binary]'"
            )
            return ExtractionResult(url=url, extraction_method="none", content=None)

        try:
            from io import BytesIO

            doc = python_docx.Document(BytesIO(blob))
            parts: list[str] = []
            for para in doc.paragraphs:
                if para.text:
                    parts.append(para.text)
            # Tables: dump cells row-by-row, tab-separated, with a marker.
            for table in doc.tables:
                parts.append("# Table")
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        parts.append("\t".join(cells))
            text = "\n".join(parts).strip()
            if not text:
                return ExtractionResult(url=url, extraction_method="none", content=None)
            # /Title from core properties when available
            title: Optional[str] = None
            try:
                cp = doc.core_properties
                if cp is not None and cp.title:
                    title = str(cp.title)
            except Exception:
                pass
            return ExtractionResult(
                url=url,
                title=title,
                content=text,
                extraction_method="docx",
                content_length=len(text),
            )
        except Exception as exc:
            logger.warning("DOCX extraction failed for {url}: {e}", url=url, e=exc)
            return ExtractionResult(url=url, extraction_method="none", content=None)

    @staticmethod
    def _extract_csv(blob: bytes, url: str) -> ExtractionResult:
        """Extract text from CSV/TSV bytes using stdlib csv (no dep required).

        Auto-detects delimiter via :class:`csv.Sniffer`. Falls back to comma
        on detection failure. Returns the raw content as TSV-style text so
        downstream LLM consumers see one row per line with tab separators.
        """
        try:
            import csv as csv_mod
            from io import StringIO

            # Decode bytes -> text. Try utf-8 first, then latin-1 as a
            # last-resort fallback. We never crash on encoding issues.
            text_in: str
            for enc in ("utf-8-sig", "utf-8", "latin-1"):
                try:
                    text_in = blob.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                return ExtractionResult(url=url, extraction_method="none", content=None)

            sample = text_in[:4096]
            try:
                dialect = csv_mod.Sniffer().sniff(sample, delimiters=",;\t|")
            except Exception:
                dialect = csv_mod.excel  # default: comma-separated
            reader = csv_mod.reader(StringIO(text_in), dialect)
            rows = ["\t".join(row) for row in reader if any(c.strip() for c in row)]
            text = "\n".join(rows).strip()
            if not text:
                return ExtractionResult(url=url, extraction_method="none", content=None)
            return ExtractionResult(
                url=url,
                content=text,
                extraction_method="csv",
                content_length=len(text),
            )
        except Exception as exc:
            logger.warning("CSV extraction failed for {url}: {e}", url=url, e=exc)
            return ExtractionResult(url=url, extraction_method="none", content=None)

    def _extract_trafilatura(self, html: str, url: str) -> Optional[ExtractionResult]:
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

            # Second pass: ask trafilatura for a markdown rendering of
            # the same page. Cheap (HTML re-parsed once) and the result
            # is what most LLMs prefer to consume because it preserves
            # headings, lists, links, and emphasis. Best-effort -- on
            # failure we leave markdown=None.
            markdown: Optional[str] = None
            try:
                markdown = trafilatura.extract(
                    html,
                    url=url,
                    output_format="markdown",
                    favor_precision=self._config.extraction.favor_precision,
                    favor_recall=self._config.extraction.favor_recall,
                    include_tables=self._config.extraction.include_tables,
                    include_links=self._config.extraction.include_links,
                    include_comments=self._config.extraction.include_comments,
                )
            except Exception as md_exc:
                logger.debug("Markdown rendering failed for {url}: {e}", url=url, e=md_exc)

            return ExtractionResult(
                url=url,
                title=getattr(doc, "title", None),
                description=getattr(doc, "description", None),
                author=getattr(doc, "author", None),
                date=getattr(doc, "date", None),
                sitename=getattr(doc, "sitename", None),
                content=text,
                markdown=markdown,
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
                description = meta_desc.get("content", "")

            # Author
            author = None
            meta_author = soup.find("meta", attrs={"name": "author"})
            if meta_author:
                author = meta_author.get("content", "")

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
