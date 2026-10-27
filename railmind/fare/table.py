"""The printed fare table, and the distance a fare is charged for.

Source: Railway Board Commercial Circular 24 of 2025, effective 26.12.2025, read
off a scan by `scripts/extract_fare_table.py` and shipped as
`data/fare-table.json`. Nothing here computes a fare out of kilometres: the
circular prints a price for every class in every distance slab, and a table lookup
is the only honest way to reproduce it. An interpolation between slabs would be a
number the railway never published, and a hole in the table must therefore surface
as "unpriced" rather than be filled in.

Two shapes of answer the rest of the engine depends on:

- A slab is charged once it is entered. A 163 km journey is charged the 161-165 km
  slab, not a fraction of it.
- A class missing from a slab is a hole, not a zero. The extractor refuses to
  guess, so a handful of slabs carry no value for one class; `base_fare` returns
  `None` for those and the quote it feeds says so.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
TABLE_PATH = DATA_DIR / "fare-table.json"
BEYOND_PATH = DATA_DIR / "fare-beyond-5000.json"

#: Table section -> the section of the beyond-5,000 km increment table that
#: applies to it. The circular prints increments for Ordinary and Mail/Express
#: services only, so a Rajdhani journey beyond 5,000 km is unpriced by design.
BEYOND_SECTION = {
    "mail_express_ac": "mail_express",
    "mail_express_non_ac": "mail_express",
}


class FareTableError(RuntimeError):
    """The shipped table contradicts itself, so no price from it can be trusted."""


@dataclass(frozen=True, slots=True)
class Slab:
    """One printed row: a distance band and the fare the circular charges for it."""

    start: int
    end: int
    fares: dict[str, int | None]

    def fare(self, class_code: str) -> int | None:
        return self.fares.get(class_code)


@dataclass(frozen=True, slots=True)
class Section:
    """One printed table: the classes it carries and its slabs, in printed order."""

    name: str
    classes: tuple[str, ...]
    slabs: tuple[Slab, ...]

    @property
    def starts(self) -> tuple[int, ...]:
        return tuple(slab.start for slab in self.slabs)

    def slab_for(self, km: float) -> Slab | None:
        """The slab a distance falls in, or None past the last printed slab."""
        if km < 1:
            return None
        starts = self.starts
        index = bisect.bisect_right(starts, km) - 1
        if index < 0:
            return None
        slab = self.slabs[index]
        return slab if km <= slab.end else None


class FareTable:
    """The whole fare table: its sections, and the rule for journeys past 5,000 km."""

    def __init__(
        self,
        sections: dict[str, Section],
        beyond: dict[str, dict[str, float]],
        *,
        source: str,
        effective: str,
    ) -> None:
        self._sections = sections
        self._beyond = beyond
        self.source = source
        self.effective = effective
        self._validate()

    # -- construction ----------------------------------------------------

    @classmethod
    def load(
        cls, table_path: Path | str = TABLE_PATH, beyond_path: Path | str = BEYOND_PATH
    ) -> FareTable:
        return _load(Path(table_path), Path(beyond_path))

    # -- reading ---------------------------------------------------------

    def sections(self) -> tuple[str, ...]:
        return tuple(self._sections)

    def classes(self, section: str) -> tuple[str, ...]:
        return self._section(section).classes

    def section(self, section: str) -> Section:
        return self._section(section)

    def base_fare(self, section: str, class_code: str, km: float) -> int | None:
        """The basic fare this table prints for a class at a distance.

        `None` means the circular does not price that journey: the class is not
        carried in that section, the slab carries a hole, or the distance is past
        the last printed slab and no increment rule covers the section.
        """
        printed = self._section(section)
        slab = printed.slab_for(km)
        if slab is not None:
            return slab.fare(class_code)
        if km <= 0:
            return None
        last = printed.slabs[-1]
        if km <= last.end:
            return None
        beyond_section = BEYOND_SECTION.get(section)
        if beyond_section is None:
            return None
        increments = self._beyond.get(beyond_section, {})
        key = _beyond_key(class_code)
        step = increments.get(key)
        if step is None:
            return None
        at_last = last.fare(class_code)
        if at_last is None:
            return None
        # One increment per 50 km slab entered past the last printed one.
        extra_slabs = -(-(km - last.end) // 50)
        return round(at_last + step * extra_slabs)

    def has_row(self, section: str, start: int) -> bool:
        printed = self._section(section)
        index = bisect.bisect_left(printed.starts, start)
        return index < len(printed.slabs) and printed.slabs[index].start == start

    def gaps(self) -> dict[str, tuple[tuple[int, int], ...]]:
        """Distance ranges the printed table covers with no row at all.

        A gap is not a bug to paper over: the extractor drops a row it cannot read
        rather than invent one, so a journey that lands in a gap is unpriced. The
        engine reports these so the gap is visible instead of invisible.
        """
        found: dict[str, tuple[tuple[int, int], ...]] = {}
        for name, section in self._sections.items():
            gaps: list[tuple[int, int]] = []
            previous_end = 0
            for slab in section.slabs:
                if slab.start > previous_end + 1:
                    gaps.append((previous_end + 1, slab.start - 1))
                previous_end = slab.end
            if gaps:
                found[name] = tuple(gaps)
        return found

    # -- internals -------------------------------------------------------

    def _section(self, name: str) -> Section:
        try:
            return self._sections[name]
        except KeyError:
            raise FareTableError(
                f"no such fare section {name!r}; the table has {sorted(self._sections)}"
            ) from None

    def _validate(self) -> None:
        """Refuse a table that contradicts what the circular promises.

        The extractor already enforces these while reading, and the shipped file is
        its output, but a hand edit to the JSON is a real possibility and a table
        that quietly breaks monotonicity would quote a cheaper fare for a longer
        journey without complaint.
        """
        for name, section in self._sections.items():
            previous_end = 0
            previous = dict.fromkeys(section.classes, 0)
            for slab in section.slabs:
                # Gaps are tolerated and reported by `gaps()`; a slab that overlaps
                # or precedes its neighbour is a different failure, because then two
                # rows claim the same distance and the answer depends on row order.
                if slab.start <= previous_end:
                    raise FareTableError(
                        f"{name}: slab {slab.start}-{slab.end} overlaps the previous "
                        f"row ending at {previous_end}"
                    )
                if slab.start > slab.end:
                    raise FareTableError(f"{name}: slab {slab.start}-{slab.end} is empty")
                for class_code in section.classes:
                    value = slab.fare(class_code)
                    if value is None:
                        continue
                    if value < previous[class_code]:
                        raise FareTableError(
                            f"{name} {class_code}: fare falls from {previous[class_code]} "
                            f"to {value} at slab {slab.start}-{slab.end}"
                        )
                    previous[class_code] = value
                previous_end = slab.end


def _beyond_key(class_code: str) -> str:
    """Map a season-split class code onto the single increment the page prints."""
    for suffix in ("_peak", "_lean"):
        if class_code.endswith(suffix):
            return class_code[: -len(suffix)]
    return class_code


@lru_cache(maxsize=4)
def _load(table_path: Path, beyond_path: Path) -> FareTable:
    raw: dict[str, Any] = json.loads(table_path.read_text())
    beyond_raw: dict[str, Any] = json.loads(beyond_path.read_text())
    sections = {
        name: Section(
            name=name,
            classes=tuple(body["classes"]),
            slabs=tuple(
                Slab(
                    start=int(row["start"]),
                    end=int(row["end"]),
                    fares={code: row.get(code) for code in body["classes"]},
                )
                for row in body["rows"]
            ),
        )
        for name, body in raw["sections"].items()
    }
    return FareTable(
        sections,
        beyond_raw["increments"],
        source=raw["source"],
        effective=raw["effective"],
    )
