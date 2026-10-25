"""Audit every corridor the database can answer, without touching the network.

Why this exists. Three of the defects found on 2026-10-25 were found by accident:
someone typed "delhi to chennai" and got an error. Luck is not a test strategy, and a
9,000-train timetable has far more corridors than anyone will type by hand. This
script walks a deterministic sample of them and reports what breaks, so a data problem
is a number in a report rather than a surprise during a demo.

It is deliberately offline. The provider is given a client that raises on any use, so
a corridor audit can never spend vendor quota and can never be blamed for a rate
limit. If the audit ever reports "network", the audit itself is broken.

What it measures per corridor:
  built      itineraries the adapter produced
  dropped    options it refused to build (malformed times) and skipped
  empty      corridors with local trains that still produced nothing
  error      an exception escaped, with its type and message

Exit code is 1 when any corridor errored. Dropped options are reported but do not
fail the run, because skipping a malformed schedule is the intended behaviour.

Usage:
    python scripts/audit_corridors.py                 # 200 corridors, seed 0
    python scripts/audit_corridors.py --sample 1000 --seed 7
    python scripts/audit_corridors.py --all           # every corridor in the database
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from railmind.core.types import Query
from railmind.data.timetable import DEFAULT_DB, TimetableRepo
from railmind.providers.rail_india import RailIndiaProvider, _stop


class NoNetwork:
    """Stands in for the vendor client. Any use is a bug in the audit."""

    async def trains_between(self, *args, **kwargs):  # pragma: no cover - guard
        raise AssertionError("the corridor audit must not call the vendor")

    async def train_schedule(self, *args, **kwargs):  # pragma: no cover - guard
        raise AssertionError("the corridor audit must not call the vendor")


@dataclass(frozen=True, slots=True)
class Outcome:
    origin: str
    destination: str
    built: int
    empty: bool
    error: str | None
    elapsed_ms: float


def corridors(repo: TimetableRepo) -> list[tuple[str, str]]:
    """Every ordered pair of stations that shares at least one train."""
    rows = repo._conn.execute(
        """
        SELECT DISTINCT a.station_code AS origin, b.station_code AS destination
        FROM stops a
        JOIN stops b ON a.train_number = b.train_number AND b.sequence > a.sequence
        WHERE a.departure IS NOT NULL AND a.departure <> ''
          AND b.arrival IS NOT NULL AND b.arrival <> ''
        """
    ).fetchall()
    return [(str(row["origin"]).upper(), str(row["destination"]).upper()) for row in rows]


def build_provider(repo: TimetableRepo) -> RailIndiaProvider:
    return RailIndiaProvider(client=NoNetwork(), repo=repo, seat_availability=False)


def make_query(origin: str, destination: str, day: dt.date) -> Query:
    return Query(
        origin=_stop(origin, origin, None, None),
        destination=_stop(destination, destination, None, None),
        service_date=day,
    )


async def audit(pairs: list[tuple[str, str]], repo: TimetableRepo, day: dt.date) -> list[Outcome]:
    provider = build_provider(repo)
    outcomes: list[Outcome] = []
    for origin, destination in pairs:
        started = time.perf_counter()
        try:
            itineraries = await provider.search(make_query(origin, destination, day))
            error = None
            built = len(itineraries)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            built = 0
        elapsed = (time.perf_counter() - started) * 1000
        outcomes.append(
            Outcome(
                origin=origin,
                destination=destination,
                built=built,
                empty=built == 0 and error is None,
                error=error,
                elapsed_ms=elapsed,
            )
        )
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--sample", type=int, default=200, help="how many corridors to test")
    parser.add_argument("--seed", type=int, default=0, help="sampling seed, for reproducibility")
    parser.add_argument("--all", action="store_true", help="audit every corridor, not a sample")
    parser.add_argument("--day-offset", type=int, default=1, help="service date, days from today")
    parser.add_argument("--show", type=int, default=10, help="how many problem corridors to print")
    args = parser.parse_args(argv)

    repo = TimetableRepo(args.db)
    try:
        everything = corridors(repo)
        if not everything:
            print("no corridors in the database; seed or import the timetable first")
            return 2
        if args.all:
            chosen = everything
        else:
            rng = random.Random(args.seed)
            chosen = rng.sample(everything, min(args.sample, len(everything)))

        day = dt.date.today() + dt.timedelta(days=args.day_offset)
        print(f"corridors in database: {len(everything)} | auditing: {len(chosen)} | date: {day}")

        started = time.perf_counter()
        outcomes = asyncio.run(audit(chosen, repo, day))
        wall = time.perf_counter() - started
    finally:
        repo.close()

    errors = [item for item in outcomes if item.error]
    empties = [item for item in outcomes if item.empty]
    built = [item for item in outcomes if not item.error and not item.empty]
    timings = sorted(item.elapsed_ms for item in outcomes)
    p95 = timings[int(0.95 * (len(timings) - 1))] if timings else 0.0

    print(
        f"built {len(built)} | empty {len(empties)} | errors {len(errors)} | "
        f"wall {wall:.2f}s | p95 {p95:.1f} ms per corridor"
    )

    if errors:
        kinds = Counter(item.error.split(":")[0] for item in errors)
        print("error kinds:", dict(kinds))
        for item in errors[: args.show]:
            print(f"  {item.origin}->{item.destination}: {item.error}")
    if empties:
        for item in empties[: args.show]:
            print(f"  empty: {item.origin}->{item.destination}")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
