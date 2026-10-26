"""Why does a half table lose its last row? Print the bands and their contents.

Usage: python scripts/diag_bands.py <page> [<page> ...]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.extract_fare_table as E

PDF = Path("/home/sk/.jcode/scratch/CC24_2025.pdf")
CACHE = Path(__file__).resolve().parent.parent / ".cache" / "fare-ocr"


def main(argv: list[str]) -> int:
    for raw in argv:
        page = int(raw)
        _section, classes = E.PLAN[page]
        png = E.render_page(PDF, page, 400, CACHE)
        words = E.ocr_words(png, "6")
        grid = E.page_grid(words + E.ocr_words(png, "11"), classes)
        if grid is None:
            print(f"page {page}: no grid")
            continue
        centres, bands, tables = grid
        print(f"page {page}: {tables} table(s), centres={[round(c) for c in centres]}")
        for half in sorted(bands):
            print(f"  half {half}: {len(bands[half])} bands")
            shown = bands[half][:2] + bands[half][-3:]
            for top, bottom in shown:
                inside = [w for w in words if top <= w["y"] < bottom]
                print(f"    band {top}-{bottom}: {[w['text'] for w in inside][:9]}")
            below = [w for w in words if w["y"] >= bands[half][-1][1]]
            if below:
                ys = sorted({w["y"] for w in below})
                print(f"    words below the last band: {len(below)}, ys {ys[:6]}..{ys[-3:]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
