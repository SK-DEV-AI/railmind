"""Freshness tests: the policy, the migration, and the "nothing changed" case.

The interesting assertions are the ones that stop a refresh from lying to itself:
an unknown timestamp must count as stale, and re-fetching an identical schedule
must report "same" rather than looking like an update.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3

import pytest

from railmind.data.freshness import (
    SCHEDULE_MAX_AGE_DAYS,
    STATIC_MAX_AGE_DAYS,
    age_of,
)
from railmind.data.timetable import TimetableRepo

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "railradar"


def schedule(number: str = "12951") -> dict:
    return json.loads((FIXTURES / f"train_{number}.json").read_text())["data"]


@pytest.fixture
def repo(tmp_path):
    database = TimetableRepo(tmp_path / "timetable.db")
    yield database
    database.close()


def test_a_missing_timestamp_counts_as_stale_not_fresh():
    age = age_of(None)

    assert age.stale is True
    assert "unknown" in age.describe()


def test_a_fresh_schedule_is_fresh_and_a_31_day_old_one_is_not():
    now = dt.datetime(2026, 10, 25, tzinfo=dt.UTC)

    fresh = age_of(now - dt.timedelta(days=5), now=now)
    stale = age_of(now - dt.timedelta(days=SCHEDULE_MAX_AGE_DAYS + 1), now=now)

    assert fresh.stale is False
    assert stale.stale is True
    assert "days" in stale.describe()


def test_each_tier_has_its_own_limit():
    now = dt.datetime(2026, 10, 25, tzinfo=dt.UTC)
    forty_days = now - dt.timedelta(days=40)

    assert age_of(forty_days, now=now, tier="schedule").stale is True
    assert age_of(forty_days, now=now, tier="static").stale is False
    assert STATIC_MAX_AGE_DAYS > SCHEDULE_MAX_AGE_DAYS


def test_an_unknown_tier_is_refused():
    with pytest.raises(ValueError, match="unknown freshness tier"):
        age_of(dt.datetime.now(dt.UTC), tier="whatever")


def test_ingest_stamps_the_time_and_reports_new(repo):
    written = repo.upsert_schedule(schedule())

    assert written.number == "12951"
    assert written.outcome == "new"
    assert repo.fetched_at("12951") is not None
    assert repo.schedule_age("12951").stale is False


def test_the_same_schedule_twice_reports_same_not_changed(repo):
    """A refresh that learns nothing must not look like work in the log."""
    repo.upsert_schedule(schedule())
    again = repo.upsert_schedule(schedule())

    assert again.outcome == "same"


def test_a_changed_schedule_is_detected_by_the_hash(repo):
    payload = schedule()
    repo.upsert_schedule(payload)

    payload["route"][1]["departure"] = "23:59"
    changed = repo.upsert_schedule(payload)

    assert changed.outcome == "changed"


def test_a_train_with_no_timestamp_is_always_due_and_sorted_first(repo):
    repo.upsert_schedule(schedule("12951"))
    repo._conn.execute("UPDATE trains SET fetched_at = NULL WHERE number = '12951'")
    repo._conn.commit()

    due = repo.due_for_refresh()

    assert due[0] == "12951"


def test_due_ordering_is_oldest_first(repo):
    now = dt.datetime(2026, 10, 25, tzinfo=dt.UTC)
    repo.upsert_schedule(schedule("12951"))
    repo.upsert_schedule(schedule("12127"))

    repo._conn.execute(
        "UPDATE trains SET fetched_at = ? WHERE number = '12951'",
        ((now - dt.timedelta(days=90)).isoformat(),),
    )
    repo._conn.execute(
        "UPDATE trains SET fetched_at = ? WHERE number = '12127'",
        ((now - dt.timedelta(days=40)).isoformat(),),
    )
    repo._conn.commit()

    assert repo.due_for_refresh(now=now) == ["12951", "12127"]
    assert repo.due_for_refresh(now=now, limit=1) == ["12951"]
    assert repo.due_for_refresh(now=now, older_than_days=100) == []


def test_the_staleness_report_answers_the_question_in_one_line(repo):
    repo.upsert_schedule(schedule())

    report = repo.staleness()

    assert report["trains"] == 1
    assert report["missing_timestamp"] == 0
    assert report["due"] == 0
    assert report["newest_days"] is not None
    assert report["policy_days"] == SCHEDULE_MAX_AGE_DAYS


def test_an_old_database_gains_the_new_columns_instead_of_refusing_to_open(tmp_path):
    """A demo machine may hold a database created before freshness existed."""
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE stations (code TEXT PRIMARY KEY, name TEXT NOT NULL, lat REAL, lon REAL);
        CREATE TABLE trains (
            number TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT, run_days TEXT NOT NULL,
            source_code TEXT, dest_code TEXT, distance_km REAL, classes TEXT
        );
        CREATE TABLE stops (
            train_number TEXT NOT NULL, sequence INTEGER NOT NULL, station_code TEXT NOT NULL,
            arrival TEXT, departure TEXT, day INTEGER NOT NULL DEFAULT 1, distance_km REAL,
            PRIMARY KEY (train_number, sequence)
        );
        """
    )
    legacy.execute(
        "INSERT INTO trains (number, name, run_days) VALUES ('12951', 'Rajdhani', 'daily')"
    )
    legacy.commit()
    legacy.close()

    repo = TimetableRepo(path)
    try:
        columns = {
            row["name"] for row in repo._conn.execute("PRAGMA table_info(trains)").fetchall()
        }
        assert {"fetched_at", "content_hash"} <= columns
        # The legacy row is old by definition, so it is due for a refresh.
        assert repo.due_for_refresh() == ["12951"]
    finally:
        repo.close()


def test_derived_lookups_are_cached_and_invalidated_by_new_stops(repo):
    """The cache must not outlive the data it describes."""
    repo.upsert_schedule(schedule("12951"))
    first = repo.station_traffic()
    second = repo.station_traffic()
    assert second == first
    assert repo._derived["station_traffic"][1] == first

    # A different train adds stops, so the cache must be rebuilt.
    repo.upsert_schedule(schedule("12127"))
    third = repo.station_traffic()

    assert third != first
    assert repo._derived["station_traffic"][1] == third
