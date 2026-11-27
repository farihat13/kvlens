#!/usr/bin/env python3
"""Price every layout-choice workload (python/kvcost/layout_cost/workloads/) on each of the 9 uniform grid layouts, from the grid
captures (analysis only: nothing runs, nothing is re-measured).

Per workload and cell, four raw totals, each = sum over items of (how often x the item's mean over its params):
  read_ops     weighted read ops of the read queries, G:S:N = 1:1.1:0.01 (the paper's read currency)
  read_bytes   bytes those reads return from the store (keys + values read, misses included)
  write_bytes  bytes written by the write queries (keys + values of every put, plus the keys of deletes)
  write_ops    their PUTs + DELs; write_rmw_ops / write_rmw_bytes = the weighted reads a write does first
               (read-modify-write) and the bytes they return
A read query is priced at the plan the shared rule picks for the cell (candidates.pick_body through
picks.choose: validity = every param ran and gave the planner's answer; the pick = --pick-by, default the
optimizer's `wops_bytes4k` = ops + bytes / 4096 over the 7 read lanes, the rule the pins and the grids use; `wops`
= the ops-only pick). The pick's bytes and the REPORTED bytes are the same 7 lanes
(REPORT_BYTE_LANES, miss keys included).
The workload's answer is not one number: the Pareto front on (read_ops, read_bytes, write_bytes) is marked,
plus the cheapest cell on each axis. Writes raw totals to <out root>/workload_costs.csv
(benchmarks.out_root: output/summary/<dataset>/<scale>/layout-choice/<params>/<criterion>; the plan pick rule is an identity axis).
The loaders and a workload's totals on one cell are python/kvcost/layout_cost/compose.py's (load_measured, price, pareto).

    python3 experiments/layout_choice/cost_workloads.py --sf 0.1 [--grid DIR] [--pick-by wops_bytes4k|wops|<spec>]
"""
import argparse
import csv
import os
import sys
from kvevaluator.paths import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.layout_cost import picks
from kvcost.layout_cost import compose as P  # (RULES: the pick rule(s) each cell's picks came from)
from kvcost.layout_cost.compose import (CELLS, KNOWN, MEASURES, Unmeasured, add_pick_by_arg, criterion_name,
                                      load_measured, pareto, workload_cost)
from kvcost.layout_cost import benchmarks
from kvevaluator.output_tree import Tree
from kvevaluator import stale
from kvcost.layout_cost.labels import display_title


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True, help="the suite's axis value: LDBC's scale factor, LinkBench's scale tag")
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS,
                    help="whose store and workloads (default ldbc; linkbench: --params picks anchors, quick, quick-next or log)")
    ap.add_argument("--grid", default=None, help="the grid: a captures/<dataset>/<scale> dir (default: the output tree's)")
    benchmarks.add_workload_arg(ap)
    add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    benchmarks.add_chunk_view_arg(ap)
    ap.add_argument("--in-sample", action="store_true", help="price a capture with no held-out set in-sample (a slice, a smoke, an old copy)")
    a = ap.parse_args()
    root = a.grid or str(Tree().place_dir("captures", a.dataset, a.sf))
    data = load_measured(a.dataset, root, a.criterion, rule=picks.PlanRule(in_sample=True if a.in_sample else None),
                         param_set=a.param_set, view=P.resolve_chunk_view(a.chunk_view))
    out_dir = benchmarks.out_root(a.dataset, a.sf, a.criterion, a.param_set)
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    skipped = []
    for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items():
        try:
            cost = {cell: workload_cost(w, data[cell]) for cell in CELLS}
        except Unmeasured as e:
            skipped.append((name, str(e)))
            continue
        front = pareto({c: (v["read_ops"], v["read_bytes"], v["write_bytes"]) for c, v in cost.items()})
        print(f"\n== {name}: {display_title(w)}\n   expected: {w['expect']}")
        print(f"   {'layout':30s} {'read ops':>12s} {'read bytes':>14s} {'write bytes':>14s} {'write ops':>10s}")
        for cell in sorted(CELLS, key=lambda c: (cost[c]["read_ops"] + cost[c]["write_bytes"], c)):
            v = cost[cell]
            mark = " *front" if cell in front else ""
            label = cell.replace("props_", "P:").replace("_adj_", " A:") + (f" ({KNOWN[cell]})" if KNOWN.get(cell, "").startswith("All-") else "")
            print(f"   {label:30s} {v['read_ops']:>12,.1f} {v['read_bytes']:>14,.0f} {v['write_bytes']:>14,.0f} "
                  f"{v['write_ops']:>10,.0f}{mark}")
            rows.append({"sf": a.sf, "workload": name, "cell": cell, "criterion": criterion_name(a.criterion),
                         "rule": next((r for (_root, c), r in P.RULES.items() if c == cell), ""),
                         # the kinds of a moved input the cell's picks were made from (kvevaluator.stale), empty when clean
                         "stale": stale.column(next((k for (_root, c), k in P.STALE.items() if c == cell), [])),
                         **{k: round(v[k], 3) for k in MEASURES}, "pareto": int(cell in front)})
        for k in ("read_ops", "read_bytes", "write_bytes"):
            if any(cost[c][k] for c in CELLS):
                print(f"   cheapest in {k}: {min(CELLS, key=lambda c: cost[c][k])}")
    path = os.path.join(out_dir, benchmarks.chunk_view_name("workload_costs", a.chunk_view) + ".csv")        # under out_root's <criterion> level
    if a.workload:
        print(f"\n--workload: {path} not written (it covers every workload)")
    else:
        with open(path, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
        print(f"\nwrote {path} ({len(rows)} rows)")
    for name, why in skipped:
        print(f"NOT PRICED {name}: {why}")
    return 1 if skipped else 0


if __name__ == "__main__":
    sys.exit(main())
