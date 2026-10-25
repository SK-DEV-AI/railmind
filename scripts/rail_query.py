#!/usr/bin/env python3
"""Ask the real agent for real trains, from the command line.

This is the Step 2 acceptance check before the API exists: a real corridor, real
timetable rows, an optional real fare, and a real IRCTC handoff link.

Examples
--------
    python3 scripts/rail_query.py CSMT PUNE 2026-10-26
    python3 scripts/rail_query.py CSMT PUNE 2026-10-26 --price --handoff
    python3 scripts/rail_query.py CSMT NDLS 2026-10-26 --limit 3

`--price` costs one data request per finalist, so it is opt-in. A plain search
costs zero requests when the corridor is seeded.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from railmind.core.errors import ProviderError
from railmind.core.types import Query, format_inr
from railmind.engine.ranking import rank
from railmind.providers.rail_india import DEFAULT_CLASS_ORDER, _stop


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env reader, so the CLI works without exporting anything by hand."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def duration_text(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours}h{minutes:02d}m"


async def run(args: argparse.Namespace) -> int:
    load_dotenv()
    from railmind.wiring import build_rail_provider

    provider = build_rail_provider(db_path=args.db)
    query = Query(
        origin=_stop(args.origin.upper(), args.origin.upper(), None, None),
        destination=_stop(args.destination.upper(), args.destination.upper(), None, None),
        service_date=args.date,
        classes=(args.travel_class,) if args.travel_class else (),
    )

    try:
        itineraries = rank(await provider.search(query)).itineraries
    except ProviderError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1

    weekday = args.date.strftime("%a")
    print(f"{args.origin.upper()} -> {args.destination.upper()}  {args.date} ({weekday})")
    print(f"{len(itineraries)} train(s) from the local timetable\n")

    if not itineraries:
        print("Nothing seeded for this corridor. Seed more routes with")
        print(
            "  python3 scripts/railradar_probe.py /v1/trains/<number> "
            "--out tests/fixtures/railradar/train_<number>.json"
        )
        print("  python3 scripts/seed_timetable.py")
        return 0

    for itinerary in itineraries[: args.limit]:
        leg = itinerary.legs[0]
        service = leg.service
        number = service.service_id if service else "?"
        name = (service.name if service else "")[:40]
        print(
            f"{itinerary.rank:>2}. {number} {name:40} "
            f"{leg.depart_at.strftime('%H:%M')} -> {leg.arrive_at.strftime('%H:%M')} "
            f"(+{leg.arrive_at.day - leg.depart_at.day}d) "
            f"{duration_text(itinerary.total_duration_s):>7} "
            f"{leg.distance_km or 0:6.1f} km"
        )

        if args.price:
            print(f"    class {args.travel_class or DEFAULT_CLASS_ORDER[1]}: ", end="")
            try:
                money = await provider.price(itinerary)
                print(format_inr(money))
            except ProviderError as exc:
                print(f"unavailable ({exc.to_problem().code.value})")

        if args.handoff:
            handoff = await provider.handoff(itinerary, [])
            print(f"    book yourself: {handoff.url}")
            print(f"    steps: {', '.join(s.requires.value for s in handoff.steps)}")

    print("\nwarnings on the first option:")
    for warning in itineraries[0].warnings:
        print(f"  - {warning}")

    if provider.client.api_key:
        print("\ndata requests this run: see the ledger at data/railradar-usage.json")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("origin", help="origin station code, e.g. CSMT")
    parser.add_argument("destination", help="destination station code, e.g. PUNE")
    parser.add_argument("date", help="travel date, YYYY-MM-DD")
    parser.add_argument(
        "--travel-class", default="", help="3A, SL, 2A ... (default: 3A for pricing)"
    )
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument(
        "--price", action="store_true", help="fetch a real fare (costs 1 request per train)"
    )
    parser.add_argument("--handoff", action="store_true", help="print the IRCTC link and its steps")
    parser.add_argument("--db", type=Path, default=None, help="timetable database path")
    args = parser.parse_args()

    try:
        args.date = dt.date.fromisoformat(args.date)
    except ValueError:
        print(f"FAIL not a date: {args.date}", file=sys.stderr)
        return 2

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
