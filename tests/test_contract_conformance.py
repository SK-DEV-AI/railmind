"""Contract conformance: the server's real responses, checked against openapi.yaml.

Why this is separate from the API tests. Those assert behaviour ("this returns 422
with a policy title"). This asserts *shape*: every response the server produces is
valid against the schema published in the contract, so a client generated from
`openapi.yaml` cannot be surprised.

It is the test that catches the quiet drift nobody notices: a field renamed in the
Python model, a value that is a string in one place and an enum in another, a
`null` where the contract says a number. The generated pydantic models catch a lot
of that at construction time, but they are generated *from* the same file, so this
check is the independent reader: a JSON Schema validator that has never seen our
Python.

If it ever fails, the contract and the implementation disagree and a client is
about to break. It is not a test to loosen.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from openapi_spec_validator.readers import read_from_filename
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from railmind.api.app import create_app
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

ROOT = pathlib.Path(__file__).resolve().parent.parent
# The contract is YAML. Reading it with the same library the gate uses keeps one
# parser in the project rather than two that could disagree.
SPEC, _BASE_URI = read_from_filename(str(ROOT / "openapi.yaml"))
FIXTURES = ROOT / "tests" / "fixtures" / "railradar"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")

#: One registry holding the whole document, so `$ref: '#/components/...'` resolves.
# An OpenAPI document has no `$schema` at the root, so the dialect has to be
# declared here rather than sniffed. 3.1 uses JSON Schema 2020-12.
_REGISTRY = Registry().with_resource(
    "https://railmind.dev/openapi.yaml",
    Resource.from_contents(SPEC, default_specification=DRAFT202012),
)


def validate(body: object, schema: str | dict) -> None:
    """Validate one response body against a contract schema, by name or inline."""
    if isinstance(schema, str):
        schema = {"$ref": f"https://railmind.dev/openapi.yaml#/components/schemas/{schema}"}
    validator = Draft202012Validator(schema, registry=_REGISTRY)
    errors = sorted(validator.iter_errors(body), key=lambda error: list(error.path))
    assert not errors, "\n".join(
        f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in errors
    )


def make_intent() -> dict:
    start = dt.datetime(2026, 10, 26, 8, 0, tzinfo=IST)
    intent = BookingIntent(
        intent_id="int_spec",
        created_at=start,
        expires_at=start + dt.timedelta(hours=1),
        raw_text="mumbai to delhi tomorrow morning",
        origin=make_stop("IN:CSMT", "Mumbai CSMT"),
        destination=make_stop("IN:NDLS", "New Delhi", lat=28.64, lon=77.22),
        depart_window=TimeWindow(start=start, end=start + dt.timedelta(hours=12)),
        modes=[Mode.rail],
        optimise=Optimise.balanced,
        confidence=0.9,
        parsed_by=ParsedBy.rules,
    )
    return intent.model_dump(mode="json")


@pytest.fixture
def client(tmp_path):
    database = TimetableRepo(tmp_path / "timetable.db")
    seed_from_fixtures(database, FIXTURES)
    registry = ProviderRegistry()
    registry.register(FakeProvider(itineraries=[make_itinerary()]))
    log = RequestLog(tmp_path / "requests.jsonl")
    app = create_app(
        registry=registry,
        engine=SearchEngine(registry, log=log),
        log=log,
        timetable=database,
    )
    yield TestClient(app)
    database.close()


def test_health_matches_its_schema(client):
    validate(client.get("/health").json(), "Health")


def test_providers_matches_its_schema(client):
    body = client.get("/providers").json()

    # The contract wraps the list in an object, and for a while we returned a bare
    # array. This test is how that was found.
    validate(
        body,
        {
            "type": "object",
            "required": ["providers"],
            "properties": {
                "providers": {
                    "type": "array",
                    # Absolute, because a relative ref inside an inline schema
                    # resolves against that schema, not against the document.
                    "items": {
                        "$ref": (
                            "https://railmind.dev/openapi.yaml#/components/schemas/ProviderInfo"
                        )
                    },
                }
            },
            "additionalProperties": False,
        },
    )


def test_metrics_matches_its_schema(client):
    client.post("/search", json={"intent": make_intent()})  # give the log a record

    validate(client.get("/metrics", params={"window_seconds": 3600}).json(), "Metrics")


def test_intent_matches_its_schema(client):
    body = client.post("/intent", json={"text": "mumbai to pune tomorrow morning 3ac"}).json()

    validate(body, "IntentResponse")
    validate(body["intent"], "BookingIntent")


def test_search_matches_its_schema(client):
    body = client.post("/search", json={"intent": make_intent()}).json()

    validate(body, "SearchResponse")
    for itinerary in body["itineraries"]:
        validate(itinerary, "Itinerary")
        for leg in itinerary["legs"]:
            validate(leg, "Leg")


def test_itinerary_and_handoff_match_their_schemas(client):
    body = client.post("/search", json={"intent": make_intent()}).json()
    itinerary_id = body["itineraries"][0]["itinerary_id"]

    validate(client.get(f"/itinerary/{itinerary_id}").json(), "Itinerary")
    validate(client.post("/handoff", json={"itinerary_id": itinerary_id}).json(), "Handoff")


def test_live_matches_its_schema(client):
    body = client.post("/search", json={"intent": make_intent()}).json()
    leg_id = body["itineraries"][0]["legs"][0]["leg_id"]

    validate(client.get(f"/live/{leg_id}").json(), "LiveStatus")


def test_every_error_body_matches_the_problem_schema(client):
    """One error shape for every failure, or a client cannot parse any of them."""
    cases = [
        client.get("/itinerary/does-not-exist"),  # 404 not_found
        client.post("/search", json={}),  # 400 invalid_query, no intent
        client.post("/handoff", json={"itinerary_id": "nope"}),  # 404
        client.get("/metrics", params={"window_seconds": 1}),  # 400 validation
    ]

    for response in cases:
        assert response.headers["content-type"].startswith("application/problem+json")
        validate(response.json(), "Problem")


def test_the_contract_still_declares_eight_operations():
    """A count is a cheap tripwire that the spec was not silently trimmed."""
    paths = [path for path in SPEC["paths"] if not path.startswith("x-")]
    operations = [
        method
        for path in paths
        for method in SPEC["paths"][path]
        if method in {"get", "post", "put", "patch", "delete"}
    ]

    assert len(operations) == 8
    assert sorted(paths) == [
        "/handoff",
        "/health",
        "/intent",
        "/itinerary/{itinerary_id}",
        "/live/{leg_id}",
        "/metrics",
        "/providers",
        "/search",
    ]
