"""The Tatkal premium, read off the printed scheme, and where it came from.

Source: the Indian Railways PRS static page "Tatkal Scheme", table "Tatkal Charges
applicable for journey starting from 24.12.2015 onwards", transcribed in
`data/tatkal-charges.json`. The scheme states the charge in one sentence: "10% of
basic fare for second class and 30% of basic fare for all other classes subject to
minimum and maximum as given in the table below", and it adds that the charges "will
be levied uniformly both in peak period & non-peak periods".

Two things this module refuses to do:

- Invent a charge for a class the scheme does not print. AC First Class has no row,
  and IRCTC's terms say a Tatkal ticket may be booked in any class except 1 AC, so
  asking for 1A returns a reason instead of a number.
- Claim the premium sits outside tax. The Tatkal charge is part of the price of the
  same passenger-transport supply, so it belongs in the tax base: the one published
  Tatkal fare this project could check against, CC on 22105 at 192 km at Rs 525, is
  (288 basic + 40 reservation + 45 superfast + 125 Tatkal) x 1.05 = 522.90 rounded up
  to the next five rupees. Putting the premium outside the tax gives 520 and misses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
TATKAL_PATH = DATA_DIR / "tatkal-charges.json"


@dataclass(frozen=True, slots=True)
class TatkalRow:
    """One printed row of the scheme's charge table, in rupees."""

    class_code: str
    percent: float
    floor: int
    ceiling: int
    min_km: int | None


@dataclass(frozen=True, slots=True)
class TatkalCharge:
    """The premium for one seat, in paise, with the working that produced it."""

    amount_minor: int
    note: str


class TatkalCharges:
    """The scheme's charge table, keyed by class code."""

    def __init__(
        self,
        rows: dict[str, TatkalRow],
        *,
        source: str,
        effective: str,
        inherited: dict[str, str] | None = None,
        seasonal: bool = False,
        season_note: str = "",
    ):
        self._rows = rows
        self._inherited = inherited or {}
        self.source = source
        self.effective = effective
        #: Whether the scheme prints a peak and a lean charge. It does not: the page
        #: says the charges are "levied uniformly both in peak period & non-peak
        #: periods", so a seasonal premium would be an invention.
        self.seasonal = seasonal
        self.season_note = season_note

    def row(self, class_code: str) -> TatkalRow | str:
        """The printed row for a class, or the reason there is none."""
        base = class_code.split("_")[0]
        if base in self._rows:
            return self._rows[base]
        donor = self._inherited.get(base)
        if donor is not None and donor in self._rows:
            row = self._rows[donor]
            return TatkalRow(
                # The printed row's own class, not the class that asked for it: it is
                # what lets the charge say which band it borrowed and why.
                class_code=row.class_code,
                percent=row.percent,
                floor=row.floor,
                ceiling=row.ceiling,
                min_km=row.min_km,
            )
        return (
            f"the Tatkal scheme prints no charge for {base}: a Tatkal ticket cannot "
            f"be booked in this class, so no premium is quoted for it"
        )

    def charge(
        self, class_code: str, basic_rupees: float, distance_km: float
    ) -> TatkalCharge | str:
        """The premium for one seat in this class at this basic fare.

        The charge is a percentage of the basic fare clamped between the printed
        floor and ceiling. The floor is what makes the published fares come out
        right at short distances, where the percentage alone falls below it.
        """
        found = self.row(class_code)
        if isinstance(found, str):
            return found
        base = class_code.split("_")[0]
        computed = found.percent * basic_rupees
        amount = min(max(computed, found.floor), found.ceiling)
        amount_minor = round(amount * 100)

        if amount == found.floor and computed < found.floor:
            working = (
                f"the printed minimum of Rs {found.floor} for {found.class_code} applies "
                f"because {found.percent:.0%} of the basic fare is Rs {computed:.2f}"
            )
        elif amount == found.ceiling and computed > found.ceiling:
            working = (
                f"the printed maximum of Rs {found.ceiling} for {found.class_code} applies "
                f"because {found.percent:.0%} of the basic fare is Rs {computed:.2f}"
            )
        else:
            working = (
                f"{found.percent:.0%} of the basic fare, within the printed "
                f"Rs {found.floor}-{found.ceiling} band for {found.class_code}"
            )
        if base != found.class_code:
            working += (
                f"; the scheme prints no row for {base}, so the {found.class_code} band "
                f"is used because its basic fare is the same"
            )
        if found.min_km is not None and distance_km < found.min_km:
            working += (
                f"; the scheme prints a minimum distance for charge of {found.min_km} km "
                f"for {found.class_code} and this journey is {distance_km:g} km"
            )
        return TatkalCharge(amount_minor=amount_minor, note=working)


@lru_cache(maxsize=1)
def load_tatkal(path: Path | str = TATKAL_PATH) -> TatkalCharges | None:
    """The shipped Tatkal scheme, or None when the file is not recorded."""
    file = Path(path)
    if not file.exists():
        return None
    raw = json.loads(file.read_text())
    rows = {
        str(code): TatkalRow(
            class_code=str(code),
            percent=float(body["percent"]),
            floor=int(body["min"]),
            ceiling=int(body["max"]),
            min_km=None if body.get("min_km") is None else int(body["min_km"]),
        )
        for code, body in raw["classes"].items()
    }
    return TatkalCharges(
        rows,
        source=str(raw["source"]),
        effective=str(raw["effective"]),
        inherited={str(k): str(v) for k, v in raw.get("inherits", {}).items()},
        seasonal=bool(raw.get("seasonal", False)),
        season_note=str(raw.get("season_note", "")),
    )
