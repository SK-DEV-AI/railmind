"""Import a railpull crawl into our timetable database.

railpull (`shwetankg07/railpull`) pulls the current Indian Railways timetable out of
NTES, the government enquiry service, one train at a time. That is the only free way
to have more than the handful of schedules the paid vendor gives us, and it produces
`data/out/schedules.jsonl`: one record per train with every stop, its run days, and
the exact dates it starts.

This script turns those records into rows in `data/timetable.db`. It does not
re-implement storage: it converts a railpull record into the same shape our vendor
adapter produces and hands it to `TimetableRepo.upsert_schedule`, so both sources
share one write path, one content hash, and one freshness timestamp. Two write paths
would drift, and the drift would be invisible.

Rules it follows, all of them learned from the data:

- **Run days are normalised, not trusted.** NTES says `Daily` or `Mon,Wed,Fri`; our
  contracts store `daily` or `mon,wed,fri`, because a search filters on weekday. A
  train whose days cannot be read is stored as `unknown` and never silently becomes
  daily.
- **Station coordinates are never overwritten with nothing.** railpull's
  `stations.csv` starts with empty lat/lon and is filled by an optional OSM step. An
  empty value there must not erase a coordinate the vendor already gave us.
- **Times are kept as strings in HH:MM.** NTES returns "06:40" and sometimes
  "06:40:00"; both become "06:40", because a seconds field that is always zero is a
  lie about precision.
- **Nothing is invented.** No class codes, no fares, no coordinates. A field railpull
  does not provide stays absent, exactly as with the vendor path.

Usage:
    python scripts/import_railpull.py --dry-run
    python scripts/import_railpull.py
    python scripts/import_railpull.py --limit 500 --stations

Exit codes: 0 imported, 1 failures, 2 nothing to import.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from railmind.data.timetable import DEFAULT_DB, TimetableRepo

#: Where a railpull checkout keeps its exported tables, relative to that checkout.
DEFAULT_INPUT = Path.home() / "session-root" / "repos" / "railpull" / "data" / "out"

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_run_days(value: str) -> list[str]:
    """`Daily`, `Mon,Wed,Fri`, or `Unknown` to the day names our schema stores."""
    text = (value or "").strip()
    if not text:
        return []
    if text.lower() == "daily":
        return list(WEEKDAYS)
    found = [part.strip()[:3].lower() for part in text.split(",") if part.strip()]
    return [day for day in found if day in WEEKDAYS]


def normalise_time(value: object) -> str | None:
    """`06:40`, `06:40:00`, `6:40` to `06:40`. None when there is no time."""
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def to_vendor_payload(record: dict) -> dict | None:
    """One railpull record in the shape `upsert_schedule` already understands."""
    number = str(record.get("number") or "").strip()
    stops = record.get("stops") or []
    if not number or len(stops) < 2:
        return None

    route = []
    for stop in stops:
        code = str(stop.get("code") or "").strip().upper()
        if not code:
            continue
        route.append(
            {
                "station": {
                    "code": code,
                    "name": str(stop.get("name") or code).strip(),
                    # Coordinates are absent here on purpose: NTES does not publish
                    # them, and the empty string in stations.csv is not a location.
                },
                "sequence": stop.get("seq") or len(route) + 1,
                "arrival": normalise_time(stop.get("arr")),
                "departure": normalise_time(stop.get("dep")),
                "arrivalDay": stop.get("day") or 1,
                "distance": stop.get("distance_km") or None,
            }
        )
    if len(route) < 2:
        return None

    origin = str(record.get("source_code") or "").strip() or route[0]["station"]["code"]
    destination = str(record.get("dest_code") or "").strip() or route[-1]["station"]["code"]

    return {
        "train": {
            "number": number,
            "name": str(record.get("name") or number).strip(),
            "type": str(record.get("type_label") or record.get("type") or "").strip() or None,
            "runDays": parse_run_days(str(record.get("runs_days") or "")),
            "source": {"code": origin},
            "destination": {"code": destination},
            "distance": record.get("distance_km") or None,
        },
        "route": route,
    }


def read_records(path: Path) -> list[dict]:
    """The exported JSON lines, skipping blank and unparseable ones."""
    records: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                records.append(json.loads(text))
            except json.JSONDecodeError:
                continue
    return records


def import_stations(repo: TimetableRepo, path: Path) -> int:
    """Fill coordinates from the OSM step, without erasing what we already have."""
    if not path.exists():
        return 0
    updated = 0
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            code = (row.get("code") or "").strip().upper()
            lat = (row.get("lat") or "").strip()
            lon = (row.get("lon") or "").strip()
            if not code or not lat or not lon:
                continue
            try:
                repo.set_station_coords(code, float(lat), float(lon))
            except ValueError:
                continue
            updated += 1
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="railpull data/out")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=None, help="import at most N trains")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument(
        "--stations",
        action="store_true",
        help="also import station coordinates from stations.csv, if the OSM step ran",
    )
    args = parser.parse_args(argv)

    schedules = args.input / "schedules.jsonl"
    if not schedules.exists():
        print(f"nothing to import: {schedules} does not exist")
        print("run the railpull crawl, then transform/export.py")
        return 2

    records = read_records(schedules)
    if not records:
        print(f"{schedules} is empty")
        return 2
    if args.limit is not None:
        records = records[: args.limit]

    fetched_at = dt.datetime.fromtimestamp(schedules.stat().st_mtime, tz=dt.UTC)
    counts = {"new": 0, "changed": 0, "same": 0, "skipped": 0, "failed": 0}
    unknown_days: list[str] = []

    if args.dry_run:
        # A dry run must not even create the database file, so nothing is opened.
        outcomes = [to_vendor_payload(record) for record in records]
        readable = sum(1 for payload in outcomes if payload and payload["train"]["runDays"])
        print(f"records: {len(records)}  would import: {readable}")
        print("dry run: nothing written")
        return 0

    repo = TimetableRepo(args.db)

    try:
        for record in records:
            payload = to_vendor_payload(record)
            if payload is None:
                counts["skipped"] += 1
                continue
            if not payload["train"]["runDays"]:
                # Skipping is visible in the counts. Assuming "daily" would quietly
                # put a twice-a-week special into every search result.
                counts["skipped"] += 1
                unknown_days.append(str(record.get("number")))
                continue
            try:
                written = repo.upsert_schedule(payload, fetched_at=fetched_at)
            except (ValueError, KeyError) as exc:
                counts["failed"] += 1
                print(f"  {record.get('number')}: {exc}")
                continue
            counts[written.outcome] = counts.get(written.outcome, 0) + 1

        if args.stations:
            updated = import_stations(repo, args.input / "stations.csv")
            print(f"station coordinates updated: {updated}")
    finally:
        report = repo.staleness()
        repo.close()

    print(f"records: {len(records)}  {counts}")
    if unknown_days:
        print(f"skipped {len(unknown_days)} with unreadable run days: {unknown_days[:10]}")
    print(f"timetable now: {report}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
