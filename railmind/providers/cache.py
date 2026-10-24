"""Route-keyed cache with stale-serve and single-flight.

Three decisions, each forced by a real constraint:

1. **The key is the route, not the user.** `(from, to, date, class)` means a
   hundred people asking about CSMT to PUNE tomorrow share one upstream fetch.
   On a 100-request monthly budget this is the difference between a product and a
   demo that dies on the second visitor.

2. **Stale data beats an error, up to a point.** A 14-minute-old seat count is
   still useful; a 3-hour-old live position is a lie. So each kind has a TTL and
   a separate maximum staleness, and past the latter we raise.

3. **Single-flight.** Ten concurrent requests for the same route must produce one
   upstream call, not ten. Our own retries are indistinguishable from a bot to
   Akamai, so the fan-out has to be deduplicated at the bottom, not hoped for at
   the top.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar

from railmind.core.errors import ProviderError
from railmind.core.types import Query

T = TypeVar("T")


class CacheKind(StrEnum):
    """What is being cached. Different kinds have very different shelf lives."""

    TIMETABLE = "timetable"
    AVAILABILITY = "availability"
    FARE = "fare"
    LIVE = "live"
    SEARCH = "search"


#: Seconds a value stays fresh. From BUILD-ORDER Decision 5, unchanged.
DEFAULT_TTL_S: dict[CacheKind, float] = {
    CacheKind.TIMETABLE: 24 * 3600,
    CacheKind.AVAILABILITY: 10 * 60,
    CacheKind.FARE: 6 * 3600,
    CacheKind.LIVE: 60,
    CacheKind.SEARCH: 10 * 60,
}

#: Seconds a value may still be served *after* it went stale, when upstream fails.
#: `None` means "no limit" and is only acceptable for data that barely moves.
MAX_STALE_S: dict[CacheKind, float | None] = {
    CacheKind.TIMETABLE: None,
    CacheKind.AVAILABILITY: 3600,  # an hour-old seat count is a caveat, not a lie
    CacheKind.FARE: 24 * 3600,
    CacheKind.LIVE: 300,  # a five-minute-old position is no longer a position
    CacheKind.SEARCH: 3600,
}


@dataclass(frozen=True, slots=True)
class CacheKey:
    """Route-scoped identity for a cached value.

    `travel_class` is empty when the value is class-agnostic (a timetable). Note
    the deliberate absence of any user or session component.
    """

    provider_id: str
    kind: CacheKind
    origin: str
    destination: str
    service_date: dt.date
    travel_class: str = ""

    def __post_init__(self) -> None:
        # Class codes are case-insensitive on the wire, so the key must be too, or
        # "3a" and "3A" would be two fetches against the same 100/month budget.
        if self.travel_class:
            object.__setattr__(self, "travel_class", self.travel_class.upper())

    @classmethod
    def for_query(
        cls,
        provider_id: str,
        kind: CacheKind,
        query: Query,
        travel_class: str = "",
    ) -> CacheKey:
        return cls(
            provider_id=provider_id,
            kind=kind,
            origin=query.origin.stop_id,
            destination=query.destination.stop_id,
            service_date=query.service_date,
            travel_class=travel_class.upper(),
        )

    def as_str(self) -> str:
        """Stable string form, for logs and for a future SQLite-backed store."""
        return (
            f"{self.kind.value}|{self.provider_id}|{self.origin}|"
            f"{self.destination}|{self.service_date.isoformat()}|{self.travel_class}"
        )


@dataclass(slots=True)
class _Entry:
    value: object
    stored_monotonic: float
    stored_wall: dt.datetime
    ttl_s: float

    def age_s(self, now: float) -> float:
        return max(0.0, now - self.stored_monotonic)

    def is_fresh(self, now: float) -> bool:
        return self.age_s(now) < self.ttl_s


@dataclass(frozen=True, slots=True)
class CacheResult[T]:
    """What the caller gets back, and the truth about where it came from."""

    value: T
    #: True when the value was served from the cache rather than loaded now.
    from_cache: bool
    #: True when the value is past its TTL and was served anyway, because upstream failed.
    stale: bool
    age_s: float


@dataclass(slots=True)
class CacheStats:
    """Counters for `/metrics`. Every one of these is real, none is estimated."""

    hits: int = 0
    misses: int = 0
    stale_served: int = 0
    upstream_calls: int = 0
    upstream_errors: int = 0
    evictions: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stale_served": self.stale_served,
            "upstream_calls": self.upstream_calls,
            "upstream_errors": self.upstream_errors,
            "evictions": self.evictions,
            "hit_rate": self.hit_rate,
        }


@dataclass
class RouteCache:
    """In-memory cache. The interface is what matters; the store can move to SQLite.

    `clock` is injectable so TTL behaviour is tested in microseconds instead of
    by sleeping. Tests that sleep are tests that flake in CI.
    """

    ttl_s: Mapping[CacheKind, float] = field(default_factory=lambda: dict(DEFAULT_TTL_S))
    max_stale_s: Mapping[CacheKind, float | None] = field(default_factory=lambda: dict(MAX_STALE_S))
    max_entries: int = 2048
    clock: Callable[[], float] = time.monotonic

    _entries: dict[str, _Entry] = field(default_factory=dict, init=False, repr=False)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False, repr=False)
    _stats: CacheStats = field(default_factory=CacheStats, init=False)

    # -- reads --

    def peek(self, key: CacheKey) -> CacheResult[object] | None:
        """Look without loading. Useful for optimistic render before a fetch."""
        entry = self._entries.get(key.as_str())
        if entry is None:
            return None
        now = self.clock()
        return CacheResult(
            value=entry.value,
            from_cache=True,
            stale=not entry.is_fresh(now),
            age_s=entry.age_s(now),
        )

    async def get_or_load(
        self,
        key: CacheKey,
        loader: Callable[[], Awaitable[T]],
        *,
        allow_stale: bool = True,
        ttl_s: float | None = None,
    ) -> CacheResult[T]:
        """Return a fresh value, or load one, or serve stale if the loader fails.

        Concurrency: callers for the same key queue on one lock, and the second
        caller re-checks the cache after acquiring it, so a burst of ten becomes
        exactly one upstream call.
        """
        name = key.as_str()
        now = self.clock()

        entry = self._entries.get(name)
        if entry is not None and entry.is_fresh(now):
            self._stats.hits += 1
            return CacheResult(entry.value, True, False, entry.age_s(now))

        async with self._lock_for(name):
            # Re-check inside the lock: another caller may have filled it while we waited.
            now = self.clock()
            entry = self._entries.get(name)
            if entry is not None and entry.is_fresh(now):
                self._stats.hits += 1
                return CacheResult(entry.value, True, False, entry.age_s(now))

            self._stats.misses += 1
            self._stats.upstream_calls += 1
            try:
                value = await loader()
            except ProviderError:
                self._stats.upstream_errors += 1
                if allow_stale and entry is not None:
                    age = entry.age_s(now)
                    limit = self.max_stale_s.get(key.kind)
                    if limit is None or age <= limit:
                        self._stats.stale_served += 1
                        return CacheResult(entry.value, True, True, age)
                raise

            self._store(name, key.kind, value, ttl_s)
            return CacheResult(value, False, False, 0.0)

    # -- writes --

    def _store(self, name: str, kind: CacheKind, value: object, ttl_s: float | None) -> None:
        if len(self._entries) >= self.max_entries and name not in self._entries:
            self._evict_oldest()
        self._entries[name] = _Entry(
            value=value,
            stored_monotonic=self.clock(),
            stored_wall=dt.datetime.now(dt.UTC),
            ttl_s=ttl_s if ttl_s is not None else self.ttl_s.get(kind, 60.0),
        )

    def _evict_oldest(self) -> None:
        oldest = min(self._entries, key=lambda k: self._entries[k].stored_monotonic)
        self._entries.pop(oldest, None)
        self._stats.evictions += 1

    def invalidate(self, key: CacheKey) -> bool:
        return self._entries.pop(key.as_str(), None) is not None

    def clear(self) -> None:
        self._entries.clear()

    # -- introspection --

    @property
    def stats(self) -> CacheStats:
        return self._stats

    def __len__(self) -> int:
        return len(self._entries)

    def _lock_for(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[name] = lock
        return lock
