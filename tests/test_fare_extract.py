"""The fare extractor's cell readers, tested on the cases that broke them.

Nothing here touches the PDF or tesseract: these are the decisions the extractor
makes *about* what a reader returned. Each case is a defect that shipped a wrong
or missing fare in an earlier revision, so the test names the measurement.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("numpy")
pytest.importorskip("sys")

E = pytest.importorskip("scripts.extract_fare_table")

from scripts.extract_fare_table import Cell, Row  # noqa: E402

#: The non-AC half's fare columns, in the order `Row.values` stores them.
CLASSES = ["2S", "SL", "FC"]


def cell(page=10, x=0, y=0, votes=None, box=None) -> Cell:
    return Cell(page=page, x=x, y=y, votes=list(votes or []), box=box)


def row(band, start_box, others, start=191) -> Row:
    values = {}
    for index, code in enumerate(("2S", "SL", "FC")):
        values[code] = others[index]
    end_box = (100, start_box[1], 63, 29) if start_box else None
    return Row(
        page=10,
        section="mail_express_non_ac",
        half=0,
        y=0,
        start_cell=cell(votes=[str(start)], box=start_box),
        band=band,
        end_cell=cell(votes=["195"], box=end_box),
        values=values,
    )


class TestGlyphConfusions:
    def test_trailing_s_is_a_five(self):
        # Measured: the printed `235` of page 10 slab 11-15, second engine.
        assert E.normalize_digits("23S") == "235"

    def test_letters_outside_the_map_stay_unread(self):
        # Measured: `4061A` for the printed `406`, where `1A` is the grid's
        # border. Mapping it would have produced 40614 and priced a journey.
        assert E.normalize_digits("4061A") is None

    def test_plain_digits_pass_through(self):
        assert E.normalize_digits("900") == "900"

    def test_o_and_l_are_read_as_digits(self):
        assert E.normalize_digits("O8l") == "081"


class TestRuleTrimming:
    def test_full_height_bar_at_the_edge_is_dropped(self):
        digits = np.full((40, 30), 255, dtype=np.uint8)
        digits[8:32, 5:25] = 0
        bar = np.zeros((40, 6), dtype=np.uint8)
        crop = np.hstack([digits, bar])
        assert E.trim_rule(crop).shape[1] == 30

    def test_a_clean_crop_is_unchanged(self):
        digits = np.full((40, 30), 255, dtype=np.uint8)
        digits[8:32, 5:25] = 0
        assert E.trim_rule(digits).shape == digits.shape

    def test_a_crop_that_is_mostly_rule_is_left_alone(self):
        bar = np.zeros((40, 20), dtype=np.uint8)
        assert E.trim_rule(bar).shape == bar.shape

    def test_a_rule_inside_the_crop_is_whitened_out(self):
        # Measured: slab 21-25's Sleeper box was 180 px wide and reached the
        # column separator, so both readers turned the separator into a `1` and
        # settled on `471` for the printed `47`. Agreement is only evidence when
        # the two readers are looking at different pictures of the same ink.
        digits = np.full((44, 196), 255, dtype=np.uint8)
        digits[6:38, 106:124] = 0
        digits[:, 155:160] = 0
        trimmed = E.trim_rule(digits)
        assert (trimmed[:, 155:160] == 255).all()
        assert (trimmed[6:38, 106:124] == 0).all()

    def test_a_wide_block_of_ink_is_not_mistaken_for_a_rule(self):
        # A run wider than any printed rule is left alone: whitening it would
        # delete the number the crop was taken to read.
        block = np.full((44, 60), 255, dtype=np.uint8)
        block[:, 20:40] = 0
        assert (E.trim_rule(block)[:, 20:40] == 0).all()


class TestRowTextWindow:
    def test_the_line_is_learned_from_the_tight_boxes(self):
        # Measured: slab 191-195's FC box was (1668, 2820, 101, 58) while every
        # other cell of the row was ~(x, 2841, 63, 28). The tall box held the
        # `414` printed on the next line, and reading it returned `408`.
        oversized = cell(votes=[], box=(1668, 2820, 101, 58))
        others = [
            cell(votes=["81"], box=(700, 2841, 63, 28)),
            cell(votes=["128"], box=(1100, 2841, 63, 28)),
            oversized,
        ]
        line = E.row_line(row((2818, 2880), (300, 2841, 63, 28), others), CLASSES)
        assert line.window(1668.0) == (2841, 2873)

    def test_a_tilted_line_is_followed_across_the_page(self):
        # Measured on page 10 row 191-195: one printed line, boxes with tops
        # 2844, 2839, 2833, 2827 from left to right. A flat window would cut the
        # top off the rightmost cell's `406`.
        tilted = row(
            (2818, 2880),
            (857, 2844, 60, 29),
            [
                cell(votes=["81"], box=(1287, 2833, 39, 28)),
                cell(votes=["128"], box=(1474, 2827, 61, 28)),
                None,
            ],
        )
        line = E.row_line(tilted, CLASSES)
        assert line.slope < 0
        # The rightmost column's line is above the leftmost one's.
        assert line.window(1698.0)[0] < line.window(887.0)[0]

    def test_a_row_with_no_boxes_has_no_window(self):
        assert E.row_line(row((2818, 2880), None, [None, None, None]), CLASSES) is None


class TestProbeBox:
    def test_a_merged_box_is_replaced_by_geometry(self):
        centres = [300.0, 400.0, 500.0, 1100.0, 1668.0]
        oversized = cell(votes=[], box=(1668, 2820, 101, 58))
        r = row((2818, 2880), (300, 2841, 63, 28), [None, None, oversized])
        line = E.LineModel(intercept=2841.0, slope=0.0, height=28)
        box = E.probe_box(r, oversized, 4, centres, line)
        assert box[1] == 2843 and box[3] == 28

    def test_a_tight_box_is_kept(self):
        centres = [300.0, 400.0, 500.0, 1100.0, 1668.0]
        tight = cell(votes=["406"], box=(1679, 2841, 63, 28))
        r = row((2818, 2880), (300, 2841, 63, 28), [None, None, tight])
        line = E.LineModel(intercept=2841.0, slope=0.0, height=28)
        assert E.probe_box(r, tight, 4, centres, line) == (1679, 2841, 63, 28)

    def test_a_missing_cell_falls_back_to_geometry(self):
        centres = [300.0, 400.0, 500.0, 1100.0, 1668.0]
        r = row((2818, 2880), (300, 2841, 63, 28), [None, None, None])
        box = E.probe_box(r, None, 4, centres, None)
        assert box[0] < 1668 < box[0] + box[2]
