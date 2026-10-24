"""Fake providers for tests and for the UI before a real adapter exists.

"Fake" here means *these classes stand in for a provider*, not that the product
runs on invented data. The product ends at a real IRCTC handoff with real trains
and real fares. These builders exist so the core can be tested before a network
key is in play, and so the web client can be built against the frozen contract.

`BrokenProvider` is not a joke: it raises a bare `RuntimeError` so the test suite
can prove that an accidental exception is converted to `internal` and never leaks
its text to a client.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence

from railmind.core.capabilities import Capabilities, Feature, rail_capabilities, static_capabilities
from railmind.core.errors import ProviderError
from railmind.core.ids import itinerary_id, leg_id
from railmind.core.provider import BaseProvider
from railmind.core.types import (
    Availability,
    BookingChannel,
    GeoPoint,
    Handoff,
    HandoffStep,
    HandoffSummary,
    Itinerary,
    Leg,
    LineString,
    LiveStatus,
    Mode,
    Money,
    Passenger,
    Query,
    Risk,
    ScoreBreakdown,
    ServiceRef,
    Stop,
    inr,
)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")


# --- builders ----------------------------------------------------------------


def make_stop(
    code: str = "CSMT", name: str = "Mumbai CSMT", *, lat: float = 18.94, lon: float = 72.835
) -> Stop:
    country, _, short = code.rpartition(":")
    return Stop(
        stop_id=code if short else f"IN:{code}",
        name=name,
        code=short or code,
        city=name.split()[-1] if name else None,
        country=(country or "IN").upper(),
        geo=GeoPoint(lat=lat, lon=lon),
        timezone="Asia/Kolkata",
        modes=[Mode.rail],
    )


def make_itinerary(
    *,
    provider_id: str = "fake_rail",
    service_id: str = "12345",
    service_name: str = "Fake Express",
    service_date: dt.date = dt.date(2026, 10, 25),
    depart_hour: int = 17,
    duration_h: float = 15.5,
    price: Money | None = None,
    seats: int | None = 12,
    delay_probability: float = 0.13,
    risk_band: str = "low",
) -> Itinerary:
    """One plausible itinerary. Deterministic, so tests can assert on exact values."""
    origin = make_stop("IN:CSMT", "Mumbai CSMT")
    destination = make_stop("IN:NDLS", "New Delhi", lat=28.64, lon=77.22)
    depart_at = dt.datetime.combine(service_date, dt.time(depart_hour), tzinfo=IST)
    duration_s = int(duration_h * 3600)
    arrive_at = depart_at + dt.timedelta(seconds=duration_s)

    leg = Leg(
        leg_id=leg_id(provider_id, service_id, service_date),
        mode=Mode.rail,
        origin=origin,
        destination=destination,
        depart_at=depart_at,
        arrive_at=arrive_at,
        service_date=service_date,
        duration_s=duration_s,
        service=ServiceRef(
            provider_id=provider_id,
            service_id=service_id,
            name=service_name,
            operator="Fake Railway",
        ),
        distance_km=1388.4,
        price=price or inr(2605.50),
        availability=Availability(
            status="available" if seats else "sold_out",
            class_code="3A",
            seats_available=seats if seats is not None else None,
            checked_at=dt.datetime.now(IST),
            source=provider_id,
            stale=False,
        ),
        risk=Risk(
            delay_probability=delay_probability,
            expected_delay_min=18.0,
            p90_delay_min=47.0,
            model_version="fake-0.0.1",
            confidence=0.6,
        ),
        live_available=True,
        booking_channel=BookingChannel.deeplink,
        geometry=LineString(type="LineString", coordinates=[[72.835, 18.94], [77.22, 28.64]]),
    )

    return Itinerary(
        itinerary_id=itinerary_id([leg.leg_id]),
        legs=[leg],
        depart_at=depart_at,
        arrive_at=arrive_at,
        total_duration_s=duration_s,
        modes=[Mode.rail],
        total_price=price or inr(2605.50),
        score=0.72,
        score_breakdown=ScoreBreakdown(
            price=0.6, duration=0.7, delay_risk=0.87, comfort=0.5, transfers=1.0
        ),
        rank=1,
        risk_band=risk_band,
        risk_reasons=["Synthetic fixture, not a real prediction"],
        confidence=0.6,
        handoff=HandoffSummary(available=True, provider_id=provider_id),
        tags=["fake"],
        warnings=[],
    )


# --- providers ---------------------------------------------------------------


class FakeProvider(BaseProvider):
    """A working provider with no network. Deterministic and inspectable."""

    id = "fake_rail"
    display_name = "Fake Rail"
    modes = frozenset({Mode.rail})
    regions = frozenset({"IN"})
    capabilities: Capabilities = rail_capabilities()

    def __init__(
        self,
        *,
        itineraries: list[Itinerary] | None = None,
        fail: dict[str, type[ProviderError]] | None = None,
        latency_s: float = 0.0,
    ) -> None:
        super().__init__()
        self._itineraries = itineraries if itineraries is not None else [make_itinerary()]
        self._fail = fail or {}
        self._latency_s = latency_s
        #: Verb -> call count, so tests can prove single-flight and caching.
        self.calls: dict[str, int] = {
            "search": 0,
            "availability": 0,
            "price": 0,
            "live": 0,
            "handoff": 0,
        }

    async def _enter(self, verb: str) -> None:
        self.calls[verb] += 1
        if self._latency_s:
            await asyncio.sleep(self._latency_s)
        error = self._fail.get(verb)
        if error is not None:
            raise error(f"{verb} failed by test configuration", provider_id=self.id)

    async def search(self, query: Query) -> list[Itinerary]:
        await self._enter("search")
        self._require(Feature.SEARCH)
        return list(self._itineraries)

    async def availability(self, itinerary: Itinerary) -> Availability:
        await self._enter("availability")
        self._require(Feature.AVAILABILITY)
        return itinerary.legs[0].availability

    async def price(self, itinerary: Itinerary) -> Money:
        await self._enter("price")
        self._require(Feature.PRICE)
        return itinerary.total_price or inr(0)

    async def live(self, leg: Leg) -> LiveStatus:
        await self._enter("live")
        self._require(Feature.LIVE)
        return LiveStatus(
            leg_id=leg.leg_id,
            status="on_time",
            delay_min=0,
            last_reported_at=dt.datetime.now(IST),
            source=self.id,
            stale=False,
        )

    async def handoff(self, itinerary: Itinerary, passengers: Sequence[Passenger]) -> Handoff:
        await self._enter("handoff")
        self._require(Feature.BOOKING)
        return Handoff(
            handoff_id="hof_fake",
            itinerary_id=itinerary.itinerary_id,
            provider_id=self.id,
            method="deeplink",
            url="https://example.invalid/handoff",
            steps=[
                HandoffStep(
                    order=1,
                    title="Complete the booking yourself",
                    requires="human",
                    est_seconds=45,
                )
            ],
            expires_at=dt.datetime.now(IST) + dt.timedelta(minutes=10),
            legal_notice=(
                "RailMind does not book. This link opens the operator's own site, "
                "where you complete the booking and payment yourself."
            ),
            warnings=[],
        )


class TimetableProvider(BaseProvider):
    """A provider that can only search: proves capability gating without network."""

    id = "fake_timetable"
    display_name = "Fake Timetable"
    modes = frozenset({Mode.metro})
    regions = frozenset({"IN"})
    capabilities: Capabilities = static_capabilities({Mode.metro})

    def __init__(self) -> None:
        super().__init__()

    async def search(self, query: Query) -> list[Itinerary]:
        self._require(Feature.SEARCH)
        return []


class BrokenProvider(BaseProvider):
    """Raises an accidental, non-provider exception. Must never reach a client.

    The API edge converts this to `internal` with a generic message, which the
    test suite asserts. This is the class that catches a future contributor
    wrapping a bare `httpx` call in an adapter.
    """

    id = "broken_rail"
    display_name = "Broken Rail"
    modes = frozenset({Mode.rail})
    regions = frozenset({"IN"})
    capabilities: Capabilities = rail_capabilities()

    def __init__(self) -> None:
        super().__init__()

    async def search(self, query: Query) -> list[Itinerary]:
        raise RuntimeError("secret upstream detail: <html>503 Service Unavailable</html>")
