#!/usr/bin/env python3
"""ONE per type mix for several workloads at once: an exact joint solve, and the regret table. Analysis only.

Per workload w and currency c:   r_w = cost_c(w, layout) / min over the nine uniform layouts of cost_c(w, .)
    ops   = read weighted ops (G + 1.1 S + 0.01 N) + the writes' own reads (same weights) + PUTs + DELs
    bytes = bytes read + the bytes the writes read first + bytes written
    sum   = ops + bytes / 4,096
Readings (the headline fixed before any joint result): A "max" = minimize the max over
workloads of r_w, ties by the geomean (the regret claim); B "geomean" = minimize the geomean of r_w; C "mean" = minimize
the mean of r_w (a plain sum: also solved by exact_search.solve on the re-weighted union, the cross-check).
The parts are the union of the workloads' parts, each on one of the grid's three settings (a buildable mix); equal
weight per workload. The search: branch and bound over the parts in one shared order; a partial assignment's lower
bound per workload is exact_search's (each query's cheapest completion), and max, geomean and mean are monotone in the
workloads' costs, so each of them over the per-workload bounds is a lower bound of every completion.

    python3 experiments/layout_choice/joint_search.py --sf 3 --grid <grid capture> --root <out dir>
        -> <root>/joint_optimum.csv, joint_regret.csv, README.md
"""
import argparse
import csv
import math
import os
from kvevaluator.output_tree import Tree
from kvevaluator.paths import recorded_path
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.layout_cost.compose import CHANNELS, MEASURES, PlanRule, Composer, currencies, load, cost_mix, workload_units
from kvcost.layout_search.search import INF, Problem, solve, uniform
from kvcost.layout_cost.compose import CELLS, LAYOUTS, add_pick_by_arg
from kvcost.layout_cost.mixes import assignment_label
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost import cost_models as CM  # (the cost models: one definition)
from kvcost.layout_cost.cost_models import cost_of
from kvcost.layout_search.search import TOL, Joint, key_of, stats, worse  # noqa: F401  (the joint solve)

CURRENCIES = ("ops", "bytes", "sum")
READINGS = ("max", "geomean", "mean")
RAW = list(MEASURES) + list(CHANNELS)


