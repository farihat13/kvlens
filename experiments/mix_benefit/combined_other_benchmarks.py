#!/usr/bin/env python3
"""The combined panel on LDBC BI, IMDb / JOB and LinkBench at SF3: the nine uniform layouts and the fewest-combined mix
(per vertex type and per edge type and direction, no byte limit). Analysis only: every cost is composed from the nine
uniform captures; no store is opened.

    c(q, L)  = wops + bytes / 4,096 of one run of query q on layout L (the model wops_bytes4k), where
               wops  = G + 1.1 S + 0.01 N, plus PUTs and DELETEs and a write's own reads
               bytes = bytes read (missed keys in) + bytes written + the bytes a write reads first
    BI, JOB  (equal contribution: they define no frequencies; python/kvcost/layout_cost/query_weighting.py)
               plotted:  score(L) = geomean over q of c(q, L)
               solved:   additive(L) = (1/n) sum over q of c(q, L) / c(q, G), G = the geomean-best uniform layout,
                         i.e. weights w_q = 1 / c(q, G); the mix found is then scored under both. With --combine-queries
                         geomean the mix is the one solved under the geomean itself, with --combine-queries sum the one
                         solved on each query once (the sum); either is scored under its own form too
    LinkBench  score(L) = sum over q of f_q c(q, L), f_q = the request log's count (workload L01); solved on that sum
    Each score is shown over Sp-Co's under the same form.

How each mix is found: not here. Each is read from a result row of kvcost search-mix in the tree --from names, at its
LDBC scale's place, as every question's (kvcost search-mix --question bi, job, linkbench write them): B01 and J01 per type and direction, the fewest wops_bytes4k under the --combine-queries form (default
additive); L01 per type and direction, the fewest wops_bytes4k summed. Each row must say proven (BI and LinkBench by branch and bound on the
per-query costs; JOB by the solve without the cost cache, layout_search/bodies.py: a JOB query reads up to 33 of 54 parts, so its
cost cache does not fit), and its score must equal this script's pricing of its assignment (relative difference <= 1e-9),
else exit 1. The ceiling bounds every mix from below: each query at its cheapest plan with every part at its cheapest
source, validity ignored.
--check-two-step: the two-step search ((a) each query's plan fixed at the best uniform layout B, each part alone at
its cheapest setting, re-priced with each query's own pick; (b) from (a), one part at a time while the re-priced sum
drops) against the exact one on every one-metric workload the exact search solves (W01-W15, I01, I02, BI,
LinkBench L01; per type and per type and direction); "equal" = relative difference <= 1e-12.

Gate: the nine uniform layouts' wops, bytes and combined under each benchmark's headline form equal
cross_benchmark_frequencies.csv (81 cells), and each mix's score equals its result row's (3 cells), else exit 1.

    uv run --locked python experiments/mix_benefit/combined_other_benchmarks.py [--dataset ldbc=3,imdb=full,linkbench=n1m]
        [--from TREE] [--out DIR] [--combine-queries additive|geomean|sum] [--check-two-step]
        -> <out>/combined_options.csv    per benchmark: the nine, the mix, the ceiling; each score, its ratio to Sp-Co,
                                         best uniform / it, the mix's assignment, how it was found
           <out>/combined_per_query.csv  per (benchmark, query, option): frequency, weight, wops, bytes, combined, every
                                         raw channel
           <out>/two_step_vs_exact.csv   with --check-two-step: per (workload, option set) steps (a), (b) and the exact
                                         optimum, their times, assignments
           <out>/README.md
Inputs: --from, the output tree everything is read from (default the output dir; paper-results for the paper's run):
the mixes' rows (summary/<dataset>/<scale>/mix-search/<b01|j01|l01>/, each its dataset's), the frequencies file,
and each benchmark's layouts by output_tree.layout_capture (measured itself, else composed/: kvcost compose).
"""
import argparse
import csv
import math
import os
import sys
import time
from collections import defaultdict

from kvcost.layout_cost import benchmarks, picks
from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost.mixes import assignment_label
from kvcost.layout_cost.compose import (CELLS, LAYOUTS, Composer, currencies, load, params_of, cost_mix, query_pick,
                                      workload_units)
