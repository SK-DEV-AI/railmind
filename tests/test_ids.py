"""Ids must be derivable, canonical, and impossible to confuse with each other."""

from __future__ import annotations

import datetime as dt

import pytest

from railmind.core.ids import (
    IdError,
    as_service_date,
    intent_id,
    itinerary_id,
    leg_id,
    parse_leg_id,
    parse_stop_id,
    stop_id,
)


class TestStopId:
    def test_builds_canonical_form(self):
        assert stop_id("in", "csmt") == "IN:CSMT"

    def test_parses_back(self):
        assert parse_stop_id("IN:CSMT") == ("IN", "CSMT")

    @pytest.mark.parametrize("bad", ["", "IN", ":CSMT", "IND:CSMT", "IN:C SMT", "IN:A:B"])
    def test_rejects_rubbish(self, bad):
        with pytest.raises(IdError):
            parse_stop_id(bad)

    def test_lower_case_is_not_canonical(self):
        with pytest.raises(IdError):
            parse_stop_id("in:csmt")


class TestLegId:
    def test_builds_from_parts(self):
        assert leg_id("rail_india", "12951", "2026-10-25") == "rail_india:12951:2026-10-25:1"

    def test_sequence_distinguishes_same_service(self):
        assert leg_id("rail_india", "12951", "2026-10-25", 2) != leg_id(
            "rail_india", "12951", "2026-10-25", 1
        )

    def test_round_trip(self):
        parsed = parse_leg_id("rail_india:12951:2026-10-25:3")
        assert parsed.provider_id == "rail_india"
        assert parsed.service_id == "12951"
        assert parsed.service_date == dt.date(2026, 10, 25)
        assert parsed.sequence == 3
        assert parsed.value == "rail_india:12951:2026-10-25:3"

    def test_accepts_date_objects(self):
        assert leg_id("p", "1", dt.date(2026, 1, 2)) == "p:1:2026-01-02:1"

    @pytest.mark.parametrize(
        "bad",
        [
            "rail_india:12951:2026-10-25",  # missing sequence
            "rail_india:12951:2026-10-25:0",  # sequence must start at 1
            "rail_india:12951:2026-10-25:x",
            "rail_india:12951:2026-10-25:1:9",
            "rail_india::2026-10-25:1",
        ],
    )
    def test_rejects_non_canonical(self, bad):
        with pytest.raises(IdError):
            parse_leg_id(bad)

    def test_rejects_colon_in_parts(self):
        with pytest.raises(IdError):
            leg_id("rail:india", "12951", "2026-10-25")

    def test_rejects_bool_and_zero_sequence(self):
        with pytest.raises(IdError):
            leg_id("p", "1", "2026-10-25", True)
        with pytest.raises(IdError):
            leg_id("p", "1", "2026-10-25", 0)


class TestItineraryId:
    def test_stable_for_same_legs(self):
        legs = ["p:1:2026-10-25:1", "p:2:2026-10-25:2"]
        assert itinerary_id(legs) == itinerary_id(tuple(legs))

    def test_order_matters(self):
        a = ["p:1:2026-10-25:1", "p:2:2026-10-25:2"]
        b = list(reversed(a))
        assert itinerary_id(a) != itinerary_id(b)

    def test_requires_a_leg(self):
        with pytest.raises(IdError):
            itinerary_id([])

    def test_rejects_invalid_leg_ids(self):
        with pytest.raises(IdError):
            itinerary_id(["not-a-leg"])


class TestServiceDate:
    def test_datetime_keeps_local_date(self):
        # 23:50 IST is still the 25th for booking purposes. UTC conversion would
        # move it to the 25th at 18:20, but a naive utcfromtimestamp would say 24th.
        late = dt.datetime(
            2026, 10, 25, 23, 50, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))
        )
        assert as_service_date(late) == dt.date(2026, 10, 25)

    def test_rejects_bad_string(self):
        with pytest.raises(IdError):
            as_service_date("25-10-2026")


class TestEventIds:
    def test_intent_ids_are_not_derivable(self):
        assert intent_id() != intent_id()
        assert intent_id().startswith("int_")
