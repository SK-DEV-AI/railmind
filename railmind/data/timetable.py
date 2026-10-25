"""Offline timetable: SQLite seeded from real data, queried with no network.

Why a local database at all: the free data plan gives 100 requests a month. A
search that costs a request cannot survive a second visitor, and a demo must run
with the network unplugged. So the timetable lives on disk, built once from
recorded real responses, and only volatile things (fare, live position) ever go
over the wire.

What is real here: 13,010 station codes and names, and full routes for seeded
trains, every stop with its own coordinates, arrival, departure, day number and
cumulative distance. Nothing is invented, and nothing is estimated.

SQLite calls are synchronous on purpose. A local indexed lookup is microseconds,
so a thread hop would cost more than it saves, and the alternative is an async
wrapper that pretends to be non-blocking while holding the GIL anyway.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB = Path("data/timetable.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    lat REAL,
    lon REAL,
    fetched_at TEXT,
    name_source TEXT                 -- 'vendor' or 'ntes'; see NAME_SOURCE_PRIORITY
);

CREATE TABLE IF NOT EXISTS trains (
    number TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT,
    run_days TEXT NOT NULL,          -- "daily" or "mon,tue,fri"
    source_code TEXT,
    dest_code TEXT,
    distance_km REAL,
    classes TEXT,                    -- comma separated, e.g. "1A,2A,3A"
    fetched_at TEXT,                 -- ISO-8601 UTC, last successful refresh
    content_hash TEXT                -- hash of the schedule we stored, so a
                                     -- refresh can say "unchanged" rather than
                                     -- rewriting every row and calling it work
);

CREATE TABLE IF NOT EXISTS stops (
    train_number TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    station_code TEXT NOT NULL,
    arrival TEXT,                    -- "HH:MM", NULL at the origin
    departure TEXT,                  -- "HH:MM", NULL at the terminus
    day INTEGER NOT NULL DEFAULT 1,
    distance_km REAL,
    PRIMARY KEY (train_number, sequence)
);

CREATE INDEX IF NOT EXISTS idx_stops_station ON stops (station_code);
CREATE INDEX IF NOT EXISTS idx_stops_train ON stops (train_number);
"""

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True, slots=True)
class TripOption:
    """One train that runs a requested origin to destination on the seeded data."""

    train_number: str
    train_name: str
    train_type: str | None
    run_days: str
    classes: tuple[str, ...]

    origin_code: str
    origin_name: str
    origin_lat: float | None
    origin_lon: float | None
    departure: str
    departure_day: int
    origin_distance_km: float

    destination_code: str
    destination_name: str
    destination_lat: float | None
    destination_lon: float | None
    arrival: str
    arrival_day: int
    destination_distance_km: float

    @property
    def distance_km(self) -> float:
        return max(0.0, self.destination_distance_km - self.origin_distance_km)

    def runs_on(self, weekday: int) -> bool:
        """`weekday` is 0 = Monday. Dates in the past are the caller's problem."""
        if self.run_days.strip().lower() == "daily":
            return True
        return WEEKDAYS[weekday] in {part.strip() for part in self.run_days.split(",")}

    # Two sources give station names and they disagree: the vendor writes
    # "Mumbai CSMT", NTES writes "C SHIVAJI MAH T" because it truncates. Merging
    # blindly replaced 13,010 readable names with truncated ones and, worse, broke
    # name-based station lookup: "mumbai" stopped matching CSMT entirely.
    # Length is the wrong test (the truncated name is often the longer string), so
    # names carry their source and a higher-priority source wins.


#: Where a station's display name came from, highest priority first. The vendor
#: directory is written for humans; NTES truncates to fit a column.
NAME_SOURCE_PRIORITY: dict[str, int] = {"vendor": 3, "ntes": 1}


@dataclass(frozen=True, slots=True)
class ScheduleWrite:
    """What a schedule ingest did: created, replaced, or found nothing new."""

    number: str
    outcome: str
    content_hash: str


def _schedule_hash(train: dict, route: list[dict]) -> str:
    """Canonical fingerprint of a schedule, stable across formatting differences."""
    stops = [
        [
            str(stop.get("station", {}).get("code") or ""),
            str(stop.get("sequence") or ""),
            str(stop.get("arrival") or ""),
            str(stop.get("departure") or ""),
            str(stop.get("arrivalDay") or stop.get("departureDay") or ""),
            str(stop.get("distance") or ""),
        ]
        for stop in route
    ]
    payload = {
        "name": str(train.get("name") or ""),
        "type": str(train.get("type") or ""),
        "runDays": [str(day).lower() for day in (train.get("runDays") or [])],
        "distance": str(train.get("distance") or ""),
        "stops": stops,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _parse_moment(text: object) -> dt.datetime | None:
    """An ISO timestamp from the database, or None when absent or unreadable."""
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.UTC) if parsed.tzinfo is None else parsed