from kvcost.layout_cost.workloads import make_workloads
from kvcost.layout_search.search import Problem, solve, uniform
from kvcost.layout_search.results import ResultFile  # (one reader: refuses a file mixing frequencies)
from kvevaluator import sizes
from kvevaluator.output_tree import Tree
from kvcost.layout_search import results as R

REF = "props_split_adj_condensed"   # the 1x reference: Sp-Co (as the I01 panel)
TOL = 1e-12                          # "equal" for two sums: relative difference
ROW_TOL = 1e-9                       # a result row's score against this script's pricing of its assignment
INF = float("inf")


def the_sum(t):
    return CM.cost(t, "wops_bytes4k")


def geomean(xs):
    return math.exp(math.fsum(math.log(x) for x in xs) / len(xs))


def lb_workload(tree, run):
    """LinkBench L01 with the request log's counts: the weight column of the tree's per-query costs (its grid carries no
    params/mix.json), at the run's scales"""
    with open(tree.summary("ldbc", sizes.scale(run, "ldbc"), "mix-benefit", "per-query-disagreement") / "costs.csv") as f:
        counts = {r["query"]: float(r["weight"]) for r in csv.DictReader(f) if r["family"] == "LinkBench"}
    return make_workloads.linkbench(sizes.scale(run, "linkbench"), counts=counts)["lb_default"]   # the recipe on these counts


def bi_workload(data):
    qs = sorted({q for f, q in data if f == "bi"}, key=lambda q: (int("".join(ch for ch in q if ch.isdigit())), q))
    return {"id": "bi", "title": "LDBC BI, every query", "items": [("bi", q, 1.0, None) for q in qs]}


def one(data, item, assign):
    return cost_mix({"items": [(item[0], item[1], 1.0, item[3])]}, data, assign)


def units_of(d):
    return {u for per in d["cost"].values() for u in per if u is not None}


def pick(d, assign):
    """(the plan the query picks under assign, {plan: its cost vector})"""
    best, tots, _ = query_pick(d, params_of(d, None), assign, units_of(d))
    return best, tots


def fixed_sum(w, data, assign, plans):
    """the weighted sum with each query at its given plan (INF where that plan is not usable under assign)"""
    tot = 0.0
    for fam, q, often, _ in w["items"]:
        _, tots = pick(data[(fam, q)], assign)
        if not tots or plans[(fam, q)] not in tots:
            return INF
        tot += often * the_sum(tots[plans[(fam, q)]])
    return tot


def two_step(w, data, units, singles):
    """the two-step search: {B, B's sum, (a)'s assignment and sum (fixed plans; re-priced), (b)'s, times, moves, the
    plans at B}"""
    t0 = time.time()
    sums = {c: the_sum(cost_mix(w, data, singles[c])) for c in CELLS}
    B = min(CELLS, key=lambda c: (sums[c], c))
    base = singles[B]
    plans = {(f, q): pick(data[(f, q)], base)[0] for f, q, _, _ in w["items"]}
    base_val = fixed_sum(w, data, base, plans)
    a = dict(base)
    for u in sorted(units):                        # (a): each part alone, the others at B, the plans fixed
        best_v = base_val
        for l in LAYOUTS:
            if l == base[u]:
                continue
            v = fixed_sum(w, data, {**base, u: l}, plans)
            if v < best_v * (1 - 1e-12):
                a[u], best_v = l, v
    a_fixed = fixed_sum(w, data, a, plans)
    ta = cost_mix(w, data, a)
    a_val = the_sum(ta) if ta is not None else INF
    secs_a = time.time() - t0
    cur, val = (dict(a), a_val) if a_val <= sums[B] else (dict(base), sums[B])
    tried, improved = 0, True
    while improved:                                # (b): one part at a time while the re-priced sum drops
        improved = False
        for u in sorted(units):
            for l in LAYOUTS:
                if l == cur[u]:
                    continue
                t = cost_mix(w, data, {**cur, u: l})
                tried += 1
                if t is not None and the_sum(t) < val * (1 - 1e-12):
                    cur, val, improved = {**cur, u: l}, the_sum(t), True
    return {"B": B, "B_sum": sums[B], "a": a, "a_fixed": a_fixed, "a_sum": a_val, "secs_a": secs_a, "b": cur,
            "b_sum": val, "secs_b": time.time() - t0, "tried": tried, "plans": plans}


