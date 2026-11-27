#!/usr/bin/env python3
"""chunk_size_front.must_equal refuses two fronts whose B is taken against different references (owner: experiments/layout_choice).

Synthetic rows, no captures: 1. the same reference: the rows are compared (and a planted count difference is a "differ");
2. the planted case: the other file's B = 1 limit is another reference's bytes: refused, naming both; 3. a file with no
B = 1 row: refused.    python3 experiments/layout_choice/check_front_must_equal_reference.py"""
import csv
import os
import sys
import tempfile

import chunk_size_front as F     # beside this file: the study's own module

ZERO = {k: "0" for k in F.RAW}


def run_row(cap, kind, x, ops):
    return {"unit_set": "per type", "line": "chunked_c64k", "cap_bytes": str(cap), "cap_kind": kind, "cap_x_reference": str(x),
            **ZERO, "total_ops": str(ops), "total_bytes": str(cap), "assignment": "Person props=chunked_c64k"}


def file_row(cap, kind, x, ops):
    return {"workload": "i01", "option_set": "per type", "option": "fewest_ops_within_cap", "cap_bytes": str(cap),
            "cap_kind": kind, "cap_x_reference": str(x), **ZERO, "total_ops": str(ops), "total_bytes": str(cap),
            "assignment": "Person props=chunked"}


def write(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main():
    mine = [run_row(100.0, "reference_x1", 1.0, 7), run_row(200.0, "reference_x2", 2.0, 5)]
    cases = []
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "equal_bytes_front.csv")
        write(p, [file_row(100.0, "reference_x1", 1.0, 7), file_row(200.0, "reference_x2", 2.0, 5)])
        n, bad, _ = F.must_equal(mine, p, "64k")
        cases.append(("the same reference: compared, equal", n == 2 and bad == 0))
        write(p, [file_row(100.0, "reference_x1", 1.0, 7), file_row(200.0, "reference_x2", 2.0, 6)])
        n, bad, _ = F.must_equal(mine, p, "64k")
        cases.append(("the same reference, a planted count difference: 1 differ", n == 2 and bad == 1))
        write(p, [file_row(400.0, "reference_x1", 1.0, 7), file_row(800.0, "reference_x2", 2.0, 5)])
        try:
            F.must_equal(mine, p, "64k")
            msg = ""
        except SystemExit as e:
            msg = str(e)
        cases.append(("planted: another reference is refused, naming both", "different references" in msg
                      and "0.000 GB" in msg and msg.count("GB") == 2))
        write(p, [file_row(150.0, "grid", 1.5, 7)])
        try:
            F.must_equal(mine, p, "64k")
            msg = ""
        except SystemExit as e:
            msg = str(e)
        cases.append(("a file with no B = 1 row is refused", "no B = 1 row" in msg))
    for name, ok in cases:
        print(f"{'PASS' if ok else 'FAIL'} {name}")
    bad = sum(not ok for _, ok in cases)
    print(f"check_front_must_equal_reference: {len(cases)} cases, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
