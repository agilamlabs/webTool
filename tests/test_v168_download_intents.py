"""v1.6.8 download-intent capture tests (page.on('download') notification)."""

from __future__ import annotations

from unittest.mock import MagicMock

from web_agent import DiagnosticsConfig, NetworkCollector


def _make_page() -> MagicMock:
    page = MagicMock()
    page._listeners: dict[str, list] = {}

    def _on(event, handler):
        page._listeners.setdefault(event, []).append(handler)

    page.on.side_effect = _on
    return page


def _emit(page: MagicMock, event: str, payload) -> None:
    for handler in page._listeners.get(event, []):
        handler(payload)


# ---------------------------------------------------------------------------
# off-by-default
# ---------------------------------------------------------------------------


def test_download_intents_off_by_default() -> None:
    nc = NetworkCollector(DiagnosticsConfig())  # both switches False
    page = _make_page()
    nc.attach(page)
    assert "download" not in page._listeners
    assert nc.download_intents_for(page) == []


# ---------------------------------------------------------------------------
# Captured via page.on('download') notification
# ---------------------------------------------------------------------------


def test_download_intents_captured_via_page_on_download() -> None:
    nc = NetworkCollector(DiagnosticsConfig(capture_download_intents=True))
    page = _make_page()
    nc.attach(page)
    dl = MagicMock()
    dl.url = "https://example.com/report.pdf"
    _emit(page, "download", dl)
    assert nc.download_intents_for(page) == ["https://example.com/report.pdf"]


# ---------------------------------------------------------------------------
# Page-on-download listener coexists with expect_download (no interference)
# ---------------------------------------------------------------------------


def test_download_intents_independent_of_explicit_expect_download() -> None:
    """``page.on('download')`` should NOT be wired when only
    ``capture_network`` is true -- the two diagnostics are independent."""
    nc = NetworkCollector(DiagnosticsConfig(capture_network=True))
    page = _make_page()
    nc.attach(page)
    # capture_network alone should NOT register a 'download' listener
    assert "download" not in page._listeners
    # Request/response/requestfailed listeners DO appear:
    assert "request" in page._listeners


# ---------------------------------------------------------------------------
# Multiple download events accumulate in order
# ---------------------------------------------------------------------------


def test_download_intents_accumulate_in_order() -> None:
    nc = NetworkCollector(DiagnosticsConfig(capture_download_intents=True))
    page = _make_page()
    nc.attach(page)
    for url in (
        "https://example.com/a.pdf",
        "https://example.com/b.pdf",
        "https://example.com/c.pdf",
    ):
        dl = MagicMock()
        dl.url = url
        _emit(page, "download", dl)
    assert nc.download_intents_for(page) == [
        "https://example.com/a.pdf",
        "https://example.com/b.pdf",
        "https://example.com/c.pdf",
    ]


# ---------------------------------------------------------------------------
# clear() drops download intents along with network events
# ---------------------------------------------------------------------------


def test_download_intents_cleared_by_clear() -> None:
    nc = NetworkCollector(DiagnosticsConfig(capture_download_intents=True))
    page = _make_page()
    nc.attach(page)
    dl = MagicMock()
    dl.url = "https://example.com/a.pdf"
    _emit(page, "download", dl)
    assert len(nc.download_intents_for(page)) == 1
    nc.clear(page)
    assert nc.download_intents_for(page) == []


# ---------------------------------------------------------------------------
# FetchResult / ActionSequenceResult surface fields exist
# ---------------------------------------------------------------------------


def test_fetch_result_exposes_download_candidates_runtime() -> None:
    from web_agent.models import FetchResult, FetchStatus

    fr = FetchResult(url="https://x", final_url="https://x", status=FetchStatus.SUCCESS)
    assert fr.download_candidates_runtime == []


def test_action_sequence_result_exposes_download_candidates() -> None:
    from web_agent.models import ActionSequenceResult

    asr = ActionSequenceResult(url="https://x")
    assert asr.download_candidates == []
    assert asr.api_candidates == []
    assert asr.network_events == []
