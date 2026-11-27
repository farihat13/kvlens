#!/usr/bin/env python3
"""ONE mixed layout for a whole dataset: LDBC's workloads W01-W15, I01, I02 and LDBC BI on one store. Analysis only: every cost
is composed from the nine uniform captures; no store is opened.

    c(w, a) = ops + bytes / 4,096 of workload w under layout a (the model wops_bytes4k), where
              ops   = read weighted ops (G + 1.1 S + 0.01 N) + the writes' own reads (same weights) + PUTs + DELETEs
              bytes = bytes read (miss keys in) + the bytes the writes read first + bytes written
              and w's queries are weighted by its frequencies (I01, I02: LDBC's; W01-W15: stated; BI: every query once)
    u(w)    = min over the nine uniform cells of c(w, cell)
    r_w(a)  = c(w, a) / u(w)
    m(w)    = min over the mixed layouts of the same option set of c(w, .)      (w's OWN best mix, an exact solve)
Two objectives over the n workloads, each an exact solve (kvcost.layout_search.search.Joint, branch and bound):
    worst case   minimize max over w of r_w(a), ties by the geomean
    equal share  minimize (1 / n) x the sum over w of r_w(a)
n = 18: W01-W15, I01, I02 and BI as ONE workload. Beside it n = 34: each BI query a workload of its own.
Two option sets: per type and direction (the headline), per type. The grid's three settings only (a buildable mix).
BI is a read family of the LDBC store here: registered for this process, priced in-sample (it has no held-out params).

    python3 experiments/mix_benefit/one_mix_per_dataset.py [--grid DIR] [--out DIR] [--option-sets per_type,per_dir]
        -> <out>/one_mix_layouts.csv        one row per (option set, n, layout): the two solved mixes and the nine cells,
                                            with max / geomean / mean of r_w and the assignment
           <out>/one_mix_by_workload.csv    one row per (option set, n, layout, workload): the cost, every raw channel,
                                            u(w), r_w, m(w), c / m(w), bytes over the reference layout's
           <out>/README.md                  the cost cache
Each query's costs are kept in the mix cost cache (<out>/mix-cost-cache) and reused, so a second run
solves in seconds."""
import argparse
import csv
import os
import sys
import time
from kvevaluator.paths import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.layout_cost import compose
from kvcost.layout_search import search as JS  # (Joint, stats: the joint solve)
from kvcost.layout_search import search as cost_cache  # (the mix cost cache)
from kvcost.layout_cost.cost_models import cost_of
from kvcost.layout_cost import benchmarks
from kvcost.layout_search.search import solve, uniform
from kvcost.layout_cost.compose import CELLS
from kvcost.layout_cost.settings import B_UNIT
from kvcost.layout_cost.mixes import assignment_label
from kvevaluator.output_tree import Tree

GRID = str(Tree().place_dir("captures", "ldbc", "3"))   # the SF3 captures place of the output tree
REFERENCE = B_UNIT                                      # the byte limit's unit cell (Sp-Co)
OPTION_SETS = {"per_dir": ("per type and direction", True), "per_type": ("per type", False)}
OBJECTIVES = {"max": "worst case", "mean": "equal share"}
CUR = "sum"                                             # joint_search's name of ops + bytes / 4,096
RAW = list(compose.MEASURES) + list(compose.CHANNELS)




def cell_name(cell):
    p, a = cell[len("props_"):].split("_adj_")
    return f"{p.capitalize()}/{a.capitalize()}"


