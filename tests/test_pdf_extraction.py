"""Tests for v1.7.0 Wave 3C PDF extraction (pdfplumber-preferred path).

Offline by design: the pdfplumber / pypdf engines are MOCKED via injected
fake modules + a patched ``_module_available`` availability probe, so the
default suite never needs a real PDF binary or the optional ``[binary]``
extra installed. A single light real-smoke test runs only when pdfplumber
is genuinely importable (guarded by ``importorskip``).

Covered:
  - pdfplumber path: 3 text pages + a table -> page markers, markdown
    table, page_count=3, extraction_method='pdfplumber', tables exposed.
  - pypdf fallback: pdfplumber unavailable, pypdf present -> page markers,
    extraction_method='pdf', no tables.
  - neither installed: extraction_method='none' + install hint.
  - scanned/empty: pages present but no text -> error_message names the
    image-only / OCR reason (not a bare empty success).
  - table cap: more tables than pdf_max_tables -> truncated flag.
  - cell cap: an oversize table is row-truncated.
  - config gating: pdf_extract_tables=False skips tables;
    pdf_page_markers=False omits markers.
  - backward-compat: old ExtractionResult(...) still validates; a non-PDF
    (CSV) extraction leaves page_count None / tables [].
"""

from __future__ import annotations

import sys
import types
from typing import Any, Optional

import pytest
import web_agent.content_extractor as ce_mod
from web_agent.config import AppConfig
from web_agent.content_extractor import ContentExtractor, _render_markdown_table
from web_agent.models import ExtractionResult, FetchResult, FetchStatus

# ----------------------------------------------------------------------
# Fakes for pdfplumber / pypdf
# ----------------------------------------------------------------------


class _FakePlumberPage:
    def __init__(self, text: str, tables: Optional[list[list[list[Any]]]] = None) -> None:
        self._text = text
        self._tables = tables or []

    def extract_text(self) -> str:
        return self._text

    def extract_tables(self) -> list[list[list[Any]]]:
        return self._tables


