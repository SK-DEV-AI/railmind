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
    tax=fare.default_tax(),  # 5% AC/FC, nil others, cited
    tatkal=fare.default_tatkal(),  # omit unless the journey is under Tatkal
    season="peak",
    superfast=None,
)
quote.total_minor  # 52000 paise, the whole answer
quote.components  # base 452.00 + reservation 40.00 + GST 24.60 + round-up 3.40
quote.assumptions  # plain-English caveats, always populated
quote.source  # "Commercial Circular 24 of 2025"
quote.effective  # "2025-12-26"
```

Three of those arguments deserve a word:

- `tatkal` is only passed when the journey is under the Tatkal quota. Passing it
  adds the printed premium and puts it inside the tax base (§5).
- `season=None` means peak. The lean-season rate comes back as an alternate on
  the same quote, so the caller chooses instead of the code choosing. The
  provider always passes a season, because it has the journey date.
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

Train 22105 Indrayani SF Express, 190.7 km, priced with the shipped tables and
the shipped tax rule:

| Class | Total | Components | Slab actually used |
|---|---|---|---|
| SL | ₹150 | 130 base + 20 reservation, no tax | 196-200 (minimum distance 200) |
| 3A | ₹520 | 452 + 40 + 24.60 GST + 3.40 round-up | 296-300 (minimum distance 300) |
| 2S | ₹100 | 81 + 15 + 4.00 round-up, no tax | 191-195 |
| CC | ₹345 | 288 + 40 + 16.40 GST + 0.60 round-up | 296-300 |
| 2A | ₹725 | 637 + 50 + 34.35 GST + 3.65 round-up, peak | 296-300 (minimum distance 300) |

Note what those rows show: for 190.7 km the minimum-distance rule, not the raw
distance, picks the slab for SL, 3A and CC. That is the document's rule, not a
rounding choice of ours.

Every total is a multiple of ₹5 because the fare table's note 5.1 says the
chargeable fare is rounded up to the next higher multiple of ₹5. It is its own
component, so the parts still sum to the total exactly.

One apparent oddity that is **not** a bug: `mail_express_ac` prints `3A: 452`
flat from slab 1-150 through 296-300, then 504 at 301. The page image confirms
the circular itself prints it that way (`mail_express_ac`, page 12). Do not
"fix" it.

---

## 5. What is included, what is not

Included, each with the published text it came from:

| Part | Rule | Source |
|---|---|---|
| **GST** | 5% on first class and air conditioned coaches; nil on every other class | `data/tax-rules.json`: CBIC 11/2017 S.No. 8 Heading 9964 (2.5% CGST) and 12/2017 S.No. 17 Heading 9964 (Nil) |
| **Tatkal premium** | 10% of basic fare for 2S, 30% for others, clamped to a per-class floor and ceiling | `data/tatkal-charges.json`: indianrail.gov.in Tatkal Scheme page, cross-checked against IRCTC's Tatkal FAQ |
| **Round-up** | chargeable fare rounded **up** to the next ₹5 | fare table note 5.1, printed on the held copy |
| **Season** | AC 2-Tier and AC First Class are printed twice, peak and lean | `data/season-calendar.json`: notes 6 and 6.1 |

Two consequences worth stating plainly:

- **First Class is taxable.** The exemption covers rail "in a class other than
  first class or an air-conditioned coach", so non-AC First Class pays 5%. It
  used to be read as non-AC here and left untaxed, which was wrong.
- **The Tatkal premium sits inside the tax base.** The one published Tatkal fare
  this project could check is CC on 22105 at 192 km, ₹525: (288 + 40 + 45 + 125)
  × 1.05 = 522.90, rounded up. Taxing the fare alone gives ₹520 and misses it.

Not included, and not silently zero:

| Excluded | Why | What the caller sees |
|---|---|---|
| **Convenience fee / IRCTC charges** | Not in this circular, and IRCTC does not publish a settled schedule | Not charged, and not claimed to be the whole price |
| **Onboard catering, insurance, blankets** | Not fares | Not modelled |
| **Any class the scheme excludes** | 1A and FC are outside Tatkal | `total_minor is None` with the reason, never a guessed premium |

The season boundaries have not been re-printed in a 2025-dated document we hold.
The circular still prices the two seasons, so the split is real, but the dates
carry the 2014-15 edition's provenance in the data file and are labelled as
such. A quote states which season it used.

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

`tests/test_fare.py` covers slab arithmetic, minimum distance, the part-km rule,
the tri-state superfast note, gap refusal, tax-absent behaviour and the round-up.
`tests/test_tatkal.py` covers the printed percentages, floors and ceilings, the
3E→3A inheritance, the 1A refusal, the peak/lean uniformity claim, and the
premium inside the tax base (the ₹525 ticket is reproduced in one test).
`tests/test_season.py` covers every printed season boundary, the 1 Aug-31 Aug
window, that a class printed once ignores the season, and that First Class is
taxed.
`tests/test_rail_india.py::TestFare` covers the provider end: prices without a
network call, parts add up to the total, source cited, Vande Bharat and `EC`
refused, unpriced journey → warning, and "prices only the named class".
