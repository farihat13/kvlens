#!/usr/bin/env python3
"""The exact mixed layout per type and direction that is the fewest under each model (wops, bytes, wops + bytes / 4 KB),
with no byte limit, for the layout-choice workloads at SF3. Analysis only: every cost is composed from the uniform
captures; no store is opened.

Priced as the layout-choice run that wrote `options.csv` (`analysis/layout-choice/wops_bytes4k/`): plans picked by the
pick record under the wops_bytes4k criterion, the grid's three settings per part. The fewest-wops and fewest-bytes
mixes are options.csv's `per_type_direction_fewest_{ops,bytes}`; this script solves them again
and exits 1 unless they match, so the third (fewest wops + bytes / 4 KB, not in options.csv) is priced the same way.

    python3 experiments/mix_benefit/fewest_mix_by_model.py --grid <grid capture> \\
        --options <layout-choice analysis dir>/wops_bytes4k --out DIR
        -> <out>/fewest_mix_by_model.csv   one row per (workload, model): the mix's ops, bytes, ops + bytes / 4 KB, every
                                           raw channel, the assignment"""
import argparse
import csv
import glob
import multiprocessing
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.layout_cost import compose
from kvcost.layout_cost import benchmarks
from kvcost.layout_search.search import Problem, named_solves, solve
from kvcost.layout_cost.cost_models import cost_of
from kvcost.layout_cost.mixes import assignment_label

WORKLOADS = ("w03", "w06", "w07", "w08", "w09", "w11", "w14", "i01", "i02")   # the paper's W1-W7, LDBC, LDBC reads
MODELS = ("wops", "bytes", "wops_bytes4k")
IN_OPTIONS = {"wops": "per_type_direction_fewest_ops", "bytes": "per_type_direction_fewest_bytes"}
RAW = list(compose.MEASURES) + list(compose.CHANNELS)
DATA = None                                             # the grid, loaded once before the workers fork


def one(task):
    """the three mixes of one workload, as rows"""
    name, w = task
    t0 = time.time()
    units = compose.workload_units(w, DATA)
    pb = Problem(w, DATA, units, extra={"sum": lambda t: cost_of(t, "sum")})
    mixes = {"wops_bytes4k": solve(pb.under("sum"), "ops", None, None, plain=True)[0]}
    for q, assign, _, _ in named_solves(pb, 1.0, budgets=(), plain=True):   # fewest ops, fewest bytes; no byte limit
        mixes[{"fewest ops": "wops", "fewest bytes": "bytes"}[q]] = assign
    rows = []
    for m in MODELS:
        t = compose.cost_mix(w, DATA, mixes[m])
        cc = compose.currencies(t)
        rows.append({"workload_id": w["id"], "workload": name, "model": m, "total_ops": round(cc["ops"], 3),
                     "total_bytes": round(cc["bytes"], 3), "ops_plus_bytes_per_4kb": round(cost_of(t, "sum"), 3),
                     **{x: round(t[x], 3) for x in RAW}, "parts": len(mixes[m]), "assignment": assignment_label(mixes[m])})
    print(f"{w['id']}: {len(units)} parts, {time.time() - t0:.0f} s", flush=True)
    return rows


def check_against_options(rows, options):
    """the fewest-wops and fewest-bytes mixes equal options.csv's; returns the number compared"""
    n = 0
    for r in rows:
        if r["model"] not in IN_OPTIONS:
            continue
        path = glob.glob(os.path.join(options, f"{r['workload_id']}-*", "options.csv"))
        if len(path) != 1:
            sys.exit(f"{options}: {len(path)} options.csv for {r['workload_id']}")
        opts = {x["option"]: x for x in csv.DictReader(open(path[0]))}
        name = IN_OPTIONS[r["model"]]
        o = opts.get(name.replace("per_type_direction_", "per_type_direction_no_grouping_")) or opts[name]   # a file with grouped props solves
        for col in ("total_ops", "total_bytes"):
            if abs(r[col] - float(o[col])) > 1e-6 * max(1.0, abs(r[col])):
                sys.exit(f"{r['workload_id']} {r['model']}: {col} {r[col]} here, {o[col]} in {path[0]}")
        n += 1
    return n


def main():
    global DATA
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--grid", required=True, help="the grid capture (nine cells, held-out rows, writes, microbench)")
    ap.add_argument("--options", required=True, help="the layout-choice analysis dir whose options.csv the mixes must match")
    ap.add_argument("--out", required=True)
    ap.add_argument("--jobs", type=int, default=len(WORKLOADS))
    a = ap.parse_args()
    t0 = time.time()
    DATA = compose.load(a.grid, True, composer=compose.Composer(criterion="wops_bytes4k",
                                                                          plan_rule=compose.PlanRule()))
    print(f"loaded {a.grid} in {time.time() - t0:.0f} s", flush=True)
    tasks = sorted(((n, w) for n, w in benchmarks.select("ldbc", DATA.sf, None).items() if w["id"] in WORKLOADS),
                   key=lambda t: t[1]["id"])
    if [w["id"] for _, w in tasks] != list(WORKLOADS):
        sys.exit(f"workloads found: {[w['id'] for _, w in tasks]}, expected {list(WORKLOADS)}")
    with multiprocessing.get_context("fork").Pool(min(a.jobs, len(tasks))) as pool:
        rows = [r for rs in pool.map(one, tasks) for r in rs]
    n = check_against_options(rows, a.options)
    print(f"check: {n} mixes equal options.csv's (expected {2 * len(WORKLOADS)})")
    if n != 2 * len(WORKLOADS):
        sys.exit(1)
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "fewest_mix_by_model.csv")
    with open(path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        wr.writeheader()
        wr.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows) in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
