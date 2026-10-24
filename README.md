# RailMind

A conversational travel agent for Indian Railways. You describe the trip in
plain language; it returns a priced, delay-risk-assessed, one-tap booking card in
under two seconds, including at 10:00 when Tatkal opens.

**It does not book.** The final transaction happens on the operator's own site,
by a human. That is not a limitation we worked around, it is the design: IRCTC's
terms prohibit automation software, authorised agents are locked out of
opening-day Tatkal for the first 30 minutes, and Tatkal requires an Aadhaar OTP
to the passenger's phone. A machine cannot legally or practically complete that
last step. So we made the other 90 seconds disappear instead.

The full reasoning, the evidence with sources, and the legal analysis live in
`docs/` and in the project's research folder. Start with `docs/CONTRACT.md`.

---

## Status

Contract and core complete and tested. No network adapter yet.

| Step | State |
|---|---|
| 0. API contract | **done** — `openapi.yaml`, validated, both clients generate |
| 1. Core domain + provider layer | **done** — ids, errors, capabilities, registry, cache, 143 tests |
| 2. One real provider end to end | next |
| 3. Engine + delay model | |
| 4. Web client | |
| 5. PWA | |
| 6. Second provider (proof of agnosticism) | |

## Layout

```
openapi.yaml                 the product; everything else implements it
docs/CONTRACT.md             why the contract looks like this

railmind/core/
  ids.py                     derivable ids: IN:CSMT, provider:service:date:seq
  errors.py                  the closed error set, and no upstream text on the wire
  capabilities.py            Feature enum; providers answer by data, not by type
  provider.py                TransportProvider protocol: five verbs
  types.py                   Query, money arithmetic, re-exported wire models
railmind/providers/
  cache.py                   route-keyed TTL, stale-serve, single-flight
  registry.py                capability lookup, GET /providers payload
  fake.py                    fake, timetable-only, and deliberately broken providers
railmind/wire/models.py      GENERATED from openapi.yaml, never hand-edited

packages/contract/src/       GENERATED TypeScript types
scripts/check.sh             one command, all gates
scripts/check_contract.py    duplicate keys, broken refs, missing operationIds
scripts/generate-clients.sh  validate + regenerate both typed clients
scripts/railradar_probe.py   IPv6-first probe with a hard monthly budget ledger
tests/                       143 tests, no network, no sleeps
```

## Verify everything

```bash
./scripts/check.sh      # contract, generated-code sync, lint, format, tests
```

Individual gates:

```bash
python3 scripts/check_contract.py     # contract structure
./scripts/generate-clients.sh         # spec conformance + both clients
.venv/bin/ruff check .                # lint
.venv/bin/pytest                      # tests
```

Setup, once:

```bash
python3 -m venv .venv
.venv/bin/pip install pydantic pytest pytest-asyncio ruff \
    openapi-spec-validator datamodel-code-generator
```

## What the core guarantees

1. **Ids are derivable.** `IN:CSMT`, `rail_india:12951:2026-10-25:1`, itinerary =
   hash of its leg ids. Nothing is a random token that dies with the process.
2. **One closed error set.** A provider may raise only a `ProviderError`
   subclass. An accidental exception becomes `internal` with no detail attached.
   `cause_detail` is for logs and never serialises.
3. **Capabilities, not classes.** `registry.find(Feature.LIVE, mode=Mode.metro)`
   is the whole interface. A test scans the code for provider-type branches and
   fails the build if one appears.
4. **The cache key is the route, not the user.** `(from, to, date, class)` plus
   single-flight, so a burst of ten requests is one upstream call worth 1% of a
   monthly budget.
5. **Stale beats broken, up to a limit.** Availability serves stale for an hour;
   a live position stops at five minutes, because an old position is a lie.

## The three layers

```
L1  deliberative   LLM, 0.3-2s   off the hot path; runs the night before
L2  reactive       <800ms        deterministic fan-out, scoring, ranking
L3  human          40-60s        CAPTCHA, Aadhaar OTP, UPI
```

No LLM on the hot path. A 1.2-second model call at 10:00:00 destroys the entire
value proposition.

## Invariants

1. No endpoint completes a booking or a payment.
2. Optional means absent, never `null`.
3. Every monetary value is integer minor units.
4. Every leg id is derivable, not random.
5. Every provider failure maps to one of the ten normalised error codes.
6. Every metric names the log it came from.
