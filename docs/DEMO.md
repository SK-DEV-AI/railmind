# Demo runbook

Everything below is measured on this machine, not projected. Commands are in the
order you would run them in front of an examiner, and each one has an expected output
you can eyeball. If an output does not match, stop and read the note under it rather
than improvising.

## 0. Start the server (30 seconds)

```bash
cd ~/session-root/repos/railmind
.venv/bin/uvicorn railmind.api.main:app --port 8000
```

Needs `.env` in the repo root (gitignored) with `RAILRADAR_API_KEY`. There is no
database server and no cloud dependency: SQLite and the delay model are local files.

```bash
curl -s localhost:8000/health | python3 -m json.tool
```

Expect `status: ok` and two dependencies, `timetable_db` and `timetable_schedule`.
`timetable_schedule` turns `degraded` when schedules are older than 30 days. That is
a feature: point at it before the examiner does.

## 1. One sentence in, one card out

```bash
curl -s -X POST localhost:8000/intent -H 'content-type: application/json' \
  -d '{"text":"book me mumbai to pune on 27 oct tatkal 3ac under 2000, must not arrive late"}'
```

Expect: `origin CSMT`, `destination PUNE`, `26 Oct window`, `["3A"]`, `tatkal`,
budget 2000, `parsed_by: rules`, ~15-20 ms.

Two things to say while it prints:

- The parser is rules, not a model. `parsed_by` says so, and the UI shows that field.
- `mumbai` matches six stations in the vendor's directory, including a loco shed. It
  resolved to CSMT because that is where the seeded trains actually stop, and it will
  **ask** (with `IN:CODE` options) when two candidates are genuinely tied.

## 2. Search, with real punctuality

Take the `intent` object from step 1 and post it:

```bash
curl -s -X POST localhost:8000/search -H 'content-type: application/json' \
  -d '{"intent": <the intent object>, "limit": 3}' | python3 -m json.tool
```

Expect three real trains, each with `risk.delay_probability`, `expected_delay_min`,
`p90_delay_min`, `model_version: logit-2024.09.1`, and `risk_band`. On this corridor
the ranking puts **22105 first** because it is the least likely to be late, which is
the point of having a model at all.

Say this, because it is the honest version:

- The probability comes from 1.28 million real per-stop delay observations
  (September 2024). Validation ROC-AUC **0.7778** on a date split.
- It is trained on one month, so it has no seasonality. Confidence is discounted for
  that and the docstring says why.
- A train absent from that data gets **no** prediction and keeps `unknown`.

## 3. The Tatkal guardrail refuses something

```bash
curl -s -X POST localhost:8000/handoff -H 'content-type: application/json' \
  -d '{"itinerary_id":"<id from step 2>", "passengers":[5 passengers]}'
```

Expect `422`, title *"Refused by a booking rule"*, and a sentence naming the rule that
was broken (party cap, or the Tatkal window not being open, or both).

This is the slide that answers "why doesn't it just book for me": the rules are
encoded, not dodged.

## 4. The handoff is a human's path, not a bot's

```bash
curl -s -X POST localhost:8000/handoff -H 'content-type: application/json' \
  -d '{"itinerary_id":"<id>"}'
```

Expect a real IRCTC URL plus five ordered steps, each with `requires`: `human`,
`captcha`, `aadhaar`, `payment`. Each step says why a machine cannot do it. The
warning states plainly that IRCTC ignores journey parameters in a URL, verified in a
browser on 2026-10-24, so the user types the journey.

No endpoint in this service books or pays. A test enumerates every route and fails if
a path ever contains `book`, `pay`, `checkout` or `order`.

## 5. Claims with numbers

```bash
.venv/bin/python scripts/measure_t90.py --base-url http://127.0.0.1:8000 \
  --requests 200 --concurrency 10
```

Measured on this laptop, 2026-10-25:

| Claim | Number | What it does not prove |
|---|---|---|
| Time to a ranked card, 90th percentile | **12.3 ms** client-side, **4.5 ms** server-side | the corridor is answered from SQLite; a live vendor call is bounded by the network, not by this |
| Throughput, 10 in flight | **920 req/s** | one process, one laptop, no TLS, no reverse proxy |
| Outbound vendor calls for those 200 searches | **0** | because the timetable is local; that is the design, and it is why 100 requests/month is enough |
| Ranking correctness | not measured | there is no ground-truth "best train". The weights are a stated policy (price .30, duration .28, delay .22, comfort .12, changes .08), not a fitted model. Say that. |
| Punctuality model quality | ROC-AUC 0.7778, Brier 0.1242 | one month of training data; no weather, congestion or cancellations |

```bash
curl -s 'localhost:8000/metrics?window_seconds=3600' | python3 -m json.tool
```

Every number there is computed from `data/requests.jsonl`. `counted_from:
request_log` says so. If the log is empty, `t90_ms` is `null`, not zero.

## 6. If asked "is the data current?"

```bash
.venv/bin/python scripts/refresh_timetable.py            # dry run: what is due
```

It prints the timetable's age and lists schedules past the 30-day policy, oldest
first. Add `--apply` to refresh, at one vendor request per train, and it reports
`new / changed / same / failed` per train. `same` is the normal answer and is the
proof that a refresh is not just rewriting rows.

## 6b. Does the whole timetable answer correctly?

```bash
.venv/bin/python scripts/audit_corridors.py --sample 400 --seed 0
```

It walks a deterministic sample of real corridors offline, with a vendor client that
raises on any use, and prints built / empty / errors. A non-zero exit means something
escaped. Run it after every import: it is how the quota leak and the boardable-segment
bug were both caught.

## What is honestly missing

Say these before they are found:

1. **No fares and no seat availability.** Both are vendor walls: the fare endpoint is
   broken upstream, availability is a paid plan (`403 PLAN_UPGRADE_REQUIRED`,
   measured). Cards say so in words.
2. **Cancellations and diversions on the day are not modelled.** A train cancelled
   this morning still appears in a search for next week. This is the largest
   remaining accuracy gap.
3. **No front end.** Deliberate: backend only, by instruction.
4. **`t_tap_ms` is null.** The human's time cannot be measured server-side, and
   putting a server number there would be the one dishonest metric in the system.
5. **Breadth**: the database holds what has been crawled or seeded so far; see
   `data/timetable.db` counts via `/health` and the importer's summary.

## Rebuilding everything from scratch

```bash
cd ~/session-root/repos/railmind
python3 -m venv .venv && .venv/bin/pip install -e '.[dev,api]'
cp .env.example .env                     # then put the vendor key in it
.venv/bin/python scripts/seed_timetable.py --fetched-at 2026-10-24T10:05:33+00:00

# optional: the full timetable from NTES (hours, resumable)
git clone https://github.com/shwetankg07/railpull ~/session-root/repos/railpull
cd ~/session-root/repos/railpull && python3 -m venv .venv \
  && .venv/bin/pip install -r requirements.txt \
  && .venv/bin/python ntes/crawl.py && .venv/bin/python transform/export.py
cd ~/session-root/repos/railmind && .venv/bin/python scripts/import_railpull.py

# optional: the punctuality model (needs numpy + scikit-learn)
.venv/bin/python scripts/build_delay_model.py

./scripts/check.sh    # contract, spec, generated clients, lint, format, 349 tests
```