def union_workload(ws, weights):
    """one workload holding every item of `ws`, each workload's items times its weight (reading C as a plain sum)"""
    return {"id": "union", "items": [(fam, q, often * weights[n], params) for n, w in ws.items() for fam, q, often, params in w["items"]]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--grid", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--currencies", default=",".join(CURRENCIES))
    benchmarks.add_workload_arg(ap)
    add_pick_by_arg(ap)
    a = ap.parse_args()
    # the grid's three settings only: a buildable mix
    data = load(a.grid, False, composer=Composer(criterion=a.criterion, plan_rule=PlanRule()))
    ws = benchmarks.select("ldbc", a.sf, a.workload)
    t0 = time.time()
    J = Joint(ws, data)
    print(f"{len(ws)} workloads, {len(J.order)} parts in the union, tables in {time.time() - t0:.0f} s", flush=True)
    opt_rows, regret_rows, bad = [], [], 0
    for cur in a.currencies.split(","):
        refs = J.refs(cur)
        options = {c: uniform(J.order, *c[len("props_"):].split("_adj_")) for c in CELLS}
        solved = {}
        for reading in READINGS:
            assign, key, nodes, secs = J.solve(cur, reading)
            options[f"one_mix_min_{reading}"] = assign
            solved[f"one_mix_min_{reading}"] = (nodes, secs)
            print(f"[{cur}] min {reading}: key {tuple(round(k, 4) for k in key)}, {nodes:,} nodes, {secs:.0f} s", flush=True)
        # reading C again by the landed search, on the re-weighted union (a plain sum)
        uw = union_workload(ws, {n: 1.0 / refs[n][1] for n in ws})
        pbu = Problem(uw, data, J.order, extra={"sum": lambda t: cost_of(t, "sum")})
        view = pbu if cur in ("ops", "bytes") else pbu.under("sum")
        _, o, b, _, _ = solve(view, "bytes" if cur == "bytes" else "ops", None, None, plain=True)
        got_mean = (b if cur == "bytes" else o) / len(ws)
        for option, assign in options.items():
            per, ratios = {}, []
            for n, w in ws.items():
                t = cost_mix(w, data, {u: assign[u] for u in J.own[n]})
                c = cost_of(t, cur)
                if abs(c - J.cost(n, cur, assign)) > 1e-6 * max(1.0, c):      # the cost cache == the re-pricing
                    bad += 1
                    print(f"   DIFF {cur} {option} {n}: cost_mix {c} vs the cost cache {J.cost(n, cur, assign)}")
                per[n] = (t, c)
                ratios.append(c / refs[n][1])
            mx, geo, mean = stats(ratios)
            worst = max(ws, key=lambda n: per[n][1] / refs[n][1])
            nodes, secs = solved.get(option, ("", ""))
            opt_rows.append({"currency": cur, "option_set": "single" if option in CELLS else "one per type mix", "option": option,
                             "max_ratio": round(mx, 4), "max_at_workload": f"{ws[worst]['id']} {worst}", "geomean_ratio": round(geo, 4),
                             "mean_ratio": round(mean, 4), "workloads_over_1": sum(r > 1 + 1e-9 for r in ratios), "workloads": len(ratios),
                             "nodes": nodes, "assignment": assignment_label(assign)})
            for n, w in ws.items():
                t, c = per[n]
                cc = currencies(t)
                regret_rows.append({"currency": cur, "option": option, "workload": n, "id": w["id"], "cost": round(c, 3),
                                    "total_ops": round(cc["ops"], 3), "total_bytes": round(cc["bytes"], 3),
                                    **{k: round(t[k], 3) for k in RAW}, "best_single": refs[n][0],
                                    "best_single_cost": round(refs[n][1], 3), "ratio": round(c / refs[n][1], 4)})
        mine = next(r for r in opt_rows if r["currency"] == cur and r["option"] == "one_mix_min_mean")
        if abs(mine["mean_ratio"] - got_mean) > 1e-4:
            bad += 1
            print(f"   DIFF {cur}: reading C by the joint search {mine['mean_ratio']} vs exact_search on the union {got_mean:.4f}")
        else:
            print(f"[{cur}] reading C == exact_search on the re-weighted union ({got_mean:.4f})")
    os.makedirs(a.root, exist_ok=True)
    for fname, rws in (("joint_optimum.csv", opt_rows), ("joint_regret.csv", regret_rows)):
        with open(os.path.join(a.root, fname), "w", newline="") as fh:        # no wall time: a re-run gives the same bytes
            wr = csv.DictWriter(fh, fieldnames=list(rws[0]))
            wr.writeheader()
            wr.writerows(rws)
    L = [f"# One mix for {len(ws)} workloads: the regret table, SF{a.sf}", "",
         "> Generated by `experiments/layout_choice/joint_search.py`; working output, not a snapshot.", "",
         f"Capture: `{recorded_path(os.path.abspath(a.grid), Tree().root)}`. Plans: the pick record's ({a.criterion or 'wops_bytes4k'}). Workloads: {', '.join(w['id'].upper() for w in ws.values())}; {len(J.order)} parts in their union.",
         "**ratio** r_w = a layout's cost on workload w / the cost of the best of the nine uniform layouts on w, same currency (1.00 = as good as the best uniform layout FOR THAT workload; below 1.00 = the mix beats every uniform layout on it).",
         "**ops** = read weighted ops (G + 1.1 S + 0.01 N) + the writes' own reads (same weights) + PUTs + DELs. **bytes** = bytes read + the bytes the writes read first + bytes written. **sum** = ops + bytes / 4,096.",
         "**The mixes:** one per type mix for all workloads (the grid's three settings per part), exact: `min max` minimizes the worst r_w, ties by the geomean (the headline reading, fixed before any result); `min geomean` and `min mean` beside.",
         f"Covered: {len(a.currencies.split(','))} currencies x ({len(CELLS)} uniform layouts + {len(READINGS)} mixes) x {len(ws)} workloads = {len(regret_rows)} rows.", ""]
    for cur in a.currencies.split(","):
        L += [f"## {cur}", "", "| layout | max r_w | where the max falls | geomean r_w | mean r_w | workloads above 1.00 |", "|---|---|---|---|---|---|"]
        for r in (x for x in opt_rows if x["currency"] == cur):
            L.append(f"| {r['option']} | {r['max_ratio']:.2f} | {r['max_at_workload']} | {r['geomean_ratio']:.2f} | {r['mean_ratio']:.2f} | {r['workloads_over_1']} of {r['workloads']} |")
        head = next(x for x in opt_rows if x["currency"] == cur and x["option"] == "one_mix_min_max")
        L += ["", f"The `min max` mix: {head['assignment']}", "", "Its r_w per workload: " +
              ", ".join(f"{x['id'].upper()} {x['ratio']:.2f}" for x in regret_rows if x["currency"] == cur and x["option"] == "one_mix_min_max"), ""]
    with open(os.path.join(a.root, "README.md"), "w") as fh:
        fh.write("\n".join(L) + "\n")
    print(f"{len(opt_rows)} option rows, {len(regret_rows)} regret rows, {bad} differences -> {a.root}; {time.time() - t0:.0f} s")
    return 1 if bad or not regret_rows else 0


if __name__ == "__main__":
    sys.exit(main())
