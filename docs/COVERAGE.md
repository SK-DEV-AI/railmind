# COVERAGE.md — how much of the country the local timetable can answer for

One question, one number: **for a journey a person would actually search, how often can
we answer from `data/timetable.db` with no network call at all?**

Answer, measured: **2,134 of 2,970 ordered station pairs, 71.9%**, over a 55-station
long-distance set. Before legacy-code resolution the same set gave 2,048/2,970, 69.0%.

Everything below is reproducible: the set is written out, the query is the production
one, and the database is the one the app ships with.

---

## 1. The set

`Major stations (55, as a user types them)`

```
NDLS CSMT HWH MAS SBC HYB PUNE ADI LKO PNBE JP BPL NGP BBS VSKP GKP PRYJ
BSB CNB NJP AGC MTJ BRC ST BZA KPD UMB LDH JBP RTM KOTA GWL DDU MB SDAH
SHM KOAA ERS TVC CBE MYS UBL MAQ JAT ASR JU AII BKN RNC GHY PURI TPTY SC
BCT BSBS
```

Selection rule: principal city termini and junctions that long-distance travellers use.
Deliberately **excluded**: pure suburban hubs (SDAH is kept as a terminal, MSB, KYN, TBM,
DDJ and the other EMU halts are not), because a local train between two suburban halts is
not a journey anyone asks us for and it inflates the score enormously.

Deliberately **included**: `BCT` (Mumbai Central, retired) and `BSBS` (Banaras, retired)
as a user would type them, not only their current codes `MMCT` and `BNRS`. That is the
honest input, and it is what exposes the station-code defect.

## 2. The measurement

```python
from railmind.data.timetable import TimetableRepo
import itertools

codes = open("docs/major-stations.txt").read().split()
repo = TimetableRepo("data/timetable.db")
pairs = list(itertools.permutations(codes, 2))
answered = sum(1 for origin, dest in pairs if repo.trains_between(origin, dest))
print(f"{answered}/{len(pairs)} = {100 * answered / len(pairs):.1f}%")
```

`trains_between` is the production path, not a copy: it resolves legacy codes, requires a
usable departure at the origin and a usable arrival at the destination, and requires the
destination to come later in the sequence. A pair that a stop geometry says is
reachable but that no timetable entry serves counts as unanswered.

Result on the shipped database (13,010 stations, 10,861 trains, 213,105 stops):
**2,134/2,970 = 71.9%.**

## 3. Why this number and not the older ones

| Figure | Where it came from | Why it is not the headline |
|---|---|---|
| 44.5% (694/1,560) | top 40 stations by total stop count | **Wrong set.** Ranking by stop count puts suburban halts on the list (SDAH, KYN, MSB, TBM, DDJ, BNXR…). No long-distance train runs SDAH→KYN, so the metric measured suburban topology, not our coverage. |
| 71.6% (1,547/2,162) | an ad-hoc 47-code list from 2026-10-31 | Correct in spirit, but the list was never written down, so the number is not reproducible. Superseded by the set above. |
| 69.0% (2,048/2,970) | the 55-station set, **before** legacy codes resolve | The right set with the old bug in place. Kept as the baseline the fix is measured against. |
| **71.9% (2,134/2,970)** | the 55-station set, production code | **The headline.** |

The gain of +86 pairs (+2.9 pp) is entirely from `BCT → MMCT` and `BSBS → BNRS`: the only
retired codes in the set. That is what a stale code costs, and it is small here only
because this set is hand-picked from current termini.

## 4. What the missing 28% actually is

Not all of it is a data gap, and the difference matters:

- **Real gap, fixable for free.** The crawl holds 10,861 of the 10,870 numbers NTES
  advertised (9 failed with `empty-or-error`, 54 remaining/discontinued). Re-running the
  resumable crawl is ~2 minutes and needs no key, and the import path is a single write
  path, so it cannot drift.
- **Not a gap, a fact about the railway.** Ranchi→Puri has no direct train. A large part
  of the missing 28% is pairs where the honest answer is "no direct train, here is a
  change at X", which is a routing problem, not a data problem.
- **Not a gap, a code we cannot infer.** Clicking a code rename that changes the *name*
  too (Mughal Sarai → DDU) is invisible to the alias rule, which matches on normalised
  name. `MGS`→`DDU` therefore still returns nothing. Known, not fixed.

## 5. Reproducing the whole thing from a clean clone

`data/timetable.db` and `data/raw/` are not committed (21.5 MB of SQLite plus 23 MB of
crawl). A fresh clone has the recorded fixtures only, so the same script answers for 5
trains and 3 corridors. Rebuilding the full database:

```
python scripts/import_railpull.py     # from a railpull crawl in data/raw
```

The number above was measured on that full import.
