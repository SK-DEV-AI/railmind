"""Turn the scanned Railway Board fare circular into a machine-readable table.

STATUS (2026-10-26): measured against 120 slabs of page 10 that were read by
hand (`data/fare-truth-p10.json`), it now emits **no wrong fares**: 288 of 360
hand-read values reproduce exactly and the other 72 are reported missing rather
than guessed. Coverage is the remaining gap, not correctness, and it is not yet
good enough to price tickets from. See `docs/FARES.md` for the method and the
handoff. Do not wire its output into the product until coverage and the
cross-document check are green.

The next accuracy step is known and measured, not guessed: most missing values are
cells only one of the two passes read at all, so a third read of the same page at
800 dpi, aligned to the reference frame by column and row-band centres, turns
single reads into pairs. Scaling the high-resolution coordinates instead of
aligning them was tried and made things worse (see the traps below).

Why this exists
---------------
Indian Railways publishes its passenger fare table as a photocopy: 61 pages of
images with no text layer. `pdftotext` returns nothing. The fares are therefore
not available from any API we are allowed to use, but they *are* published, so
they can be read once, verified, and then computed locally forever.

This script does the reading. It is offline tooling and is never imported at
runtime. Running it again is how a future circular gets ingested.

How it reads a scanned grid
---------------------------
1. Render the page at 400 dpi and deskew it. The scan is a photocopy and its
   column rules lean by about a degree.
2. Ask tesseract for word boxes rather than text, so every number carries an
   (x, y) position. A table is then geometry, and OCR that returns garbage prose
   no longer matters.
3. Build one grid for the page: columns from the wide horizontal gaps between
   word positions, rows from a smoothed histogram of the words' own y positions,
   one peak per row. Both passes read the same grid, because a pass that derives
   its own grid can merge a row and shift every following row onto its
   neighbour's numbers.
4. Read the page twice with two segmentation modes and vote cell by cell. A value
   two reads agree on is used; a cell that one read produced and no other read
   confirms has no value at all, because a fare chosen from a tie, or from a lone
   read, is a plausible number nobody actually read.
5. Repair the two things the document itself defines: a slab starts one
   kilometre after the previous one ends, and the printed end of a slab is
   redundant because the next slab's start states it. Both repairs are counted.

Five failure modes, found the hard way:

- The last class column of the left half is printed hard against the column rule,
  so OCR returns `1310||` for `1310`. A token is accepted when it carries digits
  and no letters; `1121f` is a rejection, not a `1121`.
- Rows are not uniformly spaced, so bands cannot come from a constant pitch, and
the ink profile of the slab column is not good enough either: on page 10 it
yields 41 bands for 60 rows, since the scan's rows touch. A histogram of token y
positions has hundreds of samples per row and is what works.
- Reading a row as one strip mangles it: tesseract's line model is worse at a
  table row than its page model plus geometry, measured on page 11.
- At 400 dpi this scan reads `552` as `592`; at 800 dpi it reads `552`. A
  high-resolution pass therefore improves accuracy, but deskew translates the
  page differently at each resolution, so its rows must be matched to the
  reference frame by geometry before it can vote. Until that alignment exists,
  the pass is behind `FARE_HIGH_RES_PASS=1` and off by default.
- Header bands hold numbers too ("1", "2", "3" column indices), so the body starts
  at the first slab number, never at the top of the ink.

Nothing is repaired silently. A cell no read could confirm becomes a counted gap,
and the validator refuses to interpolate across it.

Usage
-----
    python scripts/extract_fare_table.py --pdf CC24_2025.pdf --out data/fare-table.json
    python scripts/extract_fare_table.py --pdf CC24_2025.pdf --pages 11 --report
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# --- the page plan -----------------------------------------------------------
#
# Read off the document by looking at each page's title band and column headers.
# Written down because OCR of a merged-cell header is unreliable, and because a
# revised circular must be re-checked by a human rather than trusted to a
# heuristic that guesses which class a column holds.
#
# Value: `page -> (section id, [class codes left to right])`. A page holds one
# section only. Tables set side by side carry the same classes in both, so the
# class list is a property of the section, not of the table.

#: The AC pages (12-16 of the current circular) print seven fare columns under
#: the heading "Basic fare of AC classes Mail Express w.e.f 26.12.2025": air
#: conditioned chair car, 3 economy, 3-tier, and then 2-tier and first class
#: twice each, because the circular sets a peak-season and a lean-season fare for
#: those two classes.
AC_CLASSES = ["CC", "3E", "3A", "2A_peak", "2A_lean", "1A_peak", "1A_lean"]

#: Rajdhani prints one table of five columns, under "Basic fare Rajdhani Exp.
#: w.e.f. 26.12.2025". It has no chair car and no 3-economy column: the train is
#: all air-conditioned, and its cheapest accommodation is 3-tier.
RAJDHANI_CLASSES = ["3A", "2A_peak", "2A_lean", "1A_peak", "1A_lean"]

#: The non-AC pages print three columns headed "Second Class", "Sleeper Class"
#: and "First Class". The circular never prints a code for any of them; these are
#: the codes IRCTC sells those accommodations under, and the fare engine keys on
#: codes, not on headings. First Class is `FC` -- not `1S`, which some timetable
#: dumps use for a class this circular does not have a column for.
NON_AC_CLASSES = ["2S", "SL", "FC"]

PLAN: dict[int, tuple[str, list[str]]] = {
    10: ("mail_express_non_ac", NON_AC_CLASSES),
    11: ("mail_express_non_ac", NON_AC_CLASSES),
    12: ("mail_express_ac", AC_CLASSES),
    13: ("mail_express_ac", AC_CLASSES),
    14: ("mail_express_ac", AC_CLASSES),
    15: ("mail_express_ac", AC_CLASSES),
    16: ("mail_express_ac", AC_CLASSES),
    18: ("rajdhani", RAJDHANI_CLASSES),
    19: ("rajdhani", RAJDHANI_CLASSES),
    20: ("rajdhani", RAJDHANI_CLASSES),
}

#: Which classes a section's columns hold, in printed order.
SECTION_CLASSES = {
    "mail_express_non_ac": NON_AC_CLASSES,
    "mail_express_ac": AC_CLASSES,
    "rajdhani": RAJDHANI_CLASSES,
}

#: Junk characters tolerated around a number before the token is rejected. A
#: photographed column rule contributes one or two.
MAX_JUNK = 2

#: Below this slab start, a fare is dominated by the minimum fare rather than by
#: distance, so rupees per kilometre is not a meaningful sanity check there: the
#: genuine minimum of 31 rupees over 3 kilometres reads as 10 rupees per
#: kilometre, which looks impossible and is correct.
LONG_SLAB_KM = 100

#: A printed column rule is a few pixels wide; the widest one seen on these pages
#: is 8. Anything wider than this is left alone rather than guessed at, so a real
#: glyph is never whitened out of a crop.
RULE_MAX_WIDTH = 12

#: Extra columns cleared on each side of a rule, for its blurred edge.
RULE_EDGE = 2

#: The steepest scan skew worth fitting, in pixels of rise per pixel of width.
#: Measured skew on page 10 is 0.028; anything above this is not a tilted scan but
#: boxes taken from two different printed lines, and the fit is refused.
MAX_SKEW = 0.08

#: The enlargements a crop is read at. Two renderings, because tesseract's answer
#: moves with scale: at three times size it read slab 21-25's `47` as `7` in two
#: of three modes, while two times and four times size read `47` in all three.
CROP_SCALES = (2, 4)


# --- tokens ------------------------------------------------------------------


def read_number(raw: str) -> str | None:
    """Digits from an OCR token, or None when the token cannot be trusted.

    Scanned column rules touch the digits, so `1310||` must read as 1310. A
    letter means the reader hallucinated, so `1121f` must not read as anything.
    Separating those two cases is what keeps a rule character from becoming a
    silent digit and a hallucination from becoming a silent fare.
    """
    digits = "".join(character for character in raw if character.isdigit())
    if not digits or len(digits) > 5:
        return None
    if any(character.isalpha() for character in raw):
        return None
    if len(raw) - len(digits) > MAX_JUNK:
        return None
    return digits


#: One pass's reading of one cell: the digits it produced, and the pixel box of
#: the token they came from so the cell can be re-read in isolation later.
Scrap = tuple[str, tuple[int, int, int, int] | None]


@dataclass
class Cell:
    """One number in one cell, with every reading of it that was attempted.

    Three independent reads are collected rather than one, because the failure
    here is not noise but specific glyph runs the photocopier blurred: at 400 dpi
    this scan reads `552` as `592`, and at 800 dpi it reads `552`. The passes
    disagree on those cells and agree everywhere else, so a majority vote is
    strong evidence and a lone vote is not.
    """

    page: int
    x: int
    y: int
    votes: list[str] = field(default_factory=list)
    #: Pixel box of the token this cell came from, so a re-read can crop exactly
    #: that glyph run instead of guessing at a column boundary.
    box: tuple[int, int, int, int] | None = None
    conf: float = 0.0

    @property
    def text(self) -> str | None:
        """The value the reads agree on, or None when they do not agree.

        A tie is not resolved by picking one: three passes that all disagree mean
        nobody has read the cell, and a fare chosen from a tie is exactly the kind
        of plausible number this project refuses to show.
        """
        if not self.votes:
            return None
        counts = Counter(self.votes)
        value, support = counts.most_common(1)[0]
        if support < 2:
            # One read is not a value. Measured on page 10: the only two wrong
            # fares in 360 hand-read ones were both cells that a single pass read
            # confidently and no other pass read at all (`438` for `48`). A lone
            # read of a scanned digit run is a guess that looks like a
            # measurement.
            return None
        return value

    @property
    def value(self) -> int | None:
        text = self.text
        return int(text) if text else None

    @property
    def support(self) -> int:
        """How many reads produced the winning answer."""
        text = self.text
        return self.votes.count(text) if text else 0

    @property
    def confirmed(self) -> bool:
        """At least two independent reads agree. One read alone is not evidence."""
        return self.support >= 2

    @property
    def disputed(self) -> bool:
        """Reads exist and no two of them agree."""
        return bool(self.votes) and not self.confirmed


@dataclass
class Row:
    """One distance slab: `start-end` plus one value per class column."""

    page: int
    section: str
    half: int
    y: int
    start_cell: Cell
    band: tuple[int, int] = (0, 0)
    end_cell: Cell | None = None
    values: dict[str, Cell | None] = field(default_factory=dict)
    repaired: int = 0
    #: The end this slab must have, taken from where the next slab starts. It is
    #: not a vote and cannot be one: it comes from a different row's number, which
    #: the document itself repeats as that row's identity.
    implied_end: int | None = None
    #: The start this slab must have, taken from where the slab above ended. Also
    #: not a vote: the number is printed twice, and one of the two copies is
    #: legible.
    implied_start: int | None = None

    @property
    def start(self) -> int | None:
        if self.implied_start is not None:
            return self.implied_start
        return self.start_cell.value

    @property
    def end(self) -> int | None:
        """
        The end taken from where the next slab starts, when the two disagree.

        The next slab's start is the same number this slab prints as its end, so
        it is a second reading of the same fact rather than a different one. It
        wins because it is also that row's identity: a wrong start moves a row,
        which is visible, where a wrong end only shortens a slab. Measured on
        page 13 of the current circular, which prints `470` and is read as `0`
        by both passes; trusting the printed end there costs the row all seven
        of its fares, since a slab that does not span any distance is discarded.
        """
        if self.implied_end is not None:
            return self.implied_end
        printed = self.end_cell.value if self.end_cell else None
        return printed


# --- rendering and OCR -------------------------------------------------------


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_page(pdf: Path, page: int, dpi: int, cache: Path) -> Path:
    """A deskewed PNG of one page, cached by (pdf digest, page, dpi)."""
    key = file_digest(pdf)[:16]
    out = cache / f"{key}-p{page}-{dpi}.png"
    if out.exists():
        return out
    cache.mkdir(parents=True, exist_ok=True)
    # `pdftoppm -singlefile` appends its own extension, so the root passed in
    # must not carry one; the intermediate is named separately.
    raw_root = cache / f"{key}-p{page}-{dpi}-raw"
    raw = raw_root.with_suffix(".png")
    subprocess.run(
        [
            "pdftoppm",
            "-r",
            str(dpi),
            "-f",
            str(page),
            "-l",
            str(page),
            "-png",
            "-singlefile",
            str(pdf),
            str(raw_root),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["magick", str(raw), "-deskew", "40%", "-colorspace", "Gray", str(out)],
        check=True,
        capture_output=True,
    )
    raw.unlink(missing_ok=True)
    return out


def ocr_words(png: Path, psm: str, scale: float = 1.0) -> list[dict]:
    """Numeric words with positions, from one tesseract segmentation mode.

    `scale` converts the coordinates back to the reference resolution, so three
    passes rendered at different sizes can be compared cell by cell.
    """
    result = subprocess.run(
        ["tesseract", str(png), "-", "--psm", psm, "tsv"],
        check=True,
        capture_output=True,
        text=True,
    )
    reader = csv.DictReader(result.stdout.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE)
    words: list[dict] = []
    for record in reader:
        raw = (record.get("text") or "").strip()
        if not raw:
            continue
        text = read_number(raw)
        if text is None:
            continue
        try:
            left = int(record["left"])
            width = int(record["width"])
            height = int(record["height"])
            top = int(record["top"])
            words.append(
                {
                    "x": int((left + width // 2) / scale),
                    "y": int((top + height // 2) / scale),
                    "text": text,
                    "conf": float(record["conf"]),
                    "box": (
                        int(left / scale),
                        int(top / scale),
                        max(1, int(width / scale)),
                        max(1, int(height / scale)),
                    ),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return words


# --- geometry ----------------------------------------------------------------


def group_columns(words: list[dict], gap: int = 60) -> list[list[dict]]:
    """Group words into vertical columns wherever a wide horizontal gap appears.

    Ten columns of numbers have nine wide gaps between them and narrow jitter
    inside them, so one pass over the sorted x positions recovers the grid with
    no template. Gap grouping is used rather than k-means because a handful of
    stray tokens must not move a cluster centre, which is exactly what k-means
    did when tried.
    """
    if not words:
        return []
    ordered = sorted(words, key=lambda w: w["x"])
    groups: list[list[dict]] = [[ordered[0]]]
    for word in ordered[1:]:
        if word["x"] - groups[-1][-1]["x"] > gap:
            groups.append([word])
        else:
            groups[-1].append(word)
    # A column holds one number per row, so a group of two words is a speck of
    # dirt or a page number, not a column. Dropping it loses at most a cell or
    # two, which the validator then reports as missing rather than inventing.
    if len(groups) > 3:
        sizes = sorted(len(group) for group in groups)
        floor = max(3, sizes[len(sizes) // 2] // 5)
        groups = [group for group in groups if len(group) >= floor]
    return groups


def column_centres(columns: list[list[dict]]) -> list[float]:
    centres = []
    for column in columns:
        xs = sorted(word["x"] for word in column)
        centres.append(float(xs[len(xs) // 2]))
    return centres


def column_of(x: int, centres: list[float]) -> int:
    return min(range(len(centres)), key=lambda index: abs(centres[index] - x))


def median_pitch(ys: list[int]) -> float:
    unique = sorted(set(ys))
    gaps = [b - a for a, b in itertools.pairwise(unique) if b - a > 1]
    if not gaps:
        return 56.0
    gaps.sort()
    return float(gaps[len(gaps) // 2])


def load_gray(png: Path) -> np.ndarray:
    """The page as an 8-bit greyscale array, via ImageMagick's PGM output."""
    raw = subprocess.run(
        ["magick", str(png), "-colorspace", "Gray", "pgm:-"],
        check=True,
        capture_output=True,
    ).stdout
    offset = 0
    fields: list[bytes] = []
    while len(fields) < 4:
        end = raw.index(b"\n", offset)
        line = raw[offset:end]
        offset = end + 1
        if line.startswith(b"#"):
            continue
        fields += line.split()
    width, height = int(fields[1]), int(fields[2])
    return np.frombuffer(raw[offset : offset + width * height], dtype=np.uint8).reshape(
        height, width
    )


