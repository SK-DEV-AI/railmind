"""Which engine reads one cell? Ask both, on the same crop, and show the votes.

The extractor's own two passes are tesseract. When they agree on something wrong,
or cannot agree at all, the only honest next move is a reader whose failure modes
are unrelated to tesseract's. This prints, per column of a page's bands:

    votes:    every reading the whole-page tesseract passes produced
    tesseract: what an isolated crop read of the cell returns (psm 7, 8, 13)
    rapidocr:  what RapidOCR returns for the same crop, with its confidence

RapidOCR is optional: install it with `uv run --with rapidocr-onnxruntime ...` and
the column appears, otherwise it prints as unavailable.

Usage:
    python scripts/diag_cell_engines.py <page> <half> [from_band] [to_band]
    uv run --with rapidocr-onnxruntime --with opencv-python-headless \
        python scripts/diag_cell_engines.py 10 0 0 6
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

import scripts.extract_fare_table as E

PDF = Path("/home/sk/.jcode/scratch/CC24_2025.pdf")
CACHE = Path(__file__).resolve().parent.parent / ".cache" / "fare-ocr"
SCRATCH = Path(__file__).resolve().parent.parent / ".cache" / "fare-ocr" / "diag"


def rapid_read(image: np.ndarray, box: tuple[int, int, int, int]) -> str:
    """What an unrelated engine, on the same pixels, says the cell is.

    Runs the extractor's own second-engine path, so what this prints is what the
    extractor will believe -- a diagnostic with its own copy of the reader stops
    describing the code the moment one of them changes.
    """
    if E.second_engine() is None:
        return "unavailable"
    SCRATCH.mkdir(parents=True, exist_ok=True)
    value, raw, score = E.second_engine_probe(image, box, SCRATCH / "cell.pgm")
    return f"{value} (raw {raw!r} conf {score:.2f})"


def main(argv: list[str]) -> int:
    page = int(argv[0])
    half_wanted = int(argv[1])
    low = int(argv[2]) if len(argv) > 2 else 0
    high = int(argv[3]) if len(argv) > 3 else 10**6
    slabs = {int(raw) for raw in argv[4:]}
    section, classes = E.PLAN[page]
    png = E.render_page(PDF, page, 400, CACHE)
    image = E.load_gray(png)
    passes = {"psm6": E.ocr_words(png, "6"), "psm11": E.ocr_words(png, "11")}
    grid = E.page_grid(passes["psm6"] + passes["psm11"], classes)
    if grid is None:
        print(f"page {page}: no grid")
        return 1
    centres, bands, _ = grid
    rows = E.merge_passes(
        [E.cells_from_pass(words, centres, bands, classes) for words in passes.values()],
        bands,
        page,
        section,
        classes,
    )
    width = 2 + len(classes)
    for row in rows:
        if row.half != half_wanted or not low <= row.y <= high:
            continue
        if slabs and row.start not in slabs:
            continue
        print(f"band {row.y} {row.band}:")
        window = E.row_text_window(row, classes)
        columns = [
            ("start", row.start_cell, row.half * width),
            ("end", row.end_cell, row.half * width + 1),
            *(
                ("fare " + code, row.values.get(code), row.half * width + 2 + i)
                for i, code in enumerate(classes)
            ),
        ]
        for name, cell, column_index in columns:
            if cell is None:
                continue
            box = E.probe_box(row, cell, column_index, centres, window)
            crop = E.crop_reads(image, box, SCRATCH / "probe.pgm")
            print(
                f"  {name:>9}: box={box} votes={cell.votes} tesseract-crop={crop} "
                f"rapidocr={rapid_read(image, box)}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
