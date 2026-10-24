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
    lon REAL
);

CREATE TABLE IF NOT EXISTS trains (
    number TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT,
    run_days TEXT NOT NULL,          -- "daily" or "mon,tue,fri"
    source_code TEXT,
    dest_code TEXT,
    distance_km REAL,
    classes TEXT                     -- comma separated, e.g. "1A,2A,3A"
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
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- ingestion --

    def upsert_stations(self, stations: dict[str, str]) -> int:
        """Bulk insert `{code: name}`. Coordinates arrive later, with the routes."""
        rows = [(code, name) for code, name in stations.items()]
        self._conn.executemany(
            "INSERT INTO stations (code, name) VALUES (?, ?) "
            "ON CONFLICT(code) DO UPDATE SET name = excluded.name",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def upsert_schedule(self, payload: dict) -> str:
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

        run_days = train.get("runDays") or []
        normalised_days = (
            "daily" if len(run_days) == 7 else ",".join(str(day).lower() for day in run_days)
        )
        source = (train.get("source") or {}).get("code")
        destination = (train.get("destination") or {}).get("code")
        classes = train.get("classes") or train.get("availableClasses") or []

        self._conn.execute(
            "INSERT INTO trains (number, name, type, run_days, source_code, dest_code, "
            "distance_km, classes) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(number) DO UPDATE SET name = excluded.name, type = excluded.type, "
            "run_days = excluded.run_days, source_code = excluded.source_code, "
            "dest_code = excluded.dest_code, distance_km = excluded.distance_km, "
            "classes = excluded.classes",
            (
                number,
                train.get("name") or number,
                train.get("type"),
                normalised_days,
                source,
                destination,
                train.get("distance"),
                ",".join(str(c) for c in classes),
            ),
        )

        self._conn.execute("DELETE FROM stops WHERE train_number = ?", (number,))
        stop_rows = []
        for stop in route:
            station = stop.get("station") or {}
            code = str(station.get("code") or "").strip()
            if not code:
                continue
            self._conn.execute(
                "INSERT INTO stations (code, name, lat, lon) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(code) DO UPDATE SET name = excluded.name, "
                "lat = COALESCE(excluded.lat, stations.lat), "
                "lon = COALESCE(excluded.lon, stations.lon)",
                (code, station.get("name") or code, station.get("lat"), station.get("lng")),
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
        return number

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
            WHERE a.station_code = ? AND b.station_code = ?
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

    def counts(self) -> dict[str, int]:
        one = self._conn.execute("SELECT COUNT(*) AS n FROM trains").fetchone()["n"]
        two = self._conn.execute("SELECT COUNT(*) AS n FROM stops").fetchone()["n"]
        three = self._conn.execute("SELECT COUNT(*) AS n FROM stations").fetchone()["n"]
        return {"trains": one, "stops": two, "stations": three}


def seed_from_fixtures(repo: TimetableRepo, fixtures_dir: str | Path) -> dict[str, int]:
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
        repo.upsert_schedule(payload.get("data") or {})

    return seeded
