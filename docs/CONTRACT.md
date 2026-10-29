# CONTRACT.md — Why the API looks like this

`openapi.yaml` is the product. Everything else is an implementation of it.
This file records the reasoning so nobody "fixes" a deliberate decision later.

Validated with `openapi-spec-validator` (OpenAPI 3.1.0). Typed clients generate
for both stacks. If a change breaks either generator, the change is wrong.

---

## 1. The endpoints

| Method | Path | Layer | Why it exists |
|---|---|---|---|
| POST | `/intent` | L1, LLM allowed | Turns messy prose into a frozen `BookingIntent`. Off the hot path. |
| POST | `/search` | L2, deterministic | The race. Ranked, priced, risk-assessed itineraries under a hard deadline. |
| GET | `/itinerary/{id}` | L2 | One option in full, for the detail view and for refresh. |
| POST | `/handoff` | L3 | Builds the human's one-tap path. Never books. |
| GET | `/live/{leg_id}` | data | Position and delay for a running leg. |
| GET | `/metrics` | ops | T90, T_tap, provider calls, cache hits. Measured, from the log. |
| GET | `/providers` | ops | The capability registry, exposed. |
| GET | `/health` | ops | Deploy target needs it. |

BUILD-ORDER names six endpoints. Two ops endpoints were added, deliberately:

- **`/health`** — Fly.io and any load balancer need a liveness probe. Without it
  the demo deploy is a guess.
- **`/providers`** — BUILD-ORDER Decision 3.3 says the UI and the engine render
  from capabilities, not from `isinstance`. A capability registry that no client
  can read is unverifiable. Exposing it is what makes the agnosticism claim
  demonstrable in the viva, and it makes the Step 6 diff measurably small.

Both are read-only and return no user data.

### What is absent on purpose

**There is no endpoint that submits a booking, and none that takes a payment.**
That absence is the legal design, not an oversight. Do not add one. A POST that
completes a transaction against IRCTC would violate their terms regardless of
how it is implemented, and it would make the whole project indefensible in one
sentence. If a future version integrates an authorised partner, that is a
separate, licensed provider with `capabilities.booking: "api"` — and it still
would not be this rail path.

Also absent: auth (v0.1 is single-user local), rate-limit tiers, websockets
(polling is fine at 60 s TTL), and the LLM tool schemas (internal, in
`railmind/agent/tools.py`).

---

## 2. Conventions, and the reason for each

| Convention | Reason |
|---|---|
| **Absent, not null** | "Missing" and "known to be zero" must not look the same. `seats_available` absent means unknown; `0` means sold out. In Python this is `None`/`Field(default=None)`; on the wire the key is simply not sent. |
| **Money as `amount_minor: int`** | JSON has no decimal type. A float `2605.50` is untrustworthy by the second arithmetic step, and a string forces every client to parse. Integer paise is exact, sortable, and sums correctly. |
| **RFC 3339 with offset, plus `service_date`** | A train departing 23:50 IST is a different booking date than the same instant in UTC. Every leg carries both the instant and the local booking date, so nobody has to guess which one to use. |
| **Stable derivable ids** | `IN:CSMT`, `rail_india:12951:2026-10-25:1`, itinerary = hash of leg ids. Debuggable by eye, cacheable without a lookup, and stable across providers restarting. |
| **RFC 9457 `application/problem+json`** | One error shape everywhere, with a stable `code`. Clients switch on `code`, never on prose. A provider's raw error string never reaches a client — that is how provider internals leak into the UI. |
| **`X-Request-Id` on every response** | One id to grep in the audit log. Free, and it makes the demo debuggable. |
| **`meta.degraded` + `meta.notes`** | A deadline-bound system must be able to say "this is 14 minutes old and here is why" and still succeed. Degraded is a first-class outcome, not an error. |
| **`deadline_ms` in the request** | The client declares its patience. The server never silently exceeds it. A missed deadline is a `200` with `degraded: true`, the providers that missed it named in `notes`, and whatever cached itineraries exist — never a `500`, and never a `504`: the deadline is how the answer stays fast, not a way to fail. |

---

## 3. Deliberate deviations from the sketches in BUILD-ORDER.md

The sketches there are illustrative Python; the contract is normative. Four
names changed on purpose. Each is a one-line mapping, not a redesign.

| Sketch | Contract | Reason |
|---|---|---|
| `from_stop` / `to_stop` | `origin` / `destination` | `from` is a Python keyword, so a literal `from` field needs an alias hack in every model on both stacks. `origin`/`destination` reads correctly in TypeScript, Python, and JSON, and still maps to Beckn `fulfillment.start` / `fulfillment.end`. |
| `amount: Decimal` | `amount_minor: int` | See above. Pydantic will happily serialise `Decimal` as a JSON string or float depending on config; that ambiguity is exactly the bug class we refuse. |
| `reliability: float` | `risk: {delay_probability, expected_delay_min, p90_delay_min, model_version, confidence, top_features[]}` | One float cannot render "31% likely to arrive 15+ minutes late, p90 is 47 minutes, and here is why". The card needs the reasons. `score_breakdown.delay_risk` carries the single normalised number the ranker uses. |
| flat `operator` + `service_id` | nested `service: ServiceRef` | A train has number, name, operator, and rolling stock. Flat fields force provider-specific extras (`vehicle_class`) into the `Leg` root, which is how a generic model starts leaking rail concepts. |
| `availability` inside `Leg` | also inside `Leg`, plus `booking_channel` and `live_available` | Kept, and extended: `booking_channel` is what lets the UI say "this one you complete yourself" truthfully, and `live_available` stops the client from rendering a map for a leg that has no telemetry. |

