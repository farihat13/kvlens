#!/usr/bin/env python3
"""The byte limit B's unit is read from each file, never assumed (owner: python/kvcost/layout_search/front.py).

Synthetic rows, no captures. A front or sweep file names its limits reference_x<B> (B x Sp-Co's bytes, since
or nebula_x<B> (B x Co-Sp's, before): front.b_kind / ref_bytes_of read either and refuse a file with both
kinds or none; search.options_b_unit reads an options.csv's unit from its named-limit set and refuses an unknown set;
labels.b_style draws an older file's B = 0.5; labels.budgets_note names the file's own unit.
    python3 tests/unit/check_b_unit_readers.py"""
import sys

from kvcost.layout_cost import labels
from kvcost.layout_cost.settings import B_UNIT, OLD_B_UNIT
from kvcost.layout_search import front as F
from kvcost.layout_search.search import options_b_unit


def rows(kind, ref):
    return [{"cap_kind": f"{kind}{b:g}", "cap_bytes": str(b * ref)} for b in (1, 2)] + [{"cap_kind": "grid", "cap_bytes": "7"}]


def refused(fn, *args):
    try:
        fn(*args)
        return False
    except SystemExit:
        return True


def main():
    cases = [
        ("a new front: reference_x, B = 1 = Sp-Co's bytes", F.ref_bytes_of(rows("reference_x", 90.0)) == (90.0, B_UNIT)),
        ("an old front: nebula_x, B = 1 = Co-Sp's bytes", F.ref_bytes_of(rows("nebula_x", 375.0)) == (375.0, OLD_B_UNIT)),
        ("a file with both kinds is refused", refused(F.b_kind, rows("reference_x", 90.0) + rows("nebula_x", 375.0))),
        ("a file with no named limit is refused", refused(F.b_kind, [{"cap_kind": "grid", "cap_bytes": "1"}])),
        ("b_of reads either kind", F.b_of("reference_x2") == 2.0 and F.b_of("nebula_x0.5") == 0.5 and F.b_of("grid") is None),
        ("an old options.csv (B = 0.5, 1, 2, 3) is Co-Sp's", options_b_unit(["fewest ops", "B=0.5", "B=1", "B=2", "B=3"]) == OLD_B_UNIT),
        ("a new options.csv (B = 1, 2, 3, 5) is Sp-Co's", options_b_unit(["fewest ops", "B=1", "B=2", "B=3", "B=5"]) == B_UNIT),
        ("an options.csv with another set is refused", refused(options_b_unit, ["B=1", "B=2"])),
        ("an old file's B = 0.5 has a style", labels.b_style(0.5) == labels.OLD_B_STYLE[0.5]),
        ("an unknown B is drawn grey, not a KeyError", labels.b_style(7)[0] == "0.45"),
        ("the note names an old file's unit", "Co-Sp" in labels.budgets_note(OLD_B_UNIT, [0.5, 1, 2, 3])
         and "a file in Co-Sp units" in labels.budgets_note(OLD_B_UNIT, [0.5, 1, 2, 3])),
        ("the note for a new file is BUDGETS_NOTE", labels.budgets_note(B_UNIT, [1, 2, 3, 5]) == labels.BUDGETS_NOTE),
    ]
    for name, ok in cases:
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    bad = sum(not ok for _, ok in cases)
    print(f"check_b_unit_readers: {len(cases)} cases, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
