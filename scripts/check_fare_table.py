"""Score an extracted fare table against rows that were read by a human.

Why this exists
---------------
The extractor validates itself with rules the document states (slabs are
contiguous, a fare never falls as distance rises), but a rule cannot catch a
misread that happens to satisfy it. The only way to know whether the OCR is right
is to compare it with numbers nobody's OCR produced, so a sample of the printed
table was read by eye from the rendered page images and stored in
`data/fare-truth-p10.json`.

What it reports
---------------
For every class in the sample: how many of the hand-read slabs the table has at
all (recall), how many of those carry the same fare (accuracy), and every
disagreement in full. A table that is missing a row is not "roughly right": the
engine would have to interpolate, which is the one thing this project refuses.

Exit status is 1 unless every hand-read value is present and equal, because a
partly correct price list is a price list that silently quotes the neighbour's
fare for a journey near a hole.

Usage
-----
    python scripts/check_fare_table.py data/fare-table.json
    python scripts/check_fare_table.py data/fare-table.json --truth data/fare-truth-p10.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_TRUTH = Path(__file__).resolve().parent.parent / "data" / "fare-truth-p10.json"


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("table", type=Path, help="a table written by extract_fare_table.py")
    parser.add_argument("--truth", type=Path, default=DEFAULT_TRUTH)
    parser.add_argument(
        "--allow-missing",
        type=int,
        default=0,
        help="how many hand-read slabs may be absent before this is a failure",
    )
    args = parser.parse_args(argv)

    table, truth = load(args.table), load(args.truth)
    section = truth["section"]
    classes = truth["classes"]
    rows = table.get("sections", {}).get(section, {}).get("rows", [])
    if not rows:
        print(f"the table has no {section} rows to check", file=sys.stderr)
        return 2
    by_start = {int(row["start"]): row for row in rows}

    missing = 0
    wrong = 0
    checked = 0
    for wanted in truth["rows"]:
        start = wanted["start"]
        got = by_start.get(start)
        if got is None:
            missing += 1
            print(f"missing slab {start}-{wanted['end']}")
            continue
        if int(got["end"]) != int(wanted["end"]):
            wrong += 1
            print(f"slab {start}: end {got['end']} but the page says {wanted['end']}")
        for code in classes:
            checked += 1
            if got.get(code) is None:
                missing += 1
                print(f"missing {code} at slab {start}-{wanted['end']}")
            elif int(got[code]) != int(wanted[code]):
                wrong += 1
                print(
                    f"wrong {code} at slab {start}-{wanted['end']}: "
                    f"{got[code]} but the page says {wanted[code]}"
                )

    total = len(truth["rows"]) * len(classes)
    print(
        f"\n{len(truth['rows'])} hand-read slabs, {total} hand-read values: "
        f"{total - missing - wrong} right, {missing} missing, {wrong} wrong"
    )
    ok = missing <= args.allow_missing and wrong == 0
    print("OK: the extractor reproduces every hand-read value" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
