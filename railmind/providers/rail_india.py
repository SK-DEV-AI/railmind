"""Indian Railways provider: real timetable, real fares, real handoff.

Composition, and why each part is where it is:

- `TimetableRepo` answers "which trains run this corridor" with zero network. That
  is what makes a 100-request month survivable.
- `RailRadarClient` is consulted only for volatile facts: a route it has not seen,
  a live position.
- `railmind.fare` answers "what does a seat cost" from the Railway Board's printed
  fare table, so a price costs nothing at all.
- `RouteCache` sits between them, keyed by route, with stale-serve.
- The IRCTC deep link is built locally. No request, no automation, no booking.

Deliberate limits, stated rather than hidden:

- A fare exists only for the classes and services the circular prices: Mail/Express
  and Rajdhani today. A Shatabdi or Vande Bharat is refused rather than priced off
  the wrong table, and so is a distance the printed rows do not cover.
- Seat availability is not on the free data plan, so the capability is advertised
  as absent and `availability()` raises `NotSupported`. It is never guessed.
- Ranking here uses scheduled duration only. The delay model arrives in Step 3,
  so `risk_band` is `unknown` and confidence is low. A "low risk" badge with no
  model behind it would be a lie with a nice colour.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from railmind import fare
from railmind.core import services as services_module
from railmind.core.capabilities import Capabilities, Feature, rail_capabilities
from railmind.core.errors import InvalidQuery, NotSupported
from railmind.core.ids import itinerary_id, leg_id, stop_id
from railmind.core.provider import BaseProvider
from railmind.core.types import (
    Availability,
    BookingChannel,
    FareComponent,
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
    Quota,
    ScoreBreakdown,
    ServiceRef,
    Stop,
    TimeWindow,
    Title,
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

#: Quote component code -> the wire's label for it.
FARE_TITLES = {
    "basic": Title.base_fare,
    "reservation": Title.reservation_charge,
    "superfast": Title.superfast_charge,
    "tatkal": Title.tatkal_premium,
    "gst": Title.gst,
    "rounding": Title.other,
}

#: Services whose base fare the circular prints in a table of their own. Until each
#: table is transcribed, pricing these off the Mail/Express table would understate
#: the real fare, so they are refused and named instead.
UNTABLED_SERVICES = (
    ("SHATABDI", "Shatabdi"),
    ("VANDE BHARAT", "Vande Bharat"),
    ("DURONTO", "Duronto"),
    ("GATIMAAN", "Gatimaan"),
    ("TEJAS", "Tejas"),
    ("HUMSAFAR", "Humsafar"),
    ("GARIB RATH", "Garib Rath"),
    ("AMRIT BHARAT", "Amrit Bharat"),
    ("MAHAMANA", "Mahamana"),
    ("ANTYODAYA", "Antyodaya"),
    ("JAN SHATABDI", "Jan Shatabdi"),
    ("YUVA", "Yuva"),
    ("VISTADOME", "Vistadome"),
    ("ANUBHUTI", "Anubhuti"),
    ("NAMO BHARAT", "Namo Bharat"),
)


def _is_rajdhani(name: str) -> bool:
    """Rajdhani keeps its own base fare table, including the Tejas Rajdhani."""
    return "RAJDHANI" in name.upper()


def _is_tatkal(quota: Quota | str | None) -> bool:
    """Whether this journey is a Tatkal one, which is what adds the premium.

    The wire default is a bare string, so an unknown value is read as it arrives and
    answers no: only the Tatkal quota adds a Tatkal premium.
    """
    if quota is None:
        return False
    return str(getattr(quota, "value", quota)).lower() == Quota.tatkal.value


def _untabled_service(name: str) -> str | None:
    upper = name.upper()
    for marker, label in UNTABLED_SERVICES:
        if marker in upper:
            return label
    return None


def _unpriced_quote(
    table: fare.FareTable, class_code: str, distance_km: float, reason: str
) -> fare.FareQuote:
    return fare.FareQuote(
        section="",
        class_code=class_code,
        distance_km=distance_km,
        unpriced_reason=reason,
        source=table.source,
        effective=table.effective,
    )


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

        seeded = self.repo.trains_between(origin_code, destination_code)
        weekday = query.service_date.weekday()

        if seeded:
            # The corridor is in the local timetable, so the answer is local, full
            # stop. Filtering by run-day may leave nothing, and that is a real
            # answer: no train on that date. Asking the vendor here would spend
            # quota to learn what we already know, and a corridor audit found 21
            # percent of sampled corridors doing exactly that.
            options = [option for option in seeded if option.runs_on(weekday)]
            if not options:
                return []
        else:
            # Genuinely not seeded. One request tells us which trains run it; their
            # stops and coordinates come from the local station table where known.
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
        itineraries = self._for_services(itineraries, query.services)
        itineraries = self._within_window(itineraries, query.depart_window)
        return self._order_by_departure(itineraries)

    @staticmethod
    def _for_services(itineraries: list[Itinerary], services: tuple[str, ...]) -> list[Itinerary]:
        """Keep the trains that carry the brand the user named.

        Asking for a Vande Bharat and being handed a 21:15 Mail/Express is not a
        near miss, it is a different journey. The brand is matched against the
        train name already in hand, so this costs no request and cannot invent a
        train. Same shape as the window rule: filter first, note second -- if no
        such train runs this corridor, the whole list comes back with the
        mismatch stated on every card, because an empty shortlist hides real
        trains from someone in a hurry.
        """
        if not services:
            return itineraries

        def named(item: Itinerary) -> bool:
            leg = item.legs[0] if item.legs else None
            name = leg.service.name if leg is not None and leg.service is not None else ""
            return any(services_module.matches(key, name) for key in services)

        inside = [item for item in itineraries if named(item)]
        if inside:
            return inside
        wanted = services_module.labels(services)
        for item in itineraries:
            leg = item.legs[0] if item.legs else None
            name = leg.service.name if leg is not None and leg.service is not None else "unknown"
            item.warnings = [
                *(item.warnings or ()),
                f"No {wanted} train runs this corridor; this one is {name}",
            ]
        return itineraries

    @staticmethod
    def _within_window(itineraries: list[Itinerary], window: TimeWindow | None) -> list[Itinerary]:
        """Keep the trains that leave when the user said they want to leave.

        *When* a train departs is the fact that decides a Tatkal decision, so a
        request for "tomorrow morning" answered with a 17:10 train is a wrong
        answer even though every field on the card is true. The window is a filter
        first and a note second: if no train runs inside it, the whole day comes
        back with the mismatch stated on each card, because answering "when does a
        train leave" with an empty list hides a real train from someone in a hurry.
        """
        if window is None:
            return itineraries
        inside = [item for item in itineraries if window.start <= item.depart_at <= window.end]
        if inside:
            return inside
        for item in itineraries:
            item.warnings = [
                *(item.warnings or []),
                (
                    "No train departs inside the requested window; this one leaves at "
                    f"{item.depart_at:%H:%M}"
                ),
            ]
        return itineraries

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

        total_price: Money | None = None
        price_breakup: list[FareComponent] | None = None
        if query.classes:
            # A price is only attached when the user named a class: the wire has one
            # total per itinerary, and a total for a class nobody asked for reads as
            # the price of the train.
            wanted = query.classes[0]
            if option.classes and wanted not in option.classes:
                # The timetable says what this train carries, and it does not carry
                # the requested class. A fare for that class would be a price for a
                # seat that does not exist on this train.
                warnings.append(
                    f"No fare is shown for {wanted}: this train carries {', '.join(option.classes)}"
                )
            else:
                quote = self.quote_for_leg(leg, wanted, quota=query.quota)
                if quote.total_minor is not None:
                    total_price = Money(amount_minor=quote.total_minor, currency="INR")
                    price_breakup = self.fare_components(quote)
                    if not option.classes:
                        # The crawl records no class list, so the price is the printed
                        # fare for that class at this distance on this service type,
                        # which is the right number when the class runs and a
                        # hypothetical one when it does not. Said out loud rather than
                        # left for the user to discover on the booking page.
                        warnings.append(
                            f"The timetable records no class list for this train, so the "
                            f"{wanted} fare is the printed fare for this distance; confirm "
                            f"{wanted} runs on this train before booking"
                        )
                elif quote.unpriced_reason:
                    warnings.append(f"No fare is shown for {wanted}: {quote.unpriced_reason}")

        return Itinerary(
            itinerary_id=itinerary_id([leg.leg_id]),
            legs=[leg],
            depart_at=departure,
            arrive_at=arrival,
            total_duration_s=duration_s,
            modes=[Mode.rail],
            total_price=total_price,
            price_breakup=price_breakup,
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
        """A published fare, computed locally. No request, no spend, no guess.

        The fare used to come from the vendor, one request per finalist, from a
        response shape nobody had ever seen succeed. It now comes from the Railway
        Board's own printed table, which is both authoritative and free, so a
        decision costs zero requests and there is no per-train budget to ration.

        Anything the table does not price raises instead of returning a number: a
        clever-looking estimate on a booking card is the one error that costs a
        user money.
        """
        self._require(Feature.PRICE)
        quote = self.fare_quote(itinerary)
        if quote.total_minor is None:
            raise NotSupported(
                quote.unpriced_reason or "this journey is not in the published fare table",
                provider_id=self.id,
            )
        return Money(amount_minor=quote.total_minor, currency="INR")

    def fare_quote(
        self, itinerary: Itinerary, class_code: str | None = None, quota: Quota | None = None
    ) -> fare.FareQuote:
        """The worked quote for this leg: parts, assumptions, and its source."""
        return self.quote_for_leg(itinerary.legs[0], class_code, quota=quota)

    def quote_for_leg(
        self, leg: Leg, class_code: str | None = None, quota: Quota | None = None
    ) -> fare.FareQuote:
        """Price one leg from the printed table, naming what it cannot price.

        `quota` decides whether the printed Tatkal premium belongs in the total. Under
        Tatkal it does, and it goes inside the tax base; under any other quota the
        quote says nothing about Tatkal at all.
        """
        wanted = class_code or (leg.availability.class_code if leg.availability else "")
        wanted = wanted or DEFAULT_CLASS_ORDER[0]
        name = (leg.service.name if leg.service else "") or ""
        table = fare.default_table()
        distance = leg.distance_km

        if distance is None or distance <= 0:
            return _unpriced_quote(
                table,
                wanted,
                0.0,
                "the timetable does not give this train's distance, so no fare applies",
            )
        untabled = _untabled_service(name)
        if untabled is not None:
            return _unpriced_quote(
                table,
                wanted,
                distance,
                f"this is a {untabled} service, whose fares the circular prints in its "
                f"own table; that table has not been transcribed yet, so quoting the "
                f"Mail/Express fare would understate the real price",
            )
        try:
            section = fare.resolve(table, wanted, rajdhani=_is_rajdhani(name))
        except fare.UnknownServiceError as error:
            return _unpriced_quote(table, wanted, distance, str(error))
        return fare.quote(
            section=section,
            class_code=wanted,
            # Neither the table nor the timetable carries a superfast flag, so the
            # supplementary charge is unknown here. Saying so lets the quote carry
            # the printed upper bound instead of a total that is quietly short.
            superfast=None,
            distance_km=distance,
            # AC First and AC 2-Tier are printed twice, once per season. The journey
            # date settles which column applies; a class that is printed once ignores it.
            season=fare.season_for(leg.service_date),
            table=table,
            charges=fare.default_charges(),
            tax=fare.default_tax(),
            tatkal=fare.default_tatkal() if _is_tatkal(quota) else None,
        )

    def fare_components(self, quote: fare.FareQuote) -> list[FareComponent]:
        """The quote's parts in the wire shape, so a card can show its working."""
        return [
            FareComponent(
                title=FARE_TITLES[component.code],
                amount=Money(amount_minor=component.amount_minor, currency="INR"),
                note=component.note,
            )
            for component in quote.components
            if component.code in FARE_TITLES
        ]

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
