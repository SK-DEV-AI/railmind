#!/usr/bin/env python3
"""Build data/timetable.db from recorded vendor responses. No network.

The database is a build artifact, not source: it is gitignored and reproducible
from the fixtures in one command. That is deliberate. If the demo machine loses it,
`python3 scripts/seed_timetable.py` rebuilds 13,010 stations and every seeded route
in under a second, with the wifi off.

Usage:
    python3 scripts/seed_timetable.py
    python3 scripts/seed_timetable.py --fixtures tests/fixtures/railradar --db data/timetable.db
    python3 scripts/seed_timetable.py --sample CSMT PUNE      # query what you just built
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from railmind.data.timetable import DEFAULT_DB, TimetableRepo, seed_from_fixtures


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/railradar"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--sample",
        nargs=2,
        metavar=("FROM", "TO"),
        help="after seeding, print the trains found between two station codes",
    )
    args = parser.parse_args()

    if not args.fixtures.exists():
        print(f"FAIL no fixtures at {args.fixtures}", file=sys.stderr)
        return 2

    repo = TimetableRepo(args.db)
    seeded = seed_from_fixtures(repo, args.fixtures)
    counts = repo.counts()
    print(f"seeded from {args.fixtures}:")
    print(f"  stations ingested: {seeded['stations']}")
    print(f"  schedules ingested: {seeded['trains']}")
    print(
        f"database {args.db}: {counts['stations']} stations, "
        f"{counts['trains']} trains, {counts['stops']} stops"
    )

    if args.sample:
        origin, destination = (code.upper() for code in args.sample)
        options = repo.trains_between(origin, destination)
        print(f"\n{origin} -> {destination}: {len(options)} trains")
        for option in options[:10]:
            print(
                f"  {option.train_number} {option.train_name[:38]:38} "
                f"{option.departure:>5} -> {option.arrival:>5} "
                f"(day {option.arrival_day}) {option.distance_km:6.1f} km  [{option.run_days}]"
            )

    repo.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
