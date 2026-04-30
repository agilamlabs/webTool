"""Tests for the audit log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from web_agent.audit import AuditLogger
from web_agent.correlation import correlation_scope


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class TestAuditLogger:
    @pytest.mark.asyncio
    async def test_disabled_writes_nothing(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=str(log), enabled=False)
        async with audit.scope("test_op", {"x": 1}):
            pass
        assert not log.exists()

    @pytest.mark.asyncio
    async def test_enabled_writes_one_line_per_call(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=str(log), enabled=True)

        async with audit.scope("op_a", {"q": "foo"}):
            pass
        async with audit.scope("op_b", {"url": "https://example.com"}):
            pass

        entries = _read_lines(log)
        assert len(entries) == 2
        assert entries[0]["method"] == "op_a"
        assert entries[0]["status"] == "success"
        assert entries[0]["args"] == {"q": "foo"}
        assert entries[1]["method"] == "op_b"
        assert entries[1]["args"] == {"url": "https://example.com"}

    @pytest.mark.asyncio
    async def test_records_correlation_id(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=str(log), enabled=True)

        with correlation_scope("test-cid-xyz"):
            async with audit.scope("op", {}):
                pass

        entries = _read_lines(log)
        assert entries[0]["correlation_id"] == "test-cid-xyz"

    @pytest.mark.asyncio
    async def test_records_error_on_exception(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=str(log), enabled=True)

        with pytest.raises(ValueError, match="boom"):
            async with audit.scope("failing_op", {"x": 1}):
                raise ValueError("boom")

        entries = _read_lines(log)
        assert len(entries) == 1
        assert entries[0]["status"] == "error"
        assert "ValueError" in entries[0]["error"]
        assert "boom" in entries[0]["error"]

    @pytest.mark.asyncio
    async def test_records_elapsed_ms(self, tmp_path: Path) -> None:
        import asyncio

        log = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=str(log), enabled=True)

        async with audit.scope("slow_op", {}):
            await asyncio.sleep(0.05)

        entries = _read_lines(log)
        assert entries[0]["elapsed_ms"] >= 40  # 50ms minus jitter

    @pytest.mark.asyncio
    async def test_creates_parent_directory(self, tmp_path: Path) -> None:
        log = tmp_path / "subdir" / "deeper" / "audit.jsonl"
        audit = AuditLogger(path=str(log), enabled=True)
        async with audit.scope("op", {}):
            pass
        assert log.exists()

    @pytest.mark.asyncio
    async def test_caller_can_mutate_entry(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=str(log), enabled=True)

        async with audit.scope("op", {"url": "https://x"}) as entry:
            entry["result_size_bytes"] = 4096
            entry["custom_field"] = "anything"

        entries = _read_lines(log)
        assert entries[0]["result_size_bytes"] == 4096
        assert entries[0]["custom_field"] == "anything"

    @pytest.mark.asyncio
    async def test_jsonl_format_one_object_per_line(self, tmp_path: Path) -> None:
        log = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=str(log), enabled=True)
        for i in range(5):
            async with audit.scope("op", {"i": i}):
                pass
        # Each line must be valid JSON, no trailing whitespace artifacts
        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5
        for line in lines:
            obj = json.loads(line)
            assert "timestamp" in obj
            assert "method" in obj
