#!/usr/bin/env python3
"""The exact best mix under the sum itself (wops_bytes4k at the workload level), no byte limit. Analysis only.

    sum = total ops + total bytes / 4,096
    total ops   = read weighted ops (G + 1.1 S + 0.01 N) + the writes' own reads (same weights) + PUTs + DELs
    total bytes = bytes read + the bytes the writes read first + bytes written
The plans are the pick record's (compose.query_pick), as for every other number. The sum enters exact_search.Problem as
one more table priced at the same plan (the way bill_search.py adds the bill); python/kvcost/layout_search/search.py itself is unchanged.
Per workload: the nine uniform layouts and the exact fewest-sum mix per option set (per type; per type and direction).
--oracle: full enumeration by cost_mix on every
space of at most ORACLE_MAX assignments must give the same optimum.

    python3 experiments/layout_choice/sum_search.py --sf 3 --grid <grid capture> --root <out dir>
        -> <root>/<workload dir>/sum_optimum.csv ; <root>/sum_optimum_all.csv ; <root>/README.md
"""
import argparse
import csv
import itertools
import os
from kvevaluator.output_tree import Tree
from kvevaluator.paths import recorded_path
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.layout_cost.compose import (CHANNELS, MEASURES, Composer, currencies, load, options_of,
                                      cost_mix, workload_units)
from kvcost.layout_search.search import Problem, solve, uniform
from kvcost.layout_cost.compose import CELLS, LAYOUTS, add_pick_by_arg
from kvcost.layout_cost.mixes import assignment_label
from kvcost.layout_cost import cost_models as CM  # (the cost models: one definition)
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost.labels import axis_label

ORACLE_MAX = 20_000
RAW = list(MEASURES) + list(CHANNELS)
FIELDS = ["workload", "id", "option_set", "option", "sum", "total_ops", "total_bytes"] + RAW + \
         ["best_single", "best_single_sum", "sum_single_over_option", "nodes", "assignment"]
ALL_FIELDS = ["workload", "id", "best_single", "best_single_sum", "best_single_ops", "best_single_bytes", "option_set", "sum",
              "total_ops", "total_bytes", "sum_single_over_mix", "parts_not_on_best_single", "nodes", "assignment"]


def the_sum(t):
    return CM.cost(t, "wops_bytes4k")