def exact(w, data, units, singles, B):
    t0 = time.time()
    pb = Problem(w, data, units, extra={"sum": the_sum})
    view = pb.under("sum")
    assign, got, _, nodes, _ = solve(view, "ops", None, view.from_assign(singles[B]), plain=True)
    assert abs(the_sum(cost_mix(w, data, assign)) - got) <= 1e-6 * got     # the cost cache == the re-pricing
    return assign, got, nodes, time.time() - t0


def equal_share(w, data, singles):
    """(w with each item's `often` = 1 / its cost on the geomean-best uniform layout G, G)"""
    cost = {(it[1], c): the_sum(one(data, it, singles[c])) for it in w["items"] for c in CELLS}
    geo = {c: geomean([cost[(it[1], c)] for it in w["items"]]) for c in CELLS}
    G = min(CELLS, key=lambda c: (geo[c], c))
    return {**w, "items": [(f, q, 1.0 / cost[(q, G)], p) for f, q, _, p in w["items"]]}, G


def query_floor(d):
    """a lower bound on one query's cost over every assignment: its cheapest plan with each part at its own cheapest
    source cell, validity ignored (min over plans of the sum over parts of the min)"""
    params = params_of(d, None)
    by_v = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for (cell, v, i), rows in d["cost"].items():
        if cell in CELLS and i in params:
            for u, vec in rows.items():
                if vec:
                    by_v[v][u][cell] += the_sum(vec) / len(params)
    return min((math.fsum(min(cs.values()) for cs in units.values()) for v, units in by_v.items()
                if any(v in d["valid"].get(c, set()) for c in CELLS)), default=INF)


def benchmark_cases(a):
    """(name, suite, grid, families, historical, frequencies column, form, the result row's workload id, workload(data))
    of each benchmark whose dataset the run (--dataset) holds"""
    cases = [
        ("LDBC BI", "ldbc", place_of(a, "ldbc"), benchmarks.BI, False, "equal contribution", "geomean", "b01", bi_workload),
        ("IMDb / JOB", "imdb", place_of(a, "imdb"), None, False, "equal contribution", "geomean", "j01",
         lambda data: next(iter(benchmarks.select("imdb", sizes.scale(a.run, "imdb")).values()))),
        # LinkBench's capture predates the plan files at HEAD: read as captured (PlanRule.historical)
        ("LinkBench", "linkbench", place_of(a, "linkbench"), None, True, "its log's frequencies", "sum", "l01",
         lambda data: lb_workload(Tree(a.root), a.run)),
    ]
    for c in cases:
        if not sizes.scale(a.run, c[1]):
            print(f"{c[0]}: {c[1]} is not in this run (--dataset {sizes.text(a.run)}): left out")
    return [c for c in cases if sizes.scale(a.run, c[1])]


def place_of(a, dataset):
    """the benchmark's place in the tree read, captures/<dataset>/<scale> (each layout read from it by
    output_tree.layout_capture), or None when the run leaves it out"""
    sc = sizes.scale(a.run, dataset)
    return sc and str(Tree(a.root).place_dir("captures", dataset, sc))


def mix_row(a, dataset, wid, weighting):
    """the one proven row of kvcost search-mix for this workload: per type and direction, the fewest wops_bytes4k, no
    limit, under `weighting`, in its dataset's rows (summary/<dataset>/<scale>/mix-search/<wid>/); (its assignment as
    parts, the row)"""
    path = Tree(a.root).summary(dataset, sizes.scale(a.run, dataset), "mix-search", wid,
                                R.stem("per-type-and-direction") + ".json")
    if not os.path.exists(path):
        raise SystemExit(f"{path}: no result file (kvcost search-mix --question writes it)")
    rows = [r for r in ResultFile(path).read() if r["question"] == "fewest" and r["model"] == "wops_bytes4k"
            and r["limit"] is None and r["weighting"] == weighting]
    if len(rows) != 1:
        raise SystemExit(f"{path}: {len(rows)} rows for the fewest wops_bytes4k under {weighting}, not one")
    r = rows[0]
    if not r["proven"]:
        raise SystemExit(f"{path}: the {weighting} row is not proven ({r['nodes']} nodes): the panel shows proven mixes only")
    return R.assignment_of(r["assignment"]), r


def mode_rule(historical):
    """BI, JOB: in-sample by policy (no held-out set); LinkBench: one body per request"""
    return picks.PlanRule(in_sample=True, historical=historical)


