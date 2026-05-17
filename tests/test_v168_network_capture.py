"""v1.6.8 NetworkCollector unit tests.

Uses mock Playwright Request/Response objects so we can exercise the
collector in isolation without launching a real browser.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from web_agent import DiagnosticsConfig, NetworkCollector, NetworkEvent


def _make_mock_page() -> MagicMock:
    """Return a MagicMock that records page.on() registrations."""
    page = MagicMock()
    page._listeners: dict[str, list] = {}

    def _on(event: str, handler) -> None:
        page._listeners.setdefault(event, []).append(handler)

    page.on.side_effect = _on
    return page


def _emit(page: MagicMock, event: str, payload) -> None:
    """Invoke every handler the collector registered for *event*."""
    for handler in page._listeners.get(event, []):
        handler(payload)


def _make_request(
    url: str = "https://api.example.com/data",
    method: str = "GET",
    resource_type: str = "xhr",
    headers: dict[str, str] | None = None,
) -> MagicMock:
    req = MagicMock()
    req.url = url
    req.method = method
    req.resource_type = resource_type
    req.headers = headers or {"user-agent": "test"}
    req.failure = None
    return req


def _make_response(req: MagicMock, status: int = 200, content_type: str = "application/json") -> MagicMock:
    resp = MagicMock()
    resp.request = req
    resp.status = status
    resp.headers = {"content-type": content_type}
    return resp


# ---------------------------------------------------------------------------
# Default-off behavior
# ---------------------------------------------------------------------------


def test_network_collector_off_by_default_attach_is_noop() -> None:
    nc = NetworkCollector(DiagnosticsConfig())  # capture switches both False
    page = _make_mock_page()
    nc.attach(page)
    # No listeners should have been registered.
    assert page._listeners == {}
    assert nc.events_for(page) == []


def test_network_collector_attach_idempotent() -> None:
    nc = NetworkCollector(DiagnosticsConfig(capture_network=True))
    page = _make_mock_page()
    nc.attach(page)
    first = {k: len(v) for k, v in page._listeners.items()}
    nc.attach(page)
    nc.attach(page)
    second = {k: len(v) for k, v in page._listeners.items()}
    assert first == second  # idempotent -- no duplicate listeners


# ---------------------------------------------------------------------------
# Request / Response capture
# ---------------------------------------------------------------------------


def test_network_collector_records_request_response_pair() -> None:
    nc = NetworkCollector(DiagnosticsConfig(capture_network=True))
    page = _make_mock_page()
    nc.attach(page)
    req = _make_request()
    _emit(page, "request", req)
    _emit(page, "response", _make_response(req))
    events = nc.events_for(page)
    assert len(events) == 2
    assert events[0].event_type == "request"
    assert events[1].event_type == "response"
    assert events[1].status_code == 200
    assert events[1].content_type == "application/json"


def test_network_collector_records_requestfailed() -> None:
    nc = NetworkCollector(DiagnosticsConfig(capture_network=True))
    page = _make_mock_page()
    nc.attach(page)
    req = _make_request()
    req.failure = {"errorText": "net::ERR_NAME_NOT_RESOLVED"}
    _emit(page, "requestfailed", req)
    events = nc.events_for(page)
    assert len(events) == 1
    assert events[0].event_type == "requestfailed"
    assert events[0].failure_text == "net::ERR_NAME_NOT_RESOLVED"


def test_network_collector_enforces_max_events_cap() -> None:
    nc = NetworkCollector(DiagnosticsConfig(capture_network=True, max_network_events=3))
    page = _make_mock_page()
    nc.attach(page)
    for i in range(10):
        _emit(page, "request", _make_request(url=f"https://api.example.com/{i}"))
    events = nc.events_for(page)
    # deque(maxlen=3) keeps only the last 3
    assert len(events) == 3
    assert events[-1].url.endswith("/9")


def test_network_collector_resource_type_filter_excludes_image() -> None:
    nc = NetworkCollector(
        DiagnosticsConfig(
            capture_network=True, network_resource_types=["xhr", "fetch"]
        )
    )
    page = _make_mock_page()
    nc.attach(page)
    _emit(page, "request", _make_request(resource_type="image"))
    _emit(page, "request", _make_request(resource_type="xhr"))
    events = nc.events_for(page)
    assert len(events) == 1
    assert events[0].resource_type == "xhr"


def test_network_collector_omits_headers_by_default() -> None:
    nc = NetworkCollector(DiagnosticsConfig(capture_network=True))
    page = _make_mock_page()
    nc.attach(page)
    _emit(page, "request", _make_request(headers={"authorization": "Bearer secret"}))
    events = nc.events_for(page)
    assert events[0].request_headers == {}


def test_network_collector_includes_headers_when_opted_in() -> None:
    nc = NetworkCollector(
        DiagnosticsConfig(capture_network=True, include_request_headers=True)
    )
    page = _make_mock_page()
    nc.attach(page)
    _emit(
        page, "request", _make_request(headers={"authorization": "Bearer secret"})
    )
    events = nc.events_for(page)
    assert events[0].request_headers.get("authorization") == "Bearer secret"


# ---------------------------------------------------------------------------
# api_candidates derivation
# ---------------------------------------------------------------------------


def test_api_candidates_filter_xhr_json() -> None:
    nc = NetworkCollector(DiagnosticsConfig(capture_network=True))
    page = _make_mock_page()
    nc.attach(page)
    req = _make_request(resource_type="xhr")
    _emit(page, "request", req)
    _emit(page, "response", _make_response(req, content_type="application/json"))
    cands = nc.api_candidates_for(page)
    assert cands == ["https://api.example.com/data"]


def test_api_candidates_filter_excludes_html_documents() -> None:
    nc = NetworkCollector(
        DiagnosticsConfig(
            capture_network=True, network_resource_types=["xhr", "fetch", "document"]
        )
    )
    page = _make_mock_page()
    nc.attach(page)
    req = _make_request(resource_type="document")
    _emit(page, "request", req)
    _emit(page, "response", _make_response(req, content_type="text/html"))
    cands = nc.api_candidates_for(page)
    assert cands == []


def test_api_candidates_dedupes_preserving_order() -> None:
    nc = NetworkCollector(DiagnosticsConfig(capture_network=True))
    page = _make_mock_page()
    nc.attach(page)
    for url in ["https://a.example/d", "https://b.example/d", "https://a.example/d"]:
        req = _make_request(url=url, resource_type="xhr")
        _emit(page, "request", req)
        _emit(page, "response", _make_response(req))
    cands = nc.api_candidates_for(page)
    assert cands == ["https://a.example/d", "https://b.example/d"]


# ---------------------------------------------------------------------------
# Download intents
# ---------------------------------------------------------------------------


def test_download_intents_captured_when_opted_in() -> None:
    nc = NetworkCollector(DiagnosticsConfig(capture_download_intents=True))
    page = _make_mock_page()
    nc.attach(page)
    dl = MagicMock()
    dl.url = "https://example.com/report.pdf"
    _emit(page, "download", dl)
    assert nc.download_intents_for(page) == ["https://example.com/report.pdf"]


def test_download_intents_off_by_default() -> None:
    nc = NetworkCollector(DiagnosticsConfig())  # both off
    page = _make_mock_page()
    nc.attach(page)
    # No download listener should have been registered.
    assert "download" not in page._listeners
    assert nc.download_intents_for(page) == []


# ---------------------------------------------------------------------------
# Popup auto-attach via TabManager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_collector_popup_auto_attach_via_tab_manager() -> None:
    """Regression test: TabManager._on_new_page should call collector.attach()."""
    from web_agent.tab_manager import TabManager

    nc = NetworkCollector(DiagnosticsConfig(capture_network=True))
    # The TabManager only cares about ctx.on("page", ...) registration.
    ctx = MagicMock()
    ctx._page_handler = None

    def _on(event: str, handler):
        if event == "page":
            ctx._page_handler = handler

    ctx.on.side_effect = _on
    TabManager(ctx, network_collector=nc)
    assert ctx._page_handler is not None

    # Simulate Playwright firing "page" for a popup
    popup = _make_mock_page()
    popup.is_closed.return_value = False
    ctx._page_handler(popup)

    # popup should have had the network collector attached
    assert popup._listeners.get("request") is not None
    assert nc.events_for(popup) == []  # no events yet, but tracked


# ---------------------------------------------------------------------------
# Result-model surface
# ---------------------------------------------------------------------------


def test_network_event_is_pydantic_model() -> None:
    evt = NetworkEvent(event_type="request", url="https://example.com/x")
    d = evt.model_dump()
    assert d["event_type"] == "request"
    assert d["url"] == "https://example.com/x"
