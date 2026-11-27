#!/usr/bin/env python3
"""The byte-budget sweep: at each total-bytes cap, the fewest total ops a single layout reaches and the fewest a
per type mix reaches (an exact solve, python/kvcost/layout_search/search.py). Analysis only; plot_budget_sweep.py draws the CSV.

The caps run from the fewest-bytes mix's bytes to the fewest-ops mix's bytes, CAPS points evenly in log, plus each
single layout's own bytes (where the singles' staircase steps) and one named cap, the MIDPOINT: the geometric mean of
the bytes of the fewest-bytes and the fewest-ops single layout (one rule for every workload, no reference layout),
and the named budgets, B x Sp-Co's bytes, the reference (cap_kind reference_x<B>; B in settings.NAMED_BUDGETS).
Currencies as sweep.py. A cap no single layout fits has no single row (the mix is then the only option).

    python3 experiments/layout_choice/budget_sweep.py --sf 30 [--grid output/captures/ldbc/sf30]
        -> <out root>/<workload>/budget_sweep.csv (benchmarks.out_root)
"""
import argparse
import csv
import math
import os
import sys
import time
from kvevaluator.paths import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.layout_cost.compose import Composer, load, workload_units
from kvcost.layout_search.search import Problem, solve, uniform
from kvcost.layout_cost.compose import CELLS, LAYOUTS, add_pick_by_arg
from kvcost.layout_cost import benchmarks
from kvevaluator.output_tree import Tree

from kvcost.layout_cost.settings import B_UNIT, NAMED_BUDGETS  # noqa: F401  (B's unit cell and the named budgets B)
from kvcost.layout_search.front import CAPS, caps_of, ref_bytes_of  # noqa: F401  (the sweep's limits)
FIELDS = ["workload", "cap_bytes", "cap_kind", "option_set", "option", "ops", "bytes", "assignment"]


def check_same_solve(d):
    """refuse a budget_sweep.csv solved on another capture than options.csv: the sweep's B = 1 cap IS B_UNIT's bytes
    at solve time (Co-Sp's in a file in Co-Sp units), and options.csv's row of that cell carries the same number
    (content, not mtime: a copied snapshot keeps neither). Returns the sweep's rows."""
    sp, op = os.path.join(d, "budget_sweep.csv"), os.path.join(d, "options.csv")
    with open(sp) as f:
        rows = list(csv.DictReader(f))
    with open(op) as f:
        opts = list(csv.DictReader(f))
    cap, unit = ref_bytes_of(rows)
    ref = [float(o["total_bytes"]) for o in opts if o["option"] == unit and o["option_set"] == "single"]
    if len(ref) != 1:
        raise SystemExit(f"{d}: cannot match the sweep to options.csv ({len(ref)} {unit} rows)")
    ref = ref[0]
    if abs(cap - ref) > 1e-6 * ref:
        raise SystemExit(f"{sp} is from ANOTHER SOLVE than {op}: the sweep's B = 1 ({unit}) bytes {cap / 1e9:,.1f} GB, "
                         f"options.csv's {ref / 1e9:,.1f} GB. Re-run budget_sweep.py on this capture before reading the sweep.")
    return rows


def sweep(name, w, data):
    """one workload: (rows of budget_sweep.csv, {cell: (ops, bytes)} of the 9 single layouts, caps, nodes).
    "per type" = the mix over the grid's three settings."""
    units = workload_units(w, data)
    pb = Problem(w, data, units)
    label = lambda assign: ", ".join(f"{u[1]}{'(p)' if u[0] == 'props' else ''}={assign[u]}" for u in units)
    singles, caps, _ = caps_of(pb, units, True)
    rows, nodes = [], 0
    for cap in sorted(caps):
        fits = [c for c in CELLS if singles[c][2] <= cap * (1 + 1e-9)]
        best = min(fits, key=lambda c: (singles[c][1], singles[c][2])) if fits else None
        if best:
            rows.append({"workload": name, "cap_bytes": cap, "cap_kind": caps[cap], "option_set": "single",
                         "option": best, "ops": singles[best][1], "bytes": singles[best][2],
                         "assignment": label(pb.to_parts(singles[best][0]))})
        assign, ops, byt, n, _ = solve(pb, "ops", cap, singles[best][0] if best else None, plain=True)
        nodes += n
        if assign is not None:
            rows.append({"workload": name, "cap_bytes": cap, "cap_kind": caps[cap], "option_set": "per type",
                         "option": "per_type_fewest_ops_within_cap", "ops": ops, "bytes": byt, "assignment": label(assign)})
    return rows, {c: (singles[c][1], singles[c][2]) for c in CELLS}, len(caps), nodes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True, help="the suite's axis value: LDBC's scale factor, LinkBench's scale tag")
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    ap.add_argument("--grid", default=None)
    benchmarks.add_workload_arg(ap)
    add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    a = ap.parse_args()
    data = load(a.grid or str(Tree().place_dir("captures", a.dataset, a.sf)), False, a.dataset, composer=Composer.from_args(a))
    for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items():
        t0 = time.time()
        rows, _, ncaps, nodes = sweep(name, w, data)
        out = os.path.join(benchmarks.workload_dir(a.dataset, a.sf, name, w, criterion=a.criterion, param_set=a.param_set), "budget_sweep.csv")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=FIELDS)
            wr.writeheader()
            wr.writerows(rows)
        print(f"{name}: {ncaps} caps, {nodes:,} nodes, {time.time() - t0:.0f} s -> {out}", flush=True)


if __name__ == "__main__":
    main()