def workload_sets(data):
    """{n label: {name: workload}}: W01-W15, I01, I02 with BI as one workload (every query once), and with each BI query alone"""
    base = dict(benchmarks.select("ldbc", "3", None))
    bi = sorted({q for fam, q in data if fam == "bi"}, key=lambda q: (int("".join(ch for ch in q if ch.isdigit())), q))
    one = dict(base, bi_once_each={"id": "bi", "title": "LDBC BI, every query once", "items": [("bi", q, 1, None) for q in bi]})
    each = dict(base, **{q: {"id": q, "title": f"LDBC BI, {q.upper()}", "items": [("bi", q, 1, None)]} for q in bi})
    return {f"{len(one)}: BI as one workload": one, f"{len(each)}: each BI query a workload": each}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--grid", default=GRID, help="the grid capture (the nine cells, held-out rows, writes, microbench, bi)")
    ap.add_argument("--out", default=str(Tree().summary("ldbc", "3", "mix-benefit", "one-mix-per-dataset")))
    ap.add_argument("--option-sets", default="per_dir,per_type")
    ap.add_argument("--bi", help="a capture root that holds BI, when --grid holds none (read under the same plan rule)")
    cost_cache.add_cache_args(ap)      # --fresh: each query's costs are kept and reused across runs
    a = ap.parse_args()
    cost_cache.apply_cache_args(a, default=os.path.join(a.out, "mix-cost-cache"))
    # the grid's three settings only (a buildable mix); BI as a read family of the LDBC store
    composer = compose.Composer(criterion=None, families=benchmarks.families("ldbc", add=() if a.bi else benchmarks.BI),
                              plan_rule=compose.PlanRule())
    os.makedirs(a.out, exist_ok=True)
    layouts, rows, bad = [], [], 0
    for key in a.option_sets.split(","):
        set_name, by_dir = OPTION_SETS[key]
        data = compose.load(a.grid, by_dir, composer=composer)
        if a.bi:    # BI from its own capture, as its own load (a load reads one root)
            data.update(compose.load(a.bi, by_dir, composer=compose.Composer(criterion=None, families=benchmarks.BI,
                                                                             plan_rule=compose.PlanRule())))
        for n_label, ws in workload_sets(data).items():
            t0 = time.time()
            J = JS.Joint(ws, data)
            print(f"[{set_name}; {n_label}] {len(J.order)} parts, {sum(len(pb.queries) for pb in J.pbs.values())} (workload, query) "
                  f"cost cache in {time.time() - t0:.0f} s", flush=True)
            refs = J.refs(CUR)                                             # {workload: (its best uniform cell, u(w))}
            own = {}                                                       # {workload: m(w)}: its own best mix, exact
            for n in ws:
                _, value, _, _, _ = solve(J.pbs[n].under(CUR), "ops", None, None, plain=True)
                own[n] = value
            options = {c: (uniform(J.order, *c[len("props_"):].split("_adj_")), "uniform", "") for c in CELLS}
            for obj, obj_name in OBJECTIVES.items():
                assign, k, nodes, secs = J.solve(CUR, obj)
                options[f"one mix, {obj_name}"] = (assign, "one mix", nodes)
                print(f"[{set_name}; {n_label}] {obj_name}: {nodes:,} nodes, {secs:.0f} s", flush=True)
            ref_bytes = {n: compose.currencies(compose.cost_mix(w, data, {u: options[REFERENCE][0][u] for u in J.own[n]}))["bytes"]
                         for n, w in ws.items()}
            for option, (assign, kind, nodes) in options.items():
                ratios = {}
                for n, w in ws.items():
                    t = compose.cost_mix(w, data, {u: assign[u] for u in J.own[n]})
                    c = cost_of(t, CUR)
                    if abs(c - J.cost(n, CUR, assign)) > 1e-6 * max(1.0, c):   # the search's cost cache == the re-pricing
                        bad += 1
                        print(f"   DIFF {set_name} {option} {n}: cost_mix {c} vs the cost cache {J.cost(n, CUR, assign)}")
                    cc = compose.currencies(t)
                    ratios[n] = c / refs[n][1]
                    rows.append({"scale": "sf3", "option_set": set_name, "workloads": n_label, "layout": option, "layout_kind": kind,
                                 "workload_id": w["id"], "workload": n, "cost_ops_plus_bytes_per_4kb": round(c, 3),
                                 "total_ops": round(cc["ops"], 3), "total_bytes": round(cc["bytes"], 3),
                                 **{x: round(t[x], 3) for x in RAW},
                                 "best_uniform_cell": cell_name(refs[n][0]), "best_uniform_cost": round(refs[n][1], 3),
                                 "r_w": round(ratios[n], 4), "own_best_mix_cost": round(own[n], 3),
                                 "cost_over_own_best_mix": round(c / own[n], 4),
                                 "bytes_over_reference_layout": round(cc["bytes"] / ref_bytes[n], 4)})
                mx, geo, mean = JS.stats(list(ratios.values()))
                worst = max(ratios, key=ratios.get)
                layouts.append({"scale": "sf3", "option_set": set_name, "workloads": n_label,
                                "layout": option if kind == "one mix" else cell_name(option), "layout_kind": kind,
                                "max_r_w": round(mx, 4), "max_at_workload": f"{ws[worst]['id']} {worst}", "geomean_r_w": round(geo, 4),
                                "mean_r_w": round(mean, 4), "workloads_at_or_under_1.00": sum(r <= 1 + 1e-9 for r in ratios.values()),
                                "n_workloads": len(ratios), "search_nodes": nodes, "assignment": assignment_label(assign)})
    for fname, rws in (("one_mix_layouts.csv", layouts), ("one_mix_by_workload.csv", rows)):
        with open(os.path.join(a.out, fname), "w", newline="") as f:      # no wall time: a re-run gives the same bytes
            wr = csv.DictWriter(f, fieldnames=list(rws[0]), lineterminator="\n")
            wr.writeheader()
            wr.writerows(rws)
    with open(os.path.join(a.out, "README.md"), "w") as f:
        f.write(readme(a, layouts, rows))
    print(f"wrote {a.out}/one_mix_layouts.csv ({len(layouts)} rows), one_mix_by_workload.csv ({len(rows)} rows), README.md; "
          f"cost cache against re-pricing: {bad} differ")
    return 1 if bad else 0


