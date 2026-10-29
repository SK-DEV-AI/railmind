"""The printed peak and lean seasons, and the classes that are priced twice.

The boundaries come from notes 6 and 6.1 of the passenger fare table (lean: 1 Feb to
31 Mar and 1 Aug to 31 Aug; peak: 1 Apr to 31 Jul and 1 Sep to 31 Jan). The circular
we price from still prints both columns for AC 2-Tier and AC First Class, so the
calendar decides which column a journey date lands in.
"""

from __future__ import annotations

import datetime as dt

import pytest

from railmind import fare


class TestTheBoundaries:
    @pytest.mark.parametrize(
        "when, expected",
        [
            (dt.date(2026, 2, 1), "lean"),
            (dt.date(2026, 3, 31), "lean"),
            (dt.date(2026, 8, 1), "lean"),
            (dt.date(2026, 8, 31), "lean"),
            (dt.date(2026, 4, 1), "peak"),
            (dt.date(2026, 7, 31), "peak"),
            (dt.date(2026, 9, 1), "peak"),
            (dt.date(2026, 1, 31), "peak"),
            (dt.date(2026, 12, 25), "peak"),
        ],
    )
    def test_every_printed_boundary_lands_on_the_printed_side(self, when, expected):
        assert fare.season_for(when) == expected

    def test_september_is_peak_not_lean(self):
        # The lean window is 1 to 31 August only. Reading it as "August and
        # September" would quote the cheap fare in the busiest month of the year.
        assert fare.season_for(dt.date(2026, 9, 15)) == "peak"
        assert fare.season_for(dt.date(2026, 8, 31)) == "lean"

    def test_every_day_of_the_year_is_classified(self):
        day = dt.date(2026, 1, 1)
        while day.year == 2026:
            assert fare.season_for(day) in {"peak", "lean"}, day
            day += dt.timedelta(days=1)


class TestWhatItChanges:
    @pytest.fixture(scope="class")
    def charges(self):
        return fare.default_charges()

    def test_the_same_class_costs_less_in_the_lean_season(self, charges):
        lean = fare.quote(
            section="mail_express_ac",
            class_code="2A",
            distance_km=1500,
            table=fare.default_table(),
            charges=charges,
            season="lean",
        )
        peak = fare.quote(
            section="mail_express_ac",
            class_code="2A",
            distance_km=1500,
            table=fare.default_table(),
            charges=charges,
            season="peak",
        )
        assert lean.total_minor < peak.total_minor
        assert lean.season == "lean" and peak.season == "peak"

    def test_a_class_printed_once_ignores_the_season(self, charges):
        lean = fare.quote(
            section="mail_express_ac",
            class_code="SL",
            distance_km=1500,
            table=fare.default_table(),
            charges=charges,
            season="lean",
        )
        peak = fare.quote(
            section="mail_express_ac",
            class_code="SL",
            distance_km=1500,
            table=fare.default_table(),
            charges=charges,
            season="peak",
        )
        assert lean.total_minor == peak.total_minor

    def test_a_journey_with_no_season_shows_both_prices(self, charges):
        quote = fare.quote(
            section="mail_express_ac",
            class_code="1A",
            distance_km=3000,
            table=fare.default_table(),
            charges=charges,
        )
        assert quote.season == "peak"
        assert quote.alternate_season == "lean"
        assert quote.total_minor > quote.alternate_total_minor


class TestFirstClassIsTaxed:
    def test_first_class_pays_the_air_conditioned_rate(self):
        # The exemption covers rail "in a class other than first class or an
        # air-conditioned coach", so plain First Class is taxed, not exempt.
        rules = fare.TaxRules(0.05, 0.0, "test", rate_first_class=0.05)
        assert rules.rate_for("FC") == 0.05
        assert rules.rate_for("FC_peak") == 0.05

    def test_a_rule_that_records_no_first_class_rate_falls_back_to_the_ac_rate(self):
        rules = fare.TaxRules(0.05, 0.0, "test")
        assert rules.rate_for("FC") == 0.05

    def test_second_class_stays_exempt(self):
        rules = fare.TaxRules(0.05, 0.0, "test", rate_first_class=0.05)
        assert rules.rate_for("2S") == 0.0
        assert rules.rate_for("SL") == 0.0

    def test_the_shipped_rule_taxes_first_class_and_exempts_second(self):
        rules = fare.default_tax()
        assert rules is not None
        assert rules.rate_for("FC") == 0.05
        assert rules.rate_for("2S") == 0.0
        assert "first class" in rules.source
