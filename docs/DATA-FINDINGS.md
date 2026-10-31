# DATA-FINDINGS.md — what the vendor actually gives us

Measured against the live RailRadar API on **2026-10-24** with a free-tier key.
Every claim here comes from a real response, and every response is committed
verbatim under `tests/fixtures/railradar/` with its vendor `traceId`.

Budget: **15 of 100 requests** spent. Ledger: `data/railradar-usage.json`.

---

## 1. What works on the free plan

| Endpoint | Verified | What it returns |
|---|---|---|
| `/v1/lookup/stations` | yes | `{code: name}` for **13,010** stations. Names only, no coordinates. |
| `/v1/trains/between/{from}/{to}` | yes | direct trains with departure, arrival, day numbers, distance, duration, run days. CSMT→PUNE returned **24** trains, CSMT→NDLS **2**, MMCT→NDLS **1**. |
| `/v1/trains/{number}` | yes | full route. Punjab Mail returned **285 stops**, each with station code, name, **lat/lon**, arrival, departure, day, cumulative distance. |
| `/v1/trains/{n}/route` | not yet called | GeoJSON route, for the map. One request per train. |
| `/v1/trains/{n}/live` | not yet called | running position and delay. One request per train, 60 s TTL. |

## 2. What is walled off, and the design consequence

| Wall | Evidence | Consequence |
|---|---|---|
| **Seat availability is paid-plan only** | `/v1/trains/12137/seats` → HTTP 403, `PLAN_UPGRADE_REQUIRED`: "Live Seat Availability and PNR endpoints require an active Developer, Growth, or Scale subscription." | `capabilities.availability = False` on the free plan. `availability()` raises `NotSupported`. The card says "check on IRCTC" and never shows a seat count. |
| **PNR status is paid-plan only** | same error text names PNR | No PNR feature this term. |
| **Fare needs four parameters** | HTTP 400 chain: `journeyDate is required (YYYY-MM-DD), aliases: date, startDate` → `source and destination are required (aliases: from, to)` → `classCode is required (alias: class)` | The client sends all four. Four 400s cost four requests; that is how the parameter set was learned. |
| **Fare endpoint is flaky on the vendor side** | `/v1/trains/12137/fare` with all four params → HTTP 400, `SYSTEM:UNKNOWN_ERROR`: "The socket connection was closed unexpectedly" (their upstream, twice) | A fare is **optional**. Absent when unconfirmed. `_money_from_fare` raises rather than guessing, because the success shape was never observed. |

### Why this is not a blocker

The contract already allows absent values: `total_price` is optional, `risk_band`
has an `unknown` value, `Availability` has `status: unknown`, and every absence
carries a `warnings` string the UI must render. A card that says "fare on IRCTC"
is honest. A card showing a guessed ₹2,605 is not.

### Open options for real fares, if the project needs them

1. **Ask the vendor** about the Dev tier price (pricing page is JS-rendered, no
   public numbers). Cheapest way to unblock both fares and availability.
2. **A second data vendor.** RapidAPI's "Indian Railways" free tier claims 500,000
   requests/month; whether its fare endpoint works was never tested. One key, one
   afternoon.
3. **A fare table calculator** from Indian Railways' published fare rules. Real,
   verifiable against a few known fares, and a genuine piece of work rather than a
   workaround. Highest effort, highest marks.

## 3. Cost of a search, in requests

| Situation | Requests |
|---|---|
| Seeded corridor (CSMT→PUNE, CSMT→NDLS, MMCT→NDLS) | **0** |
| Unseeded corridor, first visit | 1 (`trains/between`), then cached |
| Same unseeded corridor again within TTL | 0 |
| Pricing one finalist | 1 per train per class, cached 6 h |
| Live position for a leg | 1 per 60 s, cached |
| Building the handoff link | **0** |

The timetable is on disk, so a search is a SQL join. That is the whole reason a
100-request month is survivable: only volatile facts touch the wire.

## 4. What one request buys, so seeding is a decision

Fetching a train's schedule gives its **entire corridor** with coordinates, which
is why seeding is measured in trains, not routes:

- 1 schedule request ≈ 1 train, every stop it makes, geodata included
- 13,010 station names cost **1** request for the whole country
- A corridor list costs 1 request and tells you *which* trains are worth seeding

`scripts/seed_timetable.py` rebuilds the database from the committed fixtures in
under a second, with the network unplugged.

## 5. Status of the IRCTC link

`https://www.irctc.co.in/nget/train-search?fromStation=CSMT&toStation=PUNE&date=20261026`

**Verified 2026-10-24: IRCTC ignores these parameters.** The page loads (HTTP 200, no
bot block), the form renders, and it is empty with today's date. Tested on the old
site and on the new beta site (`/eticket/train-search`) with five parameter
spellings. The results page `/eticket/booking/train-list` carries no query string, so
it cannot be linked to either.

Full experiment log and screenshots: `docs/IRCTC-DEEPLINK.md`, re-runnable with
`uv run --with playwright python scripts/irctc_check.py`.

So the link's job is "open the booking page", not "open it filled in". The handoff
copy says exactly that, and a test asserts the wording so the old promise cannot
sneak back.

## 6. Reproducing any of this

```bash
# one request, budget-guarded, saved as a fixture
python3 scripts/railradar_probe.py /v1/trains/12951 --out tests/fixtures/railradar/train_12951.json

# rebuild the offline database from fixtures (no network)
python3 scripts/seed_timetable.py --sample CSMT PUNE

# ask the real agent
python3 scripts/rail_query.py CSMT PUNE 2026-11-15 --handoff
```
