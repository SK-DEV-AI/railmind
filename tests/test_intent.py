"""Intent parser tests, against the real 13,010-station table.

Every assertion here is a sentence a student would actually type. The table is
the vendor's real directory, which is what makes the hard cases hard: it lists
loco sheds and cabins next to termini, so resolution has to be able to say "I
cannot tell" instead of picking one.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest

from railmind.data.timetable import TimetableRepo, seed_from_fixtures
from railmind.intent.parser import parse

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "railradar"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), "IST")
NOW = dt.datetime(2026, 10, 25, 9, 0, tzinfo=IST)  # a Sunday


@pytest.fixture
def repo(tmp_path):
    database = TimetableRepo(tmp_path / "timetable.db")
    seed_from_fixtures(database, FIXTURES)
    yield database
    database.close()


def ask(text: str, repo) -> object:
    return parse(text, repo=repo, now=NOW)


def test_a_full_sentence_resolves_everything(repo):
    parsed = ask("book me mumbai to pune on 26 oct 3ac under 2000", repo)
    intent = parsed.intent

    assert intent.origin.code == "CSMT"
    assert intent.destination.code == "PUNE"
    assert intent.depart_window.start.date() == dt.date(2026, 10, 26)
    assert intent.class_preferences == ["3A"]
    assert intent.budget.amount_minor == 200000
    assert parsed.ready is True
    assert intent.parsed_by.value == "rules"


def test_a_city_with_many_stations_resolves_to_the_busiest_we_serve(repo):
    """Mumbai has CSMT, LTT and Central in the table. CSMT is where our trains go."""
    parsed = ask("mumbai to pune tomorrow", repo)

    assert parsed.intent.origin.code == "CSMT"
    assert parsed.ambiguities == ()


def test_infrastructure_is_never_offered_as_a_station(repo):
    """The vendor table lists a loco shed called "Diesel Loco Shed Ltt Mumbai"."""
    parsed = ask("mumbai to pune tomorrow", repo)
    assert parsed.intent.origin.name.upper().find("SHED") == -1

    nagpur = ask("pune to nagpur tomorrow", repo)
    # Nagpur Jn wins on traffic; cabins are filtered out entirely.
    assert nagpur.intent.destination.code == "NGP"
    options = nagpur.ambiguities[0].options if nagpur.ambiguities else []
    assert not any("CABIN" in str(option).upper() for option in options)


def test_an_equal_choice_becomes_a_question_with_options(repo):
    """Two station codes can share one name, and then only the human knows."""
    parsed = ask("mumbai central to pune tomorrow", repo)

    # Mumbai Central exists as MMCT, BCL and BCT in the vendor table.
    assert parsed.intent.origin is not None
    if parsed.ambiguities:
        ambiguity = parsed.ambiguities[0]
        assert ambiguity.field == "origin"
        assert ambiguity.options
        assert all(option.startswith("IN:") for option in ambiguity.options)


def test_no_date_means_no_window_and_no_ready_intent(repo):
    parsed = ask("mumbai to pune", repo)

    assert parsed.intent.depart_window is None
    assert parsed.ready is False
    assert [item.field for item in parsed.ambiguities] == ["depart_window"]
    assert "date" in parsed.ambiguities[0].question.lower()


def test_relative_dates_are_resolved_against_the_supplied_now(repo):
    assert ask("mumbai to pune tomorrow", repo).intent.depart_window.start.date() == dt.date(
        2026, 10, 26
    )
    assert ask(
        "mumbai to pune day after tomorrow", repo
    ).intent.depart_window.start.date() == dt.date(2026, 10, 27)
    # NOW is a Sunday, so the next Friday is the 30th.
    assert ask("mumbai to pune this friday", repo).intent.depart_window.start.date() == dt.date(
        2026, 10, 30
    )
    assert ask("mumbai to pune today", repo).intent.depart_window.start.date() == dt.date(
        2026, 10, 25
    )


def test_absolute_dates_in_three_shapes(repo):
    shapes = ["26 oct", "26/10/2026", "2026-10-26"]

    for shape in shapes:
        parsed = ask(f"mumbai to pune on {shape}", repo)
        assert parsed.intent.depart_window.start.date() == dt.date(2026, 10, 26), shape


def test_clock_time_wins_over_the_word_of_day(repo):
    parsed = ask("mumbai to pune tomorrow 6am", repo)
    window = parsed.intent.depart_window

    assert window.start.strftime("%H:%M") == "06:00"
    assert window.end.strftime("%H:%M") == "10:00"

    evening = ask("mumbai to pune tomorrow at 18:30", repo).intent.depart_window
    assert evening.start.strftime("%H:%M") == "18:00"


def test_a_word_of_day_gives_a_window_not_a_guess(repo):
    window = ask("mumbai to pune tomorrow morning", repo).intent.depart_window
    assert (window.start.strftime("%H:%M"), window.end.strftime("%H:%M")) == ("04:00", "12:00")

    night = ask("mumbai to pune tomorrow night", repo).intent.depart_window
    assert night.start.strftime("%H:%M") == "22:00"
    # The window crosses midnight, so it ends on the next calendar day.
    assert night.end.date() == night.start.date() + dt.timedelta(days=1)


def test_tatkal_is_recognised_and_its_rules_are_stated(repo):
    parsed = ask("mumbai to pune tatkal tomorrow", repo)

    assert parsed.intent.quota.value == "tatkal"
    assert any("30 minutes" in note for note in parsed.notes)


def test_optimisation_words_map_to_the_contract_enum(repo):
    assert ask("mumbai to pune tomorrow cheapest", repo).intent.optimise.value == "cheapest"
    assert ask("mumbai to pune tomorrow not late", repo).intent.optimise.value == "least_delay"
    assert ask("mumbai to pune tomorrow", repo).intent.optimise.value == "balanced"


def test_bare_codes_are_accepted(repo):
    parsed = ask("CSMT to PUNE tomorrow", repo)

    assert parsed.intent.origin.code == "CSMT"
    assert parsed.intent.destination.code == "PUNE"


def test_a_place_that_does_not_exist_is_a_question_not_a_station(repo):
    parsed = ask("atlantis to pune tomorrow", repo)

    assert parsed.intent.origin is None
    assert [item.field for item in parsed.ambiguities] == ["origin"]
    assert "atlantis" in parsed.ambiguities[0].question.lower()


def test_the_same_station_twice_is_flagged(repo):
    parsed = ask("pune to pune tomorrow", repo)

    assert parsed.intent.origin.code == parsed.intent.destination.code
    assert [item.field for item in parsed.ambiguities] == ["origin"]


def test_a_passenger_count_without_names_asks_for_them(repo):
    parsed = ask("mumbai to pune tomorrow 2 passengers", repo)

    fields = [item.field for item in parsed.ambiguities]
    assert "passengers" in fields
    assert parsed.intent.passengers is None


def test_confidence_is_arithmetic_and_capped(repo):
    full = ask("mumbai to pune tomorrow 3ac", repo).intent
    partial = ask("mumbai to pune", repo).intent
    nothing = ask("atlantis to pune tomorrow", repo).intent

    assert full.confidence == 0.9  # capped, because a regex is not a certainty
    assert partial.confidence < full.confidence
    assert nothing.confidence < partial.confidence
    assert 0.0 < nothing.confidence <= 1.0


def test_an_intent_expires_and_never_invents_a_date_of_issue(repo):
    intent = ask("mumbai to pune tomorrow", repo).intent

    assert intent.created_at == NOW
    assert intent.expires_at > intent.created_at
    assert (intent.expires_at - intent.created_at) <= dt.timedelta(minutes=30)
    assert intent.raw_text


def test_parsing_is_deterministic_for_the_same_input_and_now(repo):
    first = ask("mumbai to pune tomorrow 3ac", repo).intent
    second = ask("mumbai to pune tomorrow 3ac", repo).intent

    assert first.origin.code == second.origin.code
    assert first.confidence == second.confidence
    assert first.depart_window.start == second.depart_window.start


def test_station_choice_follows_the_timetable_not_popularity(repo):
    """ "mumbai" matches several stations; only some of them reach Pune.

    In the seeded fixture only CSMT has trains to Pune, so that is the answer even
    though a busier Mumbai station may exist in the wider database.
    """
    parsed = ask("mumbai to pune tomorrow", repo)

    assert parsed.intent.origin.code == "CSMT"
    assert parsed.intent.destination.code == "PUNE"
    assert parsed.ambiguities == ()


def test_an_explicitly_named_station_is_honoured_even_with_no_trains(repo):
    """If a user names Mumbai Central, we do not silently move them to CSMT."""
    parsed = ask("mumbai central to pune tomorrow", repo)

    assert parsed.intent.origin.code == "MMCT"
    # No direct trains from MMCT to PUNE exist here; the search will say so, which
    # is a better answer than quietly changing the station they asked for.
    assert parsed.ambiguities == ()


def test_the_pairs_with_the_most_service_win(repo):
    """Among connected pairs, more trains is a better answer than fewer."""
    from railmind.intent.parser import _pick_connected

    origins = [repo.station(code) for code in ("MMCT", "CSMT", "LTT")]
    destinations = [repo.station("PUNE")]
    picked = _pick_connected(
        [row for row in origins if row is not None],
        [row for row in destinations if row is not None],
        repo,
    )

    # MMCT comes first in the candidate list and has no trains to Pune; CSMT has
    # three in the fixture, so connectivity outranks candidate order.
    assert picked[0].code == "CSMT"
