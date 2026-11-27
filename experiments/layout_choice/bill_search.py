#!/usr/bin/env python3
"""The bill report: the nine uniform layouts and the best per type mix under a billed key-value service's rule (DynamoDB's
request units: python/kvcost/layout_cost/compose.py bill_units), priced from the grid's saved counts.
Analysis only: nothing is measured on such a service. The mix of each form is read from its `kvcost search-mix --cost-model
bill_<form> --settings Sp,Co,Ch64k` row (--results: the summary/<dataset>/<scale>/ dir of the output tree search-mix
wrote into), solved on the same grid;
nothing is solved here.

    bill = read units + WRITE_PRICE x write units          (a write unit is priced 5 read units)
Three forms, each its own row (the units add per step row: compose.bill_units):
    lower   max(requests, bytes / unit) per step row and kind
    upper   requests + bytes / unit
    exact   reads: each request rounded up alone (the capture's get_fetch_4k + range_fetch_4k, + 1 per request that
            returned nothing); writes: each PUT and DELETE rounded up alone to 1 KB units, at least 1, a DELETE of
            unknown size 1 (the capture's put_units_1k + del_units_1k: bill_write_exact)
The plan of a query is the pick record's (compose.query_pick; the plan rule, python/kvevaluator/plan_choice/candidates.py), the same plan every other number prices:
the bill is NOT a pick rule here. bill_picks_moved.csv says what it would change if it were (the plan with the
smallest bill on the reported params, per option and query: a size, not a pick).

    python3 experiments/layout_choice/bill_search.py --sf 3 --grid output/captures/ldbc/sf3 --root <dir> --workload i01 \
        --results <the summary/<dataset>/<scale>/ dir of the output tree search-mix wrote into>
    python3 experiments/layout_choice/bill_search.py --sf 3 --results <out root>/bill/rows   (run-layout-choice.sh's
        stage: the suite's grid, every workload, into <out root>/bill/chunked-64k, where plot_bill.py --sf 3 reads it)
        -> <root>/<workload dir>/bill_optimum.csv      per form: the 9 uniform layouts and the best per type mix
           <root>/<workload dir>/bill_queries.csv      the same options per query (times how often it runs)
           <root>/<workload dir>/bill_picks_moved.csv  per option and query whose plan the bill alone would change
"""
import argparse
import csv
import json
import os
import sys
import time
from kvcost.layout_search import results as R

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.layout_cost.compose import (BILL, CHANNELS, Composer, currencies, load, measure_only_for_assignment,
                                      params_of, cost_mix, query_pick, workload_units)
from kvcost.layout_search.search import uniform
from kvcost.layout_cost.compose import CELLS, add_pick_by_arg
from kvcost.layout_cost.mixes import assignment_label
from kvcost.layout_cost import benchmarks
from kvevaluator.output_tree import Tree, same_place
from kvcost.layout_cost import picks
from kvcost.layout_cost import cost_models as CM

# the three forms are cost models (kvcost.layout_cost.cost_models: bill_lower, bill_upper, bill_exact); here by short name
FORMS = {form: CM.BILL_UNITS[f"bill_{form}"] for form in ("lower", "upper", "exact")}
TOL = 1e-9


def bill(form):
    """the cost model of one form (a module function, so the mix cost cache keys it by its code)"""
    return getattr(CM, f"bill_{form}")


def per_query(w, data, assign):
    """[(family, query, how often, the pick, {plan: vec})] under `assign`; the vec is one run's, mean over the params"""
    out = []
    for family, q, often, params in w["items"]:
        d = data[(family, q)]
        units = {u for per in d["cost"].values() for u in per if u is not None}
        best, tots, _ = query_pick(d, params_of(d, params), assign, units)
        pool = {v: t for v, t in tots.items() if v not in measure_only_for_assignment(d, assign, units)}
        out.append((family, q, often, best, pool))
    return out


