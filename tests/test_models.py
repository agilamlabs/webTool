"""Tests for Pydantic model serialization and round-trip JSON."""

from __future__ import annotations

import json

from web_agent.models import (
    AgentResult,
    DownloadResult,
    ExtractionResult,
    FetchResult,
    FetchStatus,
    SearchResponse,
    SearchResultItem,
)


class TestSearchResultItem:
    def test_basic_creation(self) -> None:
        item = SearchResultItem(
            position=1,
            title="Test Result",
            url="https://example.com",
            displayed_url="example.com",
            snippet="A test snippet.",
        )
        assert item.position == 1
        assert item.title == "Test Result"
        assert item.url == "https://example.com"

    def test_defaults(self) -> None:
        item = SearchResultItem(position=1, title="T", url="https://x.com")
        assert item.displayed_url == ""
        assert item.snippet == ""

    def test_json_round_trip(self) -> None:
        item = SearchResultItem(
            position=2, title="Example", url="https://example.com"
        )
        json_str = item.model_dump_json()
        restored = SearchResultItem.model_validate_json(json_str)
        assert restored == item


class TestSearchResponse:
    def test_empty_results(self) -> None:
        resp = SearchResponse(query="test")
        assert resp.total_results == 0
        assert resp.results == []
        assert resp.searched_at is not None

    def test_with_results(self) -> None:
        items = [
            SearchResultItem(position=i, title=f"R{i}", url=f"https://r{i}.com")
            for i in range(1, 4)
        ]
        resp = SearchResponse(query="test query", total_results=3, results=items)
        assert len(resp.results) == 3
        assert resp.results[0].title == "R1"

    def test_json_round_trip(self) -> None:
        resp = SearchResponse(
            query="q",
            total_results=1,
            results=[
                SearchResultItem(position=1, title="T", url="https://x.com")
            ],
        )
        restored = SearchResponse.model_validate_json(resp.model_dump_json())
        assert restored.query == resp.query
        assert len(restored.results) == 1


class TestFetchStatus:
    def test_enum_values(self) -> None:
        assert FetchStatus.SUCCESS == "success"
        assert FetchStatus.TIMEOUT == "timeout"
        assert FetchStatus.HTTP_ERROR == "http_error"

    def test_json_serialization(self) -> None:
        result = FetchResult(
            url="https://x.com",
            final_url="https://x.com",
            status=FetchStatus.SUCCESS,
        )
        data = json.loads(result.model_dump_json())
        assert data["status"] == "success"


class TestFetchResult:
    def test_success_result(self) -> None:
        result = FetchResult(
            url="https://example.com",
            final_url="https://example.com/page",
            status_code=200,
            status=FetchStatus.SUCCESS,
            html="<html></html>",
            response_time_ms=150.5,
        )
        assert result.status == FetchStatus.SUCCESS
        assert result.html == "<html></html>"

    def test_error_result(self) -> None:
        result = FetchResult(
            url="https://bad.com",
            final_url="https://bad.com",
            status_code=404,
            status=FetchStatus.HTTP_ERROR,
            error_message="Not found",
        )
        assert result.error_message == "Not found"
        assert result.html is None


class TestExtractionResult:
    def test_full_extraction(self) -> None:
        result = ExtractionResult(
            url="https://example.com",
            title="Test Page",
            description="A test page",
            author="Author",
            content="Main content here",
            extraction_method="trafilatura",
            content_length=17,
        )
        assert result.extraction_method == "trafilatura"
        assert result.content_length == 17

    def test_empty_extraction(self) -> None:
        result = ExtractionResult(url="https://x.com")
        assert result.extraction_method == "none"
        assert result.content is None
        assert result.content_length == 0


class TestDownloadResult:
    def test_successful_download(self) -> None:
        result = DownloadResult(
            url="https://example.com/file.pdf",
            filepath="/tmp/file.pdf",
            filename="file.pdf",
            size_bytes=1024,
            content_type="application/pdf",
            status=FetchStatus.SUCCESS,
        )
        assert result.size_bytes == 1024
        assert result.status == FetchStatus.SUCCESS


class TestAgentResult:
    def test_full_pipeline_result(self) -> None:
        search = SearchResponse(
            query="test",
            total_results=1,
            results=[
                SearchResultItem(position=1, title="R", url="https://r.com")
            ],
        )
        extraction = ExtractionResult(
            url="https://r.com",
            title="Page",
            content="Content",
            extraction_method="trafilatura",
            content_length=7,
        )
        result = AgentResult(
            query="test",
            search=search,
            pages=[extraction],
            total_time_ms=500.0,
        )
        assert len(result.pages) == 1
        assert result.errors == []

    def test_json_round_trip(self) -> None:
        search = SearchResponse(query="q")
        result = AgentResult(
            query="q", search=search, errors=["err1"], total_time_ms=100.0
        )
        restored = AgentResult.model_validate_json(result.model_dump_json())
        assert restored.errors == ["err1"]
        assert restored.total_time_ms == 100.0
