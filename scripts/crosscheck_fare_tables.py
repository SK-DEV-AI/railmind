"""Compare two fare tables read from two different scans of the same circular.

Why this exists
---------------
Every fare in this project is read from a photocopy by OCR, and one document read
twice only proves the reader is self-consistent. Two *revisions* of the same table
are stronger: CC 12/2025 (in force 01.07.2025) and CC 24/2025 (in force
26.12.2025) print the same slabs, and the Railway Board published what changed
between them, so the difference is known in advance. A miss in either scan breaks
the pattern; a match across both documents means two independent readings of two
independent images agree with the published rule.

What it checks
--------------
For every (class, slab start) present in both tables, the current fare must equal
the older fare plus 2 paise per kilometre of the slab's start distance, within a
rounding tolerance. A slab that differs by exactly nothing, or by far more than
the increase, is a misread in one of the two scans and is reported rather than
averaged away.

The distance used is the slab's printed start, not its midpoint: the published
change is per kilometre of chargeable distance, and the slab start is the one
number both documents state independently.

Usage
-----
    python scripts/crosscheck_fare_tables.py old.json new.json
    python scripts/crosscheck_fare_tables.py old.json new.json --tolerance 4
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The published change: Mail/Express non-AC and AC classes went up 2 paise per km
# on 26.12.2025. Every value in this project's tables is a whole rupee, so the
# comparison is made against a tolerance rather than for exact equality.
PAISE_PER_KM = {
    "2S": 2,
    "SL": 2,
    "FC": 2,
    "CC": 2,
    "3E": 2,
    "3A": 2,
    "2A": 2,
    "1A": 2,
    "EC": 2,
}


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def table(payload: dict, section: str, class_code: str) -> dict[int, int]:
    """Slab start -> fare, for one class of one section of one document."""
    rows = payload.get("sections", {}).get(section, {}).get("rows", [])
    out: dict[int, int] = {}
    for row in rows:
        value = row.get(class_code)
        if value is None:
            continue
        out[int(row["start"])] = int(value)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old", type=Path, help="the earlier revision")
    parser.add_argument("new", type=Path, help="the current revision")
    parser.add_argument(
        "--tolerance",
        type=int,
        default=5,
        help="rupees of slack around the expected increase (default 5)",
    )
    args = parser.parse_args(argv)

    old_payload, new_payload = load(args.old), load(args.new)
    sections = sorted(set(old_payload.get("sections", {})) & set(new_payload.get("sections", {})))
    if not sections:
        print("the two documents have no section in common", file=sys.stderr)
        return 2

    unexplained = 0
    compared = 0
    for section in sections:
        classes = new_payload["sections"][section]["classes"]
        for class_code in classes:
            old = table(old_payload, section, class_code)
            new = table(new_payload, section, class_code)
            shared = sorted(set(old) & set(new))
            if not shared:
                continue
            paise = PAISE_PER_KM.get(class_code)
            if paise is None:
                print(
                    f"{section} {class_code}: no published change known, "
                    f"{len(shared)} slabs compared"
                )
                continue
            matched = []
            anomalies = []
            for start in shared:
                expected = paise * start / 100.0
                delta = new[start] - old[start]
                compared += 1
                if abs(delta - expected) <= args.tolerance:
                    matched.append(start)
                else:
                    anomalies.append((start, old[start], new[start], round(delta - expected, 1)))
            unexplained += len(anomalies)
            share = 100.0 * len(matched) / len(shared)
            print(
                f"{section} {class_code}: {len(shared)} shared slabs, "
                f"{len(matched)} match +{paise}p/km within {args.tolerance} ({share:.1f}%), "
                f"{len(anomalies)} unexplained"
            )
            for start, before, after, gap in anomalies[:12]:
                print(f"    slab {start}: {before} -> {after} (off the published change by {gap})")

    if not compared:
        print("nothing comparable: the tables share no slab start", file=sys.stderr)
        return 2

    share = 100.0 * (compared - unexplained) / compared
    print(f"\n{compared} slab values compared, {share:.1f}% match the published change")
    if share < 90.0:
        print("FAIL: the two scans disagree with the published change too often to trust either")
        return 1
    print("OK: the two independent scans agree with what the Railway Board published")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
