"""Indian Railways provider: real timetable, real fares, real handoff.

Composition, and why each part is where it is:

- `TimetableRepo` answers "which trains run this corridor" with zero network. That
  is what makes a 100-request month survivable.
- `RailRadarClient` is consulted only for volatile facts: a route it has not seen,
  a fare for a finalist, a live position.
- `RouteCache` sits between them, keyed by route, with stale-serve.
- The IRCTC deep link is built locally. No request, no automation, no booking.

Deliberate limits, stated rather than hidden:

- `search` does not fetch fares. One fare is one request, and a search returns
  several trains. The engine prices only the finalists, so a search costs zero
  requests and a decision costs at most a few.
- Seat availability is not on the free data plan, so the capability is advertised
  as absent and `availability()` raises `NotSupported`. It is never guessed.
- Ranking here uses scheduled duration only. The delay model arrives in Step 3,
  so `risk_band` is `unknown` and confidence is low. A "low risk" badge with no
  model behind it would be a lie with a nice colour.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from railmind.core.capabilities import Capabilities, Feature, rail_capabilities
from railmind.core.errors import InvalidQuery, NotSupported
from railmind.core.ids import itinerary_id, leg_id, stop_id
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
    LiveStatus,
    Mode,
    Money,
    Passenger,
    Query,
    ScoreBreakdown,
    ServiceRef,
    Stop,
)
from railmind.data.timetable import TimetableRepo, TripOption
from railmind.handoff.deeplink import LEGAL_NOTICE, build_irctc_deeplink
from railmind.providers.cache import CacheKey, CacheKind, RouteCache
from railmind.providers.railradar import PROVIDER_ID as VENDOR_ID
from railmind.providers.railradar import RailRadarClient
from railmind.risk.predict import PunctualityModel

IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
ATTRIBUTION = (
    "Timetable and live data from RailRadar (railradar.in). "
    "RailRadar is not affiliated with IRCTC or NTES."
)

#: Vendor classes we know how to read a fare for, in preference order.
DEFAULT_CLASS_ORDER = ("SL", "3A", "2A", "1A", "CC", "2S")


def _hhmm(value: str, day: int, service_date: dt.date) -> dt.datetime | None:
    """Turn `"19:35"` plus a day number into an aware datetime.

    Day 2 means the next calendar day, which is how IRCTC timetables express an
    overnight run. Getting this wrong shifts every overnight train by 24 hours.
    """
    if not value:
        return None
    try:
        hour, minute = (int(part) for part in value.split(":")[:2])
    except (ValueError, TypeError):
        return None
    base = dt.datetime.combine(service_date, dt.time(hour, minute), tzinfo=IST)
    return base + dt.timedelta(days=max(1, day) - 1)


def _stop(code: str, name: str, lat: float | None, lon: float | None) -> Stop:
    geo = GeoPoint(lat=lat, lon=lon) if lat is not None and lon is not None else None
    return Stop(
        stop_id=stop_id("IN", code),
        name=name,
        code=code,
        country="IN",
        geo=geo,
        timezone="Asia/Kolkata",
        modes=[Mode.rail],
    )


class RailIndiaProvider(BaseProvider):
    """The one provider this project actually implements end to end."""

    id = "rail_india"
    display_name = "Indian Railways"
    modes = frozenset({Mode.rail})
    regions = frozenset({"IN"})
    attribution = ATTRIBUTION

    #: Optional punctuality model. When absent, every itinerary keeps
    #: `risk_band: unknown`, which the contract defines as a real value rather than
    #: a placeholder, so the absence is honest instead of silent.
    _model: PunctualityModel | None

    def __init__(
        self,
        *,
        client: RailRadarClient,
        repo: TimetableRepo,
        cache: RouteCache | None = None,
        seat_availability: bool = False,
        risk_model: PunctualityModel | None = None,
    ) -> None:
        # Real capabilities, not aspirational ones: on the free data plan there is
        # no seat availability, so we do not advertise it.
        caps = rail_capabilities()
        self.capabilities = Capabilities(
            search=True,
            availability=seat_availability,
            price=True,
            live=True,
            booking=caps.booking,
            seat_map=False,
            seat_select=False,
            cancel=caps.cancel,
        )
        self.client = client
        self.repo = repo
        self.cache = cache or RouteCache()
        self.seat_availability = seat_availability
        self._model = risk_model
        super().__init__()

    # -- search ---------------------------------------------------------------

    async def search(self, query: Query) -> list[Itinerary]:
        """Trains for this corridor on this date. Zero network when seeded."""
        self._require(Feature.SEARCH)
        if Mode.rail not in query.modes:
            raise InvalidQuery("this provider only serves rail")

        origin_code = (query.origin.code or "").upper()
        destination_code = (query.destination.code or "").upper()
        if not origin_code or not destination_code:
            raise InvalidQuery("a station code is required for a rail search")

        options = self.repo.trains_between(origin_code, destination_code)
        weekday = query.service_date.weekday()
        options = [option for option in options if option.runs_on(weekday)]

        if not options:
            # Not seeded. One request tells us which trains run it; their stops and
            # coordinates come from the local station table where known.
            options = self._options_from_vendor(
                await self._vendor_between(origin_code, destination_code)
            )

        itineraries: list[Itinerary] = []
        unusable = 0
        for option in options:
            try:
                itineraries.append(self._to_itinerary(option, query))
            except InvalidQuery:
                # One malformed schedule in a corridor must not hide the other
                # trains on it. Counted, and reported only if nothing survives.
                unusable += 1
        if not itineraries and unusable:
            raise InvalidQuery(
                f"none of the {unusable} trains between {origin_code} and "
                f"{destination_code} has usable departure and arrival times"
            )
        return self._order_by_departure(itineraries)

    async def _vendor_between(self, origin_code: str, destination_code: str) -> list[dict]:
        key = CacheKey(
            provider_id=self.id,
            kind=CacheKind.SEARCH,
            origin=f"IN:{origin_code}",
            destination=f"IN:{destination_code}",
            service_date=dt.datetime.now(dt.UTC).date(),
        )
        result = await self.cache.get_or_load(
            key, lambda: self.client.trains_between(origin_code, destination_code)
        )
        return list(result.value)

    def _options_from_vendor(self, rows: list[dict]) -> list[TripOption]:
        """Convert `trains/between` rows into `TripOption` using local stations."""
        options: list[TripOption] = []
        for row in rows:
            train = row.get("train") or {}
            origin = row.get("from") or {}
            destination = row.get("to") or {}
            origin_station = self.repo.station(origin.get("code") or "")
            destination_station = self.repo.station(destination.get("code") or "")
            options.append(
                TripOption(
                    train_number=str(train.get("number") or ""),
                    train_name=str(train.get("name") or ""),
                    train_type=train.get("type"),
                    run_days=(
                        "daily"
                        if len(train.get("runDays") or []) == 7
                        else ",".join(str(d).lower() for d in train.get("runDays") or [])
                    ),
                    classes=(),
                    origin_code=str(origin.get("code") or ""),
                    origin_name=str(origin.get("name") or ""),
                    origin_lat=origin_station.lat if origin_station else None,
                    origin_lon=origin_station.lon if origin_station else None,
                    departure=str(origin.get("departure") or ""),
                    departure_day=int(origin.get("day") or 1),
                    origin_distance_km=0.0,
                    destination_code=str(destination.get("code") or ""),
                    destination_name=str(destination.get("name") or ""),
                    destination_lat=destination_station.lat if destination_station else None,
                    destination_lon=destination_station.lon if destination_station else None,
                    arrival=str(destination.get("arrival") or ""),
                    arrival_day=int(destination.get("day") or 1),
                    destination_distance_km=float(row.get("distance") or 0.0),
                )
            )
        return options

    def _to_itinerary(self, option: TripOption, query: Query) -> Itinerary:
        departure = _hhmm(option.departure, option.departure_day, query.service_date)
        arrival = _hhmm(option.arrival, option.arrival_day, query.service_date)
        if departure is None or arrival is None:
            raise InvalidQuery(
                f"train {option.train_number} has no usable departure or arrival time"
            )
        if arrival <= departure:
            # Overnight where the day number was missing: assume the next day.
            arrival += dt.timedelta(days=1)

        duration_s = int((arrival - departure).total_seconds())
        orig = _stop(option.origin_code, option.origin_name, option.origin_lat, option.origin_lon)
        dest = _stop(
            option.destination_code,
            option.destination_name,
            option.destination_lat,
            option.destination_lon,
        )

        risk = self._risk_for(option, query, departure)

        # Only facts about this leg. The generic "no fare, no risk, no seats"
        # warnings belong to the engine, which knows what is missing across all
        # providers and can say it once instead of once per adapter.
        warnings: list[str] = []
        if orig.geo is None or dest.geo is None:
            warnings.append(
                "Station coordinates unavailable, so this leg cannot be drawn on the map."
            )

        leg = Leg(
            leg_id=leg_id(self.id, option.train_number, query.service_date),
            mode=Mode.rail,
            origin=orig,
            destination=dest,
            depart_at=departure,
            arrive_at=arrival,
            service_date=query.service_date,
            duration_s=duration_s,
            service=ServiceRef(
                provider_id=self.id,
                service_id=option.train_number,
                name=option.train_name,
                operator=option.train_type,
            ),
            distance_km=option.distance_km or None,
            availability=None,
            risk=risk,
            live_available=True,
            booking_channel=BookingChannel.deeplink,
        )

        return Itinerary(
            itinerary_id=itinerary_id([leg.leg_id]),
            legs=[leg],
            depart_at=departure,
            arrive_at=arrival,
            total_duration_s=duration_s,
            modes=[Mode.rail],
            total_price=None,
            score=0.5,
            score_breakdown=ScoreBreakdown(
                price=0.5, duration=0.5, delay_risk=0.5, comfort=0.5, transfers=1.0
            ),
            rank=1,
            risk_band="unknown",
            risk_reasons=_risk_reasons(risk),
            confidence=0.4,
            handoff=HandoffSummary(
                available=self.capabilities.supports(Feature.BOOKING),
                provider_id=self.id,
            ),
            tags=[],
            warnings=warnings,
        )

    def _risk_for(self, option: TripOption, query: Query, departure: dt.datetime):
        """A real probability for this train, or None when the model has no record.

        Features come from what we hold, not from the vendor: departure hour and
        weekday from the query, distance and stop count from the local timetable.
        """
        if self._model is None:
            return None
        prediction = self._model.predict(
            train_number=option.train_number,
            departure_hour=departure.hour,
            weekday=query.service_date.weekday(),
            distance_km=option.distance_km,
            stop_count=self.repo.stop_count(option.train_number),
        )
        return None if prediction is None else self._model.to_risk(prediction)

    def _order_by_departure(self, itineraries: list[Itinerary]) -> list[Itinerary]:
        """Deterministic data order only. Ranking belongs to the engine.

        The wire shape requires a `score` and a `rank` on every itinerary, so an
        adapter has to put something there. It puts the earliest departure first
        and a score of 0.0, which cannot be mistaken for a judgement, and
        `railmind.engine.ranking` overwrites both before a user sees a card.

        Two ranking implementations with different weights would be the bug this
        avoids: a second provider would silently change what a score means.
        """
        itineraries.sort(key=lambda item: (item.depart_at, item.legs[0].leg_id))
        for index, item in enumerate(itineraries, start=1):
            item.score = 0.0
            item.rank = index
        return itineraries

    # -- volatile facts -------------------------------------------------------

    async def availability(self, itinerary: Itinerary) -> Availability:
        self._require(Feature.AVAILABILITY)
        raise NotSupported(
            "Seat availability requires a paid data plan; check it on IRCTC",
            provider_id=self.id,
        )

    async def price(self, itinerary: Itinerary) -> Money:
        """One request per finalist. This is the only per-train spend we allow."""
        self._require(Feature.PRICE)
        leg = itinerary.legs[0]
        number = leg.service.service_id if leg.service else ""
        origin = (leg.origin.code or "").upper()
        destination = (leg.destination.code or "").upper()
        class_code = (leg.availability.class_code if leg.availability else "") or ""
        class_code = class_code or DEFAULT_CLASS_ORDER[1]

        key = CacheKey(
            provider_id=self.id,
            kind=CacheKind.FARE,
            origin=f"IN:{origin}",
            destination=f"IN:{destination}",
            service_date=leg.service_date,
            travel_class=f"{number}:{class_code}",
        )

        async def load() -> dict:
            return await self.client.fare(
                number,
                leg.service_date.isoformat(),
                origin,
                destination,
                class_code,
            )

        result = await self.cache.get_or_load(key, load)
        return self._money_from_fare(result.value)

    def _money_from_fare(self, payload: dict) -> Money:
        """Read a total out of the vendor's fare shape, or refuse.

        The shape was not confirmed during testing (the vendor's upstream failed),
        so this reads the common keys defensively and raises rather than guessing a
        number. A wrong price on the card is worse than a missing price.
        """
        candidates: list[object] = [
            payload.get("total"),
            payload.get("totalFare"),
            payload.get("fare"),
        ]
        fare = payload.get("fare")
        if isinstance(fare, dict):
            candidates.extend([fare.get("total"), fare.get("amount")])

        for candidate in candidates:
            if isinstance(candidate, (int, float)) and candidate > 0:
                return Money(amount_minor=round(float(candidate) * 100), currency="INR")
            if isinstance(candidate, str):
                try:
                    value = float(candidate)
                except ValueError:
                    continue
                if value > 0:
                    return Money(amount_minor=round(value * 100), currency="INR")
        raise NotSupported(
            "The fare response did not contain a total we can trust",
            provider_id=self.id,
        )

    async def live(self, leg: Leg) -> LiveStatus:
        self._require(Feature.LIVE)
        number = leg.service.service_id if leg.service else ""
        key = CacheKey(
            provider_id=self.id,
            kind=CacheKind.LIVE,
            origin=(leg.origin.code or ""),
            destination=(leg.destination.code or ""),
            service_date=leg.service_date,
            travel_class=number,
        )
        result = await self.cache.get_or_load(
            key, lambda: self.client.live(number), allow_stale=True
        )
        payload = result.value
        delay = payload.get("delayMinutes") or payload.get("delay") or 0
        try:
            delay_min = int(delay)
        except (TypeError, ValueError):
            delay_min = 0

        status = "on_time"
        if delay_min > 0:
            status = "delayed"
        elif delay_min < 0:
            status = "early"

        position = payload.get("currentLocation") or payload.get("position") or {}
        lat = position.get("lat") or position.get("latitude")
        lon = position.get("lng") or position.get("lon") or position.get("longitude")
        point = (
            GeoPoint(lat=float(lat), lon=float(lon))
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float))
            else None
        )

        return LiveStatus(
            leg_id=leg.leg_id,
            status=status,
            delay_min=delay_min,
            position=point,
            source=VENDOR_ID,
            stale=result.stale,
        )

    # -- handoff --------------------------------------------------------------

    async def handoff(self, itinerary: Itinerary, passengers: Sequence[Passenger]) -> Handoff:
        """Build the human's path. No request, no booking, no automation."""
        self._require(Feature.BOOKING)
        leg = itinerary.legs[0]
        url = build_irctc_deeplink(
            from_code=leg.origin.code or "",
            to_code=leg.destination.code or "",
            service_date=leg.service_date,
        )

        steps = [
            HandoffStep(
                order=1,
                title="Open the booking page and type your journey",
                detail=(
                    "IRCTC ignores journey details passed in a link (verified 2026-10-24), "
                    "so enter from, to and date yourself, then log in. RailMind holds no "
                    "credentials and never logs in for you."
                ),
                requires="human",
                est_seconds=14,
                url=url,
            ),
            HandoffStep(
                order=2,
                title="Pick the class and confirm the passengers",
                detail="Check every name and age before paying. Changes cost money.",
                requires="human",
                est_seconds=10,
            ),
            HandoffStep(
                order=3,
                title="Type the captcha",
                detail="The captcha exists to require a human. Solving it is not our job.",
                requires="captcha",
                est_seconds=5,
            ),
            HandoffStep(
                order=4,
                title="Answer the Aadhaar OTP if this train asks for one",
                detail=(
                    "Not every train: as of December 2025, IRCTC requires Aadhaar-verified "
                    "booking on 322 trains online. If this one does, the site prompts you "
                    "and the OTP goes to your phone. We never see it."
                ),
                requires="aadhaar",
                est_seconds=25,
            ),
            HandoffStep(
                order=5,
                title="Pay",
                detail="UPI is usually fastest. RailMind never touches your payment.",
                requires="payment",
                est_seconds=15,
            ),
        ]

        return Handoff(
            handoff_id=f"hof_{itinerary.itinerary_id[-12:]}",
            itinerary_id=itinerary.itinerary_id,
            provider_id=self.id,
            method="deeplink",
            url=url,
            prefill={
                "from_station": (leg.origin.code or "").upper(),
                "to_station": (leg.destination.code or "").upper(),
                "journey_date": leg.service_date.isoformat(),
                "train_number": leg.service.service_id if leg.service else "",
                "preferred_class": (leg.availability.class_code if leg.availability else "")
                or DEFAULT_CLASS_ORDER[1],
            },
            steps=steps,
            expires_at=dt.datetime.now(IST) + dt.timedelta(minutes=15),
            legal_notice=LEGAL_NOTICE,
            warnings=[
                "This link opens IRCTC's search page. Journey prefill could not be "
                "confirmed on the live site (verified 2026-10-24: the form opened empty), "
                "so enter the journey yourself. It takes a few seconds.",
            ],
        )


def _risk_reasons(risk) -> list[str]:
    """Plain sentences for the card. Empty when there is no model output."""
    if risk is None:
        return []
    reasons = [
        f"Observed punctuality: {risk.delay_probability * 100:.0f}% chance of arriving "
        "more than 15 minutes late"
    ]
    if risk.expected_delay_min is not None:
        reasons.append(f"Typical arrival delay on record: {risk.expected_delay_min:.0f} minutes")
    if risk.top_features:
        driver = risk.top_features[0]
        reasons.append(f"Main factor: {driver.name}")
    return reasons
