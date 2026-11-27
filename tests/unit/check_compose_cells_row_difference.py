#!/usr/bin/env python3
"""compose_cells.row_difference, the check's per-row comparison of a measured run row with its composed twin: a
differing result_hash is reported as a difference (not by reaching float() first and raising ValueError), numbers equal
as text or as numbers agree (3 and 3.0), the first numeric difference is named before a text one, and equal rows give
None. --control runs the same cases on the old clause order (float() before the text-column test): it must fail.
Synthetic rows; no data, no store. Under a second.

Run:  python3 tests/unit/check_compose_cells_row_difference.py [--control]
"""
import sys

from kvcost.layout_cost import compose_cells as CC


def old_order(x, y, cols):
    """the control comparison: the float() clause runs before the text-column test"""
    d = next((f"{col}: measured {x[col]!r}, composed {y[col]!r}" for col in cols
              if str(x[col] or 0) != str(y[col] or 0) and float(x[col] or 0) != float(y[col] or 0)
              if col not in CC.TEXT_COLS), None)
    return d or next((f"{col}: measured {x[col]!r}, composed {y.get(col)!r}" for col in CC.TEXT_COLS
                      if col in cols and x[col] != y.get(col)), None)


def main():
    control = "--control" in sys.argv[1:]
    diff = old_order if control else CC.row_difference
    cols = ["ok", "result_hash", "rows", "G", "S"]
    base = {"ok": "1", "result_hash": "a1b2c3", "rows": "10", "G": "3", "S": "0"}
    cases = [
        ("equal rows: no difference", dict(base), lambda d: d is None),
        ("3 and 3.0 agree", {**base, "G": "3.0"}, lambda d: d is None),
        ("an empty count and 0 agree", {**base, "S": ""}, lambda d: d is None),
        ("a differing result_hash is named", {**base, "result_hash": "ffff00"}, lambda d: d and d.startswith("result_hash:")),
        ("a count difference is named", {**base, "G": "4"}, lambda d: d and d.startswith("G:")),
        ("a count difference comes before a hash difference", {**base, "G": "4", "result_hash": "ffff00"},
         lambda d: d and d.startswith("G:")),
    ]
    fails = 0
    for what, y, ok in cases:
        try:
            d = diff(base, y, cols)
            good = bool(ok(d))
        except ValueError as e:
            d, good = f"ValueError: {e}", False
        fails += not good
        print(f"  {'ok  ' if good else 'FAIL'} {what}" + ("" if good else f": {d}"))
    print(f"{len(cases)} cases, {fails} failed")
    if control:
        print(f"control caught: {'PASS' if fails else 'FAIL (the old order passed)'}")
        return 0 if fails else 1
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
