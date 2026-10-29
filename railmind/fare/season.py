"""Which half of the printed year a journey falls in, for the classes priced twice.

Notes 6 and 6.1 of the passenger fare table split the year in two: lean is
"1st February to 31st March; and 1st August to 31st August", peak is "1st April to
31st July; and 1st September to 31st January". Commercial Circular 24 of 2025 still
prices AC 2-Tier and AC First Class twice, in a Peak column and a Lean column, but
carries no date definition of its own, so the periods above (held in
`data/season-calendar.json` with that provenance) are the only printed definition
available.

The lean window really is one month: 1 August to 31 August, not August and
September. It is copied as printed rather than widened into something tidier.

A journey whose date we do not have is not assigned a season. `None` is the caller
saying it cannot tell, and the quote then shows the peak fare with the lean fare
beside it, which is a price that can only be too high rather than too low.
"""

from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SEASON_PATH = DATA_DIR / "season-calendar.json"


class SeasonCalendar:
    """The printed peak and lean windows, as month-day ranges."""

    def __init__(
        self,
        windows: dict[str, tuple[tuple[tuple[int, int], tuple[int, int]], ...]],
        *,
        source: str,
        classes: tuple[str, ...],
    ):
        self._windows = windows
        self.source = source
        self.classes = classes

    def season_for(self, when: dt.date) -> str | None:
        """`"peak"` or `"lean"` for a journey date, or None when the date is outside."""
        key = (when.month, when.day)
        for name, spans in self._windows.items():
            for start, end in spans:
                inside = start <= key <= end if start <= end else (key >= start or key <= end)
                if inside:
                    return name
        return None


@lru_cache(maxsize=1)
def load_season(path: Path | str = SEASON_PATH) -> SeasonCalendar | None:
    """The shipped season calendar, or None when the file is missing."""
    file = Path(path)
    if not file.exists():
        return None
    raw = json.loads(file.read_text())
    windows = {
        str(name): tuple((_month_day(start), _month_day(end)) for start, end in spans)
        for name, spans in raw["seasons"].items()
    }
    return SeasonCalendar(
        windows, source=str(raw["source"]), classes=tuple(raw.get("split_classes", ()))
    )


def _month_day(text: str) -> tuple[int, int]:
    month, day = text.split("-")
    return int(month), int(day)