def bracket_violations(name, option, queries):
    """lower <= exact <= upper on the reads of every query (0 expected), and requests <= exact on the writes (every PUT
    and DELETE is at least 1 unit; the lower and upper forms are no bound on the writes' exact units: they count a DELETE
    as 1 unit, and the lower form counts a DELETE's key bytes as PUT bytes)"""
    bad = []
    for family, q, _, best, pool in queries:
        v = pool[best]
        s = TOL * max(1.0, v["bill_read_upper"], v["bill_write_upper"])
        if not v["bill_read_lower"] - s <= v["bill_read_exact"] <= v["bill_read_upper"] + s:
            bad.append((name, option, family, q, "reads", v["bill_read_lower"], v["bill_read_exact"], v["bill_read_upper"]))
        if not v["P"] + v["D"] - s <= v["bill_write_exact"]:
            bad.append((name, option, family, q, "writes", v["P"] + v["D"], v["bill_write_exact"], v["bill_write_upper"]))
    return bad


def row_mix(results, w, grid, form):
    """(assignment, nodes, the row's bill) of the one proven `kvcost search-mix --cost-model bill_<form> --settings Sp,Co,Ch64k`
    row of workload w solved on `grid`"""
    path = R.home(results, w["id"], "per-type")
    rows = []
    if os.path.exists(path):
        with open(path) as f:
            rows = [r for r in json.load(f)["rows"] if r["model"] == f"bill_{form}" and r["question"] == "fewest"
                    and r["limit"] is None and r["weighting"] == "sum" and same_place(r["capture"], grid)]
    if len(rows) != 1 or not rows[0]["proven"]:
        raise SystemExit(f"{path}: {len(rows)} proven fewest bill_{form} rows solved on {grid} (want 1: kvcost search-mix "
                         f"--cost-model bill_{form} --settings Sp,Co,Ch64k --from <{grid}'s tree>, written into the tree of "
                         f"{results})")
    r = rows[0]
    return R.assignment_of(r["assignment"]), r["nodes"], r["totals"][f"bill_{form}"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    ap.add_argument("--grid", default=None, help="the grid: a captures/<dataset>/<scale> dir (default: the output tree's)")
    ap.add_argument("--root", default=None, help="the analysis dir to write into, one folder per workload (default: "
                    "<out root>/bill/chunked-64k: the working grid's Chunked items are 64 KB)")
    ap.add_argument("--results", required=True, help="the summary/<dataset>/<scale>/ dir of the output tree `kvcost "
                    "search-mix` wrote the bill rows into (<results>/mix-search/<workload>/mixes-of-Sp-Co-Ch64k.json), solved on "
                    "the same grid")
    benchmarks.add_workload_arg(ap)
    add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    a = ap.parse_args()
    # the grid's three settings only; the bill's units are computed per step row in load, from the measured requests
    # (bill_exact: the measured chunk view only)
    composer = Composer(criterion=a.criterion, bill="sized", plan_rule=picks.PlanRule(), chunk_view="measured")
    a.root = a.root or os.path.join(benchmarks.out_root(a.dataset, a.sf, a.criterion, a.param_set), "bill", "chunked-64k")
    grid = a.grid or str(Tree().place_dir("captures", a.dataset, a.sf))
    data = load(grid, False, a.dataset, composer=composer)
    bad = []
    chosen = benchmarks.select(a.dataset, a.sf, a.workload)
    for name, w in chosen.items():
        t0 = time.time()
        units = workload_units(w, data)
        options = {c: uniform(units, *c[len("props_"):].split("_adj_")) for c in CELLS}
        nodes = {}
        for form in FORMS:
            assign, n, got = row_mix(a.results, w, grid, form)
            mix = bill(form)(cost_mix(w, data, assign))
            assert abs(mix - got) <= 1e-6 * got, (form, mix, got)          # the row's total == the re-pricing here
            options[f"per_type_fewest_bill_{form}"], nodes[f"per_type_fewest_bill_{form}"] = assign, n
        rows, qrows, moved = [], [], []
        for option, assign in options.items():
            t = cost_mix(w, data, assign)
            queries = per_query(w, data, assign)
            bad += bracket_violations(name, option, queries)
            for form in FORMS:
                r, wr = FORMS[form]
                best1 = min(CELLS, key=lambda c: (bill(form)(cost_mix(w, data, options[c])), c))
                rows.append({"workload": name, "form": form, "option_set": "single" if option in CELLS else "per type",
                             "option": option, "bill": round(bill(form)(t), 3), "read_units": round(t[r], 3),
                             "write_units": round(t[wr], 3),
                             "bill_x_best_single": round(bill(form)(t) / bill(form)(cost_mix(w, data, options[best1])), 4),
                             **{k: round(x, 3) for k, x in currencies(t).items()}, **{c: round(t[c], 3) for c in CHANNELS},
                             "nodes": nodes.get(option, ""), "assignment": assignment_label(assign)})
                for family, q, often, best, pool in queries:
                    v = pool[best]
                    qrows.append({"workload": name, "form": form, "option": option, "family": family, "query": q,
                                  "how_often": often, "plan": best, "bill": round(often * bill(form)(v), 3),
                                  "read_units": round(often * v[r], 3), "write_units": round(often * v[wr], 3),
                                  **{c: round(often * v[c], 3) for c in CHANNELS}})
                    alt = min(sorted(pool), key=lambda p: bill(form)(pool[p]))
                    if bill(form)(pool[alt]) < bill(form)(v) * (1 - TOL):
                        moved.append({"workload": name, "form": form, "option": option, "family": family, "query": q,
                                      "how_often": often, "plan": best, "bill": round(often * bill(form)(v), 3),
                                      "plan_fewest_bill": alt, "bill_fewest": round(often * bill(form)(pool[alt]), 3),
                                      "fewest_x_plan": round(bill(form)(pool[alt]) / bill(form)(v), 4),
                                      "plans_usable": len(pool)})
        out = benchmarks.workload_dir(a.dataset, a.sf, name, w, a.root, criterion=a.criterion, param_set=a.param_set)
        os.makedirs(out, exist_ok=True)
        for fname, rws in (("bill_optimum.csv", rows), ("bill_queries.csv", qrows), ("bill_picks_moved.csv", moved)):
            fields = list(rws[0]) if rws else ["workload", "form", "option", "family", "query"]
            with open(os.path.join(out, fname), "w", newline="") as fh:     # no wall time: a re-run gives the same bytes
                wr = csv.DictWriter(fh, fieldnames=fields)
                wr.writeheader()
                wr.writerows(rws)
        for form in FORMS:
            get = lambda o: next(x for x in rows if x["form"] == form and x["option"] == o)
            best1 = min(CELLS, key=lambda c: (get(c)["bill"], c))
            mix = get(f"per_type_fewest_bill_{form}")
            n_moved = sum(1 for m in moved if m["form"] == form and m["option"] in (best1, mix["option"]))
            print(f"{name} [{form}]: best uniform {best1} bill {get(best1)['bill']:,.0f}; best mix {mix['bill']:,.0f} "
                  f"({get(best1)['bill'] / mix['bill']:.3f}x less), {mix['nodes']:,} nodes; picks the bill alone would move on "
                  f"these two options: {n_moved}", flush=True)
        print(f"  {len(options)} options x {len(w['items'])} queries x {len(FORMS)} forms priced; the mixes from "
              f"{a.results}; all {time.time() - t0:.0f} s\n  -> {out}", flush=True)
    print(f"bracket check (reads lower <= exact <= upper; writes requests <= exact), per option and query: "
          f"{len(bad)} violations")
    for b in bad[:20]:
        print("  VIOLATION", b)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
