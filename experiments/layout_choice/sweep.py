#!/usr/bin/env python3
"""The ratio sweep: scale a workload's write items by r (writes per read, 0.01 .. 100) and total each layout's
cost at every r, for the 9 single layouts and the per type mix (props per vertex type, adjacency per edge type)
that is cheapest at that r -- an exact solve (python/kvcost/layout_search/search.py), per r and per currency. Analysis only;
plot_sweep.py draws the CSV.

Two currencies, each a sum of LIKE units (no exchange rate between ops and bytes):
  ops    read weighted ops (G:S:N = 1:1.1:0.01) + the writes' own reads (weighted) + their PUTs + DELs
  bytes  bytes read + the bytes the writes read first (read-modify-write) + bytes written
The per type mix is solved PER r and PER currency (so its assignment can change along the sweep; the CSV names it).
A workload is swept iff it declares `sweep` (its file, inputs/workloads/<benchmark>/<id>.json): those items' frequency is multiplied by r.

    python3 experiments/layout_choice/sweep.py --sf 0.1   -> <out root>/<workload>/sweep.csv (benchmarks.out_root)
"""
import argparse
import csv
import os
import sys
from kvevaluator.paths import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.layout_cost.compose import Composer, currencies, load, cost_mix, workload_units
from kvcost.layout_search.search import solve, uniform
from kvcost.layout_search import search as cost_cache
from kvcost.layout_cost.compose import CELLS, add_pick_by_arg
from kvcost.layout_cost import benchmarks
from kvevaluator.output_tree import Tree
from kvcost.layout_cost.workloads.designed import RATIOS


def split_items(w):
    """(the fixed part, the part scaled by r) as two workloads with the same shape"""
    scaled = set(w["sweep"])
    return ({"items": [it for it in w["items"] if it[1] not in scaled]},
            {"items": [it for it in w["items"] if it[1] in scaled]})


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True, help="the suite's axis value: LDBC's scale factor, LinkBench's scale tag")
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    ap.add_argument("--grid", default=None)
    benchmarks.add_workload_arg(ap)
    add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    cost_cache.add_cache_args(ap)      # --fresh: each query's costs are kept and reused across runs
    a = ap.parse_args()
    cost_cache.apply_cache_args(a)
    composer = Composer.from_args(a)
    root = a.grid or str(Tree().place_dir("captures", a.dataset, a.sf))
    data = load(root, False, a.dataset, composer=composer)
    for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items():
        if "sweep" not in w:
            continue
        fixed, scaled = split_items(w)
        units = workload_units(w, data)
        pb = cost_cache.problem(w, data, units)      # one cost cache (the driver run's); each r only re-weights it
        scaled_q = set(w["sweep"])
        label = lambda assign: ", ".join(f"{u[1]}{'(p)' if u[0] == 'props' else ''}={assign[u]}" for u in units)
        singles = {cell: uniform(units, *cell[len("props_"):].split("_adj_")) for cell in CELLS}
        parts = {cell: (cost_mix(fixed, data, a_), cost_mix(scaled, data, a_)) for cell, a_ in singles.items()}
        rows, nodes = [], 0
        for r in RATIOS:
            pb.reweight([often * (r if q in scaled_q else 1.0) for (_, q, often, _) in w["items"]])
            for cell in CELLS:
                f, sc = parts[cell]
                rows.append({"workload": name, "r": r, "config": cell, **currencies({m: f[m] + r * sc[m] for m in f}),
                             "assignment": label(singles[cell])})
            for cur in ("ops", "bytes"):
                assign, _, _, n, _ = solve(pb, cur)
                nodes += n
                f, sc = cost_mix(fixed, data, assign), cost_mix(scaled, data, assign)
                rows.append({"workload": name, "r": r, "config": f"per_type_fewest_{cur}",
                             **currencies({m: f[m] + r * sc[m] for m in f}), "assignment": label(assign)})
        out = os.path.join(benchmarks.workload_dir(a.dataset, a.sf, name, w, criterion=a.criterion, param_set=a.param_set), "sweep.csv")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=["workload", "r", "config", "ops", "bytes", "assignment"])
            wr.writeheader()
            wr.writerows(rows)
        print(f"{name}: {len(RATIOS)} ratios x 2 exact solves ({nodes:,} nodes) -> {out}")


if __name__ == "__main__":
    main()
