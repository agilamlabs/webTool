"""Disk-backed TTL cache for fetch results and search responses.

Cache entries are JSON blobs keyed by SHA256 of the canonical input
(URL for fetches, ``"search:<query>:<max>"`` for searches). Each entry
records the timestamp it was written; reads beyond ``ttl_seconds``
treat the entry as missing and best-effort delete the stale file.

Best-effort LRU eviction (oldest-mtime first) kicks in on writes when
the total cache size exceeds ``max_cache_mb``.

Example::

    cache = DiskCache(cache_dir="cache", ttl_seconds=3600)
    if cached := await cache.get("https://example.com"):
        return FetchResult(**cached)
    result = await fetch_actually(url)
    await cache.set("https://example.com", result.model_dump(mode="json"))
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import time
from abc import ABC, abstractmethod
from contextlib import suppress
from pathlib import Path
from typing import Any

from loguru import logger


def _hash_key(s: str) -> str:
    """Return a 32-char SHA256 prefix of ``s`` for use as a filename."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


def _sanitize_key_hint(key: str) -> str:
    # The hint is debug-only (never used for lookup), so strip anything that
    # could carry a secret: the URL query string (?token=/?api_key=/...) and
    # any ``://user:pass@`` userinfo. Truncate to 200 chars as before.
    hint = key.split("?", 1)[0]
    hint = re.sub(r"://[^/@\s]*@", "://", hint)
    return hint[:200]


class Cache(ABC):
    """Abstract base for cache backends.

    Implementations are responsible for serialization, TTL enforcement,
    and any size/count limits. The protocol is:

    - :meth:`get` returns the cached payload dict, or ``None`` if the
      entry is missing or expired.
    - :meth:`set` stores a payload dict under the given key.
    - :meth:`clear` empties the cache.

    Cache keys are arbitrary strings; implementations hash them to
    produce a filesystem-safe identifier.
    """

    @abstractmethod
    async def get(self, key: str) -> dict[str, Any] | None: ...

    @abstractmethod
    async def set(self, key: str, value: dict[str, Any]) -> None: ...

    @abstractmethod
    async def clear(self) -> int: ...


