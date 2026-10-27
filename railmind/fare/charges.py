"""The charges the circular adds to a basic fare, and where each one applies.

Source: Railway Board Commercial Circular 24 of 2025, page 4, "Other charges
applicable on passenger fares", transcribed in `data/fare-charges.json`. Paragraph
3 of the same circular is the sentence that makes these additive: "There shall be
no change in charges for reservation fee, superfast surcharge etc. Such charges,
wherever applicable, shall continue to be [charged] additionally."

What this module refuses to invent:

- A charge the page prints as a dash is `None`, not zero. Executive Class has no
  minimum fare because Executive Class is priced by its own table; a zero there
  would price a 10 km Executive journey at ten rupees.
- A charge for a class the page does not list is `None` as well, so a caller that
  asks for something unsupported gets told so instead of a plausible number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CHARGES_PATH = DATA_DIR / "fare-charges.json"

#: Which charge block applies to a fare-table section. The page prints one block
#: for Mail/Express services and one for Ordinary services; our tables are all
#: Mail/Express, and Rajdhani trains are Mail/Express services for this purpose.
CHARGES_SECTION = {
    "mail_express_ac": "mail_express",
    "mail_express_non_ac": "mail_express",
    "rajdhani": "mail_express",
}


class ChargesError(RuntimeError):
    """The charges file is missing something the caller asked for."""


@dataclass(frozen=True, slots=True)
class ChargeRow:
    """One printed line of the other-charges table."""

    name: str
    min_distance_km: int | None
    min_fare: int | None
    reservation_fee: int | None
    superfast: int | None


class Charges:
    """The other-charges table, keyed by service kind and class code."""

    def __init__(self, rows: dict[str, dict[str, ChargeRow]], *, source: str, effective: str):
        self._rows = rows
        self.source = source
        self.effective = effective

    @classmethod
    def load(cls, path: Path | str = CHARGES_PATH) -> Charges:
        return _load(Path(path))

    def row(self, section: str, class_code: str) -> ChargeRow:
        service = CHARGES_SECTION.get(section)
        if service is None:
            raise ChargesError(
                f"no charge block for fare section {section!r}; known: {sorted(CHARGES_SECTION)}"
            )
        try:
            return self._rows[service][class_code]
        except KeyError:
            raise ChargesError(
                f"the other-charges table has no {class_code!r} line for {service!r}; "
                f"it lists {sorted(self._rows[service])}"
            ) from None


@lru_cache(maxsize=4)
def _load(path: Path) -> Charges:
    raw = json.loads(path.read_text())

    def block(name: str) -> dict[str, ChargeRow]:
        return {
            code: ChargeRow(
                name=code,
                min_distance_km=body.get("min_distance_km"),
                min_fare=body.get("min_fare"),
                reservation_fee=body.get("reservation_fee"),
                superfast=body.get("superfast"),
            )
            for code, body in raw[name].items()
            if not code.startswith("_")
        }

    return Charges(
        {"mail_express": block("mail_express"), "ordinary": block("ordinary")},
        source=raw["source"],
        effective=raw["effective"],
    )
