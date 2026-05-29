"""v1.6.8: Session replay traces.

A ``SessionTraceRecorder`` writes one JSONL file per session under
``<diagnostics.trace_dir>/<session_id>.jsonl``. Each line is a single
action record::

    {
        "ts": "2026-05-17T11:22:33.456+00:00",
        "ordinal": 0,
        "session_id": "...",
        "correlation_id": "...",
        "method": "action.click",
        "args": {"selector": "#submit"},
        "status": "success",
        "elapsed_ms": 142.5,
        "url": "https://example.com/login"
    }

``Agent.replay_trace(path)`` reads the JSONL back, reconstructs the
``Action`` discriminated-union members via Pydantic's ``TypeAdapter``,
and re-executes them against a fresh page.

Design choice -- this is intentionally **not** an extension of
``AuditLogger``:

* ``AuditLogger`` records one Agent-call per entry (interact / fetch /
  screenshot), keyed by ``correlation_id``. ``SessionTraceRecorder``
  records one *action* per entry, keyed by ``session_id``.
* Audit logs are forensics / compliance artifacts; traces are
  developer ergonomics (replay / debug). Different retention, different
  file shapes, different consumers.

Disabled by default -- when ``DiagnosticsConfig.trace_enabled=False``,
:meth:`record` returns immediately without touching disk.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from .correlation import get_correlation_id
from .utils import _is_cross_platform_absolute

if TYPE_CHECKING:  # pragma: no cover -- types only
    from .config import DiagnosticsConfig


# Restrict session_id chars going into filenames to prevent path traversal
# even though session_ids are minted internally (defense in depth).
_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9._\-]+$")

_REDACTED = "***REDACTED***"

# v1.6.14 B-8: action methods whose args carry user-typed secrets
# (passwords, tokens). Maps ``method`` -> the arg key holding the secret.
# ``action.fill`` -> FillInput.value; ``action.type`` -> TypeInput.text;
# ``action.type_text`` -> TypeTextInput.text. The recorded value is
# replaced with a placeholder in the SERIALIZED copy only.
_SENSITIVE_ARG_BY_METHOD: dict[str, str] = {
    "action.fill": "value",
    "action.type": "text",
    "action.type_text": "text",
}


def _redact_args(method: str, args: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *args* with any secret field redacted for *method*.

    Returns the original dict unchanged (no copy) when there's nothing to
    redact, so the common path stays allocation-free. Never mutates the
    caller's dict -- it makes a shallow copy only when a redaction applies.
    """
    key = _SENSITIVE_ARG_BY_METHOD.get(method)
    if key is None or key not in args:
        return args
    redacted = dict(args)
    redacted[key] = _REDACTED
    return redacted


