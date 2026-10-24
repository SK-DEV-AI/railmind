# IRCTC-DEEPLINK.md — the prefill claim is dead, verified three ways

Tested 2026-10-24 with Playwright driving chromium, rendering the real site and
reading the real form state. Screenshots kept at `docs/shots/`.

**Conclusion: IRCTC does not accept journey prefill through a URL. The handoff is
"open the booking page", not "open it filled in".** The project's earlier research
(and every blog post repeating it) says otherwise, and it is wrong today.

---

## Experiment 1 — old site, `fromStation` / `toStation` / `date`

```
https://www.irctc.co.in/nget/train-search?fromStation=CSMT&toStation=PUNE&date=20261026
```

- HTTP 200, page renders, no bot challenge
- language overlay dismissed, then the real form read
- **From empty, To empty, date `24/10/2026`** — that is today, not the requested 26th

The page's own JavaScript bundle does contain the strings `fromStation`,
`toStation`, `URLSearchParams` and `activatedRoute`, so the names exist somewhere in
the app. They are not wired to this form on load. Directly downloading the bundle to
read the handler was blocked by the CDN, so the read remains inconclusive about
*why*; the rendered result is not inconclusive.

## Experiment 2 — new beta site, `.../eticket/train-search`

The site advertises "BETA Version" and the user found the path. Tested with five
parameter spellings:

| Query string | Date field | Source field |
|---|---|---|
| `?fromStation=CSMT&toStation=PUNE&date=20261026` | `24 Oct 2026` (today) | "Select Source" |
| `?source=CSMT&destination=PUNE&date=20261026` | today | "Select Source" |
| `?from=CSMT&to=PUNE&date=20261026` | today | "Select Source" |
| `?sourceStationCode=...&destinationStationCode=...&journeyDate=20261026` | today | "Select Source" |
| no query at all | today | "Select Source" |

Identical output in all five rows. The parse is not failing; nothing is being read.

## Experiment 3 — is the *results* URL shareable?

Drove the form by hand (CSMT → PUNE → search), then read the URL:

```
before: https://www.irctc.co.in/eticket/train-search
after : https://www.irctc.co.in/eticket/booking/train-list
```

No query string, no path segments, no hash. The journey lives in application state,
so it cannot be linked to. A "deep link to results" is therefore also impossible.

---

## What this changes

| Before | Now |
|---|---|
| "One tap opens IRCTC **pre-filled**" | "One tap opens IRCTC's booking page; you enter from, to and date (about ten seconds)" |
| `Handoff.warnings` promised journey prefill | `Handoff.warnings` states prefill was not confirmed and to enter the journey |
| T_tap estimate started at the payment screen | T_tap now starts at the booking page, so it is ~10 s longer |

The rest of the pitch is untouched and still true: the hard part was never typing
three fields, it was deciding which train, in which class, with what risk, before the
seats are gone. That is what the card does, and it does it from real data.

Measured honesty beats an unverified claim: a link that silently opens an empty form
after promising otherwise is discovered by the user at the worst possible moment.

## What could still be tried, cheapest first

1. **Ask IRCTC support** whether any supported deep-link format exists for partners
   (`etickets@irctc.co.in`, 14646). One email, definitive answer.
2. **Watch the network tab on a real search** for a JSON endpoint the frontend calls.
   If it exists and is public, a query URL may be constructible. This is the last
   remaining path, and it is a scraping-adjacent question: read-only, low rate, no
   booking. Decide deliberately, not by accident.
3. **Accept it.** The handoff still works, it just costs the human ten seconds.

## Reproducing

```bash
uv run --with playwright python scripts/irctc_check.py    # renders + reads fields
```

Uses the chromium already in `~/.cache/ms-playwright/` (build 1228) via
`executable_path`, because the installed Playwright build expects 1248. No download
needed, no profile touched, nothing logged in, no booking attempted.