def oracle(w, data, units):
    """the fewest sum over every assignment of the grid's three settings, by cost_mix; None when the space is over
    ORACLE_MAX"""
    opts = [options_of(data, u)[:len(LAYOUTS)] for u in units]
    n = 1
    for o in opts:
        n *= len(o)
    if n > ORACLE_MAX:
        return None
    best = float("inf")
    for combo in itertools.product(*opts):
        t = cost_mix(w, data, dict(zip(units, combo)))
        if t is not None:
            best = min(best, the_sum(t))
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True, help="the suite's axis value: LDBC's scale factor, LinkBench's scale tag")
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    ap.add_argument("--grid", required=True)
    ap.add_argument("--root", required=True, help="the analysis dir to write into (one folder per workload)")
    ap.add_argument("--oracle", action="store_true", help="also enumerate every space of at most ORACLE_MAX assignments")
    ap.add_argument("--in-sample", action="store_true", help="a grid with no held-out set (LinkBench: one body per "
                    "request, its planner's): plans by the rule on the capture's own params, said in the output")
    benchmarks.add_workload_arg(ap)
    add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    a = ap.parse_args()
    composer = Composer.from_args(a)
    ws = benchmarks.select(a.dataset, a.sf, a.workload)
    per, checked, bad = {name: [] for name in ws}, 0, 0
    for by_dir in (False, True):
        data = load(a.grid, by_dir, a.dataset, composer=composer)
        for name, w in ws.items():
            t0 = time.time()
            units = workload_units(w, data)
            pb = Problem(w, data, units, extra={"sum": the_sum})
            view = pb.under("sum")
            singles = {c: uniform(units, *c[len("props_"):].split("_adj_")) for c in CELLS}
            sums = {c: the_sum(cost_mix(w, data, singles[c])) for c in CELLS}
            best1 = min(CELLS, key=lambda c: (sums[c], c))
            rows = per[name]
            if not by_dir:
                for c in CELLS:
                    t = cost_mix(w, data, singles[c])
                    rows.append({"option_set": "single", "option": c, "t": t, "nodes": "", "assign": singles[c]})
            oset = "per type and direction" if by_dir else "per type"
            assign, got, _, nodes, _ = solve(view, "ops", None, view.from_assign(singles[best1]), plain=True)
            t = cost_mix(w, data, assign)
            assert abs(the_sum(t) - got) <= 1e-6 * got, (name, oset, the_sum(t), got)      # the cost cache == the re-pricing
            assert the_sum(t) <= sums[best1] * (1 + 1e-9), (name, oset)                    # a uniform layout is a mix
            rows.append({"option_set": oset, "option": "fewest_sum", "t": t, "nodes": nodes, "assign": assign})
            if a.oracle:
                want = oracle(w, data, units)
                if want is not None:
                    checked += 1
                    if abs(want - got) > 1e-9 * max(1.0, want):
                        bad += 1
                        print(f"   ORACLE DIFF {name} [{oset}]: enumeration {want} vs the search {got}")
            for r in rows:
                r.setdefault("best1", (best1, sums[best1]))
            print(f"{name} [{'per type and direction' if by_dir else 'per type'}]: {len(units)} parts, best uniform {best1} "
                  f"{sums[best1]:,.0f}; " + "; ".join(f"{r['option_set']} {the_sum(r['t']):,.0f} ({sums[best1] / the_sum(r['t']):.3f}x)"
                                                    for r in rows if r["option_set"].startswith("per type and direction" if by_dir else "per type")
                                                    and (by_dir or "direction" not in r["option_set"]))
                  + f"; {time.time() - t0:.0f} s", flush=True)
    os.makedirs(a.root, exist_ok=True)
    summary = []
    for name, w in ws.items():
        out = benchmarks.workload_dir(a.dataset, a.sf, name, w, a.root, criterion=a.criterion, param_set=a.param_set)
        os.makedirs(out, exist_ok=True)
        rows = []
        for r in per[name]:
            b1, s1 = r["best1"]
            c = currencies(r["t"])
            rows.append({"workload": name, "id": w["id"], "option_set": r["option_set"], "option": r["option"],
                         "sum": round(the_sum(r["t"]), 3), "total_ops": round(c["ops"], 3), "total_bytes": round(c["bytes"], 3),
                         **{k: round(r["t"][k], 3) for k in RAW}, "best_single": b1, "best_single_sum": round(s1, 3),
                         "sum_single_over_option": round(s1 / the_sum(r["t"]), 4), "nodes": r["nodes"],
                         "assignment": assignment_label(r["assign"])})
            if r["option_set"] != "single":
                single = next(x for x in per[name] if x["option"] == b1)
                cs = currencies(single["t"])
                p, ad = b1[len("props_"):].split("_adj_")
                moved = sum(1 for u, l in r["assign"].items() if l != (p if u[0] == "props" else ad))
                summary.append({"workload": name, "id": w["id"], "best_single": b1, "best_single_sum": round(s1, 3),
                                "best_single_ops": round(cs["ops"], 3), "best_single_bytes": round(cs["bytes"], 3),
                                "option_set": r["option_set"], "sum": round(the_sum(r["t"]), 3), "total_ops": round(c["ops"], 3),
                                "total_bytes": round(c["bytes"], 3), "sum_single_over_mix": round(s1 / the_sum(r["t"]), 4),
                                "parts_not_on_best_single": f"{moved} of {len(r['assign'])}", "nodes": r["nodes"],
                                "assignment": assignment_label(r["assign"])})
        with open(os.path.join(out, "sum_optimum.csv"), "w", newline="") as fh:   # no wall time: a re-run gives the same bytes
            wr = csv.DictWriter(fh, fieldnames=FIELDS)
            wr.writeheader()
            wr.writerows(rows)
    with open(os.path.join(a.root, "sum_optimum_all.csv"), "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=ALL_FIELDS)
        wr.writeheader()
        wr.writerows(summary)
    sets = [s for s in ("per type", "per type and direction")
            if any(r["option_set"] == s for r in summary)]
    L = [f"# The best mix under the sum itself (ops + bytes / 4,096), {axis_label(a.dataset, a.sf, a.param_set)}, no byte limit", "",
         "> Generated by `experiments/layout_choice/sum_search.py`; working output, not a snapshot.", "",
         f"Capture: `{recorded_path(os.path.abspath(a.grid), Tree().root)}`. Plans: {"IN-SAMPLE, the capture's own params" if a.in_sample else "the pick record's"} ({a.criterion or 'wops_bytes4k'}). Every mix is an exact optimum (branch and bound), re-priced by `compose.cost_mix`.",
         "**sum** = total ops + total bytes / 4,096. **total ops** = read weighted ops (G + 1.1 S + 0.01 N) + the writes' own reads (same weights) + PUTs + DELs. "
         "**total bytes** = bytes read + the bytes the writes read first + bytes written. A query's cost = its mean over the reported params x how often it runs.",
         "**x** = the sum of the best uniform layout / the sum of the mix (1.00x = no gain).",
         f"Covered: {len(ws)} workloads x {len(sets)} option sets = {len(summary)} solves; 9 uniform layouts each.", "",
         "| workload | best uniform layout (sum) | its sum | " + " | ".join(f"{s}: sum (x)" for s in sets) + " |",
         "|---|---|---|" + "---|" * len(sets)]
    for name, w in ws.items():
        mine = {r["option_set"]: r for r in summary if r["workload"] == name}
        any_row = next(iter(mine.values()))
        L.append(f"| {w['id'].upper()} {name} | {any_row['best_single']} | {any_row['best_single_sum']:,.0f} | "
                 + " | ".join(f"{mine[s]['sum']:,.0f} ({mine[s]['sum_single_over_mix']:.2f}x)" if s in mine else "" for s in sets) + " |")
    with open(os.path.join(a.root, "README.md"), "w") as fh:
        fh.write("\n".join(L) + "\n")
    print(f"{len(ws)} workloads, {len(summary)} solves -> {a.root}" + (f"; oracle: {checked} spaces enumerated, {bad} differ" if a.oracle else ""))
    if not summary or (a.oracle and (bad or not checked)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