class SessionTraceRecorder:
    """Append-only per-session JSONL action log.

    Args:
        diag: live ``DiagnosticsConfig``. Reads ``trace_enabled`` and
            ``trace_dir``. Changes to the config after construction are
            honoured at next record() call.
        base_dir: ``AppConfig.base_dir`` -- relative ``trace_dir`` paths
            are resolved against this. Absolute trace_dir overrides.
    """

    def __init__(self, diag: DiagnosticsConfig, base_dir: str) -> None:
        self._diag = diag
        raw_str = str(diag.trace_dir)
        raw = Path(raw_str)
        # _is_cross_platform_absolute is the v1.6.4 utility that handles
        # the Windows vs POSIX absolute-path semantics correctly. It takes
        # a string (not a Path), so we pass raw_str.
        base = raw if _is_cross_platform_absolute(raw_str) else Path(base_dir).resolve() / raw
        # v1.6.14 B-1: resolve ONCE at construction. Writes (path_for) and
        # the containment root (load_entries) must share an identical,
        # symlink-collapsed base -- otherwise a symlink component in
        # trace_dir makes the write path differ from the check root and
        # legitimately-written traces fail the relative_to() containment
        # test (becoming unreadable via replay).
        self._dir = base.resolve()
        self._lock = asyncio.Lock()
        # ordinal counter per-session so concurrent record() calls for
        # the same session_id always produce a strict monotonic ordering
        # (the surrounding JSONL append also preserves order, but a
        # standalone ordinal lets a consumer detect gaps from rotation).
        self._counters: dict[str, int] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._diag.trace_enabled)

    @property
    def trace_dir(self) -> Path:
        return self._dir

    def path_for(self, session_id: str) -> Path:
        """Return the JSONL path for a given session_id.

        Raises ValueError if session_id contains characters that could
        escape the trace_dir (path traversal defense; sessions are minted
        internally, but better safe).
        """
        if not _SAFE_SESSION_ID.match(session_id):
            raise ValueError(f"Unsafe session_id for trace filename: {session_id!r}")
        return self._dir / f"{session_id}.jsonl"

    async def record(
        self,
        *,
        session_id: str,
        method: str,
        args: dict[str, Any],
        status: str,
        elapsed_ms: float,
        url: str | None = None,
    ) -> None:
        """Append one trace entry for *session_id*.

        Best-effort: OS errors are logged at WARNING and swallowed so a
        full disk / permission issue never breaks a sequence. The
        ``trace_enabled`` short-circuit at the top makes this a single
        attribute read in the disabled path.
        """
        if not self.enabled:
            return
        async with self._lock:
            ordinal = self._counters.get(session_id, 0)
            self._counters[session_id] = ordinal + 1
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                path = self.path_for(session_id)
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Trace dir/path setup failed for {sid}: {e}",
                    sid=session_id,
                    e=exc,
                )
                return
            entry: dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "ordinal": ordinal,
                "session_id": session_id,
                "correlation_id": get_correlation_id(),
                "method": method,
                # v1.6.14 B-8: redact user-typed secrets (passwords/tokens
                # in fill/type values) in the serialized copy. _redact_args
                # never mutates the caller's dict / the live action object.
                "args": _redact_args(method, args),
                "status": status,
                "elapsed_ms": round(elapsed_ms, 2),
            }
            if url is not None:
                # Captured at sequence-start level, not per-action -- but
                # surfacing it on every entry simplifies the replay loader.
                entry["url"] = url
            line = json.dumps(entry, default=str, ensure_ascii=False) + "\n"
            try:
                # v1.6.14 B-6: do the blocking open()+write off the event
                # loop. We still hold self._lock (write ordering preserved),
                # but to_thread keeps the loop responsive during disk I/O.
                await asyncio.to_thread(self._append_line, path, line)
            except OSError as exc:
                logger.warning(
                    "Trace write failed for {sid} -> {p}: {e}",
                    sid=session_id,
                    p=path,
                    e=exc,
                )

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        """Blocking append of one pre-serialized line. Runs in a worker
        thread via :func:`asyncio.to_thread` so it never blocks the loop.
        """
        with path.open("a", encoding="utf-8") as f:
            f.write(line)

    def list_traces(self) -> list[str]:
        """Return session_ids of all JSONL files in the trace_dir.

        Returns an empty list when the dir does not exist (trace_enabled
        was never True). Files with non-`.jsonl` suffixes are ignored.
        """
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.jsonl"))

    def load_entries(self, trace_file: str | Path) -> list[dict[str, Any]]:
        """Parse a trace JSONL into a list of dict entries in file order.

        Raises FileNotFoundError if the path doesn't exist. Empty lines
        and lines that fail JSON parsing are skipped with a WARNING.

        v1.6.14 C-3 defense-in-depth: ``Agent.replay_trace`` is the
        primary chokepoint that validates the path lives inside
        ``trace_dir``, but ``load_entries`` is a public method on the
        recorder and could be called directly by integrators. Repeat the
        containment check here so an LLM-driven path (or any future
        caller) can't bypass it by skipping the Agent layer. Existence
        is checked first so the long-standing ``FileNotFoundError``
        contract for missing paths is preserved -- the threat C-3
        addresses is *content disclosure*, not directory probing.
        """
        p = Path(trace_file).resolve()
        if not p.exists():
            raise FileNotFoundError(f"Trace file not found: {p}")
        # v1.6.14 B-1: self._dir is already resolved at construction, so it
        # is the canonical containment root shared with the write path.
        trace_root = self._dir
        try:
            p.relative_to(trace_root)
        except ValueError as e:
            raise ValueError(f"trace_file must be inside trace_dir ({trace_root}); got {p}") from e
        entries: list[dict[str, Any]] = []
        with p.open(encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        entries.append(obj)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Skipping malformed trace line {n} in {p}: {e}",
                        n=lineno,
                        p=p,
                        e=exc,
                    )
        return entries


__all__ = ["SessionTraceRecorder"]