def row_ruler(
    words: list[dict], centres: list[float], half: int, width: int
) -> list[tuple[int, int]]:
    """Row bands from a histogram of where the numbers sit, one peak per row.

    A row is a line of print, so the numbers are the best evidence of where the
    rows are. Two cheaper rulers were measured on page 10 of the current circular
    and both fail:

    - the ink profile of the slab column returns 41 bands for 60 rows, because
      the scan's rows touch and two touching rows arrive as one band;
    - grouping the y positions by the median gap returns 216 bands for 60 rows,
      because the two OCR passes read the same page and their positions differ by
      a pixel or two, which is what the median ends up measuring.

    One lost or gained band is not one lost row: `cells_from_pass` takes the
    token nearest each band's middle, so a shifted band hands a row its
    neighbour's fare. A histogram smoothed over a few pixels is immune to both
    problems, since hundreds of tokens per row vote for one peak and no single
    misread can move it.

    Peaks are picked strongest first with a minimum separation of a third of the
    page's row pitch, so a smudge between two rows cannot become a row of its own.
    """
    columns = range(half * width, (half + 1) * width)
    ys = [word["y"] for word in words if column_of(word["x"], centres) in columns]
    if len(ys) < 4:
        return []
    low = min(ys)
    bins = np.zeros(max(ys) - low + 1, dtype=float)
    for y in ys:
        bins[y - low] += 1.0
    # A row's tokens are within about ten pixels of each other; smoothing over
    # nine makes one peak out of them and leaves the ~100 pixel gap between rows
    # empty.
    smooth = np.convolve(bins, np.ones(9) / 9.0, mode="same")
    floor = float(smooth.max()) * 0.25
    # Peaks closer than this are one row. It has to be smaller than the gap between
    # a block's column-index header and its first data line, which is printed
    # tighter than the row pitch: at 30 the header and the row under it merged into
    # one band, and because the band then held a slab-column word the trim below
    # treated the merged band as the header and dropped a printed row with it
    # (2251-2275, page 11, measured from the page image).
    separation = 20
    peaks: list[int] = []
    for index in np.argsort(smooth)[::-1]:
        index = int(index)
        if smooth[index] < floor:
            break
        if all(abs(index - taken) > separation for taken in peaks):
            peaks.append(index)
    if not peaks:
        return []
    peaks.sort()
    peaks = [peak + low for peak in peaks]

    # Bands are the midpoints between peaks, so a band ends where the next row
    # begins and no token can be nearer two rows than one.
    edges = [peaks[0] - (peaks[1] - peaks[0]) // 2 if len(peaks) > 1 else peaks[0] - 40]
    edges += [(above + below) // 2 for above, below in itertools.pairwise(peaks)]
    edges.append(peaks[-1] + (peaks[-1] - peaks[-2]) // 2 if len(peaks) > 1 else peaks[-1] + 40)
    bands = [(edges[index], edges[index + 1]) for index in range(len(peaks))]

    # The body starts where the slab column starts. Above it is one header row of
    # column indices ("1", "2", "3"), which is real printed ink and would
    # otherwise be read as the first fare row.
    slab_ys = [word["y"] for word in words if column_of(word["x"], centres) == half * width]
    first = 0
    for index, (top, bottom) in enumerate(bands):
        if any(top <= y < bottom for y in slab_ys):
            first = index
            break
    return bands[first:]


def page_grid(
    words: list[dict], classes: list[str]
) -> tuple[list[float], dict[int, list[tuple[int, int]]], int] | None:
    """Column centres, row bands and table count for a page, computed once.

    The distance axis is printed in two ways in the same circular: the non-AC
    pages set two half tables side by side to fit 240 rows, and the AC pages set
    one wide table because their rows are twice as coarse. Which one a page is
    follows from its column count alone, so the plan carries classes, not layout,
    and a page cannot be read with the wrong layout by accident.

    Both segmentation modes read the same words, so they must share one grid:
    letting each pass derive its own bands let a single merged row shift every
    later ordinal, which showed up as fifty "disputed" cells that were really one
    alignment error. The grid comes from geometry alone (word positions, and the
    ink profile only when a row has no readable number at all), so it cannot
    depend on any pass agreeing with any other.
    """
    columns = group_columns(words)
    width = 2 + len(classes)
    if len(columns) == 2 * width:
        tables = 2
    elif len(columns) == width:
        tables = 1
    else:
        return None
    centres = column_centres(columns)
    bands = {half: row_ruler(words, centres, half, width) for half in range(tables)}
    return centres, bands, tables


def cells_from_pass(
    words: list[dict],
    centres: list[float],
    bands: dict[int, list[tuple[int, int]]],
    classes: list[str],
) -> dict[int, list[list[Scrap]]]:
    """This pass's text for every cell of the shared grid, rows in order.

    Each cell carries the pixel box of the token the text came from, so a cell
    nobody could settle can be re-read as isolated glyphs instead of being read
    again in a crowd of neighbouring columns.
    """
    width = 2 + len(classes)

    def text_at(
        column_index: int, top: int, bottom: int
    ) -> tuple[str, tuple[int, int, int, int] | None]:
        candidates = [
            word
            for word in words
            if column_of(word["x"], centres) == column_index and top - 3 <= word["y"] <= bottom + 3
        ]
        if not candidates:
            return "", None
        middle = (top + bottom) / 2
        word = min(candidates, key=lambda word: abs(word["y"] - middle))
        return word["text"], word.get("box")

    return {
        half: [
            [text_at(index, top, bottom) for index in range(half * width, (half + 1) * width)]
            for top, bottom in bands[half]
        ]
        for half in sorted(bands)
    }


def merge_passes(
    readings: list[dict[int, list[list[Scrap]]]],
    bands: dict[int, list[tuple[int, int]]],
    page: int,
    section: str,
    classes: list[str],
) -> list[Row]:
    """Vote the passes cell by cell on the shared grid.

    The box of the first pass that saw a token is kept on the cell. Boxes from
    different passes are not averaged: a re-read wants one real glyph run, not a
    rectangle that belongs to none of them.
    """
    width = 2 + len(classes)
    rows: list[Row] = []
    for half in sorted(bands):
        count = max(len(reading[half]) for reading in readings)
        for index in range(count):
            cells: list[Cell] = []
            for column_index in range(width):
                seen = [
                    reading[half][index][column_index]
                    for reading in readings
                    if index < len(reading[half]) and reading[half][index][column_index][0]
                ]
                box = next((entry[1] for entry in seen if entry[1]), None)
                cells.append(
                    Cell(page=page, x=0, y=index, votes=[entry[0] for entry in seen], box=box)
                )
            if not cells[0].votes:
                # A row whose slab number could not be read at all is still a row
                # if its fares were read: the number is recoverable from the row
                # above, and dropping it here would lose five good prices to one
                # blank cell. A band with neither a number nor two fares is not a
                # row and is skipped.
                if sum(1 for cell in cells[2:] if cell.votes) < 2:
                    continue
            row = Row(
                page=page,
                section=section,
                half=half,
                y=index,
                start_cell=cells[0],
                band=bands[half][index],
                end_cell=cells[1],
            )
            for offset, class_code in enumerate(classes, start=2):
                row.values[class_code] = cells[offset]
            rows.append(row)
    return rows


def read_page(
    page: int,
    png: Path,
    image: np.ndarray,
    section: str,
    classes: list[str],
    cache: Path,
    pdf: Path | None = None,
) -> list[Row]:
    """All rows on one page, voted from independent readings of the shared grid."""
    primary = ocr_words(png, "6")
    secondary = ocr_words(png, "11")
    if not primary and not secondary:
        raise ValueError(f"page {page}: no numeric words found")
    grid = page_grid(primary + secondary, classes)
    if grid is None:
        raise ValueError(
            f"page {page}: {2 + len(classes)} or {2 * (2 + len(classes))} numeric columns "
            "expected; the plan does not match the document"
        )
    centres, bands, _tables = grid
    readings = [
        cells_from_pass(batch, centres, bands, classes) for batch in (primary, secondary) if batch
    ]
    rows = merge_passes(readings, bands, page, section, classes)
    settled = resolve_unsettled(rows, image, cache / f"cell-{page}.pgm", centres, classes)
    if settled:
        print(f"page {page}: {settled} unsettled cells confirmed by an isolated re-read")
    # Slab repair is deliberately not done here. A half table's first row has no
    # predecessor on its own page, and in this circular the non-AC table's page 11
    # opens with the row that page 10's last line ends: `891 - 900` closes page 10
    # and `901 - 910` opens page 11. Repairing page by page could not see that
    # boundary, so both rows were lost -- one for a missing end (settled only by
    # the next row's start) and one for a missing start (settled only by the
    # previous row's end). `main` repairs the whole section in reading order.
    return rows


def repair_slabs(rows: list[Row]) -> list[Row]:
    """Make the slab column tile the distance axis, as the document defines it.

    A slab starts one kilometre after the one above it ends and ends one
    kilometre before the one below it starts: the table states every boundary
    twice, once as a start and once as an end. That redundancy is what makes a
    single misread recoverable from the page itself. Three shapes occur, and all
    three are repaired from the neighbour that repeats the same number:

    - a start that overlaps its predecessor is a misread of that boundary;
    - a start that could not be read at all (the two passes disagreed, or only
      one pass produced a number) is the previous end plus one;
    - an end that disagrees with where the next slab starts is a misread of it.

    Each repair is counted. These are the document's own rules, not inferences
    about fares. A *gap* is still left alone: it means a row was missed entirely,
    and filling a gap would invent a slab that the engine would then price.

    Order matters within a walk: a row's end comes from the next row's start, so
    the starts are settled first and an implied start can then settle the end
    above it. Two walks are enough for any chain of adjacent unreadable numbers,
    since one walk makes every start readable and the second makes every end.

    The walk covers a whole section, ordered by page, then half table, then the
    row's position on the page, because that is the order the document reads in.
    Chaining within one page is not enough: the number that settles a page's first
    row is printed on the previous page.
    """
    for _ in range(2):
        for section in sorted({row.section for row in rows}):
            ordered = sorted(
                (row for row in rows if row.section == section),
                key=lambda row: (row.page, row.half, row.y),
            )
            previous_end: int | None = None
            for index, row in enumerate(ordered):
                following = ordered[index + 1] if index + 1 < len(ordered) else None
                start, end = row.start, row.end

                if previous_end is not None:
                    boundary = previous_end + 1
                    if start is None:
                        # Only when the slab would still span a distance; a row
                        # whose end is above this boundary is a real row, an
                        # empty band is not.
                        if end is None or boundary < end:
                            row.implied_start = boundary
                            row.repaired += 1
                    elif start < boundary and end is not None and end > previous_end:
                        row.implied_start = boundary
                        row.repaired += 1

                if following is not None:
                    next_start = following.start
                    if next_start is not None and start is not None and next_start > start + 1:
                        implied = next_start - 1
                        if row.end != implied:
                            # Recorded as an implication, not as a reading. Writing
                            # it into a cell gave it one vote, which the two-read
                            # rule correctly threw away, leaving the row with no
                            # end; `validate` then dropped the whole row and lost
                            # every fare on it to a redundant column.
                            row.implied_end = implied
                            row.repaired += 1

                if row.end is not None:
                    previous_end = row.end
    return rows


def crop_reads(
    image: np.ndarray, box: tuple[int, int, int, int] | None, scratch: Path
) -> list[str]:
    """Read one token's glyph run on its own, twice, as a second opinion.

    The whole-page passes fail on a handful of cells, always the same way: a
    blurred glyph run in a crowd of neighbouring columns. Cropping exactly the
    box a pass reported, and enlarging it, changes the reader's problem from "a
    table" to "one number", which is the case tesseract is good at.

    Three segmentation modes are used because they fail differently: treating the
    crop as one text line, as one word, and as a raw line with no layout analysis
    at all. Two of the three agreeing is therefore evidence within one rendering.

    The third mode is not padding. Measured on page 10's `900` (a clean crop that
    a human reads without hesitation), `--psm 7` returns `1111`, `800` and `300`
    depending only on how the crop was scaled, while `--psm 8` and `--psm 13`
    return `900` at every scale tried. Two modes that both fail on the same cell
    are not two opinions, and requiring exactly two agreeing reads meant a cell
    that two of three modes read correctly stayed unread.

    Scale alone moves the answer too, so the crop is enlarged twice and the two
    renderings each have to reach their own majority. Measured on slab 21-25's
    Sleeper `47`: at three times size, one mode read `47` and two read `7`; at two
    and four times size every mode read `47`. Returning one vote per rendering
    keeps a rendering that lost a glyph from speaking for the whole crop, which is
    what the caller's two-source rule needs: agreement between renderings, not
    between settings.
    """
    cell = cell_crop(image, box, 6)
    if cell.size == 0:
        return []
    scratch.parent.mkdir(parents=True, exist_ok=True)
    rendering_values: list[str] = []
    for scale in CROP_SCALES:
        enlarged = np.pad(
            np.kron(cell, np.ones((scale, scale), dtype=np.uint8)), 24, constant_values=255
        )
        scratch.write_bytes(
            b"P5\n%d %d\n255\n" % (enlarged.shape[1], enlarged.shape[0]) + enlarged.tobytes()
        )
        readings: list[str] = []
        for mode in ("7", "8", "13"):
            result = subprocess.run(
                [
                    "tesseract",
                    str(scratch),
                    "-",
                    "--psm",
                    mode,
                    "-c",
                    "tessedit_char_whitelist=0123456789",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            value = read_number(result.stdout.strip())
            if value is not None:
                readings.append(value)
        if len(readings) >= 2:
            candidate, support = Counter(readings).most_common(1)[0]
            if support >= 2:
                rendering_values.append(candidate)
    return rendering_values


def _readable_fares(row: Row, classes: list[str]) -> int:
    """How many of a row's class columns carry a value two reads agreed on."""
    return sum(1 for code in classes if (cell := row.values.get(code)) and cell.confirmed)


def cell_box(
    row: Row,
    column_index: int,
    centres: list[float],
    margin: int = 8,
    window: tuple[int, int] | None = None,
) -> tuple[int, int, int, int]:
    """The rectangle a column of a row occupies, from the shared grid.

    Used for the cells no whole-page pass produced any token for at all: the
    geometry still says where that number would be printed, and an isolated read
    of exactly that rectangle is a real measurement, not a guess. The rectangle
    stops short of its neighbours so a re-read cannot return the column beside it.
    A `window` overrides the band's top and bottom for a row whose band is taller
    than the line the row actually prints.
    """
    centre = centres[column_index]
    gap_left = centre - max([x for x in centres if x < centre], default=centre - 120)
    gap_right = min([x for x in centres if x > centre], default=centre + 120) - centre
    half_width = max(12.0, min(gap_left, gap_right) / 2 - margin)
    top, bottom = window or row.band
    return (round(centre - half_width), top + 2, round(2 * half_width), max(4, bottom - top - 4))


@dataclass(frozen=True)
class LineModel:
    """Where a row's printed line sits, as a function of the column.

    A scan is not level. Measured on page 10's row 191-195, the word boxes of one
    printed line have tops 2827, 2833, 2839 and 2844 from the right-hand column to
    the left: the paper is skewed by about 17 pixels across the table, so a single
    horizontal window is 17 pixels wrong at one end of it. That is how the FC cell
    of that row read as `011`: the crop cut the top off its `406`. The skew is a
    line, the row's own cells are the points on it, and fitting it costs nothing
    because those boxes were already read.
    """

    intercept: float
    slope: float
    height: int

    def window(self, centre: float) -> tuple[int, int]:
        top = round(self.intercept + self.slope * centre)
        return top, top + self.height + 4


def row_line(row: Row, classes: list[str]) -> LineModel | None:
    """The row's own printed line, fitted across the columns.

    A band is a slice of the shared grid, and where two printed values merge into
    one token a whole-page pass emits a single word box spanning both lines.
    Measured on page 10, the FC cell of slab 191-195 was such a box -- 101x58
    against 63x29 for every neighbouring cell -- and an isolated read of it
    returned `408`, a blend of the `406` printed on that line and the `414`
    printed on the next. The row's other columns still carry tight boxes on the
    one line the row occupies, so they say where that line is: each contributes a
    point, and the line through them gives the window of a column that has no box
    of its own. Returns None when the row has no box to learn from, which leaves
    the band in charge.
    """
    cells = [row.start_cell, row.end_cell, *(row.values.get(code) for code in classes)]
    boxes = [cell.box for cell in cells if cell and cell.box]
    if not boxes:
        return None
    heights = sorted(box[3] for box in boxes)
    typical = heights[len(heights) // 2]
    tight = [box for box in boxes if box[3] <= 1.5 * typical]
    xs = [box[0] + box[2] / 2 for box in tight]
    ys = [float(box[1]) for box in tight]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    slope = 0.0
    intercept = mean_y
    spread = sum((x - mean_x) ** 2 for x in xs)
    if len(xs) >= 2 and spread > 0:
        slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / spread
        intercept = mean_y - slope * mean_x
        if abs(slope) > MAX_SKEW:
            # A fit that steep is not a tilted scan, it is boxes from two lines.
            slope, intercept = 0.0, mean_y
    return LineModel(intercept=intercept, slope=slope, height=typical)


def probe_box(
    row: Row,
    cell: Cell | None,
    column_index: int,
    centres: list[float],
    line: LineModel | None,
) -> tuple[int, int, int, int]:
    """The rectangle to re-read for one cell of one row.

    The cell's own box when a pass gave it a tight one on this row's line, the
    grid geometry otherwise. A box taller than the row's line is a merged token
    spanning a neighbouring line, which is how slab 191-195's `406` came back as
    `408`: the box held the `414` printed below it too.
    """
    box = cell.box if cell else None
    window = line.window(centres[column_index]) if line is not None else None
    if box is None or (line is not None and box[3] > 1.5 * line.height):
        return cell_box(row, column_index, centres, window=window)
    return box


_SECOND_ENGINE: object | None = None
_SECOND_ENGINE_MISSING = False


def second_engine():
    """An OCR engine whose failure modes are unrelated to tesseract's, or None.

    Tesseract's modes are not independent evidence: measured on page 10, all
    three crop modes agreed on `2` where the page prints `40`, and both whole-page
    passes agreed on `408` where the page prints `406`. Two settings of one reader
    can be confidently wrong together, so a cell that matters gets a reader built
    on a different model (PP-OCR via RapidOCR, CPU, ONNX, no torch).

    Absent, the extractor still runs: it settles only what tesseract can settle
    twice, and leaves the rest unread, which costs coverage and never a wrong
    price.
    """
    global _SECOND_ENGINE, _SECOND_ENGINE_MISSING
    if _SECOND_ENGINE is not None or _SECOND_ENGINE_MISSING:
        return _SECOND_ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        _SECOND_ENGINE_MISSING = True
        print(
            "note: rapidocr-onnxruntime is not installed, so unsettled cells get one "
            "reader instead of two; install it with `uv run --extra fare ...`",
            file=sys.stderr,
        )
        return None
    _SECOND_ENGINE = RapidOCR()
    return _SECOND_ENGINE


GLYPH_CONFUSIONS = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "S": "5"})


def normalize_digits(text: str) -> str | None:
    """A digit string, allowing the glyph pairs this engine actually confuses.

    Measured on page 10: it reads the printed `235` as `23S`. The map stays this
    narrow on purpose. `A` is not in it, so the read `4061A` of a cell that prints
    `406` is still refused rather than turned into `40614` -- refusing keeps the
    fare unread, guessing would price the journey wrongly.
    """
    translated = text.translate(GLYPH_CONFUSIONS)
    if not translated.isdigit():
        return None
    return translated


def trim_rule(ink: np.ndarray) -> np.ndarray:
    """Drop full-height bars from a crop, wherever they are in it.

    The cell box is a slice of the grid, and the grid's outer border is a dark
    vertical band a few pixels wide. Handed to a crop reader, that band is ink and
    comes back as characters: the printed `406` returned `4061A`, with the `1A`
    being the border. Digits never fill the height of their cell, so a column of
    ink that does is a rule, not a numeral.

    A rule is not always at the edge. A pass reported slab 21-25's `47` with a box
    wide enough to reach the column separator, and both readers then took the
    separator for a `1` -- `471`. They agreed with each other because the `1` was
    really in the picture, which is exactly how a bad crop defeats a two-source
    rule. Interior rules are therefore whitened, not just trimmed off the ends.

    `ink` holds grey levels, so it has to be thresholded before it can be counted.
    Summing brightness directly makes every white column look like solid ink.
    """
    if ink.size == 0:
        return ink
    filled = (ink < 128).sum(axis=0)
    solid = filled >= 0.9 * ink.shape[0]
    cleaned = ink
    for start, end in _solid_runs(solid):
        # A numeral stroke is a vertical run of ink, but it cannot span the whole
        # crop: the crop is the text line plus padding above and below. A run
        # wider than any stroke is a printed rule.
        if start == 0 or end == len(solid) or end - start > RULE_MAX_WIDTH:
            continue
        if cleaned is ink:
            cleaned = ink.copy()
        # A rule is a few blurred pixels wider than its solid core: measured on
        # slab 191-195, the columns either side of the core still carried ink for
        # 33 of 40 rows, enough for a reader to call them a `1`. The edges go too.
        cleaned[:, max(0, start - RULE_EDGE) : min(len(solid), end + RULE_EDGE)] = 255
    solid = (cleaned < 128).sum(axis=0) >= 0.9 * ink.shape[0]
    left = 0
    while left < len(solid) and solid[left]:
        left += 1
    right = len(solid)
    while right > left and solid[right - 1]:
        right -= 1
    if right - left < cleaned.shape[1] // 4:
        # The crop is mostly rule; trimming would leave nothing to read.
        return ink
    return cleaned[:, left:right]


def _solid_runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """The half-open index ranges where `flags` is true, in order."""
    runs: list[tuple[int, int]] = []
    start: int | None = None
    for index, flag in enumerate(flags):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            runs.append((start, index))
            start = None
    if start is not None:
        runs.append((start, len(flags)))
    return runs


def cell_crop(image: np.ndarray, box: tuple[int, int, int, int] | None, pad: int) -> np.ndarray:
    """The pixels one cell's box covers, padded and stripped of printed rules.

    Both crop readers get the same prepared picture, so neither can score a point
    against the other by reading the grid's own lines.
    """
    if box is None:
        return np.zeros((0, 0), dtype=np.uint8)
    left, top, width, height = box
    cell = image[max(0, top - pad) : top + height + pad, max(0, left - pad) : left + width + pad]
    if cell.size == 0:
        return cell
    return trim_rule(cell)


def second_engine_probe(
    image: np.ndarray, box: tuple[int, int, int, int] | None, scratch: Path
) -> tuple[str | None, str, float]:
    """Read one cell with the unrelated engine: (value, raw text, confidence)."""
    engine = second_engine()
    if engine is None or box is None:
        return None, "", 0.0
    cell = cell_crop(image, box, 8)
    if cell.size == 0:
        return None, "", 0.0
    padded = np.pad(cell, 32, constant_values=int(np.percentile(cell, 95)))
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_bytes(b"P5\n%d %d\n255\n" % (padded.shape[1], padded.shape[0]) + padded.tobytes())
    result, _ = engine(str(scratch))
    if not result:
        return None, "", 0.0
    raw = "".join(entry[1] for entry in result)
    score = min(entry[2] for entry in result)
    return normalize_digits(raw), raw, score


def second_engine_read(
    image: np.ndarray, box: tuple[int, int, int, int] | None, scratch: Path
) -> str | None:
    """One cell, read by the unrelated engine. None when it has no opinion."""
    return second_engine_probe(image, box, scratch)[0]


def resolve_unsettled(
    rows: list[Row], image: np.ndarray, scratch: Path, centres: list[float], classes: list[str]
) -> int:
    """Give every cell no two reads agreed on another, isolated read.

    A cell the whole-page passes could not settle is cropped and handed to two
    readers built on different models, and settled only when two of the three
    sources agree: the page passes, the tesseract crop consensus, and the second
    engine's read of the same crop. Returns how many cells were settled.

    Three sources, two opinions, and the sources are not interchangeable. A crop
    is one image read twice, so its two readers are independent; tesseract's own
    modes are not, because all three measured modes agreed on `2` where the page
    prints `40`. A page pass and a crop read are also independent of each other,
    which is why they corroborate. What is refused: one source alone. Every wrong
    fare this table has produced came from believing a single confident reader.
    """
    width = 2 + len(classes)
    settled = 0
    scratch = scratch.with_suffix(".cell.pgm")
    for row in rows:
        line = row_line(row, classes)
        columns = [
            (row.start_cell, row.half * width),
            (row.end_cell, row.half * width + 1),
            *(
                (row.values.get(code), row.half * width + 2 + offset)
                for offset, code in enumerate(classes)
            ),
        ]
        for cell, column_index in columns:
            if cell is None or cell.confirmed:
                continue
            box = probe_box(row, cell, column_index, centres, line)
            extra = crop_reads(image, box, scratch)
            page_values = set(cell.votes)
            page_value = page_values.pop() if len(page_values) == 1 else None
            crop_value = None
            if len(extra) >= 2:
                counts = Counter(extra)
                candidate, support = counts.most_common(1)[0]
                if support >= 2:
                    crop_value = candidate
            second = second_engine_read(image, box, scratch)

            chosen = None
            if crop_value is not None and second is not None and crop_value == second:
                # Two models, one image: the strongest evidence available here.
                chosen = crop_value
            elif page_value is not None and page_value in (crop_value, second):
                chosen = page_value
            if chosen is None:
                continue
            cell.votes = [*cell.votes, chosen, chosen]
            settled += 1
    return settled


# --- validation --------------------------------------------------------------


@dataclass
class Report:
    section: str
    class_code: str
    rows: int = 0
    confirmed: int = 0
    single: int = 0
    disputed: int = 0
    missing: int = 0
    dropped: list[str] = field(default_factory=list)
    out_of_order: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


def validate(rows: list[Row], section: str, classes: list[str]) -> tuple[list[dict], list[Report]]:
    """Merge halves, check the table against its own shape, and refuse to fill holes.

    These checks are the whole reason to trust an OCR'd price list, because they
    need no second document: a fare table carries its own proof of correctness in
    its shape.

    - slab continuity: every slab starts one kilometre after the last ended,
      which catches a dropped, duplicated or misread row;
    - monotonicity: a longer journey never costs less;
    - digit-count consistency: fares rise smoothly, so a value printed with more
      or fewer digits than the values either side of it is a misread. This is
      what separates a garbled `6877` from its neighbours `688` and `696`, where
      monotonicity alone cannot say which of the two rows is the wrong one;
    - rate sanity on long slabs: implied rupees per kilometre stays inside a
      plausible band. It is deliberately not applied to short slabs, where the
      minimum fare dominates and a perfectly correct 31 rupees over 3 kilometres
      looks like 10 rupees per kilometre. The band is wide because it spans every
      class in both sections, from second sitting to AC first class, and this
      check is the weakest of the four for exactly that reason.

    A value failing a check is **deleted**, not reported and kept. A table that
    prints a warning next to a number somebody will read as a price is a table
    that shows the wrong price, so the value has to go.
    """
    reports: dict[str, Report] = {
        code: Report(section=section, class_code=code) for code in classes
    }
    usable_rows = [row for row in rows if row.start and row.end and row.start < row.end]
    # Two rows cannot start at the same distance: the slab column is a tiling of
    # the axis, so a repeated start means one of the two rows was moved there by
    # an implied start that the other row had already claimed. Keeping both would
    # emit two prices for one slab, and keeping the wrong one would emit a price
    # for a slab the document does not print. The row with more readable fares
    # wins because that is the one whose identity is worth trusting; the other is
    # dropped whole, and the drop is reported rather than assumed away.
    by_start: dict[int, Row] = {}
    for row in usable_rows:
        held = by_start.get(row.start)
        if held is None:
            by_start[row.start] = row
            continue
        loser = min((held, row), key=lambda candidate: _readable_fares(candidate, classes))
        winner = row if loser is held else held
        by_start[row.start] = winner
        reports[classes[0]].violations.append(
            f"{winner.start}-{winner.end}: duplicate start, kept the row with "
            f"{_readable_fares(winner, classes)} readable fares over "
            f"{loser.start}-{loser.end} with {_readable_fares(loser, classes)}"
        )
    ordered = sorted(by_start.values(), key=lambda row: (row.start, row.end))

    entries: list[dict] = []
    previous_end: int | None = None
    for row in ordered:
        entry: dict = {"start": row.start, "end": row.end}
        if previous_end is not None and row.start != previous_end + 1:
            for report in reports.values():
                report.out_of_order.append(f"{previous_end + 1}-{row.start - 1}")
        for code in classes:
            report = reports[code]
            report.rows += 1
            cell = row.values.get(code)
            value = cell.value if cell else None
            if value is None:
                report.missing += 1
                continue
            if cell.confirmed:
                report.confirmed += 1
            elif cell.disputed:
                report.disputed += 1
            else:
                report.single += 1
            entry[code] = value
        entries.append(entry)
        previous_end = row.end

    for code in classes:
        report = reports[code]
        holders = [entry for entry in entries if code in entry]
        values = [(entry["start"], entry["end"], entry[code]) for entry in holders]
        doomed: set[int] = set()

        # A value with a different number of digits from both of its neighbours,
        # which agree with each other, is a misread. Nothing else in the scan
        # makes a number change its width and change back.
        for index in range(1, len(values) - 1):
            before, here, after = values[index - 1][2], values[index][2], values[index + 1][2]
            if len(str(before)) == len(str(after)) != len(str(here)):
                report.violations.append(
                    f"{values[index][0]}-{values[index][1]}: {here} has "
                    f"{len(str(here))} digits between {before} and {after}"
                )
                doomed.add(index)

        for index, (earlier, later) in enumerate(itertools.pairwise(values)):
            if later[2] < earlier[2]:
                report.violations.append(
                    f"{later[0]}-{later[1]}: {later[2]} < {earlier[2]} at {earlier[0]}-{earlier[1]}"
                )
                if {index, index + 1} & doomed:
                    doomed.update((index, index + 1))
                    continue
                before = values[index - 1][2] if index else None
                after = values[index + 2][2] if index + 2 < len(values) else None
                if before is None or after is None:
                    # At the edge of the table there is no trend to judge
                    # against, so the value that broke the ordering goes.
                    doomed.add(index + 1)
                    continue
                # Fares rise smoothly, so the value far from the trend through
                # its outer neighbours is the misread one.
                midpoint = (before + after) / 2
                doomed.add(
                    index if abs(earlier[2] - midpoint) > abs(later[2] - midpoint) else index + 1
                )

        for index, (start, end, value) in enumerate(values):
            if start < LONG_SLAB_KM:
                continue
            rate = value / max(1, (start + end) / 2)
            if not 0.1 <= rate <= 20.0:
                report.violations.append(f"{start}-{end}: {value} is {rate:.2f}/km, implausible")
                doomed.add(index)

        for index in doomed:
            entry = holders[index]
            report.dropped.append(f"{entry['start']}-{entry['end']}: {entry[code]}")
            del entry[code]

    # A row whose every fare went unread carries no price, only the boundaries
    # of a slab, so it is dropped rather than written as an empty entry. That
    # drop is reported instead of silent, because it leaves a hole in the
    # table's coverage that a reader must be able to tell apart from a row the
    # scan never found: the hole is honest either way (a gap prices as "no
    # fare", never as an interpolation), but only the report says which of the
    # two happened.
    kept = [entry for entry in entries if len(entry) > 2]
    for entry in entries:
        if len(entry) > 2:
            continue
        reports[classes[0]].violations.append(
            f"{entry['start']}-{entry['end']}: dropped whole, no fare in any class settled"
        )
    return kept, list(reports.values())


def parse_pages(spec: str) -> list[int]:
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            first, last = part.split("-", 1)
            pages.extend(range(int(first), int(last) + 1))
        elif part:
            pages.append(int(part))
    return pages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True, type=Path, help="the scanned circular")
    parser.add_argument("--out", type=Path, help="where to write the JSON table")
    parser.add_argument("--pages", help="page list, e.g. 10-11 (default: every planned page)")
    parser.add_argument("--dpi", type=int, default=400)
    parser.add_argument("--cache", type=Path, default=Path(".cache/fare-ocr"))
    parser.add_argument("--report", action="store_true", help="print validation, write nothing")
    parser.add_argument(
        "--section",
        help="treat --pages as this section (for a document whose pages differ from PLAN)",
    )
    parser.add_argument("--classes", help="comma-separated class list, must match --section")
    parser.add_argument("--source", default="Commercial Circular 24 of 2025")
    parser.add_argument("--effective", default="2025-12-26")
    args = parser.parse_args(argv)

    pages = parse_pages(args.pages) if args.pages else sorted(PLAN)
    plan = dict(PLAN)
    if args.section:
        # A different revision of the circular prints the same table on different
        # pages, so the mapping must be overridable instead of edited in place.
        # Without this, cross-checking one document against another means
        # patching PLAN and remembering to patch it back.
        if args.section not in SECTION_CLASSES:
            print(
                f"unknown section {args.section!r}; known: {sorted(SECTION_CLASSES)}",
                file=sys.stderr,
            )
            return 2
        classes = SECTION_CLASSES[args.section]
        if args.classes:
            classes = [item.strip() for item in args.classes.split(",") if item.strip()]
            if classes != SECTION_CLASSES[args.section]:
                print(
                    f"section {args.section} is defined as {SECTION_CLASSES[args.section]}, "
                    f"not {classes}",
                    file=sys.stderr,
                )
                return 2
        for page in pages:
            plan[page] = (args.section, classes)
    unknown = [page for page in pages if page not in plan]
    if unknown:
        print(f"page(s) {unknown} are not in the plan; add them to PLAN first", file=sys.stderr)
        return 2

    rows: list[Row] = []
    for page in pages:
        section, classes = plan[page]
        png = render_page(args.pdf, page, args.dpi, args.cache)
        page_rows = read_page(page, png, load_gray(png), section, classes, args.cache, args.pdf)
        rows.extend(page_rows)
        print(
            f"page {page}: {len(page_rows)} rows",
            file=sys.stderr,
        )

    # Slab repair happens once, over the whole document, in reading order, so a
    # page boundary cannot break the chain between two halves of one table.
    repair_slabs(rows)
    for section in sorted({row.section for row in rows}):
        repairs = sum(row.repaired for row in rows if row.section == section)
        print(f"{section}: {repairs} slab fields repaired", file=sys.stderr)

    sections: dict[str, dict] = {}
    reports: list[Report] = []
    for section in sorted({row.section for row in rows}):
        classes = SECTION_CLASSES[section]
        section_rows = [row for row in rows if row.section == section]
        table, section_reports = validate(section_rows, section, classes)
        reports.extend(section_reports)
        sections[section] = {
            "classes": classes,
            "rows": sorted(table, key=lambda entry: entry["start"]),
        }

    for report in reports:
        complete = sum(
            1
            for entry in sections[report.section]["rows"]
            if all(code in entry for code in sections[report.section]["classes"])
        )
        print(
            f"{report.section} {report.class_code}: {report.confirmed} confirmed, "
            f"{report.single} single-read, {report.disputed} disputed, {report.missing} unread, "
            f"{len(report.dropped)} deleted by checks; "
            f"{len(report.out_of_order)} discontinuities, {len(report.violations)} violations"
        )
        for item in report.dropped[:12]:
            print(f"    deleted {item}")
        for item in report.out_of_order[:8]:
            print(f"    discontinuity {item}")
        for item in report.violations[:12]:
            print(f"    violation {item}")
        print(
            f"    {complete} of {len(sections[report.section]['rows'])} rows priced in every class"
        )

    if args.report or args.out is None:
        return 0

    payload = {
        "source": args.source,
        "effective": args.effective,
        "pdf_sha256": file_digest(args.pdf),
        "pages": pages,
        "sections": sections,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=1) + "\n")
    total = sum(len(section["rows"]) for section in sections.values())
    print(f"wrote {args.out} with {total} slab rows", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
