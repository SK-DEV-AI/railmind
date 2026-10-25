"""Single-flight tests. Concurrency here is real but tiny, and nothing sleeps long.

The interesting cases are the ugly ones: five callers at once, a factory that
raises, and a leader that is cancelled while others are waiting. Those are the
three ways this pattern usually breaks in production.
"""

from __future__ import annotations

import asyncio

import pytest

from railmind.engine.singleflight import SingleFlight


def test_five_concurrent_callers_run_the_factory_once():
    flight = SingleFlight()
    calls = 0

    async def body():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return ["a", "b"]

    async def run():
        return await asyncio.gather(*[flight.do("route", body) for _ in range(5)])

    results = asyncio.run(run())

    assert calls == 1
    assert results == [["a", "b"]] * 5
    assert flight.leaders == 1
    assert flight.joiners == 4


def test_different_keys_do_not_share_work():
    flight = SingleFlight()
    seen: list[str] = []

    def make(key: str):
        async def body():
            seen.append(key)
            await asyncio.sleep(0.01)
            return key

        return body

    async def run():
        return await asyncio.gather(
            flight.do("CSMT:PUNE", make("CSMT:PUNE")),
            flight.do("CSMT:NDLS", make("CSMT:NDLS")),
        )

    assert asyncio.run(run()) == ["CSMT:PUNE", "CSMT:NDLS"]
    assert sorted(seen) == ["CSMT:NDLS", "CSMT:PUNE"]
    assert flight.joiners == 0


def test_a_second_call_after_the_first_finishes_asks_again():
    """Single-flight is not a cache: freshness is not traded away."""
    flight = SingleFlight()
    calls = 0

    async def body():
        nonlocal calls
        calls += 1
        return calls

    async def run():
        first = await flight.do("k", body)
        second = await flight.do("k", body)
        return first, second

    assert asyncio.run(run()) == (1, 2)
    assert flight.leaders == 2
    assert flight.inflight == 0


def test_a_failure_reaches_every_caller_and_frees_the_key():
    flight = SingleFlight()
    attempts = 0

    async def broken():
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.01)
        raise RuntimeError("upstream said no")

    async def run():
        return await asyncio.gather(
            *[flight.do("k", broken) for _ in range(3)], return_exceptions=True
        )

    results = asyncio.run(run())

    assert attempts == 1
    assert all(isinstance(item, RuntimeError) for item in results)
    assert flight.inflight == 0

    async def recovered():
        return "ok"

    # The dead key must be usable again immediately.
    assert asyncio.run(flight.do("k", recovered)) == "ok"


def test_a_cancelled_leader_releases_its_waiters_and_leaves_no_dead_key():
    """The deadline case: the leader dies, waiters must not hang on its future."""
    flight = SingleFlight()

    async def slow():
        await asyncio.sleep(5.0)
        return "never"

    async def run():
        leader = asyncio.create_task(flight.do("k", slow))
        await asyncio.sleep(0)  # let the leader claim the key
        waiters = [asyncio.create_task(flight.do("k", slow)) for _ in range(2)]
        await asyncio.sleep(0.01)
        leader.cancel()
        outcomes = await asyncio.gather(leader, *waiters, return_exceptions=True)
        return outcomes

    outcomes = asyncio.run(run())

    assert isinstance(outcomes[0], asyncio.CancelledError)
    # Waiters were released, not left awaiting a future nobody would complete.
    assert all(isinstance(item, BaseException) for item in outcomes[1:])
    assert flight.inflight == 0

    async def fresh():
        return "new run"

    assert asyncio.run(flight.do("k", fresh)) == "new run"


def test_stats_are_counted_separately_from_inflight_state():
    flight = SingleFlight()

    async def body():
        # Must yield, or the four calls never overlap and there is nothing to
        # coalesce. A test bug here would quietly un-test the feature.
        await asyncio.sleep(0)
        return 1

    async def run():
        await asyncio.gather(*[flight.do("k", body) for _ in range(4)])

    asyncio.run(run())
    stats = flight.stats()

    assert stats["leaders"] == 1
    assert stats["joiners"] == 3
    assert stats["inflight"] == 0


@pytest.mark.parametrize("callers", [1, 2, 8])
def test_the_saving_scales_with_the_burst(callers: int):
    flight = SingleFlight()
    calls = 0

    async def body():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.005)
        return calls

    async def run():
        return await asyncio.gather(*[flight.do("k", body) for _ in range(callers)])

    results = asyncio.run(run())

    assert calls == 1
    assert results == [1] * callers
