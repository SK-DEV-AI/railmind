"""Refresh stored schedules that have gone stale, cheapest first.

This is the answer to "does the timetable rot, and how do we keep it current". The
policy lives in `railmind/data/freshness.py`; this script applies it.

Why a rolling refresh rather than a nightly full crawl:

- The free tier allows 100 requests a month. A full re-crawl of 9,300 trains is
  therefore not a schedule, it is a fantasy. A rolling sweep of what is actually
  stale costs a handful of requests.
- Schedules change a few times a year, at timetable revisions. Re-fetching a train
  that changed nothing spends quota to learn nothing, so every refresh reports
  new, changed, or same, and "same" is the normal answer.
- A corridor nobody searches does not need to be current. `--corridor` refreshes
  only the trains on a route, which is what a search for that route needs.

Usage:
    python scripts/refresh_timetable.py                 # dry run, shows what is due
    python scripts/refresh_timetable.py --limit 5 --apply
    python scripts/refresh_timetable.py --corridor CSMT PUNE --apply

Exit codes: 0 nothing to do or everything refreshed, 1 something failed, 2 quota
or configuration refused the run.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from railmind.core.errors import ProviderError
from railmind.data.freshness import SCHEDULE_MAX_AGE_DAYS
from railmind.data.timetable import TimetableRepo


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--older-than-days",
        type=float,
        default=SCHEDULE_MAX_AGE_DAYS,
        help=f"refresh schedules older than this (default {SCHEDULE_MAX_AGE_DAYS})",
    )
    parser.add_argument("--limit", type=int, default=None, help="stop after this many trains")
    parser.add_argument(
        "--corridor",
        nargs=2,
        metavar=("ORIGIN", "DESTINATION"),
        help="only refresh trains that serve this corridor",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually call the vendor; without this the run only reports",
    )
    parser.add_argument("--db", default=None, help="timetable database path")
    return parser.parse_args(argv)


def corridor_trains(repo: TimetableRepo, origin: str, destination: str) -> list[str]:
    return [option.train_number for option in repo.trains_between(origin, destination)]


async def refresh(numbers: list[str], repo: TimetableRepo) -> dict[str, int]:
    """Fetch and store each schedule. Returns counts by outcome, plus failures."""
    from railmind.wiring import build_rail_provider

    provider = build_rail_provider()
    counts = {"new": 0, "changed": 0, "same": 0, "failed": 0}

    for number in numbers:
        try:
            payload = await provider.client.train_schedule(number)
        except ProviderError as exc:
            counts["failed"] += 1
            print(f"  {number}: FAILED {exc}")
            if exc.code.value in {"rate_limited", "quota_exhausted"}:
                print("  stopping: the vendor will not take more requests today")
                break
            continue

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            counts["failed"] += 1
            print(f"  {number}: FAILED unexpected payload shape")
            continue

        written = repo.upsert_schedule(data)
        counts[written.outcome] = counts.get(written.outcome, 0) + 1
        print(f"  {number}: {written.outcome}")
    return counts


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv()
    repo = TimetableRepo(args.db) if args.db else TimetableRepo()

    before = repo.staleness()
    print(f"timetable: {before}")

    numbers = repo.due_for_refresh(older_than_days=args.older_than_days, limit=args.limit)
    if args.corridor:
        wanted = set(corridor_trains(repo, args.corridor[0], args.corridor[1]))
        if not wanted:
            print(f"no trains seeded for {args.corridor[0]} -> {args.corridor[1]}")
            return 0
        numbers = [number for number in numbers if number in wanted]

    if not numbers:
        print(f"nothing due: every schedule is younger than {args.older_than_days} days")
        return 0

    print(f"{len(numbers)} schedule(s) due, oldest first: {', '.join(numbers[:10])}")
    if not args.apply:
        print("dry run. Add --apply to fetch. Each train costs one vendor request.")
        return 0

    counts = asyncio.run(refresh(numbers, repo))
    print(f"result: {counts}")
    print(f"after: {repo.staleness()}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