@dataclass(frozen=True, slots=True)
class StationRow:
    code: str
    name: str
    lat: float | None
    lon: float | None


class TimetableRepo:
    """Local, read-mostly timetable. Seeded by `scripts/seed_timetable.py`."""

    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Derived lookups that are O(stops) and are asked for on every intent parse.
        # Keyed by the total stop count, which changes whenever a schedule is written,
        # so the cache cannot go stale without invalidating itself.
        self._derived: dict[str, tuple[int, object]] = {}
        # `check_same_thread=False` because the ASGI server may call this from a
        # different thread than the one that opened it: FastAPI runs async
        # handlers on the event loop and the test client runs them in its own
        # thread. This build's sqlite3 is compiled threadsafe (serialized), so it
        # applies its own mutex; the only writer is `scripts/seed_timetable.py`,
        # which finishes before the server starts.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- schema migration --

    #: Columns added after the first version of the database. A demo machine may
    #: hold a database created before freshness existed, and refusing to open it
    #: would be a worse outcome than adding a column.
    _ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("trains", "fetched_at", "TEXT"),
        ("trains", "content_hash", "TEXT"),
        ("stations", "fetched_at", "TEXT"),
        ("stations", "name_source", "TEXT"),
    )

    def _migrate(self) -> None:
        for table, column, kind in self._ADDED_COLUMNS:
            existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    # -- freshness --

    def schedule_age(self, train_number: str, *, now: dt.datetime | None = None):
        """How old this train's stored schedule is, judged against policy."""
        from railmind.data.freshness import age_of

        return age_of(self.fetched_at(train_number), now=now, tier="schedule")

    def fetched_at(self, train_number: str) -> dt.datetime | None:
        row = self._conn.execute(
            "SELECT fetched_at FROM trains WHERE number = ?", (str(train_number),)
        ).fetchone()
        return _parse_moment(row["fetched_at"]) if row else None

    def due_for_refresh(
        self,
        *,
        older_than_days: float | None = None,
        limit: int | None = None,
        numbers: list[str] | None = None,
        now: dt.datetime | None = None,
    ) -> list[str]:
        """Train numbers whose schedule is older than the policy allows.

        Ordered oldest first, so a limited sweep spends its requests on the stalest
        rows. A missing timestamp sorts first, because an unknown age is the best
        reason there is to re-fetch.
        """
        from railmind.data.freshness import SCHEDULE_MAX_AGE_DAYS

        days = SCHEDULE_MAX_AGE_DAYS if older_than_days is None else older_than_days
        reference = now or dt.datetime.now(dt.UTC)
        rows = self._conn.execute("SELECT number, fetched_at FROM trains").fetchall()

        due: list[tuple[float, str]] = []
        for row in rows:
            number = str(row["number"])
            if numbers is not None and number not in numbers:
                continue
            moment = _parse_moment(row["fetched_at"])
            if moment is None:
                due.append((float("inf"), number))
                continue
            age_days = (reference - moment).total_seconds() / 86_400
            if age_days > days:
                due.append((age_days, number))

        due.sort(key=lambda item: (-item[0], item[1]))
        ordered = [number for _age, number in due]
        return ordered[:limit] if limit is not None else ordered

    def staleness(self, *, now: dt.datetime | None = None) -> dict[str, object]:
        """A report a human can read: how old the whole timetable is."""
        rows = self._conn.execute("SELECT number, fetched_at FROM trains").fetchall()
        moments = [
            moment for row in rows if (moment := _parse_moment(row["fetched_at"])) is not None
        ]
        reference = now or dt.datetime.now(dt.UTC)
        missing = len(rows) - len(moments)
        if not moments:
            return {
                "trains": len(rows),
                "missing_timestamp": missing,
                "oldest_days": None,
                "newest_days": None,
                "due": len(rows),
            }
        ages = sorted((reference - moment).total_seconds() / 86_400 for moment in moments)
        from railmind.data.freshness import SCHEDULE_MAX_AGE_DAYS

        return {
            "trains": len(rows),
            "missing_timestamp": missing,
            "oldest_days": round(ages[-1], 2),
            "newest_days": round(ages[0], 2),
            "due": len(self.due_for_refresh(now=now)) + missing,
            "policy_days": SCHEDULE_MAX_AGE_DAYS,
        }

    # -- ingestion --

    def upsert_stations(self, stations: dict[str, str], *, source: str = "vendor") -> int:
        """Bulk insert `{code: name}`. Coordinates arrive later, with the routes."""
        for code, name in stations.items():
            self._merge_station(code, name, None, None, source)
        self._conn.commit()
        return len(stations)

    def _merge_station(
        self,
        code: str,
        name: str | None,
        lat: float | None,
        lon: float | None,
        source: str,
    ) -> None:
        """Write one station, resolving a name conflict by source priority.

        Two sources name the same station differently: the vendor writes "Mumbai
        CSMT", NTES writes "C SHIVAJI MAH T" because it truncates to fit a column.
        Length is the wrong test (the truncated string is often the longer one), so
        each name records where it came from and the higher priority wins. A rename
        that loses information is not an improvement.
        """
        existing = self._conn.execute(
            "SELECT name, name_source FROM stations WHERE code = ?", (code,)
        ).fetchone()
        if existing is None:
            self._conn.execute(
                "INSERT INTO stations (code, name, lat, lon, name_source) VALUES (?, ?, ?, ?, ?)",
                (code, name or code, lat, lon, source),
            )
            return

        incoming = NAME_SOURCE_PRIORITY.get(source, 1)
        current = NAME_SOURCE_PRIORITY.get(str(existing["name_source"] or ""), 0)
        keep_current_name = current > incoming and bool(existing["name"])
        self._conn.execute(
            "UPDATE stations SET name = ?, lat = COALESCE(?, lat), lon = COALESCE(?, lon), "
            "name_source = ? WHERE code = ?",
            (
                existing["name"] if keep_current_name else (name or existing["name"]),
                lat,
                lon,
                existing["name_source"] if keep_current_name else source,
                code,
            ),
        )

    def upsert_schedule(
        self,
        payload: dict,
        *,
        fetched_at: dt.datetime | None = None,
        name_source: str = "vendor",
    ) -> str:
        """Ingest one `/v1/trains/{number}` response: train row plus every stop.

        Station coordinates are taken from the route, which is the only place the
        vendor gives them for free. Coordinates are never interpolated.
        """
        train = payload.get("train") or {}
        route = payload.get("route") or []
        number = str(train.get("number") or "").strip()
        if not number:
            raise ValueError("schedule payload has no train number")
        if not route:
            raise ValueError(f"schedule for {number} has no route")

        # The hash is over the parts a passenger could notice: times, halts,
        # distances, name, run days. A refresh that changes none of them must be able
        # to say so, otherwise every sweep looks like an update.
        content_hash = _schedule_hash(train, route)

        run_days = train.get("runDays") or []
        normalised_days = (
            "daily" if len(run_days) == 7 else ",".join(str(day).lower() for day in run_days)
        )
        source = (train.get("source") or {}).get("code")
        destination = (train.get("destination") or {}).get("code")
        classes = train.get("classes") or train.get("availableClasses") or []

        # `fetched_at` is the time the vendor was actually asked, which is not
        # always now: seeding from recorded fixtures must record when they were
        # captured, otherwise a rebuild would claim the data is fresher than it is.
        moment = fetched_at or dt.datetime.now(dt.UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=dt.UTC)
        fetched = moment.isoformat(timespec="seconds")
        previous = self._conn.execute(
            "SELECT content_hash FROM trains WHERE number = ?", (number,)
        ).fetchone()

        self._conn.execute(
            "INSERT INTO trains (number, name, type, run_days, source_code, dest_code, "
            "distance_km, classes, fetched_at, content_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(number) DO UPDATE SET name = excluded.name, type = excluded.type, "
            "run_days = excluded.run_days, source_code = excluded.source_code, "
            "dest_code = excluded.dest_code, distance_km = excluded.distance_km, "
            "classes = excluded.classes, fetched_at = excluded.fetched_at, "
            "content_hash = excluded.content_hash",
            (
                number,
                train.get("name") or number,
                train.get("type"),
                normalised_days,
                source,
                destination,
                train.get("distance"),
                ",".join(str(c) for c in classes),
                fetched,
                content_hash,
            ),
        )

        self._conn.execute("DELETE FROM stops WHERE train_number = ?", (number,))
        stop_rows = []
        for stop in route:
            station = stop.get("station") or {}
            code = str(station.get("code") or "").strip()
            if not code:
                continue
            self._merge_station(
                code,
                station.get("name"),
                station.get("lat"),
                station.get("lng"),
                name_source,
            )
            stop_rows.append(
                (
                    number,
                    int(stop.get("sequence") or 0),
                    code,
                    stop.get("arrival"),
                    stop.get("departure"),
                    int(stop.get("arrivalDay") or stop.get("departureDay") or 1),
                    stop.get("distance"),
                )
            )
        self._conn.executemany(
            "INSERT INTO stops (train_number, sequence, station_code, arrival, departure, "
            "day, distance_km) VALUES (?, ?, ?, ?, ?, ?, ?)",
            stop_rows,
        )
        self._conn.commit()

        if previous is None:
            outcome = "new"
        elif previous["content_hash"] == content_hash:
            outcome = "same"
        else:
            outcome = "changed"
        return ScheduleWrite(number=number, outcome=outcome, content_hash=content_hash)

    # -- queries --

    def trains_between(self, origin_code: str, destination_code: str) -> list[TripOption]:
        """Seeded trains that stop at both stations, origin first, in order."""
        query = """
            SELECT t.number, t.name, t.type, t.run_days, t.classes,
                   a.station_code AS origin_code, sa.name AS origin_name,
                   sa.lat AS origin_lat, sa.lon AS origin_lon,
                   a.departure AS departure, a.day AS departure_day,
                   a.distance_km AS origin_distance,
                   b.station_code AS destination_code, sb.name AS destination_name,
                   sb.lat AS destination_lat, sb.lon AS destination_lon,
                   b.arrival AS arrival, b.day AS arrival_day,
                   b.distance_km AS destination_distance
            FROM stops a
            JOIN stops b ON a.train_number = b.train_number AND b.sequence > a.sequence
            JOIN trains t ON t.number = a.train_number
            JOIN stations sa ON sa.code = a.station_code
            JOIN stations sb ON sb.code = b.station_code
            -- You cannot board where a train terminates, and cannot alight where it
            -- starts, so a segment needs a departure at the origin and an arrival at
            -- the destination. Without this the query offered the reverse direction
            -- of a train as if it were a real option, and one such row rejected the
            -- whole corridor downstream.
            WHERE a.station_code = ? AND b.station_code = ?
              AND a.departure IS NOT NULL AND a.departure <> ''
              AND b.arrival IS NOT NULL AND b.arrival <> ''
            ORDER BY a.departure
        """
        rows = self._conn.execute(query, (origin_code.upper(), destination_code.upper())).fetchall()

        options: list[TripOption] = []
        for row in rows:
            options.append(
                TripOption(
                    train_number=row["number"],
                    train_name=row["name"],
                    train_type=row["type"],
                    run_days=row["run_days"],
                    classes=tuple(c for c in (row["classes"] or "").split(",") if c),
                    origin_code=row["origin_code"],
                    origin_name=row["origin_name"],
                    origin_lat=row["origin_lat"],
                    origin_lon=row["origin_lon"],
                    departure=row["departure"] or "",
                    departure_day=int(row["departure_day"] or 1),
                    origin_distance_km=float(row["origin_distance"] or 0.0),
                    destination_code=row["destination_code"],
                    destination_name=row["destination_name"],
                    destination_lat=row["destination_lat"],
                    destination_lon=row["destination_lon"],
                    arrival=row["arrival"] or "",
                    arrival_day=int(row["arrival_day"] or 1),
                    destination_distance_km=float(row["destination_distance"] or 0.0),
                )
            )
        return options

    def set_station_coords(self, code: str, lat: float, lon: float) -> None:
        """Place a station on the map. Refuses nonsense and never erases good data.

        The OSM step is optional and its output starts empty, so this is called with
        real numbers only. An out-of-range value is refused rather than clamped:
        clamping would put a station in the sea and look like a working map.
        """
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            raise ValueError(f"coordinates out of range for {code}: {lat}, {lon}")
        self._conn.execute(
            "UPDATE stations SET lat = ?, lon = ?, fetched_at = ? WHERE code = ?",
            (
                float(lat),
                float(lon),
                dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                code.upper(),
            ),
        )
        self._conn.commit()

    def station(self, code: str) -> StationRow | None:
        row = self._conn.execute(
            "SELECT code, name, lat, lon FROM stations WHERE code = ?", (code.upper(),)
        ).fetchone()
        return StationRow(row["code"], row["name"], row["lat"], row["lon"]) if row else None

    def search_stations(self, text: str, limit: int = 10) -> list[StationRow]:
        """Prefix/substring match on code or name, for resolving 'Mumbai' to codes."""
        needle = f"%{text.strip().upper()}%"
        rows = self._conn.execute(
            "SELECT code, name, lat, lon FROM stations "
            "WHERE UPPER(code) LIKE ? OR UPPER(name) LIKE ? "
            "ORDER BY CASE WHEN UPPER(code) = ? THEN 0 ELSE 1 END, name LIMIT ?",
            (needle, needle, text.strip().upper(), limit),
        ).fetchall()
        return [StationRow(r["code"], r["name"], r["lat"], r["lon"]) for r in rows]

    def _cached(self, name: str, build):
        """Memoise an O(stops) lookup until the number of stops changes.

        With the full NTES timetable this table holds ~185,000 stops, and a station
        lookup that scans them costs milliseconds per call. `/intent` asks for traffic
        and served codes on every parse, so caching is the difference between a
        15 ms parse and a 150 ms one.
        """
        stamp = int(self._conn.execute("SELECT COUNT(*) AS n FROM stops").fetchone()["n"])
        cached = self._derived.get(name)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        value = build()
        self._derived[name] = (stamp, value)
        return value

    def stop_count(self, train_number: str) -> int:
        """How many stops we hold for a train. One of the model's input features."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM stops WHERE train_number = ?", (str(train_number),)
        ).fetchone()
        return int(row["n"])

    def served_codes(self) -> set[str]:
        """Stations at least one seeded train stops at.

        The vendor's station directory is not a list of bookable stations: it also
        contains loco sheds, military sidings and parcel offices. This set is the
        honest answer to "can a train actually take me there in what we hold", and
        the intent parser uses it to prefer a real terminus when a user types a
        city that has eight of them.
        """
        return {
            str(code).upper()
            for code in self._cached(
                "served_codes",
                lambda: [
                    row["station_code"]
                    for row in self._conn.execute(
                        "SELECT DISTINCT station_code FROM stops"
                    ).fetchall()
                ],
            )
        }

    def station_traffic(self) -> dict[str, int]:
        """How many seeded stop records a station appears in.

        A crude but real popularity signal: the station our trains use most is
        the one a traveller is most likely to mean when they type a city name.
        Crude is fine here, because the alternative is guessing a terminus from a
        hard-coded list, which would be wrong the moment the data changes.
        """
        return dict(
            self._cached(
                "station_traffic",
                lambda: {
                    str(row["station_code"]).upper(): int(row["n"])
                    for row in self._conn.execute(
                        "SELECT station_code, COUNT(*) AS n FROM stops GROUP BY station_code"
                    ).fetchall()
                },
            )
        )

    def counts(self) -> dict[str, int]:
        one = self._conn.execute("SELECT COUNT(*) AS n FROM trains").fetchone()["n"]
        two = self._conn.execute("SELECT COUNT(*) AS n FROM stops").fetchone()["n"]
        three = self._conn.execute("SELECT COUNT(*) AS n FROM stations").fetchone()["n"]
        return {"trains": one, "stops": two, "stations": three}


def seed_from_fixtures(
    repo: TimetableRepo,
    fixtures_dir: str | Path,
    *,
    fetched_at: dt.datetime | None = None,
) -> dict[str, int]:
    """Build the database from recorded vendor responses. No network involved.

    Fixture layout, all raw responses from the vendor, never edited:
      stations.json                  -> 13,010 station codes and names
      train_<number>.json            -> full schedule and route
      between_<from>_<to>.json       -> which trains run a corridor (used to pick
                                        which schedules are worth spending on)
    """
    fixtures = Path(fixtures_dir)
    seeded = {"stations": 0, "trains": 0}

    stations_file = fixtures / "stations.json"
    if stations_file.exists():
        payload = json.loads(stations_file.read_text())
        seeded["stations"] = repo.upsert_stations(payload.get("data") or {})

    for schedule_file in sorted(fixtures.glob("train_*.json")):
        payload = json.loads(schedule_file.read_text())
        if payload.get("success") is not True:
            continue
        seeded["trains"] = seeded["trains"] + 1
        repo.upsert_schedule(payload.get("data") or {}, fetched_at=fetched_at)

    return seeded