class _FakePlumberPDF:
    def __init__(
        self,
        pages: list[_FakePlumberPage],
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        self.pages = pages
        self.metadata = metadata or {}

    def __enter__(self) -> _FakePlumberPDF:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _install_fake_pdfplumber(
    monkeypatch: pytest.MonkeyPatch,
    pdf: _FakePlumberPDF,
) -> None:
    """Inject a fake ``pdfplumber`` module and force it 'available'."""
    fake = types.ModuleType("pdfplumber")

    def _open(_stream: Any) -> _FakePlumberPDF:
        return pdf

    fake.open = _open  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pdfplumber", fake)

    def _available(name: str) -> bool:
        return name == "pdfplumber"

    monkeypatch.setattr(ce_mod, "_module_available", _available)


class _FakePypdfPage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakePdfReader:
    def __init__(self, _stream: Any) -> None:
        self.is_encrypted = False
        self.metadata = types.SimpleNamespace(title="Fallback Title")
        self.pages = [
            _FakePypdfPage("pypdf page one text"),
            _FakePypdfPage("pypdf page two text"),
        ]


def _install_fake_pypdf_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """pdfplumber unavailable; a fake ``pypdf`` module is available."""
    fake = types.ModuleType("pypdf")
    fake.PdfReader = _FakePdfReader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypdf", fake)

    def _available(name: str) -> bool:
        return name == "pypdf"

    monkeypatch.setattr(ce_mod, "_module_available", _available)


def _pdf_fetch_result() -> FetchResult:
    return FetchResult(
        url="https://x.com/report.pdf",
        final_url="https://x.com/report.pdf",
        status=FetchStatus.SUCCESS,
        binary=b"%PDF-1.4 fake bytes",
        content_type="application/pdf",
    )


# ----------------------------------------------------------------------
# pdfplumber path
# ----------------------------------------------------------------------


def test_pdfplumber_pages_markers_and_table(monkeypatch):
    table = [["Asset", "Value"], ["Cash", "100"], ["Bonds", "200"]]
    pdf = _FakePlumberPDF(
        pages=[
            _FakePlumberPage("Page one prose."),
            _FakePlumberPage("Page two prose.", tables=[table]),
            _FakePlumberPage("Page three prose."),
        ],
        metadata={"Title": "Quarterly Report"},
    )
    _install_fake_pdfplumber(monkeypatch, pdf)

    res = ContentExtractor(AppConfig()).extract(_pdf_fetch_result())

    assert res.extraction_method == "pdfplumber"
    assert res.page_count == 3
    assert res.title == "Quarterly Report"
    assert res.content is not None
    # Page markers present for all three pages.
    assert "===== Page 1 =====" in res.content
    assert "===== Page 2 =====" in res.content
    assert "===== Page 3 =====" in res.content
    # Markdown table rendered under its page and exposed on .tables.
    assert "| Asset | Value |" in res.content
    assert "| --- | --- |" in res.content
    assert "| Cash | 100 |" in res.content
    assert len(res.tables) == 1
    assert "| Asset | Value |" in res.tables[0]


def test_pdfplumber_table_appears_after_its_page_marker(monkeypatch):
    table = [["k", "v"], ["a", "1"]]
    pdf = _FakePlumberPDF(
        pages=[
            _FakePlumberPage("first page"),
            _FakePlumberPage("second page", tables=[table]),
        ]
    )
    _install_fake_pdfplumber(monkeypatch, pdf)

    res = ContentExtractor(AppConfig()).extract(_pdf_fetch_result())

    assert res.content is not None
    pos_marker2 = res.content.index("===== Page 2 =====")
    pos_table = res.content.index("| k | v |")
    pos_marker1 = res.content.index("===== Page 1 =====")
    # Table is interleaved at page 2's position (after marker 2, after marker 1).
    assert pos_marker1 < pos_marker2 < pos_table


# ----------------------------------------------------------------------
# pypdf fallback
# ----------------------------------------------------------------------


def test_pypdf_fallback_when_pdfplumber_absent(monkeypatch):
    _install_fake_pypdf_only(monkeypatch)

    res = ContentExtractor(AppConfig()).extract(_pdf_fetch_result())

    assert res.extraction_method == "pdf"
    assert res.page_count == 2
    assert res.title == "Fallback Title"
    assert res.content is not None
    assert "===== Page 1 =====" in res.content
    assert "===== Page 2 =====" in res.content
    assert "pypdf page one text" in res.content
    # Fallback engine never extracts tables.
    assert res.tables == []


def test_pypdf_fallback_respects_page_markers_off(monkeypatch):
    _install_fake_pypdf_only(monkeypatch)
    cfg = AppConfig()
    cfg.extraction.pdf_page_markers = False

    res = ContentExtractor(cfg).extract(_pdf_fetch_result())

    assert res.extraction_method == "pdf"
    assert res.content is not None
    assert "===== Page" not in res.content
    assert "pypdf page one text" in res.content


# ----------------------------------------------------------------------
# Neither engine installed
# ----------------------------------------------------------------------


def test_neither_engine_installed_returns_none_with_hint(monkeypatch):
    monkeypatch.setattr(ce_mod, "_module_available", lambda name: False)

    res = ContentExtractor(AppConfig()).extract(_pdf_fetch_result())

    assert res.extraction_method == "none"
    assert res.content is None
    assert res.error_message is not None
    assert "binary" in res.error_message.lower()
    assert "pdfplumber" in res.error_message


# ----------------------------------------------------------------------
# Scanned / no-text-layer detection
# ----------------------------------------------------------------------


def test_scanned_pdf_pdfplumber_sets_reason(monkeypatch):
    pdf = _FakePlumberPDF(
        pages=[
            _FakePlumberPage("   "),  # whitespace only
            _FakePlumberPage(""),  # empty
        ]
    )
    _install_fake_pdfplumber(monkeypatch, pdf)

    res = ContentExtractor(AppConfig()).extract(_pdf_fetch_result())

    assert res.extraction_method == "none"
    assert res.content is None
    assert res.page_count == 2
    assert res.error_message is not None
    assert "image-only" in res.error_message.lower() or "scanned" in res.error_message.lower()
    assert "ocr" in res.error_message.lower()
    assert res.failure_stage == "extract"


def test_scanned_pdf_pypdf_sets_reason(monkeypatch):
    fake = types.ModuleType("pypdf")

    class _EmptyReader:
        def __init__(self, _stream: Any) -> None:
            self.is_encrypted = False
            self.metadata = None
            self.pages = [_FakePypdfPage(""), _FakePypdfPage("   ")]

    fake.PdfReader = _EmptyReader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypdf", fake)
    monkeypatch.setattr(ce_mod, "_module_available", lambda name: name == "pypdf")

    res = ContentExtractor(AppConfig()).extract(_pdf_fetch_result())

    assert res.extraction_method == "none"
    assert res.page_count == 2
    assert res.error_message is not None
    assert "ocr" in res.error_message.lower()


# ----------------------------------------------------------------------
# Table caps
# ----------------------------------------------------------------------


def test_table_cap_truncates_with_flag(monkeypatch):
    one_table = [["a", "b"], ["1", "2"]]
    # 3 pages, each with 2 tables -> 6 tables total; cap to 2.
    pdf = _FakePlumberPDF(
        pages=[
            _FakePlumberPage(f"page {i}", tables=[one_table, one_table])
            for i in range(1, 4)
        ]
    )
    _install_fake_pdfplumber(monkeypatch, pdf)
    cfg = AppConfig()
    cfg.extraction.pdf_max_tables = 2

    res = ContentExtractor(cfg).extract(_pdf_fetch_result())

    assert res.extraction_method == "pdfplumber"
    assert len(res.tables) == 2
    assert res.truncated is True


def test_table_cell_cap_truncates_rows(monkeypatch):
    # A 2-col table with 100 data rows = 202 cells; cap cells at 20 -> 10 rows.
    big_table = [["c1", "c2"]] + [[str(i), str(i)] for i in range(100)]
    pdf = _FakePlumberPDF(pages=[_FakePlumberPage("p1", tables=[big_table])])
    _install_fake_pdfplumber(monkeypatch, pdf)
    cfg = AppConfig()
    cfg.extraction.pdf_max_table_cells = 20

    res = ContentExtractor(cfg).extract(_pdf_fetch_result())

    assert len(res.tables) == 1
    rendered = res.tables[0]
    # header + separator + at most 10 body rows = <= 12 lines.
    assert rendered.count("\n") + 1 <= 12


# ----------------------------------------------------------------------
# Config gating
# ----------------------------------------------------------------------


def test_pdf_extract_tables_false_skips_tables(monkeypatch):
    table = [["x", "y"], ["1", "2"]]
    pdf = _FakePlumberPDF(pages=[_FakePlumberPage("page with table", tables=[table])])
    _install_fake_pdfplumber(monkeypatch, pdf)
    cfg = AppConfig()
    cfg.extraction.pdf_extract_tables = False

    res = ContentExtractor(cfg).extract(_pdf_fetch_result())

    assert res.extraction_method == "pdfplumber"
    assert res.tables == []
    assert res.content is not None
    assert "| x | y |" not in res.content
    assert "page with table" in res.content


def test_pdf_max_tables_zero_skips_tables(monkeypatch):
    table = [["x", "y"], ["1", "2"]]
    pdf = _FakePlumberPDF(pages=[_FakePlumberPage("body", tables=[table])])
    _install_fake_pdfplumber(monkeypatch, pdf)
    cfg = AppConfig()
    cfg.extraction.pdf_max_tables = 0

    res = ContentExtractor(cfg).extract(_pdf_fetch_result())

    assert res.tables == []
    assert res.content is not None
    assert "| x | y |" not in res.content


def test_pdf_page_markers_false_omits_markers(monkeypatch):
    pdf = _FakePlumberPDF(
        pages=[_FakePlumberPage("alpha"), _FakePlumberPage("beta")]
    )
    _install_fake_pdfplumber(monkeypatch, pdf)
    cfg = AppConfig()
    cfg.extraction.pdf_page_markers = False

    res = ContentExtractor(cfg).extract(_pdf_fetch_result())

    assert res.content is not None
    assert "===== Page" not in res.content
    assert "alpha" in res.content
    assert "beta" in res.content


# ----------------------------------------------------------------------
# Markdown rendering helper (unit-level)
# ----------------------------------------------------------------------


def test_render_markdown_table_escapes_pipes_and_newlines():
    table = [["col|a", "col\nb"], ["v1", "v2"]]
    md = _render_markdown_table(table, page_index=1, url="u", max_cells=0)
    assert md is not None
    # Pipe escaped, newline collapsed to space.
    assert "col\\|a" in md
    assert "col b" in md
    assert "\n| --- | --- |\n" in md


def test_render_markdown_table_pads_ragged_rows():
    table = [["a", "b", "c"], ["1"]]  # short row padded to width 3
    md = _render_markdown_table(table, page_index=1, url="u", max_cells=0)
    assert md is not None
    lines = md.split("\n")
    # Header has 3 columns -> 4 pipes; body row padded to the same width.
    assert lines[0].count("|") == 4
    assert lines[-1].count("|") == 4


def test_render_markdown_table_empty_returns_none():
    assert _render_markdown_table([], page_index=1, url="u", max_cells=0) is None
    assert _render_markdown_table([[], []], page_index=1, url="u", max_cells=0) is None


# ----------------------------------------------------------------------
# Backward-compat
# ----------------------------------------------------------------------


def test_old_extraction_result_construction_still_validates():
    # Pre-Wave-3C constructor (no page_count / tables) must still build.
    res = ExtractionResult(
        url="https://x.com/page",
        content="hello",
        extraction_method="trafilatura",
        content_length=5,
    )
    assert res.page_count is None
    assert res.tables == []


def test_non_pdf_extraction_leaves_pdf_fields_default():
    # A CSV extraction (non-PDF) must not populate page_count / tables.
    fr = FetchResult(
        url="https://x.com/a.csv",
        final_url="https://x.com/a.csv",
        status=FetchStatus.SUCCESS,
        binary=b"name,value\nalice,1\nbob,2\n",
        content_type="text/csv",
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "csv"
    assert res.page_count is None
    assert res.tables == []


def test_content_cap_composes_with_pdf(monkeypatch):
    # The safety char cap still bounds PDF content (tables folded in).
    pages = [_FakePlumberPage("x" * 500) for _ in range(5)]
    pdf = _FakePlumberPDF(pages=pages)
    _install_fake_pdfplumber(monkeypatch, pdf)
    cfg = AppConfig()
    cfg.safety.max_chars_per_call = 300

    res = ContentExtractor(cfg).extract(_pdf_fetch_result())

    assert res.content is not None
    assert len(res.content) <= 300
    assert res.truncated is True
    assert res.page_count == 5


# ----------------------------------------------------------------------
# Optional real-engine smoke (only when pdfplumber genuinely installed)
# ----------------------------------------------------------------------

_REAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj <</Type/Catalog/Pages 2 0 R>> endobj\n"
    b"2 0 obj <</Type/Pages/Count 1/Kids[3 0 R]>> endobj\n"
    b"3 0 obj <</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>> endobj\n"
    b"4 0 obj <</Length 44>>stream\n"
    b"BT /F1 12 Tf 100 700 Td (Hello PDF World) Tj ET\n"
    b"endstream endobj\n"
    b"5 0 obj <</Type/Font/Subtype/Type1/BaseFont/Helvetica>> endobj\n"
    b"xref\n0 6\n"
    b"0000000000 65535 f \n"
    b"0000000010 00000 n \n"
    b"0000000054 00000 n \n"
    b"0000000099 00000 n \n"
    b"0000000186 00000 n \n"
    b"0000000280 00000 n \n"
    b"trailer <</Size 6/Root 1 0 R>>\n"
    b"startxref\n340\n%%EOF\n"
)


def test_real_pdfplumber_smoke():
    pytest.importorskip("pdfplumber")
    fr = FetchResult(
        url="https://x.com/real.pdf",
        final_url="https://x.com/real.pdf",
        status=FetchStatus.SUCCESS,
        binary=_REAL_PDF,
        content_type="application/pdf",
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "pdfplumber"
    assert res.page_count == 1
    assert res.content is not None
    assert "Hello" in res.content
    assert "===== Page 1 =====" in res.content
