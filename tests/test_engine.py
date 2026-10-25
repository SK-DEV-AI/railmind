"""Engine tests: fan-out, deadline, degradation, and the log.

No network and no real sleeps worth naming: the one slow provider sleeps for
half a second and the test asserts we did not wait for it, which is the whole
point of the deadline.
"""

from __future__ import annotations

import datetime as dt

import pytest

from railmind.core.errors import InvalidQuery, UpstreamDown
from railmind.core.types import Query
from railmind.engine.metrics import RequestLog
from railmind.engine.orchestrator import SearchEngine
from railmind.providers.fake import (
    BrokenProvider,
    FakeProvider,
    make_itinerary,
    make_stop,
)
from railmind.providers.registry import ProviderRegistry
from railmind.wire.models import (
    BookingIntent,
    Mode,
    Optimise,
    ParsedBy,
    SearchRequest,
    TimeWindow,
)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
SERVICE_DATE = dt.date(2026, 10, 25)


def make_intent(**overrides) -> BookingIntent:
    start = dt.datetime.combine(SERVICE_DATE, dt.time(8), tzinfo=IST)
    fields = {
        "intent_id": "int_test",
        "created_at": start,
        "expires_at": start + dt.timedelta(hours=1),
        "raw_text": "mumbai to delhi tomorrow morning",
        "origin": make_stop("IN:CSMT", "Mumbai CSMT"),
        "destination": make_stop("IN:NDLS", "New Delhi", lat=28.64, lon=77.22),
        "depart_window": TimeWindow(start=start, end=start + dt.timedelta(hours=12)),
        "modes": [Mode.rail],
        "optimise": Optimise.balanced,
        "confidence": 0.9,
        "parsed_by": ParsedBy.rules,
    }
    fields.update(overrides)
    return BookingIntent(**fields)


def make_provider(provider_id: str = "fake_rail", **kwargs) -> FakeProvider:
    """A FakeProvider under its own id, so two can share one registry."""
    return type("Variant", (FakeProvider,), {"id": provider_id})(**kwargs)


def make_registry(*providers) -> ProviderRegistry:
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return registry


async def test_search_returns_ranked_cards_and_honest_meta():
    fast = make_itinerary(service_id="fast", depart_hour=6, duration_h=9.0)
    slow = make_itinerary(service_id="slow", duration_h=18.0)
    provider = FakeProvider(itineraries=[slow, fast])
    engine = SearchEngine(make_registry(provider))

    response = await engine.search(SearchRequest(intent=make_intent(), intent_id="int_test"))

    assert [item.rank for item in response.itineraries] == [1, 2]
    assert response.itineraries[0].legs[0].service.service_id == "fast"
    assert response.meta.providers_used == ["fake_rail"]
    assert response.meta.providers_failed is None
    assert response.meta.elapsed_ms >= 0
    assert response.meta.deadline_ms == 1800
    assert response.intent_id == "int_test"


async def test_a_failing_provider_is_a_note_not_a_failure():
    healthy = make_provider("healthy_rail", itineraries=[make_itinerary()])
    dead = make_provider("dead_rail", fail={"search": UpstreamDown})
    engine = SearchEngine(make_registry(healthy, dead))

    response = await engine.search(SearchRequest(intent=make_intent()))

    assert len(response.itineraries) == 1
    assert response.meta.providers_failed == ["dead_rail"]
    assert response.meta.degraded is True
    assert any("upstream_down" in note for note in response.meta.notes)


async def test_an_adapter_that_leaks_a_crash_still_cannot_break_the_request():
    healthy = FakeProvider(itineraries=[make_itinerary()])
    engine = SearchEngine(make_registry(healthy, BrokenProvider()))

    response = await engine.search(SearchRequest(intent=make_intent()))

    assert len(response.itineraries) == 1
    assert "broken_rail" in response.meta.providers_failed
    # The upstream HTML never reaches the client.
    assert not any("503" in note for note in (response.meta.notes or []))


