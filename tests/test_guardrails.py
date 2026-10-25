"""Tatkal guardrail tests. Every boundary is a parameter, so none of this waits.

The rules are dated and legal, so each test names the fact it protects rather than
just the behaviour it observes.
"""

from __future__ import annotations

import datetime as dt

import pytest

from railmind.guardrails import (
    AGENT_BLACKOUT_MINUTES,
    GENERAL_MAX_PASSENGERS,
    IST,
    TATKAL_MAX_PASSENGERS,
    blackout_window,
    check_tatkal,
    is_ac,
    opening_time,
)

JOURNEY = dt.date(2026, 10, 26)


def at(day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=IST)


def test_ac_opens_at_ten_and_non_ac_at_eleven():
    assert opening_time(JOURNEY, ac=True) == at(25, 10)
    assert opening_time(JOURNEY, ac=False) == at(25, 11)


def test_the_blackout_is_the_thirty_minutes_after_opening():
    start, end = blackout_window(JOURNEY, ac=True)

    assert end - start == dt.timedelta(minutes=AGENT_BLACKOUT_MINUTES)
    assert start == at(25, 10)


@pytest.mark.parametrize(
    ("classes", "expected"),
    [
        (("3A",), True),
        (("SL",), False),
        (("2S",), False),
        (("CC",), True),
        ((), None),
        (("XX",), True),  # unknown class errs towards the earlier opening
    ],
)
def test_class_family_detection(classes, expected):
    assert is_ac(classes) is expected


def test_tatkal_before_the_opening_hour_is_refused_with_the_hour_in_the_reason():
    verdict = check_tatkal(
        quota="tatkal", service_date=JOURNEY, now=at(25, 9, 30), class_codes=("3A",)
    )

    assert verdict.refused
    assert verdict.codes == ("tatkal_not_open",)
    assert "10:00" in verdict.reasons[0]
    assert "09:30" in verdict.reasons[0]


def test_tatkal_after_opening_is_allowed():
    verdict = check_tatkal(
        quota="tatkal", service_date=JOURNEY, now=at(25, 12, 0), class_codes=("3A",)
    )

    assert verdict.allowed
    assert verdict.reasons == ()


def test_more_than_a_day_ahead_is_a_calendar_problem_not_a_clock_one():
    verdict = check_tatkal(
        quota="tatkal", service_date=JOURNEY, now=at(20, 12, 0), class_codes=("SL",)
    )

    assert verdict.refused
    assert verdict.codes == ("tatkal_too_early",)
    assert "one day before" in verdict.reasons[0]


def test_the_thirty_minute_window_is_a_warning_not_a_refusal():
    """Bots are barred; a human booking as themselves is not."""
    verdict = check_tatkal(
        quota="tatkal", service_date=JOURNEY, now=at(25, 10, 5), class_codes=("3A",)
    )

    assert verdict.allowed
    assert any("30 minutes" in warning for warning in verdict.warnings)


def test_the_warning_disappears_after_the_window():
    verdict = check_tatkal(
        quota="tatkal", service_date=JOURNEY, now=at(25, 10, 31), class_codes=("3A",)
    )

    assert verdict.allowed
    assert not any("30 minutes" in warning for warning in verdict.warnings)


def test_the_tatkal_party_cap_is_four():
    allowed = check_tatkal(
        quota="tatkal",
        service_date=JOURNEY,
        now=at(25, 12, 0),
        passenger_count=TATKAL_MAX_PASSENGERS,
        class_codes=("3A",),
    )
    refused = check_tatkal(
        quota="tatkal",
        service_date=JOURNEY,
        now=at(25, 12, 0),
        passenger_count=TATKAL_MAX_PASSENGERS + 1,
        class_codes=("3A",),
    )

    assert allowed.allowed
    assert refused.refused
    assert "tatkal_passenger_limit" in refused.codes


def test_tatkal_is_announced_as_non_refundable_before_it_is_booked():
    verdict = check_tatkal(
        quota="tatkal",
        service_date=JOURNEY,
        now=at(25, 12, 0),
        passenger_count=2,
        class_codes=("3A",),
    )

    assert any("not refundable" in warning for warning in verdict.warnings)


def test_general_quota_has_no_tatkal_rules_and_a_six_passenger_cap():
    verdict = check_tatkal(quota="general", service_date=JOURNEY, now=at(25, 3, 0))

    assert verdict.allowed
    assert verdict.warnings == ()

    too_many = check_tatkal(
        quota="general",
        service_date=JOURNEY,
        now=at(25, 3, 0),
        passenger_count=GENERAL_MAX_PASSENGERS + 1,
    )
    assert too_many.refused
    assert too_many.codes == ("passenger_limit",)


def test_an_unknown_class_is_flagged_because_the_opening_hour_was_assumed():
    verdict = check_tatkal(quota="tatkal", service_date=JOURNEY, now=at(25, 12, 0), class_codes=())

    assert verdict.allowed
    assert any("assumes an AC class" in warning for warning in verdict.warnings)


def test_a_utc_server_clock_is_converted_before_any_decision():
    """09:59 IST is 04:29 UTC. The rule is about IST wall clock, not the server's."""
    utc_now = dt.datetime(2026, 10, 25, 4, 29, tzinfo=dt.UTC)
    verdict = check_tatkal(quota="tatkal", service_date=JOURNEY, now=utc_now, class_codes=("3A",))

    assert verdict.refused
    assert verdict.codes == ("tatkal_not_open",)

    just_after = dt.datetime(2026, 10, 25, 4, 31, tzinfo=dt.UTC)
    assert check_tatkal(
        quota="tatkal", service_date=JOURNEY, now=just_after, class_codes=("3A",)
    ).allowed


def test_both_failures_are_reported_together_not_one_at_a_time():
    verdict = check_tatkal(
        quota="tatkal",
        service_date=JOURNEY,
        now=at(25, 8, 0),
        passenger_count=6,
        class_codes=("3A",),
    )

    assert set(verdict.codes) == {"tatkal_not_open", "tatkal_passenger_limit"}
    assert len(verdict.reasons) == 2
