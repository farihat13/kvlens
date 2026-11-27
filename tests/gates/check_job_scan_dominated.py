#!/usr/bin/env python3
"""JOB's plain scan is offered only where its entry label lacks whole_label_scan_by_default (workloads/job/queries/
__init__.py, SCAN_DOMINATED): where the label holds it (every grid cell), the label-run body is never dearer in the pick
measure. This checks that claim on a tree's JOB captures, measured and composed, at one scale.

Per (layout, query, alias) where a capture holds ok rows of both enter_<alias>_by_scan and enter_<alias>_by_label_run,
summed over the params: run <= scan in the pick measure (DEFAULT_CRITERION, its weights read off
plan_choice/optimizer/criteria.linear_score: wops_bytes4k = G + 1.1 S + 0.01 N + read_bytes / 4096). Each lane (S, G,
N, read_bytes) is counted too and printed, never a verdict: the run crosses every row of the label, so its N and bytes
can be higher. A capture made after the gate went in holds no plain-scan rows on a grid cell (they are invalid there);
point --root at a tree whose JOB captures were measured before it (paper-results/ at imdb full).

    python3 tests/gates/check_job_scan_dominated.py [--root paper-results] [--scale full]
--plant: one pair's run row is made dearer than the scan in the copy read: the check must fail (a control).
Exit 0 = run <= scan on every pair; prints the pairs and layouts covered (0 pairs = FAIL), the closest pair and the worst
exception per lane.
"""
import argparse
import csv
import os
import sys
from collections import defaultdict

from kvevaluator.output_tree import Tree
from kvevaluator.paths import ROOT
from kvevaluator.plan_choice.optimizer.criteria import DEFAULT_CRITERION, linear_score
from kvevaluator.workloads.job.queries import RUN, SCAN

LANES = ("S", "G", "N", "read_bytes")


def sums(path):
    """{(query, body): {lane: sum over the ok rows}} of one counts CSV"""
    out = defaultdict(lambda: dict.fromkeys(LANES, 0.0))
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if str(r["ok"]) == "1":
                for lane in LANES:
                    out[(r["query"], r["body"])][lane] += float(r[lane] or 0)
    return out


def pairs(agg):
    """[(query, alias, scan lanes, run lanes)] of one capture file"""
    out = []
    for (q, body), scan in sorted(agg.items()):
        if body.startswith("enter_") and body.endswith(SCAN):
            alias = body[len("enter_"):-len(SCAN)]
            run = agg.get((q, f"enter_{alias}{RUN}"))
            if run is not None:
                out.append((q, alias, scan, run))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(ROOT / "paper-results"), help="the output tree (default paper-results/)")
    ap.add_argument("--scale", default="full", help="the IMDb tag (default full)")
    ap.add_argument("--plant", action="store_true", help="a control: one run made dearer than its scan must fail")
    a = ap.parse_args()
    w = linear_score(DEFAULT_CRITERION)
    score = lambda v: w["S"] * v["S"] + w["G"] * v["G"] + w["N"] * v["N"] + w["total_bytes"] * v["read_bytes"]
    tree = Tree(a.root)
    files = [(kind, cap.path_of("counts", lay)) for kind, cap in (("measured", tree.capture("job", a.scale)),
                                                                 ("composed", tree.composed("job", a.scale)))
             for lay in cap.stores() if lay.startswith("props_")]
    n, bad, layouts, closest = 0, [], set(), None
    lane_ok, lane_worst = dict.fromkeys(LANES, 0), {lane: None for lane in LANES}
    for kind, path in files:
        lay = os.path.basename(path)[:-4]
        for q, alias, scan, run in pairs(sums(path)):
            if a.plant and n == 0:
                run = {**run, "G": run["G"] + score(scan) + 1}
            n += 1
            layouts.add(lay)
            ratio = score(run) / score(scan) if score(scan) else (0.0 if score(run) == 0 else float("inf"))
            where = (kind, lay, q, alias)
            if ratio > 1:
                bad.append((ratio, *where))
            elif closest is None or ratio > closest[0]:
                closest = (ratio, *where)
            for lane in LANES:
                if run[lane] <= scan[lane]:
                    lane_ok[lane] += 1
                else:
                    x = run[lane] / scan[lane] if scan[lane] else float("inf")
                    if lane_worst[lane] is None or x > lane_worst[lane][0]:
                        lane_worst[lane] = (x, *where)
    print(f"{n} (layout, query, alias) pairs on {len(layouts)} layouts ({len(files)} capture files: measured and "
          f"composed) at {a.scale} under {a.root}; pick measure {DEFAULT_CRITERION} {w}")
    for lane in LANES:
        worst = lane_worst[lane]
        print(f"  {lane}: run <= scan on {lane_ok[lane]} of {n}"
              + (f"; worst {worst[0]:.3g}x ({' '.join(worst[1:])})" if worst else ""))
    if closest:
        print(f"  closest pair that holds: run / scan = {closest[0]:.4f} ({' '.join(closest[1:])})")
    for b in sorted(bad, reverse=True)[:10]:
        print(f"  RUN DEARER: {b[0]:.4f}x ({' '.join(b[1:])})")
    ok = n > 0 and not bad
    print(f"check_job_scan_dominated {'OK' if ok else 'FAIL'}: run <= scan in {DEFAULT_CRITERION} on {n - len(bad)} of "
          f"{n} pairs" + ("" if n else " (0 pairs: no capture holds both bodies)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
