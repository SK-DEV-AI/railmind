"""The fare engine, checked against the two files that were read by hand.

Every assertion here is anchored to a value printed in the circular rather than to
whatever the code currently returns, so a change in the engine has to survive the
document. The strongest of these is the cross-file check: page 4 prints a minimum
fare for each class, and page 10/12 prints a fare at the minimum distance for
charge, and the two agree for every class. Two independent readings of one
document agreeing is the only external proof available offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from railmind import fare
from railmind.fare.table import FareTable, FareTableError

DATA = Path(__file__).resolve().parent.parent / "data"


@pytest.fixture(scope="module")
def table() -> FareTable:
    return fare.default_table()


@pytest.fixture(scope="module")
def charges() -> fare.Charges:
    return fare.default_charges()


def price(table, charges, class_code, km, **kwargs):
    section = fare.resolve(table, class_code, rajdhani=kwargs.pop("rajdhani", False))
    return fare.quote(
        section=section,
        class_code=class_code,
        distance_km=km,
        table=table,
        charges=charges,
        **kwargs,
    )


class TestTheShippedTable:
    def test_it_loads_all_three_transcribed_sections(self, table):
        assert set(table.sections()) == {
            "mail_express_non_ac",
            "mail_express_ac",
            "rajdhani",
        }

    def test_it_names_the_document_it_came_from(self, table):
        assert "Commercial Circular 24 of 2025" in table.source
        assert table.effective == "2025-12-26"

    def test_the_only_gap_is_the_row_the_reader_could_not_read(self, table):
        # 2,251-2,275 km has no row: the extractor drops a row it cannot read
        # rather than invent one, and the engine prices nothing in that range.
        assert table.gaps() == {"mail_express_non_ac": ((2251, 2275),)}

    def test_a_fare_never_falls_as_the_distance_grows(self, table):
        for section in table.sections():
            printed = fare.section_for("2S")  # any section name is fine here
            assert printed  # the helper is exercised elsewhere
            classes = table.classes(section)
            previous = dict.fromkeys(classes, 0)
            for slab in table.section(section).slabs:
                for code in classes:
                    value = slab.fare(code)
                    if value is None:
                        continue
                    assert value >= previous[code], (section, code, slab.start)
                    previous[code] = value


class TestTwoReadingsAgree:
    """Page 4's minimum fare against the table's fare at its minimum distance.

    Only the Mail/Express sections are checked. Page 4 prints the charges that apply
    to a Mail/Express ticket, and a Rajdhani carries its own higher base fare, so
    holding a Rajdhani against that page would be comparing two different things:
    the Rajdhani 3A fare at 300 km is 522 against the page's Mail/Express 452, and
    both are right.
    """

    def test_the_minimum_fare_is_the_fare_at_the_minimum_distance(self, table, charges):
        checked = 0
        for section in ("mail_express_non_ac", "mail_express_ac"):
            for code in table.classes(section):
                row = charges.row(section, code)
                if row.min_distance_km is None or row.min_fare is None:
                    continue
                value = table.base_fare(section, code, row.min_distance_km)
                if value is None:
                    continue
                assert value == row.min_fare, (section, code, row.min_distance_km)
                checked += 1
        assert checked >= 10


class TestDistanceBands:
    @pytest.mark.parametrize(
        ("km", "expected"),
        [(1, 31), (5, 31), (50, 31), (51, 35), (5000, 961)],
    )
    def test_a_slab_is_charged_once_its_first_kilometre_is_reached(self, table, km, expected):
        assert table.base_fare("mail_express_non_ac", "2S", km) == expected

    def test_a_distance_inside_a_slab_pays_the_slab_fare(self, table):
        assert table.base_fare("mail_express_non_ac", "2S", 47) == 31
        assert table.base_fare("mail_express_non_ac", "2S", 48) == 31

    def test_a_short_journey_is_charged_at_the_minimum_distance(self, table, charges):
        quote = price(table, charges, "SL", 10)
        assert quote.priced
        assert quote.slab == (196, 200)
        assert quote.components[0].amount_minor == 130_00
        assert any("minimum distance for charge" in note for note in quote.assumptions)

    def test_past_the_last_slab_the_printed_increment_applies(self, table):
        # 5,001-5,050 km is the first slab past the printed range, and 2S pays
        # Rs 10 for each 50 km slab beyond 5,000 km.
        at_5000 = table.base_fare("mail_express_non_ac", "2S", 5000)
        assert table.base_fare("mail_express_non_ac", "2S", 5001) == at_5000 + 10
        assert table.base_fare("mail_express_non_ac", "2S", 5051) == at_5000 + 20

    def test_rajdhani_stops_where_its_table_stops(self, table):
        assert table.base_fare("rajdhani", "3A", 3150) is not None
        assert table.base_fare("rajdhani", "3A", 3151) is None

    def test_a_part_kilometre_is_charged_as_a_whole_one(self, table, charges):
        # A timetable distance is not a whole number: CSMT-PUNE is 190.7 km, which
        # falls between the printed 186-190 and 191-195 slabs. Charging it as 190
        # would find no slab at all and silently unprice the class; charging it as
        # 191 is both what the railway does and never an under-quote.
        quote = price(table, charges, "2S", 190.7)
        assert quote.priced
        assert quote.slab == (191, 195)
        assert quote.components[0].amount_minor == 81_00
        assert any("190.7 km journey is charged as 191 km" in note for note in quote.assumptions)

    def test_a_whole_kilometre_distance_carries_no_rounding_note(self, table, charges):
        quote = price(table, charges, "2S", 191)
        assert quote.slab == (191, 195)
        assert not any("charged as" in note for note in quote.assumptions)


class TestRefusals:
    def test_a_gap_in_the_printed_rows_is_unpriced(self, table, charges):
        quote = price(table, charges, "SL", 2260)
        assert not quote.priced
        assert "no fare for SL at 2260 km" in quote.unpriced_reason

    def test_a_cell_the_reader_could_not_read_is_unpriced(self, table, charges):
        # The 921-930 km row carries no printed value for 2S on the page.
        quote = price(table, charges, "2S", 925)
        assert not quote.priced
        assert "no fare for 2S at 925 km" in quote.unpriced_reason

    def test_a_class_the_table_does_not_carry_is_refused(self, table):
        with pytest.raises(fare.UnknownServiceError):
            fare.resolve(table, "EC")

    def test_a_rajdhani_class_the_rajdhani_table_does_not_carry_is_refused(self, table):
        with pytest.raises(fare.UnknownServiceError):
            fare.resolve(table, "SL", rajdhani=True)

    def test_an_unknown_section_is_named_in_the_error(self, table):
        with pytest.raises(FareTableError):
            table.classes("shatabdi")


class TestTheTotal:
    def test_the_parts_add_up_to_the_total(self, table, charges):
        quote = price(table, charges, "CC", 300, tax=fare.TaxRules(0.05, 0.0, "test"))
        assert quote.priced
        assert quote.total_minor == sum(c.amount_minor for c in quote.components)

    def test_the_basic_fare_and_reservation_fee_are_both_charged(self, table, charges):
        quote = price(table, charges, "CC", 300)
        parts = {c.code: c.amount_minor for c in quote.components}
        assert parts == {"basic": 394_00, "reservation": 40_00, "rounding": 1_00}
        assert quote.total_minor == 435_00
        # Note 5.1 of the fare table: the chargeable fare is rounded up to the next
        # higher multiple of Rs 5, which is why the parts differ from the total above.
        assert quote.total_minor % fare.ROUNDING_STEP_MINOR == 0

    def test_a_superfast_charge_is_added_only_when_the_train_is_superfast(self, table, charges):
        plain = price(table, charges, "SL", 1000)
        fast = price(table, charges, "SL", 1000, superfast=True)
        assert dict((c.code, c.amount_minor) for c in plain.components) == {
            "basic": 489_00,
            "reservation": 20_00,
            "rounding": 1_00,
        }
        difference = fast.total_minor - plain.total_minor
        # 489 + 20 = 509 rounds up to 510; with the printed Rs 30 superfast charge,
        # 539 rounds up to 540. The round-up happens after the charge is added, so
        # the difference stays exactly the printed charge here. It would not if the
        # two loads landed on different sides of a Rs 5 step, which is why the
        # numbers are asserted rather than just the difference.
        assert plain.total_minor == 510_00
        assert fast.total_minor == 540_00
        assert difference == 30_00

    def test_an_unknown_superfast_flag_is_stated_as_a_possible_shortfall(self, table, charges):
        # The timetable has no superfast flag, so the quote cannot know. It says so
        # instead of quietly reporting a total that omits the printed charge.
        quote = price(table, charges, "SL", 1000)
        assert "superfast" not in {c.code for c in quote.components}
        assert any("up to that much higher" in note for note in quote.assumptions)
        assert any("Rs 30" in note for note in quote.assumptions)

    def test_not_superfast_carries_no_such_note(self, table, charges):
        quote = price(table, charges, "SL", 1000, superfast=False)
        assert not any("superfast" in note for note in quote.assumptions)

    def test_tax_is_charged_on_the_fare_and_its_charges(self, table, charges):
        quote = price(table, charges, "SL", 1000, tax=fare.TaxRules(0.05, 0.05, "test"))
        gst = next(c for c in quote.components if c.code == "gst")
        assert gst.amount_minor == round((489_00 + 20_00) * 0.05)

    def test_a_quote_with_no_tax_rule_says_so(self, table, charges):
        quote = price(table, charges, "SL", 1000)
        assert quote.priced
        assert any("excludes tax" in note for note in quote.assumptions)

    def test_a_missing_rate_for_the_class_is_unpriced(self, table, charges):
        quote = price(table, charges, "CC", 300, tax=fare.TaxRules(None, 0.0, "test"))
        assert not quote.priced
        assert "no tax rate is recorded" in quote.unpriced_reason


class TestSeasonalClasses:
    def test_ac_2_tier_carries_two_printed_fares(self, table):
        assert "2A_peak" in table.classes("mail_express_ac")
        assert "2A_lean" in table.classes("mail_express_ac")

    def test_the_named_season_is_the_one_quoted(self, table, charges):
        lean = price(table, charges, "2A", 1500, season="lean")
        peak = price(table, charges, "2A", 1500, season="peak")
        assert lean.season == "lean" and peak.season == "peak"
        assert lean.total_minor < peak.total_minor

    def test_without_a_season_the_peak_is_quoted_and_the_lean_is_shown(self, table, charges):
        unknown = price(table, charges, "2A", 1500)
        record = price(table, charges, "1A", 3000)
        assert unknown.season == "peak"
        assert unknown.alternate_season == "lean"
        assert unknown.alternate_total_minor < unknown.total_minor
        assert record.alternate_total_minor is not None
        assert any("peak-period fare is quoted" in note for note in record.assumptions)

    def test_a_class_without_a_season_is_quoted_once(self, table, charges):
        quote = price(table, charges, "3A", 1500)
        assert quote.alternate_total_minor is None
        assert quote.season is None


class TestValidation:
    def _write(self, tmp_path: Path, rows, classes=("2S",)) -> Path:
        path = tmp_path / "table.json"
        path.write_text(
            json.dumps(
                {
                    "source": "synthetic",
                    "effective": "2025-12-26",
                    "sections": {"s": {"classes": list(classes), "rows": rows}},
                }
            )
        )
        return path

    def test_overlapping_rows_are_refused(self, tmp_path):
        path = self._write(
            tmp_path,
            [
                {"start": 1, "end": 5, "2S": 10},
                {"start": 5, "end": 10, "2S": 12},
            ],
        )
        with pytest.raises(FareTableError, match="overlaps"):
            FareTable.load(path, DATA / "fare-beyond-5000.json")

    def test_a_falling_fare_is_refused(self, tmp_path):
        path = self._write(
            tmp_path,
            [
                {"start": 1, "end": 5, "2S": 10},
                {"start": 6, "end": 10, "2S": 9},
            ],
        )
        with pytest.raises(FareTableError, match="falls"):
            FareTable.load(path, DATA / "fare-beyond-5000.json")

    def test_a_gap_is_reported_rather_than_refused(self, tmp_path):
        path = self._write(
            tmp_path,
            [
                {"start": 1, "end": 5, "2S": 10},
                {"start": 11, "end": 15, "2S": 14},
            ],
        )
        loaded = FareTable.load(path, DATA / "fare-beyond-5000.json")
        assert loaded.gaps() == {"s": ((6, 10),)}
        assert loaded.base_fare("s", "2S", 8) is None
        assert loaded.base_fare("s", "2S", 12) == 14
