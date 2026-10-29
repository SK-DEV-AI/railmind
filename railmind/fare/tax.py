"""The tax rule a fare quote is built under, and where it came from.

The circular says tax "shall continue to be levied, as per instructions issued from
time to time" and stops there: it points at a tax notification instead of printing a
rate. So the rate cannot be read off the document we already hold, and inventing one
would put a confident wrong number on a ticket the user is about to buy.

The rate therefore ships as its own file, `data/tax-rules.json`, with the citation
that justifies it. Until that file exists the engine quotes without tax and says so
in the quote's own assumptions, which is a price the user can still trust as a floor
rather than a price that quietly understates the bill.
"""

from __future__ import annotations

import json
from pathlib import Path

from railmind.fare.quote import TaxRules

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
TAX_PATH = DATA_DIR / "tax-rules.json"


def load_tax(path: Path | str = TAX_PATH) -> TaxRules | None:
    """The tax rule from the shipped file, or None when it is not yet recorded."""
    file = Path(path)
    if not file.exists():
        return None
    raw = json.loads(file.read_text())
    return TaxRules(
        rate_ac=_rate(raw, "ac"),
        rate_non_ac=_rate(raw, "non_ac"),
        source=str(raw["source"]),
        rate_first_class=_rate(raw, "first_class"),
    )


def _rate(raw: dict, key: str) -> float | None:
    value = raw.get(key)
    return None if value is None else float(value)
