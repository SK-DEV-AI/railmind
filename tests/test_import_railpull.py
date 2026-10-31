"""Importer tests: the conversion rules that decide what reaches the database.

The fixture is a hand-built railpull record, not a copy of a real export, so these
tests pin the rules rather than the current data.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

from railmind.data.timetable import TimetableRepo

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "import_railpull.py"


def load_module():
    spec = importlib.util.spec_from_file_location("import_railpull", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


importer = load_module()


def record(**overrides) -> dict:
    base = {
        "number": "12127",
        "name": "Pune Intercity SF Express",
        "type": "SUF",
        "type_label": "Superfast",
        "runs_days": "Daily",
        "source": "Mumbai CSMT",
        "destination": "Pune Jn",
        "distance_km": "190.7",
        "stops": [
            {
                "seq": 1,
                "code": "CSMT",
                "name": "Mumbai CSMT",
                "day": 1,
                "arr": "",
                "dep": "06:40",
                "distance_km": "0",
            },
            {
                "seq": 2,
                "code": "DR",
                "name": "Dadar",
                "day": 1,
                "arr": "06:52",
                "dep": "06:54",
                "distance_km": "9",
            },
            {
                "seq": 3,
                "code": "PUNE",
                "name": "Pune Jn",
                "day": 1,
                "arr": "10:00",
                "dep": "",
                "distance_km": "190.7",
            },
        ],
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Daily", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]),
        ("Mon,Wed,Fri", ["mon", "wed", "fri"]),
        ("Sat,Sun", ["sat", "sun"]),
        ("", []),
        ("Unknown", []),
        ("Mon,Funday", ["mon"]),
    ],
)
def test_run_days_are_normalised_to_the_schema(text, expected):
    assert importer.parse_run_days(text) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("06:40", "06:40"),
        ("6:40", "06:40"),
        ("06:40:00", "06:40"),
        ("23:59", "23:59"),
        ("", None),
        (None, None),
        ("25:00", None),
        ("nonsense", None),
    ],
)
def test_times_are_normalised_and_nonsense_is_dropped(raw, expected):
    assert importer.normalise_time(raw) == expected


def test_a_record_converts_to_the_shape_the_repo_already_writes():
    payload = importer.to_vendor_payload(record())

    assert payload is not None
    assert payload["train"]["number"] == "12127"
    assert payload["train"]["type"] == "Superfast"
    assert payload["train"]["source"] == {"code": "CSMT"}
    assert payload["train"]["destination"] == {"code": "PUNE"}
    assert [stop["station"]["code"] for stop in payload["route"]] == ["CSMT", "DR", "PUNE"]
    # The origin has no arrival and the terminus no departure, which is honest.
    assert payload["route"][0]["arrival"] is None
    assert payload["route"][-1]["departure"] is None


def test_ntes_coordinates_are_never_invented():
    payload = importer.to_vendor_payload(record())

    assert all("lat" not in stop["station"] for stop in payload["route"])


def test_a_single_stop_record_is_skipped():
    assert importer.to_vendor_payload(record(stops=record()["stops"][:1])) is None


def test_a_spliced_route_keeps_its_first_clean_run_and_says_so():
    """Real railpull records can carry two schedules glued together.

    `22159` is the live example: CSMT->MAS, then a second run that starts at CSMT
    again. Importing all 64 stops invents journeys such as MAS->CSMT on a train
    that never makes one, and lists the same train more than once for a real pair.
    The honest reading is that the first run is a whole train and the rest is damage.
    """
    first = record()["stops"]
    second = [dict(stop, seq=stop["seq"] + 3) for stop in first]
    counts: dict[str, int] = {"spliced": 0}

    payload = importer.to_vendor_payload(record(stops=first + second), counts=counts)

    assert payload is not None
    assert [stop["station"]["code"] for stop in payload["route"]] == ["CSMT", "DR", "PUNE"]
    assert payload["train"]["source"] == {"code": "CSMT"}
    assert payload["train"]["destination"] == {"code": "PUNE"}
    assert counts["spliced"] == 1


def test_a_route_that_revisits_another_station_is_kept_in_full():
    """Detours, reversals and circular specials are real; do not trim them.

    `11013` reverses through SBC, `17029` detours via HG, `00913` is a circular
    tourist special. Only a restart at the route's own origin is damage.
    """
    stops = [
        {"seq": 1, "code": "PRTN", "day": 1, "arr": "", "dep": "10:00"},
        {"seq": 2, "code": "BH", "day": 2, "arr": "11:00", "dep": "11:10"},
        {"seq": 3, "code": "ST", "day": 2, "arr": "13:00", "dep": "13:10"},
        {"seq": 4, "code": "KOAA", "day": 9, "arr": "09:00", "dep": "09:30"},
        {"seq": 5, "code": "ST", "day": 11, "arr": "12:00", "dep": "12:10"},
        {"seq": 6, "code": "BH", "day": 11, "arr": "14:00", "dep": "14:10"},
        {"seq": 7, "code": "PRTN", "day": 11, "arr": "15:00", "dep": ""},
    ]
    counts: dict[str, int] = {"spliced": 0}

    payload = importer.to_vendor_payload(record(number="00913", stops=stops), counts=counts)

    assert payload is not None
    assert len(payload["route"]) == 7
    assert counts["spliced"] == 0
    # The leg that a distance-based trim would have deleted is still there.
    assert payload["route"][5]["station"]["code"] == "BH"


def test_a_spliced_record_is_imported_once_and_not_duplicated(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    base = record(number="22159")
    spliced = base["stops"] + [dict(stop, seq=stop["seq"] + 3) for stop in base["stops"]]
    (out / "schedules.jsonl").write_text(json.dumps(record(number="22159", stops=spliced)) + "\n")

    database = tmp_path / "timetable.db"
    assert importer.main(["--input", str(out), "--db", str(database)]) == 0
    printed = capsys.readouterr().out
    assert "trimmed 1 spliced" in printed

    repo = TimetableRepo(database)
    try:
        trains = repo.trains_between("CSMT", "PUNE")
        assert [train.train_number for train in trains] == ["22159"]
        # The phantom reverse leg must not exist.
        assert repo.trains_between("PUNE", "CSMT") == []
    finally:
        repo.close()


def test_import_writes_rows_and_is_idempotent(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    lines = "\n".join(json.dumps(record(number=number)) for number in ("12127", "11007"))
    (out / "schedules.jsonl").write_text(lines + "\n")

    database = tmp_path / "timetable.db"
    assert importer.main(["--input", str(out), "--db", str(database)]) == 0
    first = capsys.readouterr().out
    assert "'new': 2" in first

    assert importer.main(["--input", str(out), "--db", str(database)]) == 0
    second = capsys.readouterr().out
    assert "'same': 2" in second

    repo = TimetableRepo(database)
    try:
        assert repo.counts()["trains"] == 2
        trains = repo.trains_between("CSMT", "PUNE")
        assert {train.train_number for train in trains} == {"12127", "11007"}
        # The run days reached the schema the search filters on.
        assert all(train.runs_on(0) for train in trains)
    finally:
        repo.close()


def test_a_train_with_unreadable_run_days_is_skipped_loudly(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / "schedules.jsonl").write_text(json.dumps(record(runs_days="Unknown")) + "\n")

    database = tmp_path / "timetable.db"
    assert importer.main(["--input", str(out), "--db", str(database)]) == 0
    printed = capsys.readouterr().out
    assert "'skipped': 1" in printed
    assert "unreadable run days" in printed

    repo = TimetableRepo(database)
    try:
        assert repo.counts()["trains"] == 0
    finally:
        repo.close()


def test_dry_run_touches_nothing(tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / "schedules.jsonl").write_text(json.dumps(record()) + "\n")
    database = tmp_path / "timetable.db"

    assert importer.main(["--input", str(out), "--db", str(database), "--dry-run"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert not database.exists()


def test_station_coordinates_fill_in_and_never_erase(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "schedules.jsonl").write_text(json.dumps(record()) + "\n")
    (out / "stations.csv").write_text(
        "code,name,lat,lon\nCSMT,Mumbai CSMT,18.94,72.835\nPUNE,Pune Jn,,\n"
    )
    database = tmp_path / "timetable.db"

    assert importer.main(["--input", str(out), "--db", str(database), "--stations"]) == 0

    repo = TimetableRepo(database)
    try:
        placed = repo.station("CSMT")
        assert placed is not None and placed.lat == pytest.approx(18.94)
        # The blank row must not have written a 0,0 location for Pune.
        blank = repo.station("PUNE")
        assert blank is not None and blank.lat is None
    finally:
        repo.close()
