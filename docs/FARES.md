# FARES.md — where every rupee in a quote comes from

A quote is produced **locally**, with no network call, from the railway's own
published fare table. This page says which document, which rule, and what a quote
deliberately refuses to guess.

Source of every number: **Commercial Circular 24 of 2025**, effective
**2025-12-26** (`$JCODE_SCRATCH_DIR/CC24_2025.pdf`), transcribed by
`scripts/extract_fare_table.py` and checked against the page images by hand.
Nothing here is from memory, and nothing is from a vendor.

---

## 1. Shape of the answer

```python
from railmind import fare

quote = fare.quote(
    section="mail_express_ac",
    class_code="3A",
    distance_km=190.7,
    table=fare.default_table(),
    charges=fare.default_charges(),
    tax=fare.default_tax(),
    season=None,
    superfast=None,
)
quote.total_minor  # 49200 paise, the whole answer
quote.components  # base_fare 452.00 + reservation_charge 40.00
quote.assumptions  # plain-English caveats, always populated
quote.source  # "Commercial Circular 24 of 2025"
quote.effective  # "2025-12-26"
```

Three of those arguments are deliberately `None` rather than guessed:

- `tax=fare.default_tax()` returns nothing today, because no cited tax rule
  exists yet (see §5). A quote therefore excludes GST and says so.
- `season=None` means peak. The lean-season rate comes back as an alternate on
  the same quote, so the caller chooses instead of the code choosing.
- `superfast=None` means the timetable carries no superfast flag, so the
  supplement is stated as a possible shortfall rather than added by default.

The provider wraps this: `rail_india.price()` → `quote_for_leg()`, and
`_to_itinerary` attaches `total_price` + `price_breakup` **only when the query
named a class**. A fare for a class the user did not ask for would be invented
detail.

---

## 2. The three sections

| Section | Covers | Rows |
|---|---|---|
| `mail_express_non_ac` | 2S, SL, FC (non-AC first class) on Mail/Express | 239 |
| `mail_express_ac` | CC, 3A, 2A, 1A on Mail/Express | 211 |
| `rajdhani` | all Rajdhani classes, own table whatever the class | 144 |

Mapping is narrow and refuses by design:

- Rajdhani → `rajdhani`, whatever class the caller asked for.
- `2S`, `SL`, `FC` → `mail_express_non_ac`.
- else → `mail_express_ac`.
- a class the section does not print (`EC`) raises `UnknownServiceError`.
- services with their own tables (Vande Bharat and friends) are in
  `UNTABLED_SERVICES` and are **refused**, never priced off Mail/Express.

---

## 3. Rules, and why each one exists

1. **Slab lookup is exact integer arithmetic.** `slab_for()` uses
   `bisect_right` plus `km <= slab.end`; there is no floating-point comparison
   deciding which of two adjacent slabs a journey falls in.
