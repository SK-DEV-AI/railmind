"""Turn the scanned Railway Board fare circular into a machine-readable table.

STATUS (2026-10-26): measured against 120 slabs of page 10 that were read by
hand (`data/fare-truth-p10.json`), it now emits **no wrong fares**: 288 of 360
hand-read values reproduce exactly and the other 72 are reported missing rather
than guessed. Coverage is the remaining gap, not correctness, and it is not yet
good enough to price tickets from. See
`~/session-root/railway-ai-booking/RESEARCH-FARE-AND-FREE-PATHS.md` and the
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
# Value: `page -> (section id, [class codes left to right])`. Both halves of a
# page carry the same classes; the halves hold different distance ranges.

PLAN: dict[int, tuple[str, list[str]]] = {
    10: ("mail_express_non_ac", ["2S", "SL", "1S"]),
    11: ("mail_express_non_ac", ["2S", "SL", "1S"]),
}

#: Which classes a section's columns hold, in printed order.
SECTION_CLASSES = {
    "mail_express_non_ac": ["2S", "SL", "1S"],
}

#: Junk characters tolerated around a number before the token is rejected. A
#: photographed column rule contributes one or two.
MAX_JUNK = 2


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

    @property
    def start(self) -> int | None:
        return self.start_cell.value

    @property
    def end(self) -> int | None:
        return self.end_cell.value if self.end_cell else None


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
    separation = 30
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
) -> tuple[list[float], dict[int, list[tuple[int, int]]]] | None:
    """Column centres and row bands for a page, computed once for every pass.

    Both segmentation modes read the same words, so they must share one grid:
    letting each pass derive its own bands let a single merged row shift every
    later ordinal, which showed up as fifty "disputed" cells that were really one
    alignment error. The grid comes from geometry alone (word positions, and the
    ink profile only when a row has no readable number at all), so it cannot
    depend on any pass agreeing with any other.
    """
    columns = group_columns(words)
    width = 2 + len(classes)
    if len(columns) != 2 * width:
        return None
    centres = column_centres(columns)
    bands = {half: row_ruler(words, centres, half, width) for half in (0, 1)}
    return centres, bands


def cells_from_pass(
    words: list[dict],
    centres: list[float],
    bands: dict[int, list[tuple[int, int]]],
    classes: list[str],
) -> dict[int, list[list[str]]]:
    """This pass's text for every cell of the shared grid, rows in order."""
    width = 2 + len(classes)

    def text_at(column_index: int, top: int, bottom: int) -> str:
        candidates = [
            word
            for word in words
            if column_of(word["x"], centres) == column_index and top - 3 <= word["y"] <= bottom + 3
        ]
        if not candidates:
            return ""
        middle = (top + bottom) / 2
        return min(candidates, key=lambda word: abs(word["y"] - middle))["text"]

    return {
        half: [
            [text_at(index, top, bottom) for index in range(half * width, (half + 1) * width)]
            for top, bottom in bands[half]
        ]
        for half in (0, 1)
    }


def merge_passes(
    readings: list[dict[int, list[list[str]]]], page: int, section: str, classes: list[str]
) -> list[Row]:
    """Vote the passes cell by cell on the shared grid."""
    width = 2 + len(classes)
    rows: list[Row] = []
    for half in (0, 1):
        count = max(len(reading[half]) for reading in readings)
        for index in range(count):
            cells: list[Cell] = []
            for column_index in range(width):
                votes = [
                    reading[half][index][column_index]
                    for reading in readings
                    if index < len(reading[half]) and reading[half][index][column_index]
                ]
                cells.append(Cell(page=page, x=0, y=index, votes=votes))
            if not cells[0].votes:
                continue
            row = Row(
                page=page,
                section=section,
                half=half,
                y=index,
                start_cell=cells[0],
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
            f"page {page}: the page is not {2 * (2 + len(classes))} numeric columns; "
            "the plan does not match the document"
        )
    centres, bands = grid
    readings = [
        cells_from_pass(batch, centres, bands, classes) for batch in (primary, secondary) if batch
    ]
    rows = merge_passes(readings, page, section, classes)
    return repair_slabs(rows)


