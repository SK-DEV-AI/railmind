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

Contract complete and validated. No implementation yet.

| Step | State |
|---|---|
| 0. API contract | **done** — `openapi.yaml`, validated, both clients generate |
| 1. Core domain + provider layer | next |
| 2. One real provider end to end | |
| 3. Engine + delay model | |
| 4. Web client | |
| 5. PWA | |
| 6. Second provider (proof of agnosticism) | |

## Layout

```
openapi.yaml                 the product; everything else implements it
docs/CONTRACT.md             why the contract looks like this
scripts/check_contract.py    duplicate keys, broken refs, missing operationIds
scripts/generate-clients.sh  validate + regenerate both typed clients
packages/contract/src/       generated TypeScript types
generated/python/            generated Pydantic v2 models
```

## Verify the contract

```bash
python3 scripts/check_contract.py     # structure
./scripts/generate-clients.sh         # structure + spec conformance + both clients
```

`generate-clients.sh` needs a `.venv` with `openapi-spec-validator` and
`datamodel-code-generator`, plus Node for `openapi-typescript`.

```bash
python3 -m venv .venv
.venv/bin/pip install openapi-spec-validator datamodel-code-generator
```

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