2. **A part kilometre is charged as a whole one.** `charged_km =
   max(ceil(distance_km), min_distance_km or 0)`. Never under-quotes, matches rail
   practice, and the assumption text says it ("a 190.7 km journey is charged as
   191 km").
3. **Minimum distance for charge is applied.** Printed on page 4 of the same
   circular and stored per class in `data/fare-charges.json` with provenance:
   SL 200, 3A/2A/1A 300, CC 150, 2S 50. Also stated in `assumptions`.
4. **Reservation charge and superfast supplement come from the charges table,
   not from the fare slab.** They are separate components on the wire.
5. **Superfast is three-way.** `True` adds the charge, `False` does not and stays
   quiet, `None` (the default, because the timetable has no superfast flag) adds
   nothing **and states the printed supplement as the possible shortfall**. A
   quietly short total is the one failure mode a price list must not have.
6. **Beyond the last printed slab** (past 5000 km) the page-17 increment rule is
   applied in 50 km steps, hand-transcribed into `data/fare-beyond-5000.json`.
7. **A gap is never interpolated.** A missing slab or a missing cell means "this
   journey cannot be priced from the source", and that is what the caller gets:
   `total_minor is None` → `NotSupported` from the provider, with the reason.
8. **Overlapping slabs or a fare that falls as distance rises are refused at
   load**, because both mean the transcription is wrong and a wrong price is
   worse than no price.

---

## 4. Verified quotes (not hypotheses)

Train 22105 Indrayani SF Express, 190.7 km:

| Class | Total | Components | Slab actually used |
|---|---|---|---|
| SL | ₹150 | 130 base + 20 reservation | 196-200 (minimum distance 200) |
| 3A | ₹492 | 452 base + 40 reservation | 296-300 (minimum distance 300) |
| 2S | ₹96 | 81 base + 15 reservation | 191-195 |
| CC | ₹328 | 288 base + 40 reservation | 296-300 |

Note what those rows show: for 190.7 km the minimum-distance rule, not the raw
distance, picks the slab for SL, 3A and CC. That is the document's rule, not a
rounding choice of ours.

One apparent oddity that is **not** a bug: `mail_express_ac` prints `3A: 452`
flat from slab 1-150 through 296-300, then 504 at 301. The page image confirms
the circular itself prints it that way (`mail_express_ac`, page 12). Do not
"fix" it.

---

## 5. What a quote does not include, and says so

| Excluded | Why | What the caller sees |
|---|---|---|
| **GST** | Circular ¶4 says "as per instructions issued from time to time" and prints no rate. `data/tax-rules.json` does not exist. | `assumptions` states tax is not included. Writing a rate from memory would be a fabricated citation. |
| **Tatkal premium** | No published scheme, and peak/lean boundaries are undefined in the circular. | `season` is a parameter; when unnamed, the peak total is primary and the lean total is returned as `alternate_total_minor`. Neither is invented. |
| **Convenience fee / IRCTC charges** | Not in this circular. | Not charged. |
| **Onboard catering, insurance** | Not fares. | Not modelled. |

To start pricing tax, add `data/tax-rules.json` shaped
`{"source": "...", "ac": <rate>, "non_ac": <rate>}`. `fare.default_tax()` picks
it up; no code change. Until then every quote is a lower bound on the real
total, and says so.

---

## 6. Known hole: one non-AC slab

`mail_express_non_ac` is missing `2251-2275`. Not a lost row: the row is read
(120 rows in, 120 after repair), but all three fares on it are unreadable in this
scan (2S `592`/`552` tie, SL a lone `253`, FC `2353`/`2354` tie), so the row is
dropped with a report line (`"2251-2275: dropped whole, no fare in any class
settled"`). A journey that lands in that slab prices as "no fare".

Page-11 re-reads also delete a handful of clearly-misread cells (`2376-2400 2S
560`, `2476-2500 SL 299`, FC `7418`). Those are one unpriced cell each in one
class, which is a `None`, not a wrong price.

---

## 7. Re-running the extractor

```bash
cd ~/session-root/repos/railmind
.venv/bin/python scripts/extract_fare_table.py \
  --pdf "$JCODE_SCRATCH_DIR/CC24_2025.pdf" --pages 10-11 --report      # no writes
.venv/bin/python scripts/extract_fare_table.py \
  --pdf "$JCODE_SCRATCH_DIR/CC24_2025.pdf" --out data/fare-table.json   # all planned pages
```

Reads are done twice (two segmentation passes) plus an isolated crop re-read for
cells the passes disagree on; a cell with no two agreeing reads yields **no
value**. The report prints per class: confirmed / single-read / disputed /
missing / deleted-by-checks, plus discontinuities and monotonicity violations.
A change to the table is only acceptable with that report read, not skimmed.

Gate numbers from the v20 run that produced the shipped table: page 10 `360/360`
hand-verified cells, page 12 `329/329`, page 18 `75/75`.

---

## 8. Tests

`tests/test_fare.py` (36) covers slab arithmetic, minimum distance, the part-km
rule, the tri-state superfast note, gap refusal and tax-absent behaviour.
`tests/test_rail_india.py::TestFare` covers the provider end: prices without a
network call, parts add up to the total, source cited, Vande Bharat and `EC`
refused, unpriced journey → warning, and "prices only the named class".
