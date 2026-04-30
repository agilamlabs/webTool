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
import time
from abc import ABC, abstractmethod
from contextlib import suppress
from pathlib import Path
from typing import Any

from loguru import logger


def _hash_key(s: str) -> str:
    """Return a 32-char SHA256 prefix of ``s`` for use as a filename."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


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
        if not path.exists():
            return None
        try:
            async with self._lock:
                payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("cache read failed for {k}: {e}", k=key[:60], e=exc)
            return None

        cached_at = float(payload.get("_cached_at", 0))
        if time.time() - cached_at > self._ttl:
            with suppress(OSError):
                path.unlink()
            return None

        data = payload.get("data")
        return data if isinstance(data, dict) else None

    async def set(self, key: str, value: dict[str, Any]) -> None:
        """Cache ``value`` under ``key``. Best-effort -- failures are logged."""
        path = self._dir / f"{_hash_key(key)}.json"
        payload = {
            "_cached_at": time.time(),
            # Truncated key hint helps with debugging but is not used for lookup
            "_key_hint": key[:200],
            "data": value,
        }
        try:
            async with self._lock:
                self._dir.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(payload, default=str, ensure_ascii=False),
                    encoding="utf-8",
                )
                await self._evict_if_needed()
        except OSError as exc:
            logger.warning("cache write failed for {k}: {e}", k=key[:60], e=exc)

    async def _evict_if_needed(self) -> None:
        """Best-effort LRU eviction: delete oldest entries until under cap."""
        if not self._dir.exists():
            return
        files = list(self._dir.glob("*.json"))
        total = sum(f.stat().st_size for f in files)
        if total <= self._max_bytes:
            return
        files.sort(key=lambda f: f.stat().st_mtime)  # oldest first
        for f in files:
            if total <= self._max_bytes:
                break
            try:
                size = f.stat().st_size
                f.unlink()
                total -= size
            except OSError:
                continue

    async def clear(self) -> int:
        """Delete all cache entries. Returns the count removed."""
        if not self._dir.exists():
            return 0
        count = 0
        async with self._lock:
            for f in self._dir.glob("*.json"):
                try:
                    f.unlink()
                    count += 1
                except OSError:
                    continue
        return count