def readme(a, layouts, rows):
    """the cost cache, generated from the two CSVs' rows (no number typed)"""
    L = ["# One mixed layout for the LDBC dataset: W01-W15, I01, I02 and BI on one store (SF3)", "",
         "> Generated by "
         "`experiments/mix_benefit/one_mix_per_dataset.py`; working output, not a snapshot.", "",
         f"Capture: `{os.path.relpath(a.grid, EXP)}` (composed: no store built). Model: ops + bytes / 4 KB (wops_bytes4k). "
         "A workload's queries are weighted by its frequencies (I01, I02: LDBC frequencies; W01-W15: stated frequencies; "
         "BI: every query once each, priced in-sample: it has no held-out params).", "",
         "r_w = a layout's cost on workload w / the cost of w's best uniform cell (1.00 = as good as the best of the nine "
         "FOR THAT workload; under 1.00 = better than every uniform cell). **Worst case** minimizes the largest r_w (ties: the geomean); **equal share** minimizes the "
         "mean of r_w. Each is an exact solve over the grid's three settings per part.", ""]
    for set_name in dict.fromkeys(r["option_set"] for r in layouts):
        for n_label in dict.fromkeys(r["workloads"] for r in layouts):
            sel = [r for r in layouts if r["option_set"] == set_name and r["workloads"] == n_label]
            L += [f"## {set_name}; workloads: {n_label}", "",
                  "| layout | largest r_w | at | geomean r_w | mean r_w | workloads at or under 1.00 |", "|---|---|---|---|---|---|"]
            for r in sorted(sel, key=lambda r: (r["layout_kind"] != "one mix", r["max_r_w"])):
                L.append(f"| {r['layout']} | {r['max_r_w']:.2f} | {r['max_at_workload'].split()[0].upper()} | {r['geomean_r_w']:.2f} | "
                         f"{r['mean_r_w']:.2f} | {r['workloads_at_or_under_1.00']} of {r['n_workloads']} |")
            mixes = [r for r in sel if r["layout_kind"] == "one mix"]
            best_cell = min((r for r in sel if r["layout_kind"] == "uniform"), key=lambda r: r["max_r_w"])
            L += ["", "Per workload (the worst-case mix; the best uniform cell for all is the one with the smallest largest r_w, "
                  f"{best_cell['layout']}):", "",
                  "| workload | its best uniform cell | r_w, the one mix (worst case) | over its own best mix | r_w, the one mix "
                  f"(equal share) | r_w, {best_cell['layout']} for all | bytes / the reference layout's (worst-case mix) |",
                  "|---|---|---|---|---|---|---|"]
            by = {(r["layout"], r["workload"]): r for r in rows if r["option_set"] == set_name and r["workloads"] == n_label}
            cell_key = next(c for c in CELLS if cell_name(c) == best_cell["layout"])
            for n in dict.fromkeys(r["workload"] for r in rows if r["option_set"] == set_name and r["workloads"] == n_label):
                w, e, c = by[("one mix, worst case", n)], by[("one mix, equal share", n)], by[(cell_key, n)]
                L.append(f"| {w['workload_id'].upper()} | {w['best_uniform_cell']} | {w['r_w']:.2f} | {w['cost_over_own_best_mix']:.2f} | "
                         f"{e['r_w']:.2f} | {c['r_w']:.2f} | {w['bytes_over_reference_layout']:.2f} |")
            L += [""] + [f"**{r['layout']}** ({r['search_nodes']:,} search nodes): {r['assignment']}" for r in mixes] + [""]
    L += ["Columns of the CSVs: `one_mix_by_workload.csv` keeps every raw channel (S, G, N, P, D, each byte lane) beside the "
          "cost; `bytes_over_reference_layout` = the workload's bytes read and written under the layout / under " + cell_name(REFERENCE) + " (the byte limit's unit). "
          "The store's size on disk is not in these files: no census covers a mix that is not built.", ""]
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
