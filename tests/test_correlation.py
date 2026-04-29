"""Tests for correlation ID propagation and loguru patching."""

from __future__ import annotations

import json

from web_agent.correlation import (
    correlation_scope,
    get_correlation_id,
    new_correlation_id,
)
from web_agent.models import (
    AgentResult,
    DownloadResult,
    ExtractionResult,
    FetchResult,
    FetchStatus,
    SearchResponse,
)


class TestCorrelationScope:
    def test_outside_scope_returns_none(self) -> None:
        assert get_correlation_id() is None

    def test_scope_sets_value(self) -> None:
        with correlation_scope() as cid:
            assert cid is not None
            assert get_correlation_id() == cid
        assert get_correlation_id() is None

    def test_scope_with_explicit_id(self) -> None:
        with correlation_scope("my-test-id-123") as cid:
            assert cid == "my-test-id-123"
            assert get_correlation_id() == "my-test-id-123"

    def test_nested_scopes_reset_to_outer(self) -> None:
        with correlation_scope("outer") as outer_cid:
            assert get_correlation_id() == "outer"
            with correlation_scope("inner") as inner_cid:
                assert get_correlation_id() == "inner"
                assert inner_cid == "inner"
            assert get_correlation_id() == "outer"
        assert get_correlation_id() is None

    def test_new_correlation_id_returns_uuid(self) -> None:
        cid = new_correlation_id()
        # UUID4 format: 8-4-4-4-12 = 36 chars with 4 hyphens
        assert isinstance(cid, str)
        assert len(cid) == 36
        assert cid.count("-") == 4


class TestCorrelationOnResultModels:
    def test_fetch_result_carries_correlation_id(self) -> None:
        r = FetchResult(
            url="https://x.com",
            final_url="https://x.com",
            status=FetchStatus.SUCCESS,
            correlation_id="cid-123",
        )
        assert r.correlation_id == "cid-123"
        # Round-trip
        restored = FetchResult.model_validate_json(r.model_dump_json())
        assert restored.correlation_id == "cid-123"

    def test_extraction_result_correlation_id(self) -> None:
        r = ExtractionResult(url="https://x.com", correlation_id="trace-1")
        data = json.loads(r.model_dump_json())
        assert data["correlation_id"] == "trace-1"

    def test_download_result_correlation_id_optional(self) -> None:
        # Default None
        r = DownloadResult(
            url="https://x.com",
            filepath="/tmp/x",
            filename="x",
            status=FetchStatus.SUCCESS,
        )
        assert r.correlation_id is None

    def test_agent_result_correlation_id(self) -> None:
        r = AgentResult(
            query="test",
            search=SearchResponse(query="test"),
            correlation_id="agent-cid",
        )
        assert r.correlation_id == "agent-cid"