def load_grid(grid, by_dir, benchmark, fams, rule):
    """the grid's data under `rule`. LinkBench is loaded with no pick record: it has one body per request, so the record
    decides nothing there, and the binding rule's scale check reads a scale as a number (pick_record.tag refuses 'n1m')"""
    param_set = None
    if benchmark == "linkbench":
        rule = rule.but(record=None)
        param_set = None    # its rows: LinkBench's one default set (output_tree.default_param_set: the whole request log)
    return load(grid, by_dir, benchmark, composer=Composer(criterion=None, families=fams, plan_rule=rule, param_set=param_set))


def panel(a):
    """the three benchmarks: rows for combined_options.csv and combined_per_query.csv, and the gate's failures"""
    snap = {}
    with open(Tree(a.root).summary("ldbc", "3", "mix-benefit", "cross-benchmark-frequencies") / "cross_benchmark_frequencies.csv") as f:
        for r in csv.DictReader(f):
            snap[(r["benchmark"], r["frequencies"], r["cell"])] = r
    rows, per_query, bad, cells = [], [], 0, 0
    for name, benchmark, grid, fams, historical, freq, form, wid, wl in benchmark_cases(a):
        data = load_grid(grid, True, benchmark, fams, mode_rule(historical))
        w = wl(data)
        units = workload_units(w, data)
        singles = {c: uniform(units, *c[len("props_"):].split("_adj_")) for c in CELLS}
        often = {it[1]: it[2] for it in w["items"]}
        wq, G = equal_share(w, data, singles) if form == "geomean" else (w, None)
        weight = {it[1]: it[2] for it in wq["items"]}
        t0 = time.time()
        solved = a.weighting if form == "geomean" else "sum"      # the form BI's and JOB's mix was solved under
        assign, row = mix_row(a, benchmark, wid, solved)
        if set(assign) != set(units):
            raise SystemExit(f"{name}: the result row's parts are not this workload's ({len(assign)} vs {len(units)})")
        how = (f"exact, proven ({row['nodes']} nodes; kvcost search-mix"
               + (", no cost cache" if row.get("solver") == "no-cost-cache" else "")
               + (f"; solved under the {solved} form" if form == "geomean" and solved != "additive" else "") + ")")
        secs = time.time() - t0
        cost = {(it[1], o): one(data, it, singles[o] if o != "mix" else assign) for it in w["items"] for o in CELLS + ["mix"]}
        floor = {it[1]: query_floor(data[(it[0], it[1])]) for it in w["items"]}
        over = [q for q in floor if any(floor[q] > the_sum(cost[(q, o)]) * (1 + 1e-9) for o in CELLS + ["mix"])]
        if over:
            bad += 1
            print(f"FAIL {name}: the ceiling is above a priced option for {over[:5]}")
        forms = ["geomean", "additive"] if form == "geomean" else ["sum"]
        if solved not in forms:                    # each query once: also scored on the plain sum
            forms.append(solved)

        def score(o, h):
            vals = [floor[it[1]] if o == "floor" else the_sum(cost[(it[1], o)]) for it in w["items"]]
            if h == "geomean":
                return geomean(vals)
            if h == "additive":
                return math.fsum(v / the_sum(cost[(it[1], G)]) for v, it in zip(vals, w["items"])) / len(vals)
            return math.fsum(often[it[1]] * v for v, it in zip(vals, w["items"]))
        options = CELLS + ["mix", "floor"]
        sc = {(o, h): score(o, h) for o in options for h in forms}
        # the row's score is this script's pricing of its assignment: the same workload, plan rule and form
        want = row["score"]["value"] if solved != "sum" else row["totals"]["wops_bytes4k"]
        mine = sc[("mix", solved)]
        cells += 1
        if abs(mine - want) > ROW_TOL * max(1.0, abs(want)):
            bad += 1
            print(f"FAIL {name}: the result row's score {want!r}, priced here {mine!r}")
        for c in CELLS:                        # the gate: the nine against the snapshot
            s = snap[(name, freq, c)]
            for m, f in (("wops", lambda t: currencies(t)["ops"]), ("bytes", lambda t: currencies(t)["bytes"]),
                         ("wops_bytes4k", the_sum)):
                vals = [f(cost[(it[1], c)]) for it in w["items"]]
                mine = geomean(vals) if form == "geomean" else math.fsum(often[it[1]] * v for v, it in zip(vals, w["items"]))
                cells += 1
                if abs(mine - float(s[m])) > 1e-6 * max(1.0, float(s[m])):
                    bad += 1
                    print(f"FAIL {name} {c} {m}: {mine} here, {s[m]} in the snapshot")
        best_u = {h: min(CELLS, key=lambda c: (sc[(c, h)], c)) for h in forms}
        for o in options:
            r = {"benchmark": name, "option": o, "headline_form": form, "solved_form": solved if o == "mix" else "",
                 "geomean_best_uniform": G or "",
                 "found_by": how if o == "mix" else ("a bound: each query at its own cheapest plan and per-part source"
                                                     if o == "floor" else "uniform"),
                 "assignment": assignment_label(assign) if o == "mix" else ""}
            for h in ("geomean", "additive", "sum"):
                ok = h in forms
                r[f"{h}_score"] = round(sc[(o, h)], 6) if ok else ""
                r[f"{h}_ratio_to_spco"] = round(sc[(o, h)] / sc[(REF, h)], 6) if ok else ""
                r[f"{h}_best_uniform_over_this"] = round(sc[(best_u[h], h)] / sc[(o, h)], 6) if ok else ""
            rows.append(r)
            if o == "floor":
                continue
            for it in w["items"]:
                t = cost[(it[1], o)]
                cur = currencies(t)
                per_query.append({"benchmark": name, "query": it[1], "option": o, "frequency": often[it[1]],
                                  "weight": weight[it[1]], "wops": round(cur["ops"], 6), "bytes": round(cur["bytes"], 6),
                                  "wops_bytes4k": round(the_sum(t), 6), **{k: round(t[k], 6) for k in sorted(t)}})
        print(f"{name}: {len(w['items'])} queries, {len(units)} parts; best uniform "
              + ", ".join(f"{h} {best_u[h]}" for h in forms) + "; best uniform / mix "
              + ", ".join(f"{h} {sc[(best_u[h], h)] / sc[('mix', h)]:.4f}" for h in forms) + "; ceiling "
              + ", ".join(f"{h} {sc[(best_u[h], h)] / sc[('floor', h)]:.4f}" for h in forms)
              + f"; {how}, {secs:.0f} s", flush=True)
    print(f"gate: {cells} cells (the nine against cross_benchmark_frequencies.csv, and each mix's result row); {bad} failed")
    return rows, per_query, bad


