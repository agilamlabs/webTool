"""Tests for v1.6.2 CSV + DOCX extraction (issue #11)."""

from __future__ import annotations

from io import BytesIO

import pytest
from web_agent.config import AppConfig
from web_agent.content_extractor import (
    ContentExtractor,
    _is_csv,
    _is_docx,
)
from web_agent.models import FetchResult, FetchStatus

# ----------------------------------------------------------------------
# Format detection
# ----------------------------------------------------------------------


def test_is_csv_by_content_type():
    fr = FetchResult(
        url="https://x.com/data",
        final_url="https://x.com/data",
        status=FetchStatus.SUCCESS,
        content_type="text/csv",
    )
    assert _is_csv(fr) is True


def test_is_csv_by_extension():
    fr = FetchResult(
        url="https://x.com/data.csv",
        final_url="https://x.com/data.csv",
        status=FetchStatus.SUCCESS,
    )
    assert _is_csv(fr) is True


def test_is_csv_tsv_extension():
    fr = FetchResult(
        url="https://x.com/data.tsv",
        final_url="https://x.com/data.tsv",
        status=FetchStatus.SUCCESS,
    )
    assert _is_csv(fr) is True


def test_is_docx_by_content_type():
    fr = FetchResult(
        url="https://x.com/letter",
        final_url="https://x.com/letter",
        status=FetchStatus.SUCCESS,
        content_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )
    assert _is_docx(fr) is True


def test_is_docx_by_extension():
    fr = FetchResult(
        url="https://x.com/letter.docx",
        final_url="https://x.com/letter.docx",
        status=FetchStatus.SUCCESS,
    )
    assert _is_docx(fr) is True


# ----------------------------------------------------------------------
# CSV extraction (stdlib, no extra dep)
# ----------------------------------------------------------------------


def test_csv_extraction_basic():
    blob = b"name,value\nalice,42\nbob,7\n"
    fr = FetchResult(
        url="https://x.com/a.csv",
        final_url="https://x.com/a.csv",
        status=FetchStatus.SUCCESS,
        binary=blob,
        content_type="text/csv",
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "csv"
    assert res.content is not None
    assert "alice" in res.content
    assert "bob" in res.content


def test_csv_extraction_semicolon_delimiter():
    """Sniffer should detect ; as delimiter and emit TSV output."""
    blob = b"name;value\nalice;42\nbob;7\n"
    fr = FetchResult(
        url="https://x.com/a.csv",
        final_url="https://x.com/a.csv",
        status=FetchStatus.SUCCESS,
        binary=blob,
        content_type="text/csv",
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "csv"
    assert "alice\t42" in res.content


def test_csv_extraction_tsv():
    blob = b"name\tvalue\nalice\t42\nbob\t7\n"
    fr = FetchResult(
        url="https://x.com/a.tsv",
        final_url="https://x.com/a.tsv",
        status=FetchStatus.SUCCESS,
        binary=blob,
        content_type="text/tab-separated-values",
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "csv"


def test_csv_extraction_utf8_bom():
    blob = b"\xef\xbb\xbfname,value\nalice,42\n"
    fr = FetchResult(
        url="https://x.com/a.csv",
        final_url="https://x.com/a.csv",
        status=FetchStatus.SUCCESS,
        binary=blob,
        content_type="text/csv",
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "csv"
    # BOM should be stripped from the first column header
    assert res.content.startswith("name")


def test_csv_extraction_latin1_fallback():
    # Bytes that are NOT valid utf-8 but valid latin-1
    blob = "café,value\n".encode("latin-1")
    fr = FetchResult(
        url="https://x.com/a.csv",
        final_url="https://x.com/a.csv",
        status=FetchStatus.SUCCESS,
        binary=blob,
        content_type="text/csv",
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "csv"


def test_csv_extraction_empty_returns_none():
    fr = FetchResult(
        url="https://x.com/a.csv",
        final_url="https://x.com/a.csv",
        status=FetchStatus.SUCCESS,
        binary=b"",
        content_type="text/csv",
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "none"


# ----------------------------------------------------------------------
# DOCX extraction (synthesized fixture; skipped when python-docx absent)
# ----------------------------------------------------------------------


def _build_docx() -> bytes:
    docx = pytest.importorskip("docx")
    doc = docx.Document()
    doc.add_paragraph("Hello DOCX World")
    doc.add_paragraph("This is a second paragraph.")
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text = "header1"
    table.rows[0].cells[1].text = "header2"
    table.rows[1].cells[0].text = "value1"
    table.rows[1].cells[1].text = "value2"
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_docx_extraction_returns_text():
    blob = _build_docx()
    fr = FetchResult(
        url="https://x.com/a.docx",
        final_url="https://x.com/a.docx",
        status=FetchStatus.SUCCESS,
        binary=blob,
        content_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "docx"
    assert "Hello DOCX World" in res.content
    assert "header1\theader2" in res.content
    assert "value1\tvalue2" in res.content


def test_docx_missing_library_returns_none(monkeypatch):
    """If python-docx is absent, _extract_docx degrades gracefully."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "docx" or name.startswith("docx."):
            raise ImportError("python-docx not installed (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    fr = FetchResult(
        url="https://x.com/a.docx",
        final_url="https://x.com/a.docx",
        status=FetchStatus.SUCCESS,
        binary=b"PK\x03\x04not really a docx",
        content_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )
    res = ContentExtractor(AppConfig()).extract(fr)
    assert res.extraction_method == "none"
