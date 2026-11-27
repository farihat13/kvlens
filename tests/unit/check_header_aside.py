#!/usr/bin/env python3
"""kvstats_layout.aside_other_header: an accumulating CSV whose header is not the driver's is moved aside (never padded,
never deleted) and marked shape_sibling (kvevaluator.stale); a file of the driver's shape, or no file, is left alone.
Store-free, a temp dir.

    python3 tests/unit/check_header_aside.py
"""
import os
import sys
import tempfile

from kvevaluator import stale
from kvevaluator.measure import kvstats_layout as L


def main():
    fails = 0

    def check(ok, what):
        nonlocal fails
        print(f"{'PASS' if ok else 'FAIL'} {what}")
        fails += not ok

    cols = ["query", "param_idx", "wall_ns"]
    with tempfile.TemporaryDirectory() as t:
        p = os.path.join(t, "latency.csv")
        check(L.aside_other_header(p, cols) is None and not os.listdir(t), "1 no file: nothing moved")
        with open(p, "w") as f:
            f.write(",".join(cols + ["run"]) + "\n1,1,5,r1\n")
        check(L.aside_other_header(p, cols) is None and os.path.exists(p) and "shape_sibling" not in stale.kinds(),
              "2 the driver's own header: the file stays, no mark")
        with open(p, "w") as f:
            f.write("query,param_idx,run\nic1,1,r0\n")
        aside = L.aside_other_header(p, cols)
        kept = open(aside).read() if aside and os.path.exists(aside) else ""
        check(aside is not None and not os.path.exists(p) and kept == "query,param_idx,run\nic1,1,r0\n"
              and os.path.basename(aside).startswith("latency.csv.old-header-"),
              f"3 another header: moved aside whole ({os.path.basename(aside) if aside else None}), the path free")
        check("shape_sibling" in stale.kinds(), "4 the move is marked shape_sibling")
    print(f"check_header_aside: 4 cases, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
