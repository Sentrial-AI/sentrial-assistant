"""
Per-process in-memory TTL cache of frequently-accessed user data.

Populated by core/context_prefetch on voice-mode start and after every turn.
Read by evolution/retrieval to inject a [live context] block into the agent's
system prompt — so questions like "what's on my plate today" are answered
from cache instead of paying tool-call latency.

Each entry is keyed by source name (e.g. "todos", "calendar_today") with a
configurable TTL. Reads return the entry whether fresh or stale; the caller
decides what to do (retrieval renders fresh entries verbatim, stale entries
are discarded so the LLM doesn't get yesterday's calendar).

This is process-local memory. On Railway with multiple replicas each replica
has its own cache; that's fine — each replica also runs its own prewarm.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    value: Any
    fetched_at: datetime
    ttl_s: int
    source: str
    error: str | None = None
    # Free-form rendering hint — the retrieval layer uses this to decide
    # which formatter to call. Populated by the prefetcher.
    kind: str = "raw"

    def is_fresh(self) -> bool:
        if self.error:
            return False
        age = (datetime.now(timezone.utc) - self.fetched_at).total_seconds()
        return age < self.ttl_s


class ContextCache:
    """Asyncio-safe TTL cache. One process, many readers, occasional writers."""

    def __init__(self) -> None:
        self._data: dict[str, CacheEntry] = {}
        self._lock = asyncio.Lock()
        # Track in-flight prefetches by key so concurrent callers don't all
        # kick off parallel fetches of the same source.
        self._in_flight: dict[str, asyncio.Task] = {}

    def get(self, key: str) -> CacheEntry | None:
        return self._data.get(key)

    def get_fresh(self, key: str) -> CacheEntry | None:
        e = self._data.get(key)
        if e and e.is_fresh():
            return e
        return None

    async def put(
        self,
        key: str,
        value: Any,
        ttl_s: int,
        source: str,
        kind: str = "raw",
        error: str | None = None,
    ) -> None:
        async with self._lock:
            self._data[key] = CacheEntry(
                value=value,
                fetched_at=datetime.now(timezone.utc),
                ttl_s=ttl_s,
                source=source,
                error=error,
                kind=kind,
            )

    async def invalidate(self, *keys: str) -> None:
        async with self._lock:
            for k in keys:
                self._data.pop(k, None)

    def all_fresh(self) -> dict[str, CacheEntry]:
        return {k: e for k, e in self._data.items() if e.is_fresh()}

    def snapshot(self) -> dict[str, dict]:
        """Diagnostic view — used by /api/voice/prewarm response."""
        out: dict[str, dict] = {}
        for k, e in self._data.items():
            out[k] = {
                "source": e.source,
                "fresh": e.is_fresh(),
                "ttl_s": e.ttl_s,
                "fetched_at": e.fetched_at.isoformat(),
                "error": e.error,
                "kind": e.kind,
            }
        return out


_CACHE = ContextCache()


def cache() -> ContextCache:
    return _CACHE