def check_two_step(a):
    """the two-step search against the exact one on every workload the exact search solves; rows, failures"""
    ldbc = sizes.scale(a.run, "ldbc")
    ldbc_grid = str(Tree(a.root).place_dir("captures", "ldbc", ldbc))
    cases = [("LDBC", "ldbc", ldbc_grid, None, False, False,
              lambda data: benchmarks.select("ldbc", ldbc)),
             ("LDBC BI", "ldbc", place_of(a, "ldbc"), benchmarks.BI, True, False, lambda data: {"bi_equal_share": bi_workload(data)}),
             ("LinkBench", "linkbench", place_of(a, "linkbench"), None, True, True,
              lambda data: {"lb_default": lb_workload(Tree(a.root), a.run)})]
    cases = [c for c in cases if sizes.scale(a.run, c[1])]
    rows = []
    for label, benchmark, grid, fams, in_sample, historical, wls in cases:
        rule = picks.PlanRule(in_sample=True if in_sample or historical else None, historical=historical)
        for by_dir in (False, True):
            oset = "per type and direction" if by_dir else "per type"
            data = load_grid(grid, by_dir, benchmark, fams, rule)
            for name, w in wls(data).items():
                units = workload_units(w, data)
                singles = {c: uniform(units, *c[len("props_"):].split("_adj_")) for c in CELLS}
                if name == "bi_equal_share":
                    w, _ = equal_share(w, data, singles)
                r2 = two_step(w, data, units, singles)
                ex, ev, nodes, es = exact(w, data, units, singles, r2["B"])
                ra, rb = abs(r2["a_sum"] - ev) / ev, abs(r2["b_sum"] - ev) / ev
                rows.append({"case": label, "workload": f"{w['id']} {name}", "option_set": oset, "parts": len(units),
                             "best_uniform": r2["B"], "best_uniform_sum": r2["B_sum"], "exact_sum": ev,
                             "a_sum_fixed_plans": r2["a_fixed"], "a_sum": r2["a_sum"], "b_sum": r2["b_sum"],
                             "a_rel_diff": ra, "b_rel_diff": rb, "a_equal": ra <= TOL, "b_equal": rb <= TOL,
                             "exact_seconds": round(es, 2), "exact_nodes": nodes, "a_seconds": round(r2["secs_a"], 2),
                             "a_b_seconds": round(r2["secs_b"], 2), "b_moves": r2["tried"],
                             "exact_assignment": assignment_label(ex), "b_assignment": assignment_label(r2["b"])})
                print(f"{label} {w['id']} [{oset}] {len(units)} parts: exact {ev:,.3f} ({es:.1f} s, {nodes} nodes); "
                      f"(a) {'equal' if ra <= TOL else f'{ra:.2e} off'}, (b) {'equal' if rb <= TOL else f'{rb:.2e} off'} "
                      f"({r2['secs_b']:.1f} s)", flush=True)
    n = len(rows)
    print(f"two-step vs exact: {n} solves; (a) equal on {sum(r['a_equal'] for r in rows)}, (b) on "
          f"{sum(r['b_equal'] for r in rows)} (relative difference <= {TOL:g})")
    return rows, (0 if n else 1)


