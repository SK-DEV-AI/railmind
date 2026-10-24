"""Cache: route-keyed, TTL-correct, stale-serving, single-flight.

Every assertion here is a quota decision. A cache that reloads on a burst burns a
monthly budget in seconds, so these are not micro-tests; they are the budget.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from railmind.core.errors import UpstreamDown
from railmind.providers.cache import CacheKey, CacheKind, RouteCache


def make_key(kind: CacheKind = CacheKind.AVAILABILITY, day: int = 25, cls: str = "3A") -> CacheKey:
    return CacheKey(
        provider_id="fake_rail",
        kind=kind,
        origin="IN:CSMT",
        destination="IN:NDLS",
        service_date=dt.date(2026, 10, day),
        travel_class=cls,
    )


class Counter:
    def __init__(self, value: object = "value") -> None:
        self.calls = 0
        self.value = value

    async def __call__(self):
        self.calls += 1
        return self.value


class TestFreshness:
    async def test_first_call_loads_and_second_call_hits(self, clock):
        cache = RouteCache(clock=clock)
        loader = Counter({"seats": 12})

        first = await cache.get_or_load(make_key(), loader)
        second = await cache.get_or_load(make_key(), loader)

        assert first.from_cache is False
        assert second.from_cache is True
        assert loader.calls == 1
        assert cache.stats.hits == 1
        assert cache.stats.misses == 1
        assert cache.stats.upstream_calls == 1

    async def test_reloads_after_ttl(self, clock):
        cache = RouteCache(clock=clock)
        loader = Counter()

        await cache.get_or_load(make_key(), loader)
        clock.advance(11 * 60)  # availability TTL is 10 minutes
        await cache.get_or_load(make_key(), loader)

        assert loader.calls == 2

    async def test_still_fresh_one_second_before_ttl(self, clock):
        cache = RouteCache(clock=clock)
        loader = Counter()

        await cache.get_or_load(make_key(), loader)
        clock.advance(9 * 60 + 59)
        result = await cache.get_or_load(make_key(), loader)

        assert result.from_cache is True
        assert loader.calls == 1

    async def test_ttl_override_per_entry(self, clock):
        cache = RouteCache(clock=clock)
        loader = Counter()

        await cache.get_or_load(make_key(), loader, ttl_s=1)
        clock.advance(2)
        await cache.get_or_load(make_key(), loader, ttl_s=1)

        assert loader.calls == 2


class TestRouteKeying:
    async def test_different_dates_are_different_entries(self, clock):
        cache = RouteCache(clock=clock)
        loader = Counter()

        await cache.get_or_load(make_key(day=25), loader)
        await cache.get_or_load(make_key(day=26), loader)

        assert loader.calls == 2
        assert len(cache) == 2

    async def test_different_classes_are_different_entries(self, clock):
        cache = RouteCache(clock=clock)
        loader = Counter()

        await cache.get_or_load(make_key(cls="3A"), loader)
        await cache.get_or_load(make_key(cls="SL"), loader)

        assert loader.calls == 2

    async def test_key_is_case_insensitive_for_class(self, clock):
        cache = RouteCache(clock=clock)
        loader = Counter()

        await cache.get_or_load(make_key(cls="3a"), loader)
        await cache.get_or_load(make_key(cls="3A"), loader)

        assert loader.calls == 1


class TestStaleServing:
    async def test_serves_stale_when_upstream_fails(self, clock):
        cache = RouteCache(clock=clock)
        key = make_key()
        await cache.get_or_load(key, Counter({"seats": 12}))

        clock.advance(20 * 60)  # past TTL, inside the 1-hour stale window

        async def failing():
            raise UpstreamDown("upstream is down")

        result = await cache.get_or_load(key, failing)

        assert result.stale is True
        assert result.from_cache is True
        assert result.value == {"seats": 12}
        assert cache.stats.stale_served == 1

    async def test_refuses_stale_beyond_the_window(self, clock):
        cache = RouteCache(clock=clock)
        key = make_key(CacheKind.LIVE)
        await cache.get_or_load(key, Counter("position"))

        clock.advance(60 + 301)  # live stale window is 300s

        async def failing():
            raise UpstreamDown("upstream is down")

        with pytest.raises(UpstreamDown):
            await cache.get_or_load(key, failing)

    async def test_allow_stale_false_propagates_the_error(self, clock):
        cache = RouteCache(clock=clock)
        key = make_key()
        await cache.get_or_load(key, Counter())
        clock.advance(11 * 60)

        async def failing():
            raise UpstreamDown("upstream is down")

        with pytest.raises(UpstreamDown):
            await cache.get_or_load(key, failing, allow_stale=False)

    async def test_failure_with_nothing_cached_propagates(self, clock):
        cache = RouteCache(clock=clock)

        async def failing():
            raise UpstreamDown("upstream is down")

        with pytest.raises(UpstreamDown):
            await cache.get_or_load(make_key(), failing)

    async def test_non_provider_exception_is_not_swallowed(self, clock):
        cache = RouteCache(clock=clock)

        async def exploding():
            raise ValueError("programmer error")

        with pytest.raises(ValueError):
            await cache.get_or_load(make_key(), exploding)


class TestSingleFlight:
    async def test_burst_of_ten_becomes_one_upstream_call(self, clock):
        cache = RouteCache(clock=clock)
        loader = Counter("value")

        results = await asyncio.gather(*(cache.get_or_load(make_key(), loader) for _ in range(10)))

        assert loader.calls == 1
        assert cache.stats.upstream_calls == 1
        assert all(result.value == "value" for result in results)
        assert sum(1 for r in results if not r.from_cache) == 1

    async def test_different_keys_are_not_serialised(self, clock):
        cache = RouteCache(clock=clock)
        loader = Counter()

        await asyncio.gather(
            cache.get_or_load(make_key(day=25), loader),
            cache.get_or_load(make_key(day=26), loader),
        )

        assert loader.calls == 2


class TestMaintenance:
    def test_peek_does_not_load(self, clock):
        cache = RouteCache(clock=clock)
        assert cache.peek(make_key()) is None

    async def test_peek_reports_age_and_staleness(self, clock):
        cache = RouteCache(clock=clock)
        key = make_key()
        await cache.get_or_load(key, Counter("v"))

        clock.advance(11 * 60)
        peeked = cache.peek(key)
        assert peeked is not None
        assert peeked.stale is True
        assert peeked.age_s >= 660

    async def test_invalidate(self, clock):
        cache = RouteCache(clock=clock)
        key = make_key()
        await cache.get_or_load(key, Counter())
        assert cache.invalidate(key) is True
        assert cache.invalidate(key) is False
        assert len(cache) == 0

    async def test_eviction_is_bounded(self, clock):
        cache = RouteCache(clock=clock, max_entries=2)
        loader = Counter()

        await cache.get_or_load(make_key(day=25), loader)
        clock.advance(1)
        await cache.get_or_load(make_key(day=26), loader)
        clock.advance(1)
        await cache.get_or_load(make_key(day=27), loader)

        assert len(cache) == 2
        assert cache.stats.evictions == 1

    async def test_hit_rate_is_real(self, clock):
        cache = RouteCache(clock=clock)
        loader = Counter()
        for _ in range(4):
            await cache.get_or_load(make_key(), loader)
        assert cache.stats.hit_rate == pytest.approx(0.75)
        assert cache.stats.as_dict()["hits"] == 3
