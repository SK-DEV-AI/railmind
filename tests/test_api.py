"""API tests: the contract's shape, its error shape, and its refusals.

Everything here runs through FastAPI's TestClient with a fake provider, so no
test needs a network, a key, or a running server.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest
from fastapi.testclient import TestClient

from railmind.api.app import ItineraryStore, create_app
from railmind.core.errors import NotFound, NotSupported
from railmind.data.timetable import TimetableRepo, seed_from_fixtures
from railmind.engine.metrics import RequestLog
from railmind.engine.orchestrator import SearchEngine
from railmind.providers.fake import FakeProvider, make_itinerary, make_stop
from railmind.providers.registry import ProviderRegistry
from railmind.wire.models import (
    BookingIntent,
    Mode,
    Optimise,
    ParsedBy,
    TimeWindow,
)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "railradar"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
SERVICE_DATE = dt.date(2026, 10, 25)


def make_intent() -> BookingIntent:
    start = dt.datetime.combine(SERVICE_DATE, dt.time(8), tzinfo=IST)
    return BookingIntent(
        intent_id="int_api",
        created_at=start,
        expires_at=start + dt.timedelta(hours=1),
        raw_text="mumbai to delhi",
        origin=make_stop("IN:CSMT", "Mumbai CSMT"),
        destination=make_stop("IN:NDLS", "New Delhi", lat=28.64, lon=77.22),
        depart_window=TimeWindow(start=start, end=start + dt.timedelta(hours=12)),
        modes=[Mode.rail],
        optimise=Optimise.balanced,
        confidence=0.9,
        parsed_by=ParsedBy.rules,
    )


@pytest.fixture
def timetable(tmp_path):
    database = TimetableRepo(tmp_path / "timetable.db")
    seed_from_fixtures(database, FIXTURES)
    yield database
    database.close()


@pytest.fixture
def client(tmp_path, timetable):
    registry = ProviderRegistry()
    registry.register(FakeProvider(itineraries=[make_itinerary()]))
    log = RequestLog(tmp_path / "requests.jsonl")
    app = create_app(
        registry=registry,
        engine=SearchEngine(registry, log=log),
        log=log,
        timetable=timetable,
    )
    return TestClient(app)


def test_health_reports_a_probe_it_actually_ran(client):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"]
    names = [dependency["name"] for dependency in body["dependencies"]]
    assert names == ["timetable_db", "timetable_schedule"]
    assert body["dependencies"][0]["latency_ms"] is not None
    # The freshly seeded fixture is inside the freshness policy, so both are ok.
    assert [item["state"] for item in body["dependencies"]] == ["ok", "ok"]


def test_providers_lists_capabilities_as_data(client):
    response = client.get("/providers")

    assert response.status_code == 200
    [provider] = response.json()["providers"]
    assert provider["provider_id"] == "fake_rail"
    assert provider["capabilities"]["search"] is True
    assert provider["modes"] == ["rail"]


def test_search_returns_ranked_itineraries_and_a_logged_metric(client):
    response = client.post("/search", json={"intent": make_intent().model_dump(mode="json")})

    assert response.status_code == 200
    body = response.json()
    assert body["itineraries"][0]["rank"] == 1
    assert body["meta"]["deadline_ms"] == 1800

    metrics = client.get("/metrics", params={"window_seconds": 3600}).json()
    assert metrics["sample_size"] == 1
    assert metrics["counted_from"] == "request_log"
    assert metrics["stages"]["search"]["n"] == 1


def test_search_without_an_intent_is_a_problem_document_not_a_stack_trace(client):
    response = client.post("/search", json={})

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    problem = response.json()
    assert problem["code"] == "invalid_query"
    assert problem["retryable"] is False
    assert problem["status"] == 400


def test_an_unknown_field_is_refused_rather_than_ignored(client):
    """A typo in a field name must not silently change what the caller asked for."""
    response = client.post("/search", json={"intent": None, "typo_field": 1})

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_query"


def test_itinerary_round_trips_by_id(client):
    body = client.post("/search", json={"intent": make_intent().model_dump(mode="json")}).json()
    itinerary_id = body["itineraries"][0]["itinerary_id"]

    response = client.get(f"/itinerary/{itinerary_id}")

    assert response.status_code == 200
    assert response.json()["itinerary_id"] == itinerary_id


def test_an_unknown_itinerary_id_is_a_404_problem(client):
    response = client.get("/itinerary/itin_nope")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_handoff_returns_steps_a_machine_cannot_take(client):
    body = client.post("/search", json={"intent": make_intent().model_dump(mode="json")}).json()
    itinerary_id = body["itineraries"][0]["itinerary_id"]

    response = client.post("/handoff", json={"itinerary_id": itinerary_id})

    assert response.status_code == 200
    handoff = response.json()
    assert handoff["itinerary_id"] == itinerary_id
    assert handoff["legal_notice"]
    assert [step["order"] for step in handoff["steps"]] == sorted(
        step["order"] for step in handoff["steps"]
    )
    # Every step names why a machine cannot do it. This is the legal argument,
    # in data, so it must never be empty.
    assert all(step["requires"] for step in handoff["steps"])


def test_live_status_by_leg_id(client):
    body = client.post("/search", json={"intent": make_intent().model_dump(mode="json")}).json()
    leg_id = body["itineraries"][0]["legs"][0]["leg_id"]

    response = client.get(f"/live/{leg_id}")

    assert response.status_code == 200
    assert response.json()["leg_id"] == leg_id


def test_a_leg_never_seen_before_is_a_404(client):
    response = client.get("/live/fake_rail:99999:2026-10-25:1")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_a_provider_that_cannot_hand_off_says_422_with_a_reason():
    """Capability gating belongs to the verb, so the client gets the real reason."""
    provider = FakeProvider(itineraries=[make_itinerary()])
    provider.capabilities = provider.capabilities.model_copy(update={"booking": "none"})
    registry = ProviderRegistry()
    registry.register(provider)
    client = TestClient(create_app(registry=registry))

    body = client.post("/search", json={"intent": make_intent().model_dump(mode="json")}).json()
    itinerary_id = body["itineraries"][0]["itinerary_id"]
    response = client.post("/handoff", json={"itinerary_id": itinerary_id})

    assert response.status_code == 422
    assert response.json()["code"] == "not_supported"
    assert response.headers["content-type"] == "application/problem+json"


def test_metrics_window_is_validated(client):
    response = client.get("/metrics", params={"window_seconds": 5})

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_query"


def test_store_expires_and_bounds_itself():
    store = ItineraryStore(ttl_s=10.0, capacity=2)
    store.put([make_itinerary(service_id="a")], now=0.0)
    store.put([make_itinerary(service_id="b")], now=1.0)

    assert len(store) == 2
    store.put([make_itinerary(service_id="c")], now=2.0)
    assert len(store) == 2  # oldest dropped, capacity held

    with pytest.raises(NotFound):
        store.get("missing", now=2.0)


def test_no_route_invents_a_booking_or_a_payment(client):
    """The legal design, asserted: there is nothing to call for booking or paying."""
    paths = {route.path for route in client.app.routes}

    forbidden = {"book", "booking", "pay", "payment", "checkout", "order"}
    assert not any(word in path for path in paths for word in forbidden)
    assert "/search" in paths and "/handoff" in paths


def test_a_raising_handoff_verb_becomes_its_own_problem_document():
    class RefusingProvider(FakeProvider):
        id = "refusing_rail"

        async def handoff(self, itinerary, passengers):
            raise NotSupported("tatkal opening day is blocked by policy", provider_id=self.id)

    registry = ProviderRegistry()
    registry.register(RefusingProvider(itineraries=[make_itinerary(provider_id="refusing_rail")]))
    client = TestClient(create_app(registry=registry))

    body = client.post("/search", json={"intent": make_intent().model_dump(mode="json")}).json()
    itinerary_id = body["itineraries"][0]["itinerary_id"]
    response = client.post("/handoff", json={"itinerary_id": itinerary_id})

    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "not_supported"
    assert "tatkal opening day" in problem["detail"]


def test_intent_reads_a_sentence_and_names_what_it_could_not_read(client):
    response = client.post(
        "/intent",
        json={"text": "book me mumbai to pune tomorrow 3ac under 2000", "use_llm": False},
    )

    assert response.status_code == 200
    body = response.json()
    intent = body["intent"]
    assert intent["origin"]["code"] == "CSMT"
    assert intent["destination"]["code"] == "PUNE"
    assert intent["class_preferences"] == ["3A"]
    assert intent["parsed_by"] == "rules"
    assert body["meta"]["parser"] == "rules"
    assert body["meta"]["elapsed_ms"] is not None


def test_intent_returns_a_question_instead_of_guessing_the_date(client):
    body = client.post("/intent", json={"text": "mumbai to pune"}).json()

    # Absent, not null: the contract's first convention, and now enforced by test.
    assert "depart_window" not in body["intent"]
    assert body["intent"]["ambiguities"][0]["field"] == "depart_window"


def test_intent_then_search_works_with_no_hand_built_json(client):
    """The point of /intent: one sentence reaches ranked cards."""
    intent = client.post("/intent", json={"text": "mumbai to pune tomorrow morning"}).json()[
        "intent"
    ]

    response = client.post("/search", json={"intent": intent})

    assert response.status_code == 200
    assert response.json()["itineraries"]


def _tatkal_intent(days_ahead: int) -> dict:
    """An intent whose journey is in the future, so Tatkal cannot be open yet."""
    start = (dt.datetime.now(IST) + dt.timedelta(days=days_ahead)).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    intent = make_intent().model_dump(mode="json")
    intent["quota"] = "tatkal"
    intent["class_preferences"] = ["3A"]
    intent["depart_window"] = {
        "start": start.isoformat(),
        "end": (start + dt.timedelta(hours=6)).isoformat(),
    }
    return intent


def test_tatkal_that_has_not_opened_is_said_plainly_and_tagged(client):
    body = client.post("/search", json={"intent": _tatkal_intent(days_ahead=3)}).json()

    assert any("Tatkal" in note for note in body["meta"]["notes"])
    assert "not_bookable_yet" in body["itineraries"][0]["tags"]
    assert any("Tatkal" in warning for warning in body["itineraries"][0]["warnings"])


def test_handoff_refuses_a_tatkal_party_over_the_four_passenger_cap(client):
    body = client.post("/search", json={"intent": _tatkal_intent(days_ahead=1)}).json()
    itinerary_id = body["itineraries"][0]["itinerary_id"]
    passengers = [{"name": f"Passenger {index}", "age": 30} for index in range(5)]

    response = client.post(
        "/handoff", json={"itinerary_id": itinerary_id, "passengers": passengers}
    )

    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "not_supported"
    assert problem["title"] == "Refused by a booking rule"
    assert "4 passengers" in problem["detail"]


# -- the intent_id half of the contract ---------------------------------------
#
# `/search` accepts exactly one of `intent` or `intent_id`. The id was declared
# in openapi.yaml, accepted by the model, and then never resolved, so a caller
# that did exactly what the contract said was told to call `/intent` first, after
# it had. These two tests are the reason that cannot come back.


def test_a_search_by_intent_id_resolves_the_intent_it_was_given(client):
    parsed = client.post("/intent", json={"text": "mumbai to delhi tomorrow"}).json()
    intent_id = parsed["intent"]["intent_id"]

    response = client.post("/search", json={"intent_id": intent_id})

    assert response.status_code == 200, response.text
    quay = response.json()
    assert quay["itineraries"], "an intent that parsed must still find trains"
    assert quay["meta"]["deadline_ms"] == 1800, "defaults apply when the id is used"


def test_an_intent_id_this_process_never_parsed_is_a_404_not_a_guess(client):
    response = client.post("/search", json={"intent_id": "int_does_not_exist"})

    assert response.status_code == 404
    problem = response.json()
    assert "int_does_not_exist" in problem["detail"]
    assert problem["retryable"] is False


def test_giving_both_an_intent_and_an_intent_id_is_refused(client):
    parsed = client.post("/intent", json={"text": "mumbai to delhi tomorrow"}).json()

    response = client.post(
        "/search", json={"intent": parsed["intent"], "intent_id": parsed["intent"]["intent_id"]}
    )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_query"
