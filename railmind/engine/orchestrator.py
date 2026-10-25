"""The engine: one request in, one ranked card out, with no model on the hot path.

This is the file the latency claim is about. It does four things and nothing
else:

1. Turn an already-parsed intent into a provider-neutral `Query`. Parsing belongs
   to `/intent`, in front of this, because an LLM must never sit between a user
   and a result when the user is racing a 10:00 Tatkal opening.
2. Ask every provider that advertises search for the requested mode, all at once,
   under one deadline. Two providers cost the same wall-clock as the slow one,
   which is the whole reason the fan-out is here rather than in each adapter.
3. Rank the union with one function, so a bus and a train are scored by the same
   arithmetic and the number on a card means the same thing across modes.
4. Record what happened, so `/metrics` counts real requests instead of a
   pre-written number.

Three deliberate refusals:

- No provider failure becomes a user-facing failure while any provider answered.
  A dead adapter is a note on the response, not a 500, because half a result is
  worth more to a rushing human than an apology.
- A deadline that expires cancels the slow provider rather than extending itself.
  Every millisecond past the deadline is a millisecond the human does not have.
- Nothing here invents a fact. Missing prices, unknown availability and an
  untrained delay model all surface as warnings on the itinerary, never as a
  neutral value that silently looks like data.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace

from railmind.core.capabilities import Feature
from railmind.core.errors import InvalidQuery, ProviderError
from railmind.core.types import Mode, Query
from railmind.engine.metrics import RequestLog, RequestRecord
from railmind.engine.ranking import rank
from railmind.engine.singleflight import SingleFlight
from railmind.guardrails.tatkal import check_tatkal
from railmind.providers.accounting import meter
from railmind.wire.models import (
    Cache,
    Itinerary,
    SearchMeta,
    SearchRequest,
    SearchResponse,
)
from railmind.wire.models import Mode as WireMode


@dataclass(frozen=True, slots=True)
class FanOut:
    """What the providers collectively said, and what they cost to ask."""

    itineraries: list[Itinerary] = field(default_factory=list)
    used: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    timed_out: tuple[str, ...] = ()
    #: Provider calls that actually ran. The number that costs quota.
    upstream_calls: int = 0
    #: Callers served by someone else's in-flight call. The number that was saved.
    coalesced: int = 0


class SearchEngine:
    """Fan out to providers, rank the union, record the request.

    Holds no cache and no clock state: providers own their caches, and the only
    time this object keeps is the `perf_counter` delta of one request.
    """

    def __init__(
        self,
        registry: object,
        *,
        log: RequestLog | None = None,
        weights: Mapping[str, float] | None = None,
        clock: object = time.perf_counter,
        flight: SingleFlight | None = None,
    ) -> None:
        """`registry` is a `ProviderRegistry`. Typed loosely to keep this module
        free of a concrete import cycle, and because the engine only asks it two
        questions: which providers can search, and what their ids are."""
        self.registry = registry
        self.log = log
        self.weights = weights
        self.clock = clock
        # Shared across requests on purpose: the whole point is that two users
        # asking the same question at the same tick cost one provider call.
        self.flight = flight or SingleFlight()

    # -- the one public entry point --

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Answer a search request. Raises `InvalidQuery` rather than guessing."""
        started = self.clock()
        wall = dt.datetime.now(dt.UTC)

        intent = request.intent
        if intent is None:
            # A search without an intent is a caller bug, not a user error: the
            # contract requires one of `intent` or `intent_id` to be resolvable.
            raise InvalidQuery("search needs a parsed intent; call /intent first")

        query = Query.from_intent(intent)
        if request.modes:
            # The request may narrow modes for this call only. Anything the
            # provider does not serve is simply not asked.
            query = _with_modes(query, tuple(_mode(mode) for mode in request.modes))
        intent_done = self.clock()

        deadline_s = min(request.deadline_ms or 1800, 8000) / 1000.0
        remaining = max(0.05, deadline_s - (intent_done - started))
        # The meter counts outbound vendor calls for THIS request. A search answered
        # from the local timetable reports zero, which is the true and useful number
        # on a 100-request monthly budget.
        with meter() as counter:
            outcome = await self._fan_out(query, remaining)
        outcome = replace(outcome, upstream_calls=counter["calls"])
        search_done = self.clock()

        ranked = rank(outcome.itineraries, weights=self.weights)
        limit = request.limit or 10
        chosen = ranked.itineraries[:limit]

        notes = list(outcome.notes)
        notes.extend(ranked.notes)

        # Quota rules are evaluated once per request, not once per card: they are
        # properties of the journey, and a card that disagreed with its neighbour
        # about whether Tatkal is open would be worse than no warning at all.
        verdict = check_tatkal(
            quota=query.quota,
            service_date=query.service_date,
            now=wall,
            passenger_count=len(query.passengers),
            class_codes=query.classes,
        )
        if verdict.refused:
            notes.extend(verdict.reasons)
        for itinerary in chosen:
            # A refusal has to travel with the card, not only with the response:
            # a card can be screenshotted, cached, or read on its own, and "why
            # you cannot act on this" must survive that trip.
            extra = (*verdict.warnings, *verdict.reasons)
            itinerary.warnings = _dedupe([*(itinerary.warnings or []), *extra])
            if verdict.refused:
                itinerary.tags = _dedupe([*(itinerary.tags or []), "not_bookable_yet"])
        degraded = _is_degraded(chosen, outcome)
        if not chosen and outcome.used and not outcome.failed:
            # Empty and nothing failed means the timetable has no train for this
            # route on this date, which is an answer rather than an error.
            notes.append("no train runs this route on the requested date in the local timetable")
        elif degraded and not notes:
            notes.append("some inputs were missing or partial; see each card's warnings")

        elapsed_ms = (search_done - started) * 1000.0
        meta = SearchMeta(
            elapsed_ms=round(elapsed_ms, 1),
            deadline_ms=int(deadline_s * 1000),
            degraded=degraded,
            notes=notes or None,
            providers_used=list(outcome.used) or None,
            providers_failed=list(outcome.failed) or None,
            cache=_cache_state(chosen),
        )

        response = SearchResponse(
            search_id=_search_id(),
            intent_id=request.intent_id,
            itineraries=chosen,
            meta=meta,
        )
        self._record(
            response=response,
            wall=wall,
            elapsed_ms=elapsed_ms,
            intent_ms=(intent_done - started) * 1000.0,
            search_ms=(search_done - intent_done) * 1000.0,
            outcome=outcome,
        )
        return response

    # -- fan-out --

    async def _fan_out(self, query: Query, timeout_s: float) -> FanOut:
        """Ask every capable provider at once, and never wait past the deadline."""
        wanted = {mode.value for mode in query.modes}
        providers = [
            provider
            for provider in self.registry.find(Feature.SEARCH)  # type: ignore[attr-defined]
            if wanted.intersection({mode.value for mode in provider.modes})
        ]
        if not providers:
            return FanOut(notes=("no provider serves the requested mode",))

        # Per-request attribution, not a diff of global counters: under a burst
        # every request reads the same counters at nearly the same instant, and a
        # global delta would be counted once per request and inflate the total.
        # The factory only ever runs for the leader, so a flag set inside it is
        # exact evidence that this request paid for the call.
        tasks: dict[asyncio.Task[list[Itinerary]], tuple[str, dict[str, bool]]] = {}
        for provider in providers:
            # The key is everything that changes the answer. Two users with the
            # same route, date, class and quota asked the same question.
            # `quota` may be a bare string, because the wire default is a string.
            quota_key = getattr(query.quota, "value", query.quota)
            key = (provider.id, *query.cache_key_parts, query.classes, quota_key)
            ledger = {"led": False}

            async def factory(candidate=provider, record=ledger):
                record["led"] = True
                return await _guard(candidate, query)

            task = asyncio.create_task(self.flight.do(key, factory), name=f"search:{provider.id}")
            tasks[task] = (provider.id, ledger)

        done, pending = await asyncio.wait(tasks, timeout=timeout_s)
        for task in pending:
            # Cancelling is the point: the deadline is the user's, not ours.
            task.cancel()

        collected: list[Itinerary] = []
        used: list[str] = []
        failed: list[str] = []
        notes: list[str] = []

        led = 0
        for task in done:
            provider_id, ledger = tasks[task]
            led += 1 if ledger["led"] else 0
            error = task.exception()
            if error is None:
                collected.extend(task.result())
                used.append(provider_id)
                continue
            failed.append(provider_id)
            notes.append(_explain(provider_id, error))

        timed_out = tuple(tasks[task][0] for task in pending)
        for provider_id in timed_out:
            failed.append(provider_id)
            notes.append(
                f"{provider_id} did not answer within {timeout_s * 1000:.0f} ms "
                "and was dropped, not waited for"
            )

        return FanOut(
            itineraries=collected,
            used=tuple(used),
            failed=tuple(sorted(set(failed))),
            notes=tuple(notes),
            timed_out=timed_out,
            # Placeholder: the caller replaces this with the meter's count, which is
            # the only honest measure of what left the machine.
            upstream_calls=led,
            coalesced=len(providers) - led,
        )

    # -- recording --

    def _record(
        self,
        *,
        response: SearchResponse,
        wall: dt.datetime,
        elapsed_ms: float,
        intent_ms: float,
        search_ms: float,
        outcome: FanOut,
    ) -> None:
        """Append to the log. A broken log is a note, never a failed search.

        This is the one place a failure is downgraded on purpose: the user asked
        for a train, not for our bookkeeping. It is not silenced either, because
        the note travels back on the response where someone will see it.
        """
        if self.log is None:
            return
        record = RequestRecord(
            request_id=response.search_id,
            at=wall,
            route="search",
            elapsed_ms=elapsed_ms,
            stage_ms={"intent": intent_ms, "search": search_ms},
            result_count=len(response.itineraries),
            degraded=response.meta.degraded,
            providers_used=outcome.used,
            providers_failed=outcome.failed,
            # Reported per request, so /metrics can show what the burst saved
            # instead of claiming a perfect cache.
            upstream_calls=outcome.upstream_calls,
            cache_hits=outcome.coalesced,
        )
        try:
            self.log.append(record)
        except OSError as exc:
            notes = list(response.meta.notes or [])
            notes.append(f"request log write failed: {exc}")
            response.meta.notes = notes


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        kept.append(value)
    return kept


