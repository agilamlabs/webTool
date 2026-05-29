"""Three-tier content extraction: trafilatura -> BeautifulSoup4 -> raw text.

Also supports binary extraction for PDF (pypdf), XLSX (openpyxl), DOCX
(python-docx), and CSV (stdlib). PDF/XLSX/DOCX require the optional
``[binary]`` extra; CSV works with no additional dependency. Without
the relevant library, binary extraction returns
``ExtractionResult(extraction_method="none")`` with a clear install
hint -- it never crashes the pipeline.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urlparse

import trafilatura
from bs4 import BeautifulSoup
from loguru import logger

from .config import AppConfig
from .models import ExtractionResult, FetchResult, FetchStatus


def _extract_json_ld(html: str) -> list[dict[str, Any]]:
    """v1.6.12: parse ``<script type='application/ld+json'>`` blocks.

    Returns a flat list of schema.org objects embedded in the page.
    ``@graph`` containers are unwrapped so the result contains
    individual items, not the graph wrapper. Malformed JSON-LD
    (sadly very common -- trailing commas, single-quoted keys, etc.)
    is swallowed silently; we never raise from this helper.

    Args:
        html: Raw page HTML.

    Returns:
        Flat list of dict objects (empty when no JSON-LD found / all
        blocks malformed).
    """
    if not html:
        return []
    # v1.6.14 E-3: cap total JSON-LD blocks so a hostile page can't force
    # unbounded growth of ``structured_data`` -- a single ``@graph`` with
    # 100k entries, or thousands of ld+json scripts, would otherwise be
    # accumulated wholesale into the result (and serialized downstream).
    # 500 is far above any legitimate page's structured-data count.
    max_blocks = 500
    blocks: list[dict[str, Any]] = []
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:  # pragma: no cover -- defensive
        return []
    for script in soup.find_all("script", type="application/ld+json"):
        if len(blocks) >= max_blocks:
            break
        # ``script.string`` is None when the tag has children (e.g.
        # CDATA-wrapped); fall back to ``get_text()``.
        text = script.string if script.string else script.get_text()
        if not text or not text.strip():
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError, RecursionError, MemoryError):
            # Many sites have malformed JSON-LD. Swallow and continue.
            # ``RecursionError`` covers an adversarial DoS: an attacker
            # could embed deeply-nested JSON (>1000 levels) to crash the
            # default CPython parser. ``MemoryError`` (v1.6.14 E-3) covers a
            # huge flat blob. Neither derives from ``ValueError`` /
            # ``JSONDecodeError``, so they must be caught explicitly here.
            continue
        # JSON-LD allows either a single object or an array at the
        # top level. Handle both.
        candidates: list[Any] = data if isinstance(data, list) else [data]
        for item in candidates:
            if len(blocks) >= max_blocks:
                break
            if not isinstance(item, dict):
                continue
            # Unwrap ``@graph`` containers -- the graph wrapper is
            # rarely what callers want; they want the contained items.
            graph = item.get("@graph")
            if isinstance(graph, list):
                # v1.6.14 E-3: bounded so a single giant @graph can't blow
                # past the cap in one extend().
                for g in graph:
                    if len(blocks) >= max_blocks:
                        break
                    if isinstance(g, dict):
                        blocks.append(g)
            else:
                blocks.append(item)
    return blocks


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

    def extract(
        self,
        fetch_result: FetchResult,
        *,
        strict: bool = False,
        prefer_api: bool = False,
    ) -> ExtractionResult:
        """Extract structured content from a FetchResult.

        Dispatches on FetchResult contents:
          - ``binary`` populated -> PDF or XLSX branch (requires ``[binary]`` extra)
          - ``html`` populated -> three-tier HTML fallback chain
            (+ v1.6.12 optional API-candidates path when ``prefer_api=True``)

        HTML fallback chain:
          1. (v1.6.12) ``prefer_api=True`` AND a captured XHR/fetch JSON
             response body is available -> ``api_json`` extraction.
          2. trafilatura (best quality, F1 ~0.958)
          3. BeautifulSoup4 structural extraction
          4. Raw text stripping (last resort)

        v1.6.12: every HTML result is enriched with
        ``structured_data`` -- parsed ``<script type='application/ld+
        json'>`` blocks -- always-on (cheap, no opt-in needed). When
        the page has no JSON-LD or all blocks are malformed, the list
        is empty.

        Args:
            fetch_result: The FetchResult to extract from.
            strict: If True, raise :class:`ExtractionError` when all
                three layers fail to produce content (very rare, since
                raw is a last-resort always-success path). When False,
                returns an ExtractionResult with extraction_method="none".
            prefer_api: v1.6.12. When True AND ``fetch_result.network_events``
                contains a response event with a captured JSON body
                (requires ``DiagnosticsConfig.capture_response_bodies=
                True``), route extraction through that body instead of
                the rendered HTML. Useful on SPAs where the XHR payload
                is strictly cleaner than the DOM. Default False
                preserves the v1.6.11 behaviour.

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

        # v1.6.12: JSON-LD enrichment. The helper swallows malformed
        # JSON (including ``RecursionError`` from deeply-nested
        # adversarial payloads). Cost: one extra BS4+lxml parse per
        # fetch (~5-20 ms on a 50-200 KB page). On the bs4 / raw
        # fallback paths this duplicates the parse those extractors
        # already do; a future patch can share the parsed soup, but
        # the duplication is acceptable for now (small absolute cost,
        # keeps the JSON-LD path decoupled from the fallback chain).
        # Compute once; attach to whichever extractor wins.
        structured = _extract_json_ld(html)

        # v1.6.12: prefer_api path -- when the caller opted in AND the
        # FetchResult carries a captured JSON response body, route
        # extraction through that body instead of the rendered HTML.
        if prefer_api:
            api_result = self._extract_from_api_candidates(fetch_result, url)
            if api_result is not None:
                api_result.structured_data = structured
                return api_result
            logger.debug(
                "prefer_api=True but no usable JSON body captured for {url}; "
                "falling back to HTML extraction",
                url=url,
            )

        # Layer 1: trafilatura
        result = self._extract_trafilatura(html, url)
        if result and result.content and len(result.content) >= min_len:
            result.structured_data = structured
            return result
        logger.debug("Trafilatura insufficient for {url}, trying BS4", url=url)

        # Layer 2: BeautifulSoup
        result = self._extract_bs4(html, url)
        if result and result.content and len(result.content) >= min_len:
            result.structured_data = structured
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
        raw_result.structured_data = structured
        return raw_result

    # ------------------------------------------------------------------
    # v1.6.12: API-candidates extraction (prefer_api=True path)
    # ------------------------------------------------------------------

    def _extract_from_api_candidates(
        self, fetch_result: FetchResult, url: str
    ) -> Optional[ExtractionResult]:
        """v1.6.12: extract from a captured XHR/fetch JSON response body.

        Scans :attr:`FetchResult.network_events` for response events
        with a captured ``body_text`` (requires
        ``DiagnosticsConfig.capture_response_bodies=True``) and JSON
        content-type. Picks the LARGEST body and uses it as the
        extraction source. Returns ``None`` when no candidate found
        -- caller falls back to HTML extraction.

        The extracted ``content`` is the pretty-printed JSON; ``title``
        is derived heuristically from common top-level keys
        (``title`` / ``headline`` / ``name``) when present. The parsed
        JSON is NOT put into ``structured_data`` -- that field is
        reserved for JSON-LD blocks parsed from the rendered HTML;
        ``extract`` populates it separately.

        Heuristic limitations (known, documented for caller awareness):

        - **Picks the largest body, not the most relevant.** A page that
          emits both a small product-detail API response (~2 KB) AND a
          large analytics / Segment / GA4 batched-hit payload (~10-50
          KB) will silently extract the analytics blob. Watch the
          DEBUG log line emitted on selection to spot this.
        - **No URL-pattern filter.** Cannot tell the caller "use the
          response from ``/api/page-data``, not ``/v1/track``". A
          future patch can accept a regex / glob.
        - **Title heuristic is top-level only.** Nested shapes like
          ``{data: {title: "..."}}`` won't populate
          ``ExtractionResult.title``, though the JSON body still ends
          up in ``content`` and a caller can parse it themselves.
        """
        if not fetch_result.network_events:
            return None
        # Find responses with a captured JSON body. Prefer the largest
        # body as a simple heuristic for "the main API call".
        best_evt = None
        best_size = 0
        for evt in fetch_result.network_events:
            if evt.event_type != "response":
                continue
            if evt.resource_type not in {"xhr", "fetch"}:
                continue
            if not evt.body_text:
                continue
            ct = (evt.content_type or "").lower()
            if "json" not in ct:
                continue
            size = len(evt.body_text)
            if size > best_size:
                best_evt = evt
                best_size = size
        if best_evt is None or not best_evt.body_text:
            return None
        try:
            parsed = json.loads(best_evt.body_text)
        except (json.JSONDecodeError, ValueError, RecursionError):
            return None
        # v1.6.12: emit the chosen URL at DEBUG so callers spotting
        # garbage extractions (analytics ping picked over real API)
        # have a diagnostic signal. The heuristic is documented in
        # the docstring; this log line is the runtime telemetry.
        logger.debug(
            "prefer_api selected XHR/fetch body for {url}: chosen_url={chosen} body_size={size}",
            url=url,
            chosen=best_evt.url,
            size=best_size,
        )

        # Heuristic title from top-level keys (defensive against None /
        # non-string types).
        title: Optional[str] = None
        if isinstance(parsed, dict):
            for key in ("title", "headline", "name"):
                val = parsed.get(key)
                if isinstance(val, str) and val.strip():
                    title = val.strip()
                    break

        # Pretty-print as the content payload.
        try:
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            pretty = best_evt.body_text  # fall back to raw

        # v1.6.14 E-6: cap the emitted content. The capture-time body cap
        # bounds ``best_evt.body_text``, NOT this re-serialized ``pretty``
        # output: indent=2 re-formatting can inflate a compact JSON body
        # several-fold (a 256 KiB compact blob with many short keys can
        # balloon past 1 MB), so the prior "truncation is implicit" claim
        # was wrong. Hard-cap here so content_length stays honest and a
        # hostile API body can't blow up downstream token budgets.
        max_api_content = 512 * 1024
        if pretty and len(pretty) > max_api_content:
            pretty = pretty[:max_api_content]

        return ExtractionResult(
            url=url,
            title=title,
            content=pretty,
            extraction_method="api_json",
            content_length=len(pretty),
        )

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
            description: Optional[str] = None
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc:
                # bs4 >= 4.13 types Tag.get() as str | AttributeValueList | None;
                # ExtractionResult.description requires str | None, so we coerce.
                desc_val = meta_desc.get("content")
                description = str(desc_val) if desc_val is not None else None

            # Author
            author: Optional[str] = None
            meta_author = soup.find("meta", attrs={"name": "author"})
            if meta_author:
                author_val = meta_author.get("content")
                author = str(author_val) if author_val is not None else None

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
