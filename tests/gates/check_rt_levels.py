#!/usr/bin/env python3
"""The step column rt_levels (cpp/kvevaluator/src/store/round_trips.h RtText) against the step's other counts, on every
row of the step files given: costed as the ideal client (B = P = inf, kvcost cost_models.remote_waits_of_text) it must
give (round_trips, round_trips_critical); as the naive client (B = P = 1) its requests must be S + G + range_pages_extra
+ P + D (every SEEK one range read, every page one, every GET and written key one). Refuses a row with the three
columns but no rt_levels (an old binary), and a run that checked 0 rows.

    python tests/gates/check_rt_levels.py <steps dir or csv> [...]
"""
import argparse
import glob
import math
import os
import sys

from kvcost.layout_cost import cost_models as CM
from kvevaluator.capture import capture_layout as L


def files(paths):
    for p in paths:
        yield from (sorted(glob.glob(os.path.join(p, "**", "*.csv"), recursive=True)) if os.path.isdir(p) else [p])


def num(r, k):
    return int(float(r.get(k) or 0))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+")
    a = ap.parse_args()
    rows = bad = nfiles = levels = 0
    for path in files(a.paths):
        for r in L.read_rows(path):
            if "round_trips" not in r or r.get("round_trips") in ("", None):
                continue                                   # not a step row with round trips (or a step with none)
            if r.get("rt_levels") is None or (r["rt_levels"] == "" and int(r["round_trips"])):
                print(f"FAIL {path}: {r.get('query')} {r.get('body')} step {r.get('step_idx')}: no rt_levels "
                      f"beside round_trips {r['round_trips']} (a binary from before the column?)")
                bad += 1
                continue
            ideal = CM.remote_waits_of_text(r["rt_levels"], math.inf, math.inf, r["round_trips"])
            naive_req = CM.remote_waits_of_text(r["rt_levels"], 1, 1, r["round_trips"])[0]
            want_naive = num(r, "S") + num(r, "G") + num(r, "range_pages_extra") + num(r, "P") + num(r, "D")
            if ideal != (num(r, "round_trips"), num(r, "round_trips_critical")) or naive_req != want_naive:
                print(f"FAIL {path}: {r.get('query')} {r.get('body')} p{r.get('param_idx')} step {r.get('step_idx')}: "
                      f"rt_levels gives ideal {ideal}, naive {naive_req}; the row has ({num(r, 'round_trips')}, "
                      f"{num(r, 'round_trips_critical')}), S+G+pages+P+D {want_naive}")
                bad += 1
            rows += 1
            levels += sum(rep for lv, _, n in CM.remote_jobs(r["rt_levels"]) for _, _, rep in lv)
        nfiles += 1
    print(f"check_rt_levels: {rows} step rows in {nfiles} files checked ({levels} levels), {bad} failed")
    return 1 if bad or not rows else 0


if __name__ == "__main__":
    sys.exit(main())
