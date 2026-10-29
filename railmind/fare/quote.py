"""What a passenger pays for one seat, assembled from published parts only.

The circular tells us what each part is and that the parts are additive:

- Basic fare, from the printed slab table (`railmind.fare.table`).
- Reservation fee and superfast supplementary charge, from the other-charges table
  (`railmind.fare.charges`), which paragraph 3 says are charged additionally.
- Tax, which paragraph 4 says "shall continue to be levied, as per instructions
  issued from time to time". That is a pointer to a tax notification, not a rate, so
  the rate lives here as policy that must be supplied from a source, and a quote
  built without one is `unpriced` rather than tax-free.
- The Tatkal premium, when the journey is a Tatkal one, from the printed scheme
  (`railmind.fare.tatkal`). It sits inside the tax base: it is part of the price of
  the same passenger-transport supply, not a separate one.
- The rounding the fare table's note 5.1 requires: "Chargeable Fares, as obtained
  after adding Miscellaneous Charges like Reservation Fee, Supplementary charge for
  Superfast trains, Catering Charge, Service Tax etc. to the Basic Fare, will be
  rounded off to the next higher multiple of Rs. 5." The round-up is shown as its own
  line, so the parts still add up to the total.

Every quote therefore carries its own working: the components it summed, the
section and slab it read, and a single reason string when it could not price the
journey. The agent shows that reason to the user; it never rounds a missing part
into the total.

Season, deliberately: the circular prices AC 2-Tier and AC First Class twice, once
for a peak period and once for a lean one, but the definition of those periods is
not in this circular. A caller that does not know the season gets the peak fare as
the headline number, because under-quoting a price is the failure that hurts, with
the lean fare carried alongside so the difference is visible rather than hidden.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from railmind.fare.charges import Charges, ChargesError
from railmind.fare.table import FareTable, FareTableError
from railmind.fare.tatkal import TatkalCharges

#: Class codes whose fare the circular splits into a peak and a lean column.
SEASON_SPLIT = ("2A", "1A")

#: Classes the circular prices as air conditioned, for the tax rule. The tax
#: treatment follows the class, not the train.
AC_CLASSES = ("CC", "EC", "3E", "3A", "2A", "1A", "EA")

#: Classes the tax notification names separately from an air-conditioned coach and
#: taxes at the same rate: "rail in first class or air conditioned coach". First
#: class is named in the exception to the exemption, so non-air-conditioned FC is
#: taxable, unlike the other non-AC classes.
FIRST_CLASS = ("FC",)

#: What one Rs 5 step is, in paise, for the fare table's rounding note.
ROUNDING_STEP_MINOR = 500


@dataclass(frozen=True, slots=True)
class TaxRules:
    """The tax policy a quote is built under, with the source it came from."""

    rate_ac: float | None
    rate_non_ac: float | None
    source: str
    rate_first_class: float | None = None

    def rate_for(self, class_code: str) -> float | None:
        base = class_code.split("_")[0]
        if base in FIRST_CLASS:
            # The notification taxes first class at the air-conditioned rate, so a
            # rule that records only the two rates still answers correctly.
            return self.rate_first_class if self.rate_first_class is not None else self.rate_ac
        return self.rate_ac if base in AC_CLASSES else self.rate_non_ac


@dataclass(frozen=True, slots=True)
class Component:
    """One line of the price, in paise, named the way the ticket names it."""

    code: str
    label: str
    amount_minor: int
    note: str | None = None


@dataclass(frozen=True, slots=True)
class FareQuote:
    """A price, its parts, and the reason it is missing when it is missing."""

    section: str
    class_code: str
    distance_km: float
    components: tuple[Component, ...] = ()
    total_minor: int | None = None
    season: str | None = None
    alternate_total_minor: int | None = None
    alternate_season: str | None = None
    unpriced_reason: str | None = None
    assumptions: tuple[str, ...] = ()
    slab: tuple[int, int] | None = None
    source: str = ""
    effective: str = ""

    @property
    def priced(self) -> bool:
        return self.total_minor is not None


def quote(
    *,
    section: str,
    class_code: str,
    distance_km: float,
    table: FareTable,
    charges: Charges,
    tax: TaxRules | None = None,
    tatkal: TatkalCharges | None = None,
    season: str | None = None,
    superfast: bool | None = None,
) -> FareQuote:
    """Price one seat: a table lookup, the printed charges, and the tax rule.

    `season` is `"peak"`, `"lean"`, or `None` when the caller cannot tell. It only
    matters for classes the circular prices twice.

    `superfast` is the same three-way question about the train: `True` adds the
    printed supplementary charge, `False` says the train is not a superfast one, and
    the default `None` says the caller cannot tell. `None` does not quietly behave
    like `False`; the quote then states how much the real fare could exceed it,
    because a total that is silently short is the error that costs a passenger money.

    `tatkal` is the printed Tatkal scheme, supplied only when the journey is under the
    Tatkal quota. It is `None` for general quota, which is not the same statement as
    "no premium applies": a general-quota quote simply does not mention Tatkal at all.
    """
    base_code = class_code.split("_")[0]
    split = base_code in SEASON_SPLIT
    seasonal = season if split else None
    if split and seasonal is None:
        seasonal = "peak"

    try:
        row = charges.row(section, f"{base_code}_{seasonal}" if split else base_code)
    except ChargesError as error:
        return _unpriced(section, class_code, distance_km, str(error), table, season)

    lookup = f"{base_code}_{seasonal}" if split else base_code
    # "Minimum distance for charge" means a short journey is priced as if it were
    # that long, and a part kilometre is charged as a whole one. The last two rows
    # of the page prove the first rule matters: the SL slab for 6-10 km prints 40
    # rupees, but SL cannot be sold for less than 200 charged kilometres, whose
    # printed fare is 130.
    charged_km = max(float(math.ceil(distance_km)), row.min_distance_km or 0)
    variants = [(seasonal, lookup)]
    if split and season is None:
        other = "lean" if seasonal == "peak" else "peak"
        variants.append((other, f"{base_code}_{other}"))

    built = [
        _variant(
            section=section,
            class_code=class_code,
            lookup=code,
            charged_km=charged_km,
            table=table,
            row=row,
            tax=tax,
            tatkal=tatkal,
            superfast=superfast,
        )
        for _, code in variants
    ]
    primary = built[0]
    if isinstance(primary, str):
        return _unpriced(section, class_code, distance_km, primary, table, season)

    alternate: int | None = None
    alternate_season: str | None = None
    if len(built) > 1 and not isinstance(built[1], str):
        alternate = built[1].total_minor
        alternate_season = variants[1][0]

    assumptions: list[str] = []
    assumptions.extend(primary.assumptions)
    if split and season is None:
        assumptions.append(
            f"the peak-period fare is quoted because the peak period dates are "
            f"notified outside this circular; the lean-period fare is "
            f"{_rupees(alternate) if alternate is not None else 'unavailable'}"
        )
    if tax is None:
        assumptions.append("no tax rule was supplied, so the total excludes tax")
    if row.min_distance_km is not None and distance_km < row.min_distance_km:
        assumptions.append(
            f"the page prints a minimum distance for charge of {row.min_distance_km} km "
            f"for {base_code}, so a {distance_km:g} km journey is charged at "
            f"{row.min_distance_km} km"
        )
    elif charged_km != distance_km:
        assumptions.append(
            f"a {distance_km:g} km journey is charged as {int(charged_km)} km: the "
            f"table is printed in whole kilometres and a part kilometre is charged as one"
        )
    if superfast is True and row.superfast is None:
        assumptions.append(
            "this class carries no superfast supplementary charge on the printed table"
        )
    elif superfast is None and row.superfast is not None:
        assumptions.append(
            f"the timetable does not say whether this train is a superfast service, so "
            f"the printed supplementary charge of {_rupees(row.superfast * 100)} is not "
            f"included and the real fare is up to that much higher"
        )

    return FareQuote(
        section=section,
        class_code=class_code,
        distance_km=distance_km,
        components=primary.components,
        total_minor=primary.total_minor,
        season=seasonal,
        alternate_total_minor=alternate,
        alternate_season=alternate_season,
        assumptions=tuple(assumptions),
        slab=primary.slab,
        source=table.source,
        effective=table.effective,
    )


@dataclass(frozen=True, slots=True)
class _Built:
    components: tuple[Component, ...]
    total_minor: int
    slab: tuple[int, int] | None
    assumptions: tuple[str, ...] = ()


def _variant(
    *,
    section: str,
    class_code: str,
    lookup: str,
    charged_km: float,
    table: FareTable,
    row,
    tax: TaxRules | None,
    tatkal: TatkalCharges | None,
    superfast: bool | None,
) -> _Built | str:
    """Sum the parts for one class at one season, or say what is missing."""
    try:
        printed = table.section(section)
    except FareTableError as error:
        return str(error)
    slab = printed.slab_for(charged_km)
    base = table.base_fare(section, lookup, charged_km)
    if base is None:
        if charged_km <= printed.slabs[-1].end:
            return (
                f"the printed table has no fare for {lookup} at {charged_km:g} km: "
                f"either the row is missing from the page or the cell carries no "
                f"value, and the engine will not interpolate one"
            )
        return (
            f"the circular prices {section} only up to {printed.slabs[-1].end} km "
            f"and prints no increment for this class beyond it"
        )

    components = [
        Component(
            "basic",
            "Basic fare",
            base * 100,
            (
                f"read past the last printed slab ({printed.slabs[-1].end} km) with the "
                f"circular's per-50 km increment"
            )
            if slab is None
            else (f"row {slab.start}-{slab.end} km of {table.source}, effective {table.effective}"),
        ),
        Component(
            "reservation",
            "Reservation fee",
            (row.reservation_fee or 0) * 100,
            None if row.reservation_fee is not None else "not printed for this class",
        ),
    ]
    if superfast is True:
        components.append(
            Component(
                "superfast",
                "Superfast supplementary charge",
                (row.superfast or 0) * 100,
                None if row.superfast is not None else "not printed for this class",
            )
        )

    assumptions: list[str] = []
    if tatkal is not None:
        premium = tatkal.charge(class_code, float(base), charged_km)
        if isinstance(premium, str):
            return premium
        components.append(Component("tatkal", "Tatkal premium", premium.amount_minor, premium.note))
        assumptions.append(
            "the Tatkal premium is inside the tax base: the charge is part of the price "
            "of the same journey, not a separate supply"
        )

    subtotal = sum(component.amount_minor for component in components)
    rate = tax.rate_for(class_code) if tax is not None else None
    if tax is not None and rate is None:
        return f"no tax rate is recorded for {class_code}"
    if tax is not None and rate:
        amount = round(subtotal * rate)
        components.append(
            Component(
                "gst",
                "GST",
                amount,
                f"{rate * 100:.4g}% of {_rupees(subtotal)}",
            )
        )
    elif tax is not None:
        # A zero-rated class would otherwise carry a "GST Rs 0" line, which reads like
        # a charge that came to nothing. Say the class is exempt instead.
        assumptions.append(
            f"no tax is charged on {class_code}: the rate recorded for this class is nil"
        )
    total = sum(component.amount_minor for component in components)
    # The fare table's note 5.1: the chargeable fare, after the printed charges and
    # tax are added, is rounded up to the next higher multiple of Rs 5. It is the step
    # that turns our arithmetic into the number printed on the ticket, and it rounds
    # up rather than to nearest, so the quoted total is never below the real one.
    rounded = -(-total // ROUNDING_STEP_MINOR) * ROUNDING_STEP_MINOR
    if rounded != total:
        components.append(
            Component(
                "rounding",
                "Round up to the next Rs 5",
                rounded - total,
                "the fare table's rounding note: chargeable fares are rounded off to the "
                "next higher multiple of Rs 5",
            )
        )
        total = rounded
    return _Built(
        components=tuple(components),
        total_minor=total,
        slab=(slab.start, slab.end) if slab else None,
        assumptions=tuple(assumptions),
    )


def _unpriced(
    section: str, class_code: str, distance_km: float, reason: str, table, season
) -> FareQuote:
    return FareQuote(
        section=section,
        class_code=class_code,
        distance_km=distance_km,
        season=season,
        unpriced_reason=reason,
        source=table.source,
        effective=table.effective,
    )


def _rupees(minor: int | None) -> str:
    if minor is None:
        return "unavailable"
    return f"Rs {minor // 100}"
