# CAPABILITY-LEDGER.md — what we can do, what we cannot, and what it costs

Honest matrix as of **2026-10-24**, fares section revised **2026-10-27**. "Verified"
means we ran it, not that a blog said so. Costs are the real published or quoted
numbers, not guesses.

---

## 0. Plain answer: what a person can do with RailMind today (2026-10-27)

Say this without hedging, because every line below was run.

**You can:**

1. Type a journey in plain words ("mumbai to pune tomorrow evening, 3AC, need to
   reach by 9") and get back real trains on that corridor, from the full
   all-India roster: 10,861 trains, 213,105 stops, 13,010 stations.
2. See, for each train, the real departure and arrival times, how many days it
   runs, every stop with distance, and how long it actually takes.
3. See a **price in rupees for the class you named**, computed locally from the
   railway's own published fare table, with the base fare, reservation charge and
   any minimum-distance rule broken out, plus the circular it came from.
4. See a **delay-risk score** from a model trained on real running data
   (ROC-AUC 0.778), not a vibe.
5. Get one ranked shortlist with the reasoning shown (duration, price, delay,
   comfort, changes), answered in tens of milliseconds, which is what a Tatkal
   rush actually needs.
6. Get a **one-tap path to IRCTC** with a step-by-step handoff that says which
   steps are yours (login, captcha, Aadhaar OTP, payment).
7. Run the whole thing offline: no vendor call is needed to answer a search or a
   price.

**You cannot (and the app says so instead of pretending):**

1. **Book or pay.** By design and by IRCTC's terms: the AI never books and never
   pays. The last step is always yours.
2. **See seat availability or PNR.** No free, legal machine source exists; it is a
   paid vendor gate (`403 PLAN_UPGRADE_REQUIRED`) and IRCTC's own endpoint is
   captcha-walled. The card tells you to check on IRCTC.
3. **Get a final all-inclusive total.** GST and the Tatkal premium are not in the
   quote, because the circular prints no rate for either; the quote says exactly
   that. What you get is the railway's basic fare + reservation + superfast, which
   is a lower bound.
4. **Know about today's cancellations, diversions, specials or platform
   changes.** Not modelled; needs a source we do not have.
5. **Prefill the IRCTC page from a link.** Measured: IRCTC reads no journey
   parameters from a URL. The handoff therefore tells you what to type.
6. **Book other modes.** Metro, bus and cab are shaped into the architecture
   (provider abstraction matched to ONDC/Beckn), not implemented. Adding one is a
   new provider module, not a refactor.
7. **Price premium services with their own tables** (Vande Bharat etc.) or a
   class the circular does not print (`EC`). Those are refused with a reason, not
   approximated from the Mail/Express table.

In one line for the report: **we decide, from real data, in a second, and hand
over an honest path to the booking page, and we never claim more than we did.**

---

## 1. We CAN do, right now, for zero rupees

| Capability | Status | How |
|---|---|---|
| Full station directory, 13,010 codes and names | **verified working** | 1 request, stored in SQLite |
| Train schedules: every stop, lat/lon, arrival, departure, day number, cumulative distance | **verified working** | 1 request per train, 285 stops for Punjab Mail |
| Which trains run a corridor | **verified working** | local SQL join, zero requests when seeded |
| Local fare per class, cited and itemised | **working** (2026-10-27) | `fare/` from Commercial Circular 24 of 2025, no network call; see `docs/FARES.md` |
| Rank and present trains by real duration | **working** | real data, no invented scores |
| Open IRCTC's booking page for the chosen journey | **verified working** | plain link, opens in 200 with no bot block |
| Tell the human exactly which steps need them | **working** | 5 steps tagged human/captcha/aadhaar/payment |
| Run the whole thing offline | **working** | fixtures + SQLite, rebuild in under a second |
| Live train position and delay | **not yet tested**, free tier likely allows it | 1 request per train, 60 s cache |
| Delay prediction model | **possible, not started** | Kaggle MIT dataset, scikit-learn, no cost |
| Map with real coordinates | **possible** | MapLibre + OSM tiles, no key, coordinates already held |
| Reach a new corridor | **working** | 1 request for the train list, 1 per train to seed |
| Installable PWA | **possible** | Serwist, no cost |

## 2. We CANNOT do, and no amount of cleverness fixes it

| Blocked | Why, in one line | Evidence |
|---|---|---|
| **Seat availability** | Paid plan only. The free tier returns HTTP 403 `PLAN_UPGRADE_REQUIRED`. | live response, committed as a fixture |
| **PNR status / live PNR prediction** | Same paid plan gate | same response text |
| **Prefilling the IRCTC journey from a link** | IRCTC does not read journey parameters from the URL at all. Tested on both the old and the new beta site, five spellings. Not a pricing problem; a product decision by IRCTC. | `docs/IRCTC-DEEPLINK.md` + screenshots |
| **Deep-linking to the results page** | `/eticket/booking/train-list` carries no query string; state lives in the app | same document, experiment 3 |
| **Booking on the user's behalf** | IRCTC terms forbid automation software outright; Aadhaar OTP goes to the passenger's phone; agents are locked out of opening-day Tatkal for 30 minutes | IRCTC T&C p.7–8, Railway Board Circular 08/2025 |
| **Captcha solving** | IT Act s.43/66, and the terms name "Tatkal software" explicitly | research file |

## 3. We CANNOT do, but only because of money

| Blocked | Unlocks with | Real cost | Worth it? |
|---|---|---|---|
| Real fares | **Resolved without money on 2026-10-27**: built from the published coaching circular instead of a vendor | — | Not needed any more; the vendor was never the source of truth for a price |
| Seat availability | same | same | For a viva: not needed. For a product: essential |
| Official IRCTC B2C API | PSP registration | ₹20 lakh integration + ₹10 lakh deposit + ₹10 lakh/year minimum AMC + turnover certificate | No. Not a student project |
| Agent credential (RSP) | Registration through a PSP | ₹1,300–6,000 for 1–2 years | No: still barred from Tatkal's first 30 minutes, and the rules read counter-only |
| Flights | — | Amadeus Self-Service shut down 17 Jul 2026; enterprise contracts only | No |
| Intercity buses | redBus / AbhiBus B2B | NDA + GSTIN + commercial billing | No |

## 4. The one thing that is expensive in *time*, not money

**A complete 9,324-train timetable.** railpull can produce it from NTES for free,
but the README says "a few hours for the full roster", and it crawls a public
government service through an unofficial client. That is a decision about politeness
and time, not budget.

Today: 5 seeded trains, 681 stops, 3 corridors, all real. Seeding another corridor is
1 request for the list plus 1 per train. A 10-train corridor costs about 5 minutes.

## 5. What this means for the pitch

The product is not "we book your ticket". It is:

> **We decide, from real data, in about a second, and then we hand you a correct,
> honest path to the booking page — and we never pretend to have done more than we did.**

Every "cannot" above has a visible, honest fallback in the product: the card says
"check availability on IRCTC", the fare is absent rather than invented, and step one
of the handoff tells the user to type the journey because the link cannot carry it.

That is a weaker demo line and a much stronger engineering story. A judge who asks
"why doesn't it just book?" gets an answer with citations. A judge who asks "does the
deep link prefill?" gets "no, I measured it, here is the screenshot, here is what we
do instead."

## 6. The decision the user has to make

1. **Fares.** Ask the vendor for Dev-tier pricing, or test RapidAPI's free tier, or
   build a fare calculator from published rules. Until then, no price on the card.
2. **Scope.** Keep 5 real trains and go deep (engine, model, metrics, UI), or spend
   requests on breadth (more corridors) and stay shallow. Depth scores better.

## 6. Update 2026-10-25: independent research, and where it disagrees with us

A separate research pass enumerated every zero-budget route it could find. Two
findings are worth keeping, and one of its claims is wrong and is recorded as
wrong.

**New leads, untested by us, for the human to try in this order:**

| Route | Claim | Why it matters |
|---|---|---|
| IndianRailAPI free developer account, `indianrailapi.com/api-collection` | 100 hits per day, class-wise fare, GN-quota availability, PNR. Free account is deleted after 3 months. | 100 **per day** beats our 100 **per month**, and it claims both facts we are missing. Cheapest thing to test. |
| Blaazetech Indian Railways on RapidAPI | 500,000 requests/month free, but returns **station codes and PNR only**, no fares and no availability. | Kills the "free fares via RapidAPI" hope, while confirming it is a good PNR source. |

**A claim that contradicts our measurement, and loses:**

The same research says RailRadar's free tier includes fare and a 14-day seat
availability calendar. It does not. We measured this directly on 2026-10-24:
`GET /v1/trains/{n}/seats` returns HTTP 403 with `PLAN_UPGRADE_REQUIRED` and the
message "Live Seat Availability and PNR endpoints require an active Developer,
Growth, or Scale subscription". A vendor documentation page that lists an
endpoint does not mean the free plan serves it. Measurement beats documentation,
and the capability flags in code stay as measured: `availability: false`.

**Fare calculator: viable, with one honest limitation.** The rule set is public
and citable (telescopic distance slabs with rebate percentages, minimum fares,
per-class reservation and superfast charges, 5 percent GST on AC classes only,
Tatkal ranges by class and distance, flexi-fare caps). The exact per-km coaching
tariff table is not published as data, so a calculator built from public rules
can be close but not exact. That is acceptable **only if the card says
"estimated, confirm on IRCTC"** and never presents it as the fare. Sources worth
citing in the report: SATHEE (IIT Kanpur) fare-calculation material, PIB PRID
2208532 for the 26 Dec 2025 revision, and the coaching circulars on the zonal
railway sites.

## 7. Punctuality: moved from "cannot" to "measured" on 2026-10-25

The earlier version of this file said delay prediction was unbuilt. It is now
built, from real data, and the method and limits are in `docs/DELAY-MODEL.md`.

| | |
|---|---|
| Source | RSTGCN, 1,282,325 per-stop delay observations, September 2024, 3,892 trains |
| Coverage of our trains | all 5 |
| Model | logistic regression, 8 features, trained in 3.5 s |
| Validation | ROC-AUC **0.7778**, Brier 0.1242, date split (1-21 vs 22-30 Sept) |
| Serving cost | standard library only, microseconds, no ML dependency in the server |
| Honest limits | one month of data, so no seasonality; no weather, congestion or rake status; a train absent from the data gets **no prediction**, not a neutral one |

What this buys: the recommendation now responds to reliability. On the demo
corridor 22105 leads 12127 because it is meaningfully less likely to be late,
which is a fact from data rather than a preference in the weights.