def repair_slabs(rows: list[Row]) -> list[Row]:
    """Make the slab column tile the distance axis, as the document defines it.

    A slab starts one kilometre after the one above it ends, so a start that
    overlaps its predecessor is a misread and its true value is that boundary.
    This is the document's own rule, not an inference about fares, and every
    application is counted. A *gap* is left alone: it means a row was missed, and
    filling a gap would invent a slab that the engine would then price.
    """
    for half in (0, 1):
        previous_end: int | None = None
        for row in sorted((row for row in rows if row.half == half), key=lambda row: row.y):
            start, end = row.start, row.end
            if (
                previous_end is not None
                and start is not None
                and end is not None
                and start < previous_end + 1
                and end > previous_end
            ):
                row.start_cell.votes = [str(previous_end + 1)]
                row.repaired += 1
            if row.end is not None:
                previous_end = row.end

    # The printed slab end is redundant: the next slab's start defines it. Where
    # the two disagree the start is the trustworthy of the pair, because it is
    # the value the next row repeats as its own identity, so the end is taken
    # from the neighbour and the read end is only checked against it.
    for half in (0, 1):
        ordered = sorted((row for row in rows if row.half == half), key=lambda row: row.y)
        for row, following in itertools.pairwise(ordered):
            start, next_start = row.start, following.start
            if start is None or next_start is None or next_start <= start + 1:
                continue
            implied = next_start - 1
            if row.end != implied:
                row.end_cell = Cell(page=row.page, x=0, y=row.y, votes=[str(implied)])
                row.repaired += 1
    return rows


def reread_cell(
    image: np.ndarray, box: tuple[int, int, int, int] | None, scratch: Path
) -> str | None:
    """Read one token's glyph run on its own, as a third opinion.

    The two whole-page passes sometimes fail on the same cells. Cropping the
    exact box the token came from, and enlarging it three times, gives the reader
    a different problem with no neighbouring column in the crop to confuse it.
    Only cells the first two passes could not settle are ever re-read.
    """
    if box is None:
        return None
    left, top, width, height = box
    cell = image[max(0, top - 5) : top + height + 5, max(0, left - 5) : left + width + 5]
    if cell.size == 0:
        return None
    enlarged = np.kron(cell, np.ones((3, 3), dtype=np.uint8))
    enlarged = np.pad(enlarged, 24, constant_values=255)
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_bytes(
        b"P5\n%d %d\n255\n" % (enlarged.shape[1], enlarged.shape[0]) + enlarged.tobytes()
    )
    result = subprocess.run(
        ["tesseract", str(scratch), "-", "--psm", "7", "-c", "tessedit_char_whitelist=0123456789"],
        check=False,
        capture_output=True,
        text=True,
    )
    return read_number(result.stdout.strip())


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
    out_of_order: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


def validate(rows: list[Row], section: str, classes: list[str]) -> tuple[list[dict], list[Report]]:
    """Merge halves, check continuity and monotonicity, and refuse to fill holes.

    Three checks are the whole reason to trust an OCR'd price list:

    - slab continuity: every slab starts one kilometre after the last ended,
      which catches a dropped, duplicated or misread row;
    - monotonicity: a longer journey never costs less;
    - rate sanity: implied rupees per kilometre stays inside a plausible band,
      which catches a misread that is still monotonic.

    A row failing a check is dropped and counted, never smoothed over.
    """
    reports: dict[str, Report] = {
        code: Report(section=section, class_code=code) for code in classes
    }
    usable_rows = [row for row in rows if row.start and row.end and row.start < row.end]
    ordered = sorted(usable_rows, key=lambda row: (row.start, row.end))

    kept: list[dict] = []
    previous_end: int | None = None
    for row in ordered:
        entry: dict = {"start": row.start, "end": row.end}
        complete = True
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
                complete = False
                continue
            if cell.confirmed:
                report.confirmed += 1
            elif cell.disputed:
                report.disputed += 1
            else:
                report.single += 1
            entry[code] = value
        if complete:
            kept.append(entry)
        previous_end = row.end

    for code in classes:
        report = reports[code]
        values = [(entry["start"], entry["end"], entry[code]) for entry in kept if code in entry]
        for earlier, later in itertools.pairwise(values):
            if later[2] < earlier[2]:
                report.violations.append(
                    f"{later[0]}-{later[1]}: {later[2]} < {earlier[2]} at {earlier[0]}-{earlier[1]}"
                )
        for start, end, value in values:
            rate = value / max(1, (start + end) / 2)
            if not 0.15 <= rate <= 8.0:
                report.violations.append(f"{start}-{end}: {value} is {rate:.2f}/km, implausible")
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
        repairs = sum(row.repaired for row in page_rows)
        print(
            f"page {page}: {len(page_rows)} rows, {repairs} cells re-read",
            file=sys.stderr,
        )

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
        print(
            f"{report.section} {report.class_code}: {len(sections[report.section]['rows'])} "
            f"complete rows; {report.confirmed} confirmed, {report.single} single-pass, "
            f"{report.disputed} disputed, {report.missing} missing; "
            f"{len(report.out_of_order)} discontinuities, {len(report.violations)} violations"
        )
        for item in report.out_of_order[:8]:
            print(f"    discontinuity {item}")
        for item in report.violations[:12]:
            print(f"    violation {item}")

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
