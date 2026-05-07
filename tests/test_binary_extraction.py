"""Tests for v1.6.1 PDF/XLSX extraction in ContentExtractor (suggestion #3)."""

from __future__ import annotations

from io import BytesIO

import pytest
from web_agent.config import AppConfig
from web_agent.content_extractor import ContentExtractor, _is_pdf, _is_xlsx
from web_agent.models import FetchResult, FetchStatus

# ----------------------------------------------------------------------
# Format detection helpers
# ----------------------------------------------------------------------


def test_is_pdf_by_content_type():
    fr = FetchResult(
        url="https://x.com/a",
        final_url="https://x.com/a",
        status=FetchStatus.SUCCESS,
        content_type="application/pdf",
    )
    assert _is_pdf(fr) is True


def test_is_pdf_by_extension():
    fr = FetchResult(
        url="https://x.com/a.pdf",
        final_url="https://x.com/a.pdf",
        status=FetchStatus.SUCCESS,
    )
    assert _is_pdf(fr) is True


def test_is_xlsx_by_content_type():
    fr = FetchResult(
        url="https://x.com/a",
        final_url="https://x.com/a",
        status=FetchStatus.SUCCESS,
        content_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    )
    assert _is_xlsx(fr) is True


def test_is_xlsx_by_extension():
    fr = FetchResult(
        url="https://x.com/a.xlsx",
        final_url="https://x.com/a.xlsx",
        status=FetchStatus.SUCCESS,
    )
    assert _is_xlsx(fr) is True


# ----------------------------------------------------------------------
# XLSX extraction (synthesized fixture)
# ----------------------------------------------------------------------


def _build_xlsx() -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["name", "value"])
    ws.append(["alice", 42])
    ws.append(["bob", 7])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_extraction_returns_text():
    blob = _build_xlsx()
    fr = FetchResult(
        url="https://x.com/a.xlsx",
        final_url="https://x.com/a.xlsx",
        status=FetchStatus.SUCCESS,
        binary=blob,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "xlsx"
    assert res.content is not None
    assert "alice" in res.content
    assert "bob" in res.content
    assert "Sheet: Data" in res.content


# ----------------------------------------------------------------------
# PDF extraction (hand-crafted minimal fixture)
# ----------------------------------------------------------------------


_MINIMAL_PDF = (
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


def test_pdf_extraction_returns_text():
    pytest.importorskip("pypdf")
    fr = FetchResult(
        url="https://x.com/a.pdf",
        final_url="https://x.com/a.pdf",
        status=FetchStatus.SUCCESS,
        binary=_MINIMAL_PDF,
        content_type="application/pdf",
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "pdf"
    assert res.content is not None
    assert "Hello" in res.content


def test_pdf_extraction_with_garbage_returns_none():
    pytest.importorskip("pypdf")
    fr = FetchResult(
        url="https://x.com/a.pdf",
        final_url="https://x.com/a.pdf",
        status=FetchStatus.SUCCESS,
        binary=b"not a real pdf",
        content_type="application/pdf",
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "none"


# ----------------------------------------------------------------------
# Library-missing graceful degrade
# ----------------------------------------------------------------------


def test_pdf_missing_library_returns_none(monkeypatch):
    """If pypdf is absent, _extract_pdf logs and returns extraction_method='none'."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("pypdf not installed (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    fr = FetchResult(
        url="https://x.com/a.pdf",
        final_url="https://x.com/a.pdf",
        status=FetchStatus.SUCCESS,
        binary=_MINIMAL_PDF,
        content_type="application/pdf",
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "none"
