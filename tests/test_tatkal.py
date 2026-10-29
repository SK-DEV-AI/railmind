"""The Tatkal premium: the printed table, the bands, and where it sits in the total.

Every expectation here comes from the Railway Board's Tatkal charge table, read
from indianrail.gov.in and cross-checked against IRCTC's own Tatkal FAQ page:
10% of the basic fare for second class, 30% for every other class, clamped between
a printed floor and ceiling per class, and levied unchanged in peak and non-peak
periods.
"""

from __future__ import annotations

import pytest

from railmind import fare


@pytest.fixture(scope="module")
def tatkal() -> fare.TatkalCharges:
    charges = fare.default_tatkal()
    assert charges is not None, "data/tatkal-charges.json must ship with the app"
    return charges


class TestThePrintedTable:
    def test_the_percentage_is_ten_per_cent_for_second_class_and_thirty_otherwise(self, tatkal):
        assert tatkal.row("2S").percent == pytest.approx(0.10)
        for code in ("SL", "CC", "3A", "2A", "EC"):
            assert tatkal.row(code).percent == pytest.approx(0.30), code

    def test_the_printed_floor_and_ceiling_per_class(self, tatkal):
        for code, floor, ceiling in (
            ("2S", 10, 15),
            ("SL", 100, 200),
            ("CC", 125, 225),
            ("3A", 300, 400),
            ("2A", 400, 500),
            ("EC", 400, 500),
        ):
            row = tatkal.row(code)
            assert (row.floor, row.ceiling) == (floor, ceiling), code

    def test_third_economy_rides_the_third_ac_band(self, tatkal):
        # The table prints no 3E row. 3E is the same coach at the same basic fare as
        # 3A, so the 3A band is the one that applies, and the row says so.
        assert tatkal.row("3E").class_code == "3A"

    def test_first_ac_is_outside_the_scheme(self, tatkal):
        # No 1A row is printed, and that is not an omission: Tatkal cannot be booked
        # in first AC. The charge reader says so instead of inventing a number.
        answer = tatkal.charge("1A", basic_rupees=1000, distance_km=500)
        assert isinstance(answer, str)
        assert "cannot be booked in this class" in answer

    def test_the_scheme_is_levied_uniformly_in_both_seasons(self, tatkal):
        # "The above charges will be levied uniformly both in peak period & non-peak
        # periods." A seasonal premium is a real thing a reader would expect, so the
        # absence is recorded in the data rather than left as an accident of the code.
        assert tatkal.seasonal is False
        assert "uniformly both in peak period & non-peak periods" in tatkal.season_note

    def test_the_premium_is_a_function_of_the_basic_fare_alone(self, tatkal):
        # Two journeys at the same basic fare, in different seasons, get the same
        # premium: nothing in the charge path takes a date.
        peak = tatkal.charge("2A", basic_rupees=2000, distance_km=1200)
        lean = tatkal.charge("2A", basic_rupees=2000, distance_km=1200)
        assert not isinstance(peak, str) and not isinstance(lean, str)
        assert peak.amount_minor == lean.amount_minor == 500_00


class TestThePremium:
    def test_the_percentage_applies_inside_the_band(self, tatkal):
        premium = tatkal.charge("3A", basic_rupees=1000, distance_km=800)
        assert not isinstance(premium, str)
        assert premium.amount_minor == 300_00

    def test_the_floor_applies_at_short_distances(self, tatkal):
        # 30% of Rs 200 is Rs 60, under the printed Rs 300 floor for 3A.
        premium = tatkal.charge("3A", basic_rupees=200, distance_km=120)
        assert not isinstance(premium, str)
        assert premium.amount_minor == 300_00
        assert "printed minimum of Rs 300" in premium.note

    def test_the_ceiling_applies_at_long_distances(self, tatkal):
        # 30% of Rs 3000 is Rs 900, over the printed Rs 400 ceiling for 3A.
        premium = tatkal.charge("3A", basic_rupees=3000, distance_km=2500)
        assert not isinstance(premium, str)
        assert premium.amount_minor == 400_00
        assert "printed maximum of Rs 400" in premium.note


class TestWhereItSitsInTheTotal:
    def test_the_premium_is_inside_the_tax_base(self):
        # Worked from a printed ticket: chair car at 191 km, basic 288, reservation
        # 40, superfast 45, Tatkal 125, plus 5% on the lot, prints Rs 525 once rounded
        # up (522.90). Charging tax on the fare alone would print 520 and miss it, and
        # charging it on everything but the premium would print 500 and miss it worse.
        quote = fare.quote(
            section="mail_express_ac",
            class_code="CC",
            distance_km=191,
            table=fare.default_table(),
            charges=fare.default_charges(),
            tax=fare.TaxRules(0.05, 0.0, "test"),
            tatkal=fare.default_tatkal(),
            superfast=True,
        )
        assert quote.priced
        parts = {c.code: c.amount_minor for c in quote.components}
        subtotal = parts["basic"] + parts["reservation"] + parts["superfast"] + parts["tatkal"]
        assert subtotal == 498_00
        assert parts["gst"] == round(subtotal * 0.05) == 24_90
        assert quote.total_minor == 525_00
        assert quote.total_minor % fare.ROUNDING_STEP_MINOR == 0

    def test_the_quote_says_the_premium_is_taxed_with_the_fare(self):
        quote = fare.quote(
            section="mail_express_ac",
            class_code="3A",
            distance_km=900,
            table=fare.default_table(),
            charges=fare.default_charges(),
            tax=fare.TaxRules(0.05, 0.0, "test"),
            tatkal=fare.default_tatkal(),
        )
        assert any("inside the tax base" in note for note in quote.assumptions)

    def test_a_class_the_scheme_excludes_is_unpriced_not_silently_free(self):
        quote = fare.quote(
            section="mail_express_ac",
            class_code="1A",
            distance_km=900,
            table=fare.default_table(),
            charges=fare.default_charges(),
            tax=fare.TaxRules(0.05, 0.0, "test"),
            tatkal=fare.default_tatkal(),
        )
        assert not quote.priced
        assert "cannot be booked in this class" in quote.unpriced_reason

    def test_without_the_scheme_the_quote_carries_no_tatkal_line(self):
        quote = fare.quote(
            section="mail_express_ac",
            class_code="3A",
            distance_km=900,
            table=fare.default_table(),
            charges=fare.default_charges(),
            tax=fare.TaxRules(0.05, 0.0, "test"),
        )
        assert "tatkal" not in {c.code for c in quote.components}
