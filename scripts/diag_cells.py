"""Why is a band not a row? Print each band's cells, per OCR pass.

Usage: python scripts/diag_cells.py <page> <half> [from_band] [to_band]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.extract_fare_table as E

PDF = Path("/home/sk/.jcode/scratch/CC24_2025.pdf")
CACHE = Path(__file__).resolve().parent.parent / ".cache" / "fare-ocr"


def main(argv: list[str]) -> int:
    page = int(argv[0])
    half_wanted = int(argv[1])
    low = int(argv[2]) if len(argv) > 2 else 0
    high = int(argv[3]) if len(argv) > 3 else 10**6
    _section, classes = E.PLAN[page]
    png = E.render_page(PDF, page, 400, CACHE)
    passes = {"psm6": E.ocr_words(png, "6"), "psm11": E.ocr_words(png, "11")}
    grid = E.page_grid(passes["psm6"] + passes["psm11"], classes)
    if grid is None:
        print(f"page {page}: no grid")
        return 1
    centres, bands, _tables = grid
    print(f"page {page} half {half_wanted}: {len(bands[half_wanted])} bands")
    for name, words in passes.items():
        rows = E.cells_from_pass(words, centres, bands, classes)[half_wanted]
        print(f"  pass {name}: {len(rows)} rows")
        for index, entry in enumerate(rows):
            if not low <= index <= high:
                continue
            top, bottom = bands[half_wanted][index]
            print(f"    band {index} {top}-{bottom}: {[cell[0] for cell in entry]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
