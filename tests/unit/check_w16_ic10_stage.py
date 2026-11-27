#!/usr/bin/env python3
"""The ic10-bodies stage of run-w16-mix-latency.sh and its verdict (w16_latency_score.py ic10): no store, no binary.

  1. the stage plans every store it orders whose recorded IC10 pick is the scan, each with its point body (the
     planner's own, v1's selector) and its scan body, from the reader's view of the record alone: a dry run exits 0 and
     plans the default grid cells that carry no decision field `planner` (props_condensed_adj_split,
     props_split_adj_condensed, props_condensed_adj_chunked: no KeyError), 4 timed runs per store;
  2. the verdict expects the stores the stage planned (<root>/ic10-bodies/bodies.csv), not every store of the record
     that picks the scan: planted timings for exactly the planned stores, scan / point = 1, give rc 0 (held). Before
     it expected named stores the stage never times (arangodb, pertype_csr-c64k): rc 2, "absent".

    python3 tests/unit/check_w16_ic10_stage.py
"""
import argparse
import csv
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

from kvevaluator.paths import ROOT

_spec = importlib.util.spec_from_file_location(
    "w16_latency_score", os.path.join(str(ROOT), "experiments", "layout_choice", "w16_latency_score.py"))
S = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(S)

NO_PLANNER_FIELD = ["props_condensed_adj_split", "props_split_adj_condensed", "props_condensed_adj_chunked"]
FAILS = []


def check(name, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def stage_plan():
    """the stage's dry run: (rc, {store: (point, scan)}, timed runs)"""
    out = subprocess.run(["bash", "experiments/layout_choice/run-w16-mix-latency.sh", "--sf", "3", "--stage", "ic10-bodies",
                          "--dry-run"], cwd=str(ROOT), capture_output=True, text=True)
    plan = {m.group(1): (m.group(2), m.group(3))
            for m in re.finditer(r"^  (\S+): point=(\S+) scan=(\S+)$", out.stdout, re.M)}
    runs = sum(1 for line in out.stdout.splitlines() if line.startswith("+ ") and "kvcost.latency.grid_latency" in line)
    if out.returncode != 0:
        print(out.stdout[-2000:], out.stderr[-2000:], sep="\n")
    return out.returncode, plan, runs


def plant(root, plan):
    """a latency root with the stage's plan and warm raw files, every iteration 1 ms, for exactly the planned stores"""
    os.makedirs(os.path.join(root, "ic10-bodies"))
    with open(os.path.join(root, "ic10-bodies", S.IC10_PLAN), "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["store", "point", "scan"])
        w.writerows([s, p, b] for s, (p, b) in sorted(plan.items()))
    for store in plan:
        sub = "mixes" if store.startswith("w16_") else "grid"
        for body in ("point", "scan"):
            for copy in S.IC10_COPIES:
                for idx in range(1, 6):
                    d = os.path.join(S._wl(os.path.join(root, "ic10-bodies", body), sub), S.IC10_REGIME + copy,
                                     f"meta-{store}", f"meta-{store}", "ic10", f"p{idx}")
                    os.makedirs(d)
                    with open(os.path.join(d, "warm.json"), "w") as f:
                        json.dump({"totals": {"wall_ns_all": [1_000_000, 1_000_000, 1_000_000]}}, f)


def main():
    print("1. the stage's plan (a dry run)")
    rc, plan, runs = stage_plan()
    check("the dry run exits 0", rc == 0, f"rc {rc}")
    check("it plans at least one store", len(plan) > 0, "0 stores")
    missing = [c for c in NO_PLANNER_FIELD if c not in plan]
    check("it plans the default cells with no `planner` field", not missing, f"missing {missing}")
    check("4 timed runs per planned store (2 bodies x 2 copies)", runs == 4 * len(plan), f"{runs} runs, {len(plan)} stores")
    check("every point body differs from its scan body", all(p != s for p, s in plan.values()))
    print(f"   planned {len(plan)} stores, {runs} timed runs")

    print("2. the verdict expects the planned stores")
    with tempfile.TemporaryDirectory() as t:
        plant(os.path.join(t, "root"), plan)
        a = argparse.Namespace(latency_root=os.path.join(t, "root"), out=os.path.join(t, "out"))
        got = S.ic10(a)
        check("planted timings for exactly the planned stores: held (rc 0)", got == 0, f"rc {got}")
        with open(os.path.join(t, "out", "ic10_bodies.csv"), newline="") as f:
            rows = list(csv.DictReader(f))
        check("one verdict row per planned store", sorted(r["store"] for r in rows) == sorted(plan), f"{len(rows)} rows")
        print(f"   {len(rows)} verdict rows")
    print(f"{'FAIL' if FAILS else 'PASS'}: {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
