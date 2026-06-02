"""Append-only JSONL audit log of every Agent operation.

Distinct from regular logging:
- Audit log is structured (one JSON object per line)
- Records ONLY public Agent operations (start + end), not internal events
- Survives restarts (file-backed, not in-memory)
- Includes correlation_id for cross-referencing with regular logs

Each entry is a single JSON object on its own line:

.. code-block:: json

    {
      "timestamp": "2026-04-30T15:34:11.123456+00:00",
      "correlation_id": "abc123",
      "method": "fetch_and_extract",
      "args": {"url": "https://example.com"},
      "status": "success",
      "elapsed_ms": 432.1
    }

Usage from inside :class:`Agent`::

    async with self._audit.scope("fetch_and_extract", {"url": url}):
        result = await self._fetcher.fetch(url)
        # status defaults to "success"; raise on error to record "error"
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger


class AuditLogger:
    """Append-only JSONL audit log.

    Args:
        path: Filesystem path to the JSONL log. Created on first write.
        enabled: Master kill-switch. When False, all methods are no-ops.

    Notes:
        Writes are serialized via an :class:`asyncio.Lock` so concurrent
        Agent calls don't interleave inside a single line. Persistence is
        fsync-not-guaranteed -- callers needing durability should pair
        with a higher-level mechanism.
    """

    def __init__(self, path: str = "audit.jsonl", enabled: bool = False) -> None:
        self._enabled = enabled
        self._path = Path(path)
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def path(self) -> Path:
        return self._path

    @asynccontextmanager
    async def scope(
        self, method: str, args: dict[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Context manager wrapping an Agent method call.

        Records: timestamp, correlation_id, method, args, status,
        elapsed_ms, and (on error) repr(exception). The yielded dict
        is mutable -- callers can stash extra fields (e.g. result_url,
        result_count) which will be persisted alongside the standard
        fields.
        """
        # Local imports to avoid circular: correlation imports utils which
        # imports config; trace_recorder hosts the shared redaction helper.
        from .correlation import get_correlation_id
        from .trace_recorder import redact_sensitive_mapping

        if not self._enabled:
            yield {}
            return

        start = time.perf_counter()
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "correlation_id": get_correlation_id(),
            "method": method,
            # v1.6.16 AUDIT-1: scrub top-level sensitive kwargs (password /
            # token / secret / ...) before they hit the audit sink. Shallow
            # by key name -- nested dicts are not recursed (callers that pass
            # secret-bearing nested structures, e.g. skill inputs, redact at
            # their own call site, cf. AG-2).
            "args": redact_sensitive_mapping(dict(args)) if args else {},
            "status": "started",
        }
        try:
            yield entry
            # If caller didn't override, default to success.
            if entry.get("status") == "started":
                entry["status"] = "success"
        except Exception as exc:
            entry["status"] = "error"
            entry["error"] = repr(exc)
            raise
        finally:
            entry["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 2)
            await self._write(entry)

    async def _write(self, entry: dict[str, Any]) -> None:
        """Append a single JSON line to the log file."""
        line = json.dumps(entry, default=str, ensure_ascii=False) + "\n"
        try:
            async with self._lock:
                # Mirror SessionTraceRecorder._append_line: do the blocking
                # open()+write off the event loop via asyncio.to_thread. We
                # still hold self._lock (write ordering preserved), but the
                # loop stays responsive during disk I/O.
                self._path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(self._append_line, self._path, line)
        except OSError as exc:
            # Don't crash the agent if the audit file can't be written --
            # log the failure to the normal logger and continue.
            logger.warning("Failed to write audit log entry: {e}", e=exc)

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        """Blocking append of one pre-serialized line. Runs in a worker
        thread via :func:`asyncio.to_thread` so it never blocks the loop.
        """
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