class DiskCache(Cache):
    """Filesystem-backed TTL cache. One JSON file per entry.

    Args:
        cache_dir: Directory for cached entries. Created lazily on
            first :meth:`set` call.
        ttl_seconds: How long a cached entry is considered fresh.
            Default 1 hour.
        max_cache_mb: Soft cap on total cache size in MB. When
            exceeded after a write, oldest entries (by mtime) are
            deleted until the cache fits. Default 100 MB.

    Notes:
        Concurrent writes across coroutines are serialized via an
        :class:`asyncio.Lock`. Concurrent processes are NOT serialized;
        if multi-process safety matters, use a real KV store instead.
    """

    def __init__(
        self,
        cache_dir: str,
        ttl_seconds: float = 3600.0,
        max_cache_mb: int = 100,
    ) -> None:
        self._dir = Path(cache_dir)
        self._ttl = ttl_seconds
        self._max_bytes = max_cache_mb * 1024 * 1024
        self._lock = asyncio.Lock()

    @property
    def cache_dir(self) -> Path:
        return self._dir

    async def get(self, key: str) -> dict[str, Any] | None:
        """Return the payload for ``key`` if present and unexpired.

        Returns ``None`` on cache miss, expired entry, parse failure,
        or filesystem error. Stale entries are best-effort deleted on
        access.
        """
        path = self._dir / f"{_hash_key(key)}.json"
        # v1.6.16 CACHE-1: offload the blocking stat + read off the event
        # loop (mirrors trace_recorder's ``asyncio.to_thread`` for its
        # append). We still hold ``self._lock`` so the read ordering and
        # cache semantics are unchanged.
        #
        # L2: the TTL comparison and the stale-entry unlink MUST run inside
        # the lock too. If they ran after the lock released, a concurrent
        # set() could write a fresh entry to this same path between our read
        # and our unlink, and a stale reader would then delete that fresh
        # entry. Keeping the whole read/TTL/unlink critical section under the
        # lock closes that race; behavior for a valid unexpired entry is
        # unchanged.
        try:
            async with self._lock:
                payload = await asyncio.to_thread(self._read_payload, path)
                if payload is None:
                    return None
                cached_at = float(payload.get("_cached_at", 0))
                if time.time() - cached_at > self._ttl:
                    with suppress(OSError):
                        await asyncio.to_thread(path.unlink)
                    return None
                data = payload.get("data")
                return data if isinstance(data, dict) else None
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("cache read failed for {k}: {e}", k=key[:60], e=exc)
            return None

    @staticmethod
    def _read_payload(path: Path) -> dict[str, Any] | None:
        """Blocking read+parse of one cache file. Runs in a worker thread.

        Returns ``None`` when the file is absent (cache miss). Raises
        ``OSError`` / ``json.JSONDecodeError`` on a genuine read/parse
        failure so the caller can log+treat it as a miss.
        """
        if not path.exists():
            return None
        result: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return result

    async def set(self, key: str, value: dict[str, Any]) -> None:
        """Cache ``value`` under ``key``. Best-effort -- failures are logged."""
        path = self._dir / f"{_hash_key(key)}.json"
        payload = {
            "_cached_at": time.time(),
            # Truncated key hint helps with debugging but is not used for lookup.
            # Sanitized so a URL key carrying ?token=/?api_key=/user:pass@ never
            # lands unredacted in the plaintext cache file.
            "_key_hint": _sanitize_key_hint(key),
            "data": value,
        }
        text = json.dumps(payload, default=str, ensure_ascii=False)
        try:
            async with self._lock:
                # v1.6.16 CACHE-1: offload the blocking mkdir + write off the
                # event loop. v1.6.16 CACHE-2: write atomically (temp file in
                # the same dir, then ``os.replace``) so a crash mid-write can
                # never leave a half-written, permanently-unreadable cache
                # file -- a partial file would fail ``json.loads`` in get()
                # and be treated as a miss, but os.replace makes the swap
                # all-or-nothing so a valid prior entry is never corrupted.
                await asyncio.to_thread(self._write_atomic, path, text)
                await self._evict_if_needed()
        except OSError as exc:
            logger.warning("cache write failed for {k}: {e}", k=key[:60], e=exc)

    def _write_atomic(self, path: Path, text: str) -> None:
        """Blocking atomic write of one cache file. Runs in a worker thread.

        Writes to a uniquely-named temp file in the SAME directory (so
        ``os.replace`` is a same-filesystem atomic rename) then swaps it
        into place. On any failure the temp file is best-effort removed so
        a crashed write leaves no leftover ``*.tmp`` litter.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self._dir), suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp_path, path)
        except OSError:
            with suppress(OSError):
                tmp_path.unlink()
            raise

    async def _evict_if_needed(self) -> None:
        """Best-effort LRU eviction: delete oldest entries until under cap.

        v1.6.16 CACHE-1: the glob + N*stat() sweep and the unlink loop run
        off the event loop via ``asyncio.to_thread`` -- on a cache dir with
        thousands of entries this issues thousands of stat() syscalls on
        every write, which would otherwise stall all concurrent fetches /
        searches while held under ``self._lock``.
        """
        await asyncio.to_thread(self._evict_blocking)

    def _evict_blocking(self) -> None:
        """Blocking eviction body. Runs in a worker thread."""
        if not self._dir.exists():
            return
        files = list(self._dir.glob("*.json"))
        # L3: stat each file exactly once. A file vanishing mid-sweep (a
        # concurrent unlink, or another process) would otherwise raise
        # FileNotFoundError out of the sum()/sort-key stat() calls, escape to
        # set()'s ``except OSError``, and be mislogged as "cache write
        # failed". Skip vanished files instead.
        entries = []
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, f))
        total = sum(size for _, size, _ in entries)
        if total <= self._max_bytes:
            return
        entries.sort(key=lambda e: e[0])  # oldest first
        for _mtime, size, f in entries:
            if total <= self._max_bytes:
                break
            try:
                f.unlink()
                total -= size
            except OSError:
                continue

    async def clear(self) -> int:
        """Delete all cache entries. Returns the count removed."""
        async with self._lock:
            # v1.6.16 CACHE-1: offload the glob + unlink loop off the loop.
            return await asyncio.to_thread(self._clear_blocking)

    def _clear_blocking(self) -> int:
        """Blocking clear body. Runs in a worker thread."""
        if not self._dir.exists():
            return 0
        count = 0
        for f in self._dir.glob("*.json"):
            try:
                f.unlink()
                count += 1
            except OSError:
                continue
        return count