async def test_the_deadline_cancels_the_slow_provider_instead_of_waiting():
    slow = FakeProvider(itineraries=[make_itinerary()], latency_s=0.6)
    engine = SearchEngine(make_registry(slow))

    started = dt.datetime.now(dt.UTC)
    response = await engine.search(SearchRequest(intent=make_intent(), deadline_ms=200))
    wall_ms = (dt.datetime.now(dt.UTC) - started).total_seconds() * 1000

    assert response.itineraries == []
    assert response.meta.providers_failed == ["fake_rail"]
    assert any("did not answer" in note for note in response.meta.notes)
    # The provider would have taken 600 ms. We must not have waited for it.
    assert wall_ms < 550


async def test_a_search_without_an_intent_is_refused_rather_than_guessed():
    engine = SearchEngine(make_registry(FakeProvider()))

    with pytest.raises(InvalidQuery):
        await engine.search(SearchRequest())


async def test_a_mode_no_provider_serves_says_so_plainly():
    engine = SearchEngine(make_registry(FakeProvider()))

    response = await engine.search(SearchRequest(intent=make_intent(), modes=[Mode.bus]))

    assert response.itineraries == []
    assert response.meta.degraded is True
    assert response.meta.notes == ["no provider serves the requested mode"]


async def test_limit_is_applied_after_ranking_not_before():
    provider = FakeProvider(
        itineraries=[
            make_itinerary(service_id="c", depart_hour=12, duration_h=15.0),
            make_itinerary(service_id="b", depart_hour=9, duration_h=12.0),
            make_itinerary(service_id="a", depart_hour=6, duration_h=8.0),
        ]
    )
    engine = SearchEngine(make_registry(provider))

    response = await engine.search(SearchRequest(intent=make_intent(), limit=2))

    assert len(response.itineraries) == 2
    # The two best, and rank is renumbered from 1 within the returned slice.
    assert [item.legs[0].service.service_id for item in response.itineraries] == ["a", "b"]
    assert [item.rank for item in response.itineraries] == [1, 2]


async def test_missing_availability_marks_the_response_degraded():
    itinerary = make_itinerary()
    itinerary.legs[0].availability = None
    itinerary = itinerary.model_copy(update={"legs": itinerary.legs}, deep=True)
    engine = SearchEngine(make_registry(FakeProvider(itineraries=[itinerary])))

    response = await engine.search(SearchRequest(intent=make_intent()))

    assert response.meta.degraded is True
    assert any("Seat availability unknown" in w for w in response.itineraries[0].warnings)


async def test_every_request_reaches_the_log_and_the_metrics_count_it(tmp_path):
    log = RequestLog(tmp_path / "requests.jsonl")
    engine = SearchEngine(make_registry(FakeProvider()), log=log)

    first = await engine.search(SearchRequest(intent=make_intent()))
    await engine.search(SearchRequest(intent=make_intent()))

    records = log.records()
    assert len(records) == 2
    assert records[0].request_id == first.search_id
    assert all(record.route == "search" for record in records)

    metrics = log.metrics()
    assert metrics.sample_size == 2
    assert metrics.counted_from == "request_log"
    assert metrics.requests.total == 2
    assert metrics.stages.search.n == 2
    assert metrics.t90_ms is not None
    # Not instrumented rather than zero: we never claim a perfect cache.
    assert metrics.provider_calls is None


async def test_a_log_write_failure_is_reported_not_swallowed(tmp_path):
    unwritable = tmp_path / "file-inside-a-file"
    unwritable.write_text("not a directory")
    log = RequestLog(unwritable / "requests.jsonl")
    engine = SearchEngine(make_registry(FakeProvider()), log=log)

    response = await engine.search(SearchRequest(intent=make_intent()))

    assert len(response.itineraries) == 1
    assert any("request log write failed" in note for note in (response.meta.notes or []))


async def test_no_log_configured_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = SearchEngine(make_registry(FakeProvider()))

    await engine.search(SearchRequest(intent=make_intent()))

    assert list(tmp_path.iterdir()) == []


async def test_modes_override_keeps_the_query_immutable():
    provider = FakeProvider(itineraries=[make_itinerary()])
    engine = SearchEngine(make_registry(provider))
    intent = make_intent()
    original = Query.from_intent(intent)

    await engine.search(SearchRequest(intent=intent, modes=[Mode.rail, Mode.bus]))

    # The intent is untouched, and the provider only ever saw rail.
    assert tuple(intent.modes) == (Mode.rail,)
    assert original.modes == (Mode.rail,)
