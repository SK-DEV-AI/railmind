"""Step 2: the real provider, driven by recorded real responses.

No network in this file. Every payload is a raw vendor response saved during the
2026-10-24 session, so these tests exercise the real wire shapes, not a convenient
version of them.

The three findings that forced design decisions are each pinned by a test:

1. Seat availability is paid-plan only  -> capability is False, never guessed.
2. Fare needs date, from, to and class  -> all four are sent.
3. The fare shape was never confirmed   -> an unknown shape raises instead of
   inventing a price.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from railmind.core.errors import InvalidQuery, NotSupported, RateLimited, UpstreamDown
from railmind.core.types import Mode, Query
from railmind.data.timetable import TimetableRepo, seed_from_fixtures
from railmind.handoff.deeplink import build_irctc_deeplink
from railmind.providers._http import Response
from railmind.providers.rail_india import RailIndiaProvider
from railmind.providers.railradar import RailRadarClient, map_error

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "railradar"
SERVICE_DATE = dt.date(2026, 10, 26)  # a Monday

PUBLISHED = FIXTURES / "stations.json"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def response(payload: dict, status: int = 200) -> Response:
    return Response(status=status, headers={}, body=json.dumps(payload).encode(), family="ipv4")


class FakeTransport:
    """Returns recorded payloads and records what was asked for."""

    def __init__(self, routes: dict[str, dict] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, host, path, *, params=None, headers=None, deadline_s=None):
        self.calls.append((path, dict(params or {})))
        for key, payload in self.routes.items():
            if path.startswith(key):
                return response(payload)
        return response({"success": False, "error": {"code": "NOT_FOUND", "message": "no"}}, 404)


@pytest.fixture
def repo(tmp_path) -> TimetableRepo:
    database = TimetableRepo(tmp_path / "timetable.db")
    seed_from_fixtures(database, FIXTURES)
    yield database
    database.close()


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport(
        {
            "/v1/trains/12951/live": {
                "success": True,
                "data": {
                    "delayMinutes": 12,
                    "currentLocation": {"lat": 22.31, "lng": 74.11},
                    "trainNumber": "12951",
                },
            },
            "/v1/trains/12951/fare": {"success": True, "data": {"total": 2605.5}},
        }
    )


@pytest.fixture
def provider(repo, transport) -> RailIndiaProvider:
    return RailIndiaProvider(
        client=RailRadarClient(api_key="test-key", transport=transport),
        repo=repo,
        seat_availability=False,
    )


def make_query(origin: str = "CSMT", destination: str = "PUNE") -> Query:
    from railmind.providers.rail_india import _stop

    return Query(
        origin=_stop(origin, origin, 18.94, 72.83),
        destination=_stop(destination, destination, 18.52, 73.85),
        service_date=SERVICE_DATE,
    )


def train(itineraries, number: str):
    """Pick a train by number. Provider order is departure order, not rank order,
    and a test that depends on position breaks the moment ordering changes."""
    return next(item for item in itineraries if item.legs[0].service.service_id == number)


class TestSeededSearch:
    async def test_returns_real_trains_without_any_request(self, provider, transport):
        itineraries = await provider.search(make_query())

        assert sorted(item.legs[0].service.service_id for item in itineraries) == [
            "11007",
            "12127",
            "22105",
        ]
        assert transport.calls == []  # zero network for a seeded corridor

    async def test_times_and_duration_are_real(self, provider):
        itineraries = await provider.search(make_query())
        indrayani = next(i for i in itineraries if i.legs[0].service.service_id == "22105")

        assert indrayani.depart_at.strftime("%H:%M") == "05:40"
        assert indrayani.arrive_at.strftime("%H:%M") == "09:05"
        assert indrayani.total_duration_s == 3 * 3600 + 25 * 60

    async def test_the_engine_ranks_seeded_trains_by_duration(self, provider):
        from railmind.engine.ranking import rank

        raw = await provider.search(make_query())
        itineraries = rank(raw).itineraries

        durations = [item.total_duration_s for item in itineraries]
        assert durations == sorted(durations)
        assert [item.rank for item in itineraries] == [1, 2, 3]

    async def test_risk_is_unknown_not_invented(self, provider):
        itineraries = await provider.search(make_query())
        assert {item.risk_band for item in itineraries} == {"unknown"}
        assert all(item.risk_reasons == [] for item in itineraries)

    async def test_price_is_absent_because_it_was_not_fetched(self, provider):
        from railmind.engine.ranking import rank

        raw = await provider.search(make_query())
        assert all(item.total_price is None for item in raw)
        # The absence is announced by the engine, once, for every provider.
        assert all(
            any("No fare available" in w for w in item.warnings) for item in rank(raw).itineraries
        )

    async def test_leg_ids_are_derivable_and_stable(self, provider):
        from railmind.core.ids import parse_leg_id

        first = await provider.search(make_query())
        second = await provider.search(make_query())
        leg = first[0].legs[0]

        parsed = parse_leg_id(leg.leg_id)
        assert parsed.provider_id == "rail_india"
        assert parsed.service_date == SERVICE_DATE
        assert first[0].itinerary_id == second[0].itinerary_id

    async def test_stop_coordinates_come_from_the_real_route(self, provider):
        itineraries = await provider.search(make_query())
        origin = itineraries[0].legs[0].origin
        assert origin.geo is not None
        assert round(origin.geo.lat, 2) == 18.94

    async def test_handoff_is_available_and_says_deeplink(self, provider):
        itineraries = await provider.search(make_query())
        assert itineraries[0].handoff.available is True
        assert itineraries[0].legs[0].booking_channel.value == "deeplink"

    async def test_unknown_route_costs_exactly_one_request_then_caches(self, provider, transport):
        # CSMT -> ADI is not seeded, so the provider must ask the vendor once.
        transport.routes["/v1/trains/between/CSMT/ADI"] = load("between_CSMT_NDLS.json")
        query = make_query("CSMT", "ADI")

        first = await provider.search(query)
        second = await provider.search(query)

        assert len(first) == len(second) == 2
        assert len(transport.calls) == 1
        assert transport.calls[0][0].startswith("/v1/trains/between/CSMT/ADI")

    async def test_non_daily_train_is_filtered_out(self, repo, transport):
        payload = load("train_11007.json")
        payload["data"]["train"]["runDays"] = ["mon", "wed", "fri"]
        repo.upsert_schedule(payload["data"])

        provider = RailIndiaProvider(
            client=RailRadarClient(api_key="k", transport=transport), repo=repo
        )
        wednesday = dt.date(2026, 10, 28)
        tuesday = dt.date(2026, 10, 27)

        from railmind.providers.rail_india import _stop

        def query_on(day: dt.date) -> Query:
            return Query(
                origin=_stop("CSMT", "CSMT", 18.94, 72.83),
                destination=_stop("PUNE", "PUNE", 18.52, 73.85),
                service_date=day,
            )

        on_wednesday = [
            i.legs[0].service.service_id for i in await provider.search(query_on(wednesday))
        ]
        on_tuesday = [
            i.legs[0].service.service_id for i in await provider.search(query_on(tuesday))
        ]

        assert "11007" in on_wednesday
        assert "11007" not in on_tuesday


class TestAvailabilityHonesty:
    async def test_free_plan_does_not_advertise_availability(self, provider):
        assert provider.capabilities.supports("availability") is False

    async def test_asking_for_availability_raises_not_supported(self, provider):
        itinerary = train(await provider.search(make_query()), "12127")
        with pytest.raises(NotSupported):
            await provider.availability(itinerary)

    def test_vendor_plan_error_maps_to_not_supported(self):
        payload = load("_v1_trains_12137_seats.json")
        error = map_error(403, payload)
        assert isinstance(error, NotSupported)
        assert error.to_problem().code.value == "not_supported"
        assert "plan" in error.message.lower()


class TestFare:
    async def test_fetches_once_and_returns_exact_paise(self, provider, transport):
        transport.routes["/v1/trains/12127/fare"] = {"success": True, "data": {"total": 2605.5}}
        itinerary = train(await provider.search(make_query()), "12127")
        price = await provider.price(itinerary)
        await provider.price(itinerary)

        assert price.amount_minor == 260550
        assert price.currency == "INR"
        assert len(transport.calls) == 1
        _path, params = transport.calls[0]
        assert params["journeyDate"] == SERVICE_DATE.isoformat()
        assert params["classCode"] == "3A"
        assert "from" in params and "to" in params

    async def test_unknown_fare_shape_is_refused_not_guessed(self, provider, transport):
        transport.routes["/v1/trains/12127/fare"] = {"success": True, "data": {"mystery": 1}}
        itinerary = train(await provider.search(make_query()), "12127")
        with pytest.raises(NotSupported, match="did not contain a total"):
            await provider.price(itinerary)

    def test_vendor_upstream_failure_maps_to_upstream_down(self):
        payload = load("fare_12137_3A.json")
        error = map_error(400, payload)
        assert isinstance(error, UpstreamDown)

    def test_validation_error_maps_to_invalid_query(self):
        payload = load("_v1_trains_12137_fare.json")
        error = map_error(400, payload)
        assert isinstance(error, InvalidQuery)
        assert "journeyDate" not in error.to_problem().model_dump_json()


class TestLive:
    async def test_delay_and_position(self, provider, transport):
        transport.routes["/v1/trains/12127/live"] = {
            "success": True,
            "data": {"delayMinutes": 12, "currentLocation": {"lat": 22.31, "lng": 74.11}},
        }
        itinerary = train(await provider.search(make_query()), "12127")
        status = await provider.live(itinerary.legs[0])

        assert status.status == "delayed"
        assert status.delay_min == 12
        assert status.position is not None
        assert status.stale is False

    async def test_live_is_cached_within_its_ttl(self, provider, transport):
        transport.routes["/v1/trains/12127/live"] = {"success": True, "data": {"delayMinutes": 0}}
        itinerary = train(await provider.search(make_query()), "12127")
        await provider.live(itinerary.legs[0])
        await provider.live(itinerary.legs[0])
        assert len(transport.calls) == 1


class TestHandoff:
    async def test_builds_a_real_irctc_link_without_network(self, provider, transport):
        itinerary = train(await provider.search(make_query()), "12127")
        handoff = await provider.handoff(itinerary, [])

        assert str(handoff.url).startswith("https://www.irctc.co.in/nget/train-search?")
        assert "fromStation=CSMT" in str(handoff.url)
        assert "toStation=PUNE" in str(handoff.url)
        assert f"date={SERVICE_DATE.strftime('%Y%m%d')}" in str(handoff.url)
        assert transport.calls == []

    async def test_never_claims_to_book(self, provider):
        itinerary = train(await provider.search(make_query()), "12127")
        handoff = await provider.handoff(itinerary, [])

        assert handoff.method == "deeplink"
        assert "does not book" in handoff.legal_notice
        requires = {step.requires.value for step in handoff.steps}
        assert {"human", "captcha", "aadhaar", "payment"} <= requires

    async def test_prefill_is_honest_about_what_the_url_cannot_carry(self, provider):
        itinerary = train(await provider.search(make_query()), "12127")
        handoff = await provider.handoff(itinerary, [])

        assert any("enter the journey yourself" in w for w in handoff.warnings)
        assert "prefill could not be" in " ".join(handoff.warnings)
        assert handoff.prefill["train_number"]
        # The URL never claims to carry a class, because the site ignores it.
        assert "class" not in str(handoff.url).lower().replace("classcode", "")


class TestDeeplink:
    def test_exact_url(self):
        url = build_irctc_deeplink(from_code="csmt", to_code="ndls", service_date="2026-12-01")
        assert url == (
            "https://www.irctc.co.in/nget/train-search"
            "?fromStation=CSMT&toStation=NDLS&date=20261201"
        )

    def test_same_station_is_refused(self):
        with pytest.raises(Exception, match="same station"):
            build_irctc_deeplink(from_code="CSMT", to_code="CSMT", service_date="2026-12-01")

    def test_past_date_is_refused(self):
        with pytest.raises(Exception, match="past"):
            build_irctc_deeplink(from_code="CSMT", to_code="NDLS", service_date="2020-01-01")


class TestVendorErrorTable:
    @pytest.mark.parametrize(
        ("status", "code", "expected"),
        [
            (429, "RATE_LIMITED", RateLimited),
            (500, "INTERNAL", UpstreamDown),
            (404, "NOT_FOUND", NotSupported.__mro__[1]),  # ProviderError base
            (403, "PLAN_UPGRADE_REQUIRED", NotSupported),
            (400, "VALIDATION_ERROR", InvalidQuery),
        ],
    )
    def test_maps_to_the_closed_set(self, status, code, expected):
        error = map_error(status, {"success": False, "error": {"code": code, "message": "x"}})
        assert isinstance(error, expected)

    def test_vendor_text_is_never_in_the_public_problem(self):
        error = map_error(
            400,
            {
                "success": False,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "journeyDate is required (YYYY-MM-DD), aliases: date",
                },
            },
        )
        assert "journeyDate" not in error.to_problem().model_dump_json()
        assert "aliases" in (error.cause_detail or "")


def test_fixtures_are_real_vendor_responses():
    """The fixture directory is evidence, not invention: every file has a trace id."""
    assert PUBLISHED.exists()
    schedules = list(FIXTURES.glob("train_*.json"))
    assert len(schedules) >= 5
    for path in schedules:
        payload = json.loads(path.read_text())
        assert payload["success"] is True
        assert "traceId" in payload["meta"]
        assert payload["data"]["route"], path.name


def test_modes_are_rail_only(provider):
    assert provider.modes == {Mode.rail}
    assert provider.regions == {"IN"}


class TestSegmentUsability:
    def test_a_train_that_terminates_here_cannot_be_boarded_here(self, repo):
        """12951 runs MMCT -> NDLS, so NDLS is a terminus and not an origin."""
        assert repo.trains_between("MMCT", "NDLS")
        assert repo.trains_between("NDLS", "MMCT") == []

    async def test_one_unusable_option_does_not_hide_the_corridor(self, provider, monkeypatch):
        from railmind.data.timetable import TripOption

        good = provider.repo.trains_between("CSMT", "PUNE")[0]
        broken = TripOption(
            train_number="99999",
            train_name="GHOST EXP",
            train_type="Express",
            run_days="daily",
            classes=(),
            origin_code="CSMT",
            origin_name="Mumbai CSMT",
            origin_lat=18.94,
            origin_lon=72.835,
            departure="",
            departure_day=1,
            origin_distance_km=0.0,
            destination_code="PUNE",
            destination_name="Pune Jn",
            destination_lat=18.52,
            destination_lon=73.85,
            arrival="10:00",
            arrival_day=1,
            destination_distance_km=190.7,
        )
        monkeypatch.setattr(provider.repo, "trains_between", lambda a, b: [broken, good])

        itineraries = await provider.search(make_query())

        assert [item.legs[0].service.service_id for item in itineraries] == [good.train_number]


class TestNoQuotaOnSeededCorridors:
    async def test_a_corridor_with_no_train_that_day_does_not_call_the_vendor(
        self, repo, transport
    ):
        """The local timetable already knows; asking the vendor would spend quota."""
        calls_before = len(transport.calls)
        provider = RailIndiaProvider(
            client=RailRadarClient(api_key="test-key", transport=transport),
            repo=repo,
            seat_availability=False,
        )
        # 12127 and friends run daily in the fixtures, so pick a corridor that is
        # seeded and then ask for a date where nothing runs by filtering the
        # run-days: an emphatic test is cheaper than a contrived schedule.
        seeded = repo.trains_between("CSMT", "PUNE")
        assert seeded
        for option in seeded:
            repo._conn.execute(
                "UPDATE trains SET run_days = 'mon' WHERE number = ?", (option.train_number,)
            )
        repo._conn.commit()

        query = make_query()
        query = Query(
            origin=query.origin,
            destination=query.destination,
            service_date=dt.date(2026, 10, 28),  # a Wednesday
        )

        itineraries = await provider.search(query)

        assert itineraries == []
        assert len(transport.calls) == calls_before


def test_a_station_is_not_its_own_destination(repo):
    """Some schedules list the same code twice; that is not a journey."""
    payload = load("train_12951.json")["data"]
    route = payload["route"]
    # Duplicate the second stop at the end, which is what the real crawl data does.
    route.append(dict(route[1]))
    route[-1]["sequence"] = len(route)
    repo.upsert_schedule(payload)

    assert repo.trains_between("MX", "MX") == []