def write(path, rows):
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="root", default=None, help="the output tree read: the mixes' rows, the frequencies "
                                                               "file, each benchmark's layouts (default the output dir; "
                                                               "paper-results for the paper's run)")
    ap.add_argument("--dataset", dest="run", type=sizes.resolve, default=sizes.PAPER,
                    help="each dataset's scale, e.g. ldbc=0.1,imdb=sample1pct (kvevaluator.sizes; default the paper's)")
    ap.add_argument("--out", default=None, help="default <output>/summary/ldbc/<its scale>/mix-benefit/combined-other-benchmarks")
    ap.add_argument("--combine-queries", dest="weighting", default="additive", choices=("additive", "geomean", "sum"),
                    help="the form BI's and JOB's mixes were solved under (their rows' --combine-queries): additive (default; "
                         "the geomean's additive stand-in, w_q = 1 / c(q, G)), geomean (solved on the geomean itself), "
                         "sum (each query once). The panel scores every mix under the geomean, the headline")
    ap.add_argument("--check-two-step", action="store_true", help="also the two-step search against the exact one")
    a = ap.parse_args()
    a.out = a.out or str(Tree().summary("ldbc", sizes.scale(a.run, "ldbc"), "mix-benefit", "combined-other-benchmarks"))
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time()
    rows, per_query, bad = panel(a)
    write(os.path.join(a.out, "combined_options.csv"), rows)
    write(os.path.join(a.out, "combined_per_query.csv"), per_query)
    if a.check_two_step:
        ts, tbad = check_two_step(a)
        write(os.path.join(a.out, "two_step_vs_exact.csv"), ts)
        bad += tbad
    with open(os.path.join(a.out, "README.md"), "w") as fh:
        fh.write("# The combined panel on BI, IMDb / JOB and LinkBench (SF3)\n\n> "
                 "Generated by `experiments/mix_benefit/combined_other_benchmarks.py`; the formula and the "
                 "search are in its docstring.\n\n"
                 f"Inputs: the tree `{Tree(a.root).root}` (the mixes' rows, the frequencies, each benchmark's layouts). "
                 f"BI's and JOB's mixes: the fewest under the {a.weighting} form.\n\n"
                 "| benchmark | form | best uniform | best uniform / mix | ceiling (best uniform / bound) | mix found by |\n|---|---|---|---|---|---|\n")
        for b in ("LDBC BI", "IMDb / JOB", "LinkBench"):
            mine = {r["option"]: r for r in rows if r["benchmark"] == b}
            h = "geomean" if mine["mix"]["headline_form"] == "geomean" else "sum"
            best = min(CELLS, key=lambda c: float(mine[c][f"{h}_score"]))
            fh.write(f"| {b} | {h} | {best} | {float(mine['mix'][f'{h}_best_uniform_over_this']):.4f}x | "
                     f"{float(mine['floor'][f'{h}_best_uniform_over_this']):.4f}x | {mine['mix']['found_by']} |\n")
    print(f"combined_other_benchmarks: {'FAIL' if bad else 'PASS'} -> {a.out} ({time.time() - t0:.0f} s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