def _cache_state(itineraries: Sequence[Itinerary]) -> Cache:
    """Count the stale inputs behind this answer.

    `hit` stays False because whether an upstream call happened is the provider's
    fact; this layer refusing to claim it is the honest default.
    """
    stale = sum(
        1
        for itinerary in itineraries
        for leg in itinerary.legs
        if leg.availability is not None and leg.availability.stale
    )
    return Cache(hit=False, stale_entries=stale)


async def _guard(provider: object, query: Query) -> list[Itinerary]:
    """Call one provider, converting anything it leaks into the closed error set.

    A provider that lets an httpx or json exception escape has broken its own
    contract. The engine still must not crash, so it wraps and reports rather
    than trusting every adapter to be correct.
    """
    try:
        found = await provider.search(query)  # type: ignore[attr-defined]
    except ProviderError:
        raise
    except Exception as exc:
        # The raw text goes to `cause_detail`, which is logged and never
        # serialised. An adapter that leaked a 500-page body must not hand it to
        # a user, so the message we keep is ours and says only what happened.
        raise ProviderError(
            f"adapter raised an unexpected {type(exc).__name__}",
            provider_id=provider.id,  # type: ignore[attr-defined]
            cause_detail=str(exc),
        ) from exc
    return list(found or [])


def _explain(provider_id: str, error: BaseException) -> str:
    """One sentence for the user, built only from text we wrote ourselves.

    `ProviderError.message` is authored by us or by our adapters. `cause_detail`
    can hold an upstream response body, so it is deliberately not read here.
    """
    if isinstance(error, ProviderError):
        return f"{provider_id} failed ({error.code.value}): {error.message}"
    return f"{provider_id} failed: {type(error).__name__}"


def _is_degraded(chosen: Sequence[Itinerary], outcome: FanOut) -> bool:
    """True when any input was stale, partial, or missing.

    Checked against the cards actually shown, not against every candidate: a
    perfect top result on a broken provider day is still a perfect top result,
    and flagging it would train users to ignore the flag.
    """
    if outcome.failed:
        return True
    if not chosen:
        return True
    for itinerary in chosen:
        if itinerary.total_price is None:
            return True
        if any(leg.availability is None for leg in itinerary.legs):
            return True
        if any(leg.availability is not None and leg.availability.stale for leg in itinerary.legs):
            return True
    return False


def _with_modes(query: Query, modes: tuple[Mode, ...]) -> Query:
    """A copy of the query with different modes, keeping dataclass immutability."""
    return replace(query, modes=modes)


def _mode(mode: WireMode | Mode | str) -> Mode:
    """One enum for both layers: the internal `Mode` is the wire `Mode`."""
    return Mode(mode)


def _search_id() -> str:
    from railmind.core.ids import search_id

    return search_id()