Additions not in the sketch, all carrying their weight:

- `BookingIntent.quota` — `tatkal` changes the reservation window, the fare, the
  passenger cap, the OTP requirement, and the agent blackout. A model without
  quota cannot express the project's central scenario.
- `BookingIntent.ambiguities[]` — "Mumbai" is four stations. Resolving it by
  guessing is a hallucination; asking is correctness. The API models asking.
- `BookingIntent.parsed_by` — `llm | rules | user`, surfaced in the UI. Users
  deserve to know whether a model guessed.
- `Itinerary.score_breakdown` — the ranker's reasons, exposed so the UI explains
  instead of asserting.
- `Itinerary.confidence` vs `score` — trust in the data is not the same as
  quality of the option. Conflating them is how a confidently wrong
  recommendation ships.
- `Passenger.id_last4` with no full-ID field — minimisation by schema. The API
  physically cannot receive an Aadhaar number.
- `HandoffStep.requires` — `human | otp | captcha | payment | aadhaar`. The
  legal argument, encoded as data, rendered as the checklist the user sees.

---

## 4. Beckn / ONDC mapping (keep this true, or the future adapter is a rewrite)

| Ours | Beckn | Note |
|---|---|---|
| `BookingIntent` | `Intent` | `origin`/`destination` → `fulfillment.start`/`.end`; `service_date` → `fulfillment.time` |
| provider list | `Catalog.providers[]` | `GET /providers` already returns provider + capabilities |
| `Leg` | `Item` + `Fulfillment` | `booking_channel: api` maps to a Beckn-confirmable seller |
| `Itinerary` | `Fulfillment` group + `Quote` | `price_breakup[]` → `Quote.breakup[]` |
| `total_price` | `Quote.price` | amount_minor → `value` string at the boundary only |
| `service_preferences` | `Item.provider` / catalog filter | brand keys, never a train number |
| `Handoff` | `Order` (init → confirm) | ours stops at init, by design |
| live status | `Fulfillment.state` | |
| cancellation | order update | out of scope v0.1 |

Two rules that keep this mapping cheap:

1. Never put an IRCTC concept in a field name. `quota` is generic enough; `train_number` would not be.
2. Anything modal lives in `Leg` or `Stop`, never in `Itinerary`, because Beckn
   groups fulfillments above items and we must be able to split an itinerary into
   multiple fulfillments without renaming fields.

---

## 5. Invariants the contract must still hold at v1.0

1. No endpoint completes a booking or a payment.
2. Every optional field is absent when unknown, never `null`.
3. Every monetary value is integer minor units.
4. Every leg id is derivable from `(provider, service, service_date, sequence)`.
5. Every provider failure maps to one of the ten `Problem.code` values.
6. `POST /search` respects `deadline_ms` and reports degradation honestly.
7. Every response supports `X-Request-Id`.
8. Every metric in `/metrics` names its source in `counted_from`.
9. Every status code in `openapi.yaml` is one a code path actually returns. A
   documented `429` with no limiter behind it is a promise the server breaks, so
   the audit that found six of them deleted the claims instead of building the
   feature. The one place an upstream failure is not absorbed into a degraded
   `200` is `GET /live/{leg_id}` with no cached position, which is why that route
   and only that route lists `429` and `502`.

---

## 6. How to prove the contract is real

```bash
./scripts/check.sh          # every gate, in order
```

Individually:

```bash
python3 scripts/check_contract.py   # structure, duplicate keys, broken refs
./scripts/generate-clients.sh       # spec conformance + regenerate both clients
.venv/bin/pytest                    # 488 tests, no network, no sleeps
```

Step 0's acceptance test from BUILD-ORDER is: *both client devs can generate a
typed client from the spec.* Both do today:

- `packages/contract/src/api-types.ts` — `openapi-typescript` 7.13, 1046 lines
- `railmind/wire/models.py` — `datamodel-codegen`, Pydantic v2, 837 lines,
  `extra='forbid'`, `AwareDatetime`, `constr(max_length=...)` all correct

The generated Python models are imported by the application, not copied. That is
why there is exactly one definition of `Leg` in this codebase: the contract's.
`scripts/check.sh` regenerates them and fails if the result differs from what is
committed, so a stale or hand-edited generated file cannot survive a gate.

If a future edit makes either generator fail, that edit is a contract break and
gets reverted, not worked around.

## 7. What the core implements from this contract

| Contract idea | Where it lives |
|---|---|
| derivable ids | `railmind/core/ids.py` |
| the ten `Problem.code` values | `railmind/core/errors.py` |
| `Capabilities` / `ProviderInfo` | `railmind/core/capabilities.py`, exposed by `GET /providers` |
| five provider verbs | `railmind/core/provider.py` |
| `Query`, money arithmetic | `railmind/core/types.py` |
| route-keyed caching, stale-serve | `railmind/providers/cache.py` |
| provider lookup by capability | `railmind/providers/registry.py` |

Two guarantees are enforced by tests rather than by intention: no provider-type
branch may appear in source (a tokenizer strips comments and docstrings, then the
code is scanned), and `railmind/core/` may not import a concrete adapter.
