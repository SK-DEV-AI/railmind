"""Legacy station codes must resolve to the code trains actually stop at.

The failure this guards against is silent: IRCTC reissued codes (BCT -> MMCT), the
station directory keeps both rows, and the abandoned row answers "no trains" to
every query. A user reads that as "this route does not exist", which is a wrong
answer about the world, not a missing row.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from railmind.data.timetable import TimetableRepo

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "railradar"


def schedule(number: str = "12951") -> dict:
    return json.loads((FIXTURES / f"train_{number}.json").read_text())["data"]


@pytest.fixture
def repo(tmp_path):
    database = TimetableRepo(tmp_path / "timetable.db")
    yield database
    database.close()


def test_the_old_mumbai_central_code_finds_the_new_ones_trains(repo):
    """12951 stops at MMCT. BCT is the same station under its retired code."""
    repo.upsert_stations({"BCT": "Mumbai Central"})
    repo.upsert_schedule(schedule("12951"))

    assert repo.resolve_code("BCT") == "MMCT"
    assert repo.resolve_code("bct") == "MMCT"
    assert len(repo.trains_between("BCT", "NDLS")) == len(repo.trains_between("MMCT", "NDLS"))
    assert repo.trains_between("BCT", "NDLS")


def test_a_legacy_code_reports_the_served_station_not_the_empty_row(repo):
    """The coordinates live on the served row; returning the retired one hides them."""
    repo.upsert_stations({"BCT": "Mumbai Central"})
    repo.upsert_schedule(schedule("12951"))

    row = repo.station("BCT")

    assert row is not None
    assert row.code == "MMCT"
    assert row.lat is not None


def test_a_name_two_served_stations_share_is_left_alone(repo):
    """Two live stations with one name: neither may be remapped onto the other."""
    repo.upsert_stations({"AAA": "Ambiguous Road", "BBB": "Ambiguous Road"})
    first, second = schedule("12951"), schedule("12127")
    first["route"][0]["station"] = {"code": "AAA", "name": "Ambiguous Road"}
    second["route"][0]["station"] = {"code": "BBB", "name": "Ambiguous Road"}
    repo.upsert_schedule(first)
    repo.upsert_schedule(second)

    aliases = repo.station_aliases()

    assert "AAA" not in aliases and "BBB" not in aliases
    assert repo.resolve_code("AAA") == "AAA"


def test_an_unserved_code_points_at_the_busiest_of_its_namesakes(repo):
    """BCT is real: Mumbai Central is MMCT (mainline) and BCL (suburban) at once."""
    repo.upsert_stations({"OLD": "Split Station"})
    # Two trains call at MAIN, one at SUB, so MAIN is the busier namesake.
    for number, code in (("12951", "MAIN"), ("12127", "MAIN"), ("12137", "SUB")):
        payload = schedule(number)
        payload["route"][0]["station"] = {"code": code, "name": "Split Station"}
        repo.upsert_schedule(payload)

    aliases = repo.station_aliases()

    assert aliases["OLD"] == "MAIN"
    assert "SUB" not in aliases and "MAIN" not in aliases


def test_a_served_code_is_never_remapped(repo):
    """An alias must never point at a station whose own trains would then vanish."""
    repo.upsert_stations({"BCT": "Mumbai Central"})
    repo.upsert_schedule(schedule("12951"))

    aliases = repo.station_aliases()

    assert "MMCT" not in aliases
    assert aliases == {"BCT": "MMCT"}


def test_an_unknown_code_is_passed_through_untouched(repo):
    repo.upsert_schedule(schedule("12951"))

    assert repo.resolve_code("ZZTOP") == "ZZTOP"
    assert repo.resolve_code("") == ""
    assert repo.trains_between("ZZTOP", "NDLS") == []


def test_aliases_are_cached_and_follow_new_data(repo):
    """Same invalidation rule as the other derived lookups: the data stamp."""
    repo.upsert_stations({"BCT": "Mumbai Central"})
    first = repo.station_aliases()

    repo.upsert_schedule(schedule("12951"))
    second = repo.station_aliases()

    assert first == {}
    assert second == {"BCT": "MMCT"}
    assert repo._derived["station_aliases"][1] == second


def test_a_rename_over_the_same_rows_still_invalidates(repo):
    """A station-directory refresh changes names, not row counts. It must not be cached."""
    repo.upsert_stations({"BCT": "Mumbai Central"})
    repo.upsert_schedule(schedule("12951"))
    assert repo.station_aliases() == {"BCT": "MMCT"}

    repo.upsert_stations({"BCT": "Bombay Central"})

    assert repo.station_aliases() == {}
