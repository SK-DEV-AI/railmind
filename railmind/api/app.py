"""The HTTP layer. Thin on purpose: every decision it makes is a mapping.

What this file is allowed to do: parse a body against the contract's shapes,
call the engine or a provider verb, map a `ProviderError` onto a `Problem`, and
return JSON. What it is not allowed to do: rank, cache, retry, compute a price,
or decide what a missing fact means. Those live one layer down where they can be
tested without a server.

Two deliberate omissions, both legal rather than technical:

- There is no booking endpoint and no payment endpoint. The contract has eight
  operations and none of them submits an order. That absence is the design: this
  service recommends and hands off to the operator's own site, so it never
  becomes a party to the transaction.
- `/health` does not report provider health it has not measured. The only
  dependency it probes is the local timetable database, because that check is
  real, instant, and needs no quota. Claiming an upstream is fine because we
  hold a key would be a guess wearing a green badge.

Routing note: FastAPI's automatic 422 body does not match this contract's
`Problem` shape, so validation errors are translated. A client that parses one
error must be able to parse them all.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI
from fastapi import Query as QueryParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from railmind.core.errors import InvalidQuery, NotFound, ProviderError
from railmind.core.ids import parse_leg_id
from railmind.core.provider import TransportProvider
from railmind.data.timetable import TimetableRepo
from railmind.engine.metrics import RequestLog
from railmind.engine.orchestrator import SearchEngine
from railmind.intent.parser import parse as parse_intent
from railmind.providers.registry import ProviderRegistry
from railmind.wire.models import (
    Code,
    Dependency,
    HandoffRequest,
    Health,
    IntentRequest,
    IntentResponse,
    Itinerary,
    LiveStatus,
    Meta,
    Metrics,
    Problem,
    ProviderInfo,
    SearchRequest,
    SearchResponse,
    State1,
    Status2,
)
from railmind.wire.models import Handoff as WireHandoff

VERSION = "0.1.0"

#: How long a search result stays addressable by id. Ten minutes covers a human
#: reading a card and then tapping the booking page; beyond that the itinerary is
#: stale anyway, and holding it longer would just be a cache pretending to be a
#: store.
ITINERARY_TTL_S = 600.0

#: Bound on the in-process store, so a long-running demo cannot grow memory
#: without limit. Old entries are dropped oldest-first.
ITINERARY_CAPACITY = 500


class ItineraryStore:
    """Recent itineraries, addressable by id, in this process only.

    A real deployment would put this in Redis or a table. What matters for the
    contract is the behaviour: an id either resolves or returns 404, and the
    store never invents an itinerary it did not receive from a provider.
    """

    def __init__(self, *, ttl_s: float = ITINERARY_TTL_S, capacity: int = ITINERARY_CAPACITY):
        self.ttl_s = ttl_s
        self.capacity = capacity
        self._items: dict[str, tuple[Itinerary, float]] = {}

    def put(self, itineraries: list[Itinerary], *, now: float | None = None) -> None:
        stamp = time.monotonic() if now is None else now
        for itinerary in itineraries:
            self._items[itinerary.itinerary_id] = (itinerary, stamp + self.ttl_s)
        while len(self._items) > self.capacity:
            oldest = min(self._items, key=lambda key: self._items[key][1])
            del self._items[oldest]

    def get(self, itinerary_id: str, *, now: float | None = None) -> Itinerary:
        stamp = time.monotonic() if now is None else now
        entry = self._items.get(itinerary_id)
        if entry is None or entry[1] < stamp:
            raise NotFound(
                f"no itinerary {itinerary_id!r} in this store; it may have expired",
                provider_id=None,
            )
        return entry[0]

    def recent(self) -> Iterator[Itinerary]:
        """Every live entry. Public because two helpers below need it and
        reaching into `_items` from outside a class is how invariants leak."""
        for itinerary_id in list(self._items):
            yield self.get(itinerary_id)

    def __len__(self) -> int:
        return len(self._items)


def create_app(
    *,
    registry: ProviderRegistry,
    engine: SearchEngine | None = None,
    log: RequestLog | None = None,
    timetable: TimetableRepo | None = None,
) -> FastAPI:
    """Wire the app. Everything is injected so tests need no environment."""
    request_log = log or RequestLog()
    search_engine = engine or SearchEngine(registry, log=request_log)
    store = ItineraryStore()
    started = time.monotonic()

    app = FastAPI(
        title="RailMind",
        version=VERSION,
        description=(
            "Fast, honest travel decisions. Recommends and hands off; never books "
            "and never takes payment."
        ),
    )

    # -- error mapping ---------------------------------------------------------

    @app.exception_handler(ProviderError)
    async def _provider_error(_request, exc: ProviderError) -> JSONResponse:
        problem = exc.to_problem()
        return JSONResponse(
            problem.model_dump(mode="json"),
            status_code=exc.http_status,
            media_type="application/problem+json",
        )

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(_request, exc: RequestValidationError) -> JSONResponse:
        problem = Problem(
            type="https://railmind.dev/errors/invalid-query",
            title="Invalid request",
            status=400,
            detail=_first_error(exc),
            code=Code.invalid_query,
            retryable=False,
        )
        return JSONResponse(
            problem.model_dump(mode="json"),
            status_code=400,
            media_type="application/problem+json",
        )

    # -- ops -------------------------------------------------------------------

    @app.get("/health", response_model=Health)
    async def health() -> Health:
        checks = [_probe("timetable_db", lambda: _count_trains(timetable or _repo()))]
        return Health(
            status=Status2.ok,
            version=VERSION,
            uptime_s=round(time.monotonic() - started, 3),
            dependencies=checks,
        )

    @app.get("/metrics", response_model=Metrics)
    async def metrics(
        window_seconds: int = QueryParam(3600, ge=60, le=604800),
    ) -> Metrics:
        return request_log.metrics(window_seconds=window_seconds)

    @app.get("/providers", response_model=list[ProviderInfo])
    async def providers() -> list[ProviderInfo]:
        return registry.info()

    # -- intent ----------------------------------------------------------------

    @app.post("/intent", response_model=IntentResponse)
    async def intent(request: IntentRequest) -> IntentResponse:
        """One sentence in, a structured intent out, with the gaps named.

        Rules only, for now and by design: this is the request that has a human
        waiting, and `parsed_by` on the result says `rules` so nobody believes a
        model read it. `use_llm` is accepted and ignored until there is a model
        wired, rather than being silently pretended.
        """
        started = time.perf_counter()
        parsed = parse_intent(
            request.text,
            repo=timetable or _repo(),
            now=request.now,
            passengers=request.passengers,
            locale=request.locale or "en-IN",
        )
        elapsed = (time.perf_counter() - started) * 1000
        return IntentResponse(
            intent=parsed.intent,
            meta=Meta(elapsed_ms=round(elapsed, 1), parser="rules", cached=False),
        )

    # -- the hot path ----------------------------------------------------------

    @app.post("/search", response_model=SearchResponse)
    async def search(request: SearchRequest) -> SearchResponse:
        response = await search_engine.search(request)
        store.put(response.itineraries)
        return response

    @app.get("/itinerary/{itinerary_id}", response_model=Itinerary)
    async def get_itinerary(itinerary_id: str) -> Itinerary:
        return store.get(itinerary_id)

    # -- human-in-the-loop and live -------------------------------------------

    @app.post("/handoff", response_model=WireHandoff)
    async def handoff(request: HandoffRequest) -> WireHandoff:
        itinerary = store.get(request.itinerary_id)
        provider = _provider_for(registry, itinerary, request.provider_preference)
        return await provider.handoff(itinerary, request.passengers or [])

    @app.get("/live/{leg_id}", response_model=LiveStatus)
    async def live(leg_id: str) -> LiveStatus:
        parsed = parse_leg_id(leg_id)
        provider = registry.get(parsed.provider_id)
        itinerary = _find_leg(store, leg_id)
        leg = next(item for item in itinerary.legs if item.leg_id == leg_id)
        return await provider.live(leg)

    return app


# -- helpers ------------------------------------------------------------------


def _repo() -> TimetableRepo:
    """The default repo, opened lazily so importing this module touches nothing."""
    return TimetableRepo(Path("data/timetable.db"))


def _count_trains(repo: TimetableRepo) -> int:
    """A dependency check that is real: it reads the database it depends on."""
    trains = repo.trains_between("CSMT", "PUNE")
    if not trains:
        raise InvalidQuery("the timetable database is empty; run scripts/seed_timetable.py")
    return len(trains)


def _probe(name: str, check) -> Dependency:
    """Run one check and time it. A failing check is reported, not raised."""
    start = time.perf_counter()
    try:
        check()
    except Exception:
        return Dependency(
            name=name,
            state=State1.down,
            latency_ms=round((time.perf_counter() - start) * 1000, 3),
        )
    return Dependency(
        name=name,
        state=State1.ok,
        latency_ms=round((time.perf_counter() - start) * 1000, 3),
    )


def _provider_for(
    registry: ProviderRegistry,
    itinerary: Itinerary,
    preferred: list[str] | None,
) -> TransportProvider:
    """Which provider should build this handoff, in the caller's preferred order.

    It deliberately does not filter on the booking capability. If a provider is
    registered but cannot hand off, the right answer is that provider's own
    `NotSupported` (422, with the reason), not a 404 claiming the provider does
    not exist. Gating belongs to the verb, where `BaseProvider` already does it.
    """
    candidates = list(preferred or [])
    leg_provider = (
        itinerary.legs[0].service.provider_id
        if itinerary.legs[0].service is not None
        else itinerary.handoff.provider_id
    )
    candidates.append(leg_provider)
    for provider_id in candidates:
        if provider_id in registry:
            return registry.get(provider_id)
    raise NotFound(
        f"no registered provider in {candidates} can build a handoff for this itinerary",
        provider_id=leg_provider,
    )


def _find_leg(store: ItineraryStore, leg_id: str) -> Itinerary:
    """The stored itinerary that owns this leg, or a 404 that says why."""
    for itinerary in store.recent():
        if any(leg.leg_id == leg_id for leg in itinerary.legs):
            return itinerary
    raise NotFound(f"no recent itinerary contains leg {leg_id!r}")


def _first_error(exc: RequestValidationError) -> str:
    """One sentence from the first validation error, because a user reads one line."""
    errors = exc.errors()
    if not errors:
        return "the request body could not be validated"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = str(first.get("msg", "is not valid"))
    return f"{location}: {message}" if location else message
