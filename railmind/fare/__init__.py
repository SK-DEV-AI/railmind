"""Fare computation from the Railway Board's published fare table.

The engine never asks a third party what a ticket costs. It reads the circular
(Railway Board Commercial Circular 24 of 2025, effective 26.12.2025), which prints
a price for every class in every distance slab, and it quotes from that.

Layout:

- `table` holds the printed slabs and answers "what is the basic fare for this
  class at this distance".
- `charges` holds the other-charges table on page 4 (reservation fee, superfast
  supplementary charge, minimum distance for charge).
- `mapping` decides which printed table a class is read from.
- `quote` adds the parts together under a supplied tax rule and returns the working
  alongside the number.
- `tatkal` holds the printed Tatkal charge table, for journeys under that quota.
- `season` says which of the printed peak and lean seasons a journey date falls in.

Nothing here books, pays, or contacts a railway system. A quote is an estimate the
agent shows the user before handing the actual booking to IRCTC.
"""

from __future__ import annotations

from functools import lru_cache

from railmind.fare.charges import ChargeRow, Charges, ChargesError
from railmind.fare.mapping import UnknownServiceError, resolve, section_for, supported
from railmind.fare.quote import ROUNDING_STEP_MINOR, Component, FareQuote, TaxRules, quote
from railmind.fare.season import SeasonCalendar, load_season
from railmind.fare.table import FareTable, FareTableError, Section, Slab
from railmind.fare.tatkal import TatkalCharge, TatkalCharges, TatkalRow, load_tatkal
from railmind.fare.tax import load_tax

__all__ = [
    "ROUNDING_STEP_MINOR",
    "ChargeRow",
    "Charges",
    "ChargesError",
    "Component",
    "FareQuote",
    "FareTable",
    "FareTableError",
    "SeasonCalendar",
    "Section",
    "Slab",
    "TatkalCharge",
    "TatkalCharges",
    "TatkalRow",
    "TaxRules",
    "UnknownServiceError",
    "default_charges",
    "default_season",
    "default_table",
    "default_tatkal",
    "default_tax",
    "quote",
    "resolve",
    "season_for",
    "section_for",
    "supported",
]


@lru_cache(maxsize=1)
def default_table() -> FareTable:
    """The shipped fare table, read and validated once per process."""
    return FareTable.load()


@lru_cache(maxsize=1)
def default_charges() -> Charges:
    """The shipped other-charges table, read once per process."""
    return Charges.load()


@lru_cache(maxsize=1)
def default_tax() -> TaxRules | None:
    """The shipped tax rule, or None while no citation is recorded."""
    return load_tax()


@lru_cache(maxsize=1)
def default_tatkal() -> TatkalCharges | None:
    """The shipped Tatkal scheme, read once per process."""
    return load_tatkal()


@lru_cache(maxsize=1)
def default_season() -> SeasonCalendar | None:
    """The shipped peak and lean calendar, or None when it is absent."""
    return load_season()


def season_for(when) -> str | None:
    """`"peak"` or `"lean"` for a journey date, or None when the calendar is absent."""
    calendar = default_season()
    if calendar is None:
        return None
    return calendar.season_for(when)
