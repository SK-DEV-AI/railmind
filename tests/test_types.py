"""Money arithmetic and the provider-neutral Query."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from railmind.core.errors import InvalidQuery
from railmind.core.types import (
    BookingIntent,
    Query,
    Stop,
    add_money,
    format_inr,
    inr,
    normalise_class,
    rupees,
)
from railmind.providers.fake import make_stop


class TestMoney:
    def test_rupees_become_exact_paise(self):
        assert inr(2605.50).amount_minor == 260550
        assert inr("2605.5").amount_minor == 260550
        assert inr(Decimal("2605.50")).amount_minor == 260550
        assert inr(0).amount_minor == 0

    def test_float_artifact_does_not_corrupt_paise(self):
        # 0.1 + 0.2 is 0.30000000000000004 in binary float. Money must not care.
        assert inr(0.1 + 0.2).amount_minor == 30
        assert inr(1.005).amount_minor == 100

    def test_negative_money_is_refused(self):
        with pytest.raises(InvalidQuery):
            inr(-1)

    def test_rupees_is_exact(self):
        assert rupees(inr(2605.50)) == Decimal("2605.50")

    def test_addition(self):
        total = add_money(inr(2145), inr(400), inr(60.50))
        assert total.amount_minor == 260550
        assert total.currency == "INR"

    def test_addition_refuses_empty(self):
        with pytest.raises(InvalidQuery):
            add_money()

    def test_addition_refuses_mixed_currency(self):
        usd = inr(10).model_copy(update={"currency": "USD"})
        with pytest.raises(InvalidQuery):
            add_money(inr(10), usd)


class TestFormatting:
    @pytest.mark.parametrize(
        ("minor", "expected"),
        [
            (0, "₹0.00"),
            (5, "₹0.05"),
            (99999, "₹999.99"),
            (100000, "₹1,000.00"),
            (260550, "₹2,605.50"),
            (12345678, "₹1,23,456.78"),
            (10000000, "₹1,00,000.00"),
        ],
    )
    def test_indian_digit_grouping(self, minor, expected):
        from railmind.core.types import Money

        assert format_inr(Money(amount_minor=minor, currency="INR")) == expected


class TestClassNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("3ac", "3A"),
            ("3 AC", "3A"),
            ("third ac", "3A"),
            ("sleeper", "SL"),
            ("SL", "SL"),
            ("2s", "2S"),
            ("chair car", "CC"),
        ],
    )
    def test_aliases(self, raw, expected):
        assert normalise_class(raw) == expected

    def test_unknown_code_passes_through_upper_cased(self):
        # Guessing a class would silently change someone's booking.
        assert normalise_class("3e") == "3E"

    def test_blank_is_refused(self):
        with pytest.raises(InvalidQuery):
            normalise_class("   ")


class TestQuery:
    def test_same_stop_is_refused(self, csmt, service_date):
        with pytest.raises(InvalidQuery):
            Query(origin=csmt, destination=csmt, service_date=service_date)

    def test_needs_a_mode(self, csmt, ndls, service_date):
        with pytest.raises(InvalidQuery):
            Query(origin=csmt, destination=ndls, service_date=service_date, modes=())

    def test_cache_key_parts_exclude_the_user(self, query):
        # The whole point: one upstream fetch serves everyone on this route.
        assert query.cache_key_parts == ("IN:CSMT", "IN:NDLS", "2026-10-25")

    def test_two_users_share_cache_key_parts(self, csmt, ndls, service_date):
        first = Query(origin=csmt, destination=ndls, service_date=service_date, tags={"u1"})
        second = Query(origin=csmt, destination=ndls, service_date=service_date, tags={"u2"})
        assert first.cache_key_parts == second.cache_key_parts


def _intent(**overrides) -> BookingIntent:
    base = dict(
        intent_id="int_test",
        created_at=dt.datetime(2026, 10, 24, 9, 0, tzinfo=dt.UTC),
        expires_at=dt.datetime(2026, 10, 25, 9, 0, tzinfo=dt.UTC),
        raw_text="Mumbai to Delhi on 25 Oct, 3AC",
        modes=["rail"],
        optimise="balanced",
        confidence=0.9,
        parsed_by="llm",
        origin=make_stop("IN:CSMT"),
        destination=make_stop("IN:NDLS", "New Delhi"),
        depart_window={
            "start": "2026-10-25T16:00:00+05:30",
            "end": "2026-10-25T19:00:00+05:30",
        },
        class_preferences=["3ac"],
    )
    base.update(overrides)
    return BookingIntent(**base)


class TestQueryFromIntent:
    def test_builds_from_a_parsed_intent(self):
        query = Query.from_intent(_intent())
        assert query.service_date == dt.date(2026, 10, 25)
        assert query.classes == ("3A",)
        assert query.origin.stop_id == "IN:CSMT"

    def test_missing_origin_is_refused_not_guessed(self):
        with pytest.raises(InvalidQuery, match="origin"):
            Query.from_intent(_intent(origin=None))

    def test_missing_date_is_refused_not_defaulted_to_today(self):
        with pytest.raises(InvalidQuery, match="date"):
            Query.from_intent(_intent(depart_window=None, arrive_by=None))

    def test_arrive_by_is_used_when_no_window(self):
        query = Query.from_intent(
            _intent(depart_window=None, arrive_by="2026-10-26T09:00:00+05:30")
        )
        assert query.service_date == dt.date(2026, 10, 26)


def test_stop_model_requires_country_and_timezone():
    stop = make_stop("IN:CSMT")
    assert stop.country == "IN"
    assert stop.timezone == "Asia/Kolkata"
    assert isinstance(stop, Stop)
