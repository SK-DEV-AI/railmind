# Architecture, in one page

For a reader who has five minutes and wants to know where everything lives and why.

## The shape

```
  free text ───────────────────────────────────────────────┐
                                                           │
  ┌─────────────┐   ┌──────────────┐   ┌───────────────┐   │   ┌────────────┐
  │   /intent   │──▶│   /search    │──▶│  /itinerary   │───┴──▶│  /handoff  │
  │   rules     │   │   engine     │   │  (store)      │       │  human path│
  └─────────────┘   └──────┬───────┘   └───────────────┘       └────────────┘
                           │
                    fan-out under a deadline
                           │
        ┌──────────────────┴───────────────────┐
        ▼                                      ▼
  local SQLite timetable                 vendor API (volatile
  (0 requests, ~1 ms)                    facts only, budgeted)
```

Three layers, and only one of them has a deadline:

1. **Intent** (`/intent`) may be slow and may eventually call a model. It runs once,
   before the rush. Today it is pure rules and says so (`parsed_by`).
2. **Search** (`/search`) is deterministic, has no model in it, and must answer inside
   the request's deadline. It is the only place the T90 claim applies.
3. **Handoff** (`/handoff`) produces the human's path to a real booking. It never
   books and never pays, by design and by test.

## Module map

| Path | Responsibility | Worth reading because |
|---|---|---|
| `railmind/wire/models.py` | Generated pydantic models from `openapi.yaml` | never hand-edited; the contract is the source |
| `railmind/core/types.py` | `Query`, `Stop`, money handling | the provider-neutral vocabulary every adapter speaks |
| `railmind/core/provider.py` | `TransportProvider` protocol, five verbs | adding a provider is implementing five methods, nothing else |
| `railmind/core/capabilities.py` + `providers/registry.py` | capability registry | nothing anywhere branches on a provider's class |
| `railmind/intent/parser.py` | sentence to `BookingIntent` | the station-resolution rules, and why it asks instead of guessing |
| `railmind/engine/orchestrator.py` | fan-out, deadline, ranking hand-off, logging | the deadline cancels rather than extends |
| `railmind/engine/ranking.py` | the one scoring function | a missing fact never becomes a neutral score |
| `railmind/engine/singleflight.py` | coalescing identical concurrent searches | a burst costs one provider call |
| `railmind/engine/metrics.py` | request log and `/metrics` | every number comes from the log, never from memory |
| `railmind/providers/railradar.py` | the vendor HTTP client | one chokepoint for auth, errors, quota and call counting |
| `railmind/providers/rail_india.py` | the rail adapter | local-first: SQLite answers, vendor only for volatile facts |
| `railmind/risk/predict.py` | punctuality inference from a JSON artifact | standard library only; no ML dependency in the server |
| `railmind/guardrails/tatkal.py` | Tatkal rules as a pure function | clock injected, so every boundary is testable |
| `railmind/data/timetable.py` | SQLite storage, freshness, change hashes | one write path for both data sources |
| `railmind/data/freshness.py` | how old each kind of fact may get | the volatility ladder in one table |
| `railmind/api/app.py` | HTTP mapping and error shape | deliberately thin: it decides nothing |
| `railmind/wiring.py` | composition root | the only file that names concrete classes |

## Two invariants that shape everything

**A fact is never invented.** Missing fares, missing seats and an untrained model all
surface as `null` plus a sentence on the card, never as a zero, an average or a
plausible guess. `ranking.py` enforces it by dropping a dimension from the weighted
mean instead of scoring it neutrally, and `risk/predict.py` returns `None` for a train
it has never seen rather than a neutral probability.

**The absence of booking is structural.** There is no route, no method and no flag
that submits an order or takes a payment. A test enumerates every route and fails if a
path ever contains `book`, `pay`, `checkout` or `order`. That is the legal argument,
expressed as something a machine checks on every commit.

## Request lifecycle, in order

1. `POST /intent` → `intent/parser.py` → a `BookingIntent` with `ambiguities` for
   anything it could not read. No model, no network.
2. `POST /search` → `engine/orchestrator.py`:
   - `Query.from_intent`, refusing rather than guessing;
   - `meter()` opens a per-request counter for outbound calls;
   - every capable provider is asked at once under one deadline, through
     `singleflight` so identical concurrent questions share one execution;
   - `engine/ranking.py` scores the union, with `guardrails/tatkal.py` deciding
     whether the advice is even actionable right now;
   - the response is recorded in `data/requests.jsonl`, and `/metrics` reads only
     that file.
3. `POST /handoff` → the adapter builds the real booking URL and the ordered steps a
   machine cannot take, after the quota guardrail has approved the attempt.
4. `GET /live/{leg_id}` → the vendor's live status, cached for 60 s, served stale
   rather than failing.

## Deliberate omissions

No cache server, no queue, no ORM, no front end, no container. Every one of them would
be something to explain rather than something that helps, and the whole system runs
from a SQLite file, a JSON model artifact, and one vendor key.
