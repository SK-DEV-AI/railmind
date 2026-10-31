# RailMind Companion (Chrome MV3)

Unpacked-installable extension that fills IRCTC's train-search form from a RailMind
shortlist and shows RailMind's fare / Tatkal-risk / "why this train" beside IRCTC's
numbers. It never submits, never logs in, never captures credentials, never reads or
transmits seat availability, and never acts without a user click.

## Load unpacked (Chrome / Edge / Brave)

1. Start the RailMind backend:
   ```
   cd repos/railmind
   uv run uvicorn railmind.api.main:app --port 8000
   ```
2. Open `chrome://extensions` (or `edge://extensions`).
3. Toggle **Developer mode** (top right).
4. Click **Load unpacked** and select this `extension/` directory.
5. Pin "RailMind Companion" to the toolbar.
6. Click the icon, set Backend URL (default `http://127.0.0.1:8000`), click Save, check "Backend: ok".
7. Type a journey (e.g. `mumbai to pune tomorrow 3AC tatkal`), click Search, click a train.
   IRCTC opens in a new tab; click **Fill form from RailMind** in the floating panel.
8. Review every field, then you click **Search Trains** yourself.

## Files

- `manifest.json` — MV3, least-privilege permissions (`activeTab`, `scripting`, `storage`, `alarms`, `notifications` + `*.irctc.co.in/*`).
- `src/background.js` — service worker: message router, storage, optional Tatkal alarm.
- `src/content.js` — injected panel + fill logic; never calls `form.submit()` or clicks Search/Book/Pay.
- `src/popup.*` — backend URL, search, shortlist, handoff.
- `src/lib/selectors.js` — IRCTC DOM selectors (single source of truth, verified 2026-10-31).
- `src/lib/api.js` — backend client.
- `shots/` — selector evidence screenshots.

## Hard guarantees (enforced by code, not by policy)

- No code path calls `form.submit()`, clicks "Search Trains"/"Book"/"Pay", or reads availability endpoints.
- No credentials, captcha, OTP, or card data are read or stored.
- Fill only happens after an explicit click on "Fill form from RailMind".
- Availability is never scraped from IRCTC's results page.

## Selectors (verified 2026-10-31)

IRCTC old site (`/nget/train-search`) uses PrimeNG:
- origin/destination: `p-autocomplete[formcontrolname="origin|destination"]` with an inner `<input>`; type the station code, then click the suggestion whose text contains `(CODE)`.
- date: `p-calendar[formcontrolname="journeyDate"] input` accepts `dd/mm/yyyy` typed directly.
- class: `p-dropdown[formcontrolname="journeyClass"]`; open, click the item whose text contains the code in parens, e.g. `AC 3 Tier (3A)`.
- quota: same pattern, e.g. `TATKAL`.

Angular reactive forms ignore a plain `.value = x` assignment on the inner input for the autocomplete/dropdown (the FormControl never updates). Typing through the keyboard event path, or clicking a real suggestion/option, is what updates the model. The date input is the exception: assigning `.value` plus `input`/`change`/`blur` events works because the calendar parses on blur.

## Backend CORS note

The RailMind backend (as shipped) sends no CORS headers (`OPTIONS` → 405). The extension therefore calls it from the service worker under `host_permissions` for `127.0.0.1`/`localhost`, which is allowed for declared hosts. For a hosted backend, add CORS for the extension origin (or proxy through a backend you control).

## Store submission path (5-6 dev days + review)

1. Day 1: skeleton + detection (done here).
2. Day 2: backend client + message passing (done here).
3. Day 3: pre-fill + never-submit guard + self-test (done here, guard by code review).
4. Day 4: overlay + popup (done here).
5. Day 5: alarms + storage + errors (alarm included; errors handled).
6. Day 6: cross-browser test + store assets + single-purpose statement + permission justifications + submit.
7. Review: automated first pass (seconds-minutes) + manual review for new extensions / host permissions; typically 1-3 business days, up to 3 weeks worst case (reported, secondary sources).

Listing assets needed: 128x128 icon (have), 5 screenshots 1280x800 (capture from `shots/`), privacy policy URL (required once host permissions are used), single-purpose statement ("Pre-fill IRCTC journey fields from a RailMind shortlist and show fare/risk beside IRCTC's numbers"), permission justifications for `host_permissions: *.irctc.co.in/*`.

Costs: CWS one-time $5 developer registration; Edge Add-ons $0; Firefox AMO $0 (signing mandatory); Safari $99/yr (skip unless iOS reach is a goal).

## Honest verdict on the legal grey zone

IRCTC ToS Clause C bans "automation software and/or Scripting Software" without limiting it to booking, and enforcement is live (2026 Kerala HC bail applications name browser automation tools and extensions). A "fill, never submit, never store creds, never poll" design stays inside the single-purpose policy and the "facilitate unauthorized access" clause, but it does not make Clause C disappear: the extension is still automation software running on IRCTC's site. The honest user-facing framing is "a helper that types what you already decided, on your click" — and the user must be told, in-product, that IRCTC may block or terminate accounts that use automation. The 15-30 seconds saved per booking is real but small; the maintenance cost is selector rot on a site IRCTC changes without notice. Worth building only if (a) the product's differentiator is the decision (fare/risk/explanation), with filling as a convenience, and (b) you accept ongoing selector maintenance and a non-zero ToS risk. If the differentiator were filling alone, it would not be worth it.
