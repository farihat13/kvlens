"""predict-formulas' tables (`just figure predict-formulas`): how accurate predict mode is on a workload, and where its
error comes from, from run/predict-accuracy.sh's steps.csv and levels.json in summary/<dataset>/<scale>/predict-formulas/
<workload>/<family>/, one dir per family of the workload (its reads and its writes), and the workload's predicted capture
from the paper's sampled tallies (`just results predict-formulas` makes both).

The levels are levels.json's (cpp/kvevaluator/src/predict/terms.h kLevels), every statistic at the default tier
(kvevaluator.predict.tier1.DEFAULT_TIER). The pass's levels take some kinds of term (the counts, the sizes, the chunks
touched) from the step's truth card and the rest from the statistics; the sampled levels are kvcost predict's capture
from the tallies counted on the paper's sample of the owners (uniform_49_tallies.PAPER_SAMPLE): at the body the run
picked, then at each layout's own predicted pick, predict-49's prediction. The chain measured -> true_chunks ->
true_terms -> true_counts -> predicted -> predicted_sampled -> predicted_pick hands one cause at a time to the
statistics, so each level minus the one before it measures that cause (levels.json `measures`; the order is the
chain's: another order splits the same total another way). On a workload total the signed differences add up
exactly, to predicted_pick - measured. The plan choice's part is at most 0 on the op-byte cost by construction (the
pick is the body the prediction ranks cheapest): it says how far the picked plan's predicted cost sits below the run's.

The workload total (the paper's metric): per layout and level, T = the sum over the workload's items of its frequency
(the workload file, as predict-49 reads it: layouts.one_workload, layout_costs.frequencies) x the mean over the item's
params of the query's KV steps; error = |T_level - T_measured| / T_measured. Three scopes from the same sums: the
workload (every item), its read items and its write items (the scopes read and write: the kind of an item's family,
benchmarks.kind_of). Four lane groups, each a
sum of like units, every one under the chunk view the op-byte cost takes (compose.default_chunk_view: the half view,
which charges a write's last-chunk touch the chunk target x the contract's last_chunk_fill on both sides): KV operations
(S + G + N + P + D, unweighted), bytes read (the 7 read lanes, miss keys included; a write's own reads too), bytes
written (write_key + write_value) and the op-byte cost (wops_bytes4k = G + 1.1 S + 0.01 N + P + D + bytes / 4,096,
cost_models' one definition: predicted.vector, predicted.cost).

Into --tables:
    summary.md          its first line: per level, the median and the max over the layouts of the workload total's error on
                        the lane groups, the workload's and its reads' alone; the sum check; the check line; then the
                        error per scope, the spread and the tables
    workload_totals.csv per scope, layout, level and lane group: the total, measured, error, and the signed part (this
                        level minus the chain's level before it, over measured)
    by_operator.csv     per operator, layout, lane group and level, over the steps: measured and level totals, the error
                        (sum |level - measured| / sum measured), the step error (sum |level - the level before| / sum
                        measured), the ratio of sums, the steps within 1%
    points.csv          per KV step and level: KV operations, bytes read and the op-byte cost (the figure's per-step view)

    python -m kvcost.predict_accuracy.predict_formulas --dataset ldbc=3 --workload i01 --tables DIR [--from output]
"""
import argparse
import csv
import json
import os
import re
import statistics
from collections import defaultdict

from kvcost.layout_cost import benchmarks
from kvcost.layout_cost import compare as CP
from kvcost.layout_cost import layout_costs as LC
from kvcost.predict_accuracy import layouts as PL
from kvcost.predict_accuracy import predicted as PD
from kvcost.predict_accuracy import uniform_49_tallies as U49
from kvevaluator import layouts, sizes
from kvcost.layout_cost import compose
from kvevaluator.capture.step_record import BASE_OPS, READ_BYTE_LANES, WRITE_BYTE_LANES, WRITE_OPS
from kvevaluator.plans.ir.capabilities import KV_WRITE_OPS
from kvevaluator.output_tree import Tree
from kvevaluator.plan_choice import candidates
from kvevaluator.plan_choice.optimizer.criteria import DEFAULT_CRITERION
from kvevaluator.paths import paper_results_dir

MODEL = DEFAULT_CRITERION                        # the op-byte cost, the model that chooses a plan (wops_bytes4k)
GROUPS = ("KV operations", "bytes read", "bytes written", "op-byte cost")
VIEW = compose.default_chunk_view(MODEL)         # the chunk view of a write's lanes (the half view): every lane group's
SCOPES = ("workload", "read", "write")           # the workload's items, and its items of each kind (kind_of)
WITHIN = 0.01                                    # "within 1%": |level - measured| <= 1% of measured
ANALYSIS = "predict-formulas"                    # the item's output dir, summary/<dataset>/<scale>/<ANALYSIS>/<family>/
SUM_TOL = 1e-9                                   # the sum check: the parts add to predicted - measured, relative


def item_dir(source, dataset, scale, workload, family):
    """the item's dir for one read family of a workload in the tree --from names: summary/<dataset>/<scale>/
    predict-formulas/<workload>/<family>/ (a workload's own queries and params)"""
    tree = Tree() if source == "output" else Tree(paper_results_dir())
    return tree.summary(dataset, scale, ANALYSIS, workload, family)


def families(w):
    """the workload's families, in its items' order"""
    return list(dict.fromkeys(f for f, *_ in w["items"]))


def in_scope(family, scope):
    """whether a family's items count in a scope (the workload: every one; a kind: by benchmarks.kind_of)"""
    return scope == "workload" or benchmarks.kind_of(family) == scope


def vector_groups(v):
    """a costed vector's lane groups (predicted.vector: its raw channels, under its chunk view, and its cost)"""
    return {"KV operations": sum(v[x] for x in (*BASE_OPS, *WRITE_OPS)), "bytes read": sum(v[x] for x in READ_BYTE_LANES),
            "bytes written": sum(v[x] for x in WRITE_BYTE_LANES), "op-byte cost": PD.cost(v, MODEL)}


def group_values(r):
    """a steps.csv row's lane groups (a write step's under VIEW)"""
    return vector_groups(PD.vector(r, r["op"] in KV_WRITE_OPS, view=VIEW, cell=r["cell"]))


def pass_levels(levels):
    """the chain's levels predict --accuracy costs (the others are the sampled capture's, kvcost predict's)"""
    return [lv["name"] for lv in levels if lv["chain"] and not lv["sampled"]]


def sampled_set():
    """the predicted set the sampled levels read: kvcost predict at the default tier from the tallies counted on the
    paper's sample of the owners (uniform_49_tallies.PAPER_SAMPLE), the set predict-49 reports"""
    return U49.sampled_set(U49.PAPER_SAMPLE)


def read_levels(src):
    """the pass's levels (levels.json), and the chain: its levels whose steps add up, in order"""
    path = os.path.join(src, "levels.json")
    if not os.path.isfile(path):
        raise SystemExit(f"predict_formulas: no {path} (run/predict-accuracy.sh writes it with the pass)")
    with open(path) as f:
        levels = json.load(f)
    return levels, [lv["name"] for lv in levels if lv["chain"]]


def read_steps(path, names):
    """{(cell, query, param, step): {"op": op, level: {group: value}}} for the KV steps that every level costed; the
    steps a level did not cost, per op; the (cell, query) pairs holding one (left out of the workload totals, whole); and
    {(cell, query, param): the body_sha the run picked}"""
    by, bodies = {}, {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            k = (r["cell"], r["query"], r["param"], r["step"])
            by.setdefault(k, {"op": r["op"]})[r["source"]] = group_values(r)
            bodies[k[:3]] = r["body_sha"]
    steps, uncosted, holes = {}, defaultdict(int), set()
    for k, d in by.items():
        if any(lv not in d for lv in names):
            uncosted[d["op"]] += 1
            holes.add(k[:2])
            continue
        steps[k] = d
    return steps, dict(uncosted), holes, bodies


def cell_vectors(cap_dir, cell, family, runs, bodies, sampled):
    """one layout's vectors for the sampled levels: {(query, param, level): vector or None} (capture_sums)"""
    queries = sorted(q for c, q in runs if c == cell)
    by_body, _ = PD.by_body(cap_dir, cell, queries, view=VIEW)
    name = {(r["query"], candidates.successor_sha(family, r["query"], r["body_sha"])): r["body"]
            for r in PD.rows(cap_dir, "counts", cell) if r["query"] in queries}
    best = PD.pick(cap_dir, cell, queries, None, view=VIEW)[0] if any(lv["pick"] for lv in sampled) else {}
    out = {}
    for q in queries:
        for p in runs[(cell, q)]:
            for lv in sampled:
                if lv["pick"]:
                    out[(q, p, lv["name"])] = best[q][2].get(p) if q in best else None
                else:
                    out[(q, p, lv["name"])] = by_body.get((q, name.get((q, bodies[(cell, q, p)]))), {}).get(p)
    return out


def capture_sums(cap_dir, family, bodies, sampled):
    """the sampled levels' (cell, query, param) sums from their predicted capture (each run's counts row, costed as
    predict-49 costs it: predicted.by_body, predicted.pick): {(cell, query, param): {(level, group): value}}, and the
    (cell, query) pairs it does not cover. A level with `pick`: the body predict-49's plan rule picks per (cell, query)
    over the params run; else the body the run picked (by its body_sha, as the join finds it)"""
    out, holes = defaultdict(dict), set()
    runs = defaultdict(list)                          # (cell, query) -> the params the pass ran
    for (cell, q, p) in sorted(bodies):
        runs[(cell, q)].append(p)
    for cell in sorted({c for c, _ in runs}):
        if not os.path.isdir(cap_dir) or cell not in PD.cells(cap_dir):
            holes |= {k for k in runs if k[0] == cell}
            continue
        for (q, p, lv), v in cell_vectors(cap_dir, cell, family, runs, bodies, sampled).items():
            if v is None:
                holes.add((cell, q))
                continue
            for g, x in vector_groups(v).items():
                out[(cell, q, p)][(lv, g)] = x
    return out, holes


CHECK = re.compile(r"^exact gate on these steps: (\d+) of (\d+) lanes differ$")


def check_line(src):
    """the pass's own check line (predict --accuracy compares the truth rows with the run on every lane; its stderr is
    accuracy.err beside steps.csv): (lanes that differ, lanes compared)"""
    path = os.path.join(src, "accuracy.err")
    lines = [m for m in (CHECK.match(x.strip()) for x in open(path)) if m] if os.path.isfile(path) else []
    if len(lines) != 1:
        raise SystemExit(f"predict_formulas: {path} holds {len(lines)} check lines, not one")
    return int(lines[0].group(1)), int(lines[0].group(2))


def fmt(v):
    return "" if v is None else f"{v:.6g}"


def pct(v):
    return "-" if v in ("", None) else f"{100 * float(v):.1f}%"


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def layout_order(cells):
    """the layouts in the seven's order (kvevaluator.layouts.SEVEN), any other after them by name"""
    seven = list(layouts.SEVEN)
    return sorted(cells, key=lambda c: (seven.index(c) if c in seven else len(seven), c))


def by_operator(steps, chain):
    """per (op, layout, group, level): the step sums (module docstring)"""
    acc = defaultdict(lambda: {"steps": 0, "above0": 0, "measured": 0.0, "total": 0.0, "abs": 0.0, "step_abs": 0.0,
                               "within": 0})
    for (cell, *_), d in steps.items():
        for g in GROUPS:
            m = prev = d["measured"][g]
            for lv in chain:
                x = d[lv][g]
                a = acc[(d["op"], cell, g, lv)]
                a["steps"] += 1
                a["above0"] += m > 0
                a["measured"] += m
                a["total"] += x
                a["abs"] += abs(x - m)
                a["step_abs"] += abs(x - prev)
                a["within"] += abs(x - m) <= WITHIN * m if m > 0 else x == 0
                prev = x
    cells = layout_order({k[1] for k in acc})
    rows = []
    for (op, cell, g, lv), a in sorted(acc.items(), key=lambda kv: (kv[0][0], cells.index(kv[0][1]),
                                                                     GROUPS.index(kv[0][2]), chain.index(kv[0][3]))):
        den = a["measured"]
        rows.append([op, layouts.name_of(cell), g, lv, a["steps"], a["above0"], round(den, 6), round(a["total"], 6),
                     fmt(a["abs"] / den if den else None), fmt(a["step_abs"] / den if den else None),
                     fmt(a["total"] / den if den else None), a["within"]])
    return rows


def query_sums(steps, chain, extra=None):
    """(cell, query) -> param -> (level, group) -> the sum of its KV steps, measured as a level, for the levels of
    `chain` the steps carry; `extra`: {(cell, query, param): {(level, group): value}} for the others (capture_sums)"""
    per = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for (cell, query, param, _), d in steps.items():
        for lv in ("measured", *chain):
            if lv in d:
                for g in GROUPS:
                    per[(cell, query)][param][(lv, g)] += d[lv][g]
    for (cell, query, param), vals in sorted((extra or {}).items()):
        if (cell, query) in per and param in per[(cell, query)]:
            per[(cell, query)][param].update(vals)
    return per


def params_wanted(got, params):
    """the item's params (the workload file's, else every param run) when each has costed steps, else None"""
    want = [str(p) for p in params] if params else sorted(got or {}, key=int)
    return want if got and want and all(p in got for p in want) else None


def item_means(per, params_of, queries):
    """{(cell, query, (level, group)): the mean over the item's params}, and the (cell, query) pairs left out"""
    means, missing = {}, set()
    for cell in layout_order({c for c, _ in per}):
        for q in queries:
            got = per.get((cell, q))
            want = params_wanted(got, params_of[q])
            if not want:
                missing.add((cell, q))
                continue
            for k in sorted(set.intersection(*(set(got[p]) for p in want))):
                means[(cell, q, k)] = sum(got[p][k] for p in want) / len(want)
    return means, missing


def workload_totals(steps, chain, w, family, extra=None, left_out=()):
    """{(cell, level, group): T} with measured as a level, for the layouts that ran every item of `family`: the items at
    their frequencies (layout_costs.frequencies), each the mean over its params of its steps' sum, summed as every
    comparison sums a workload (compare.weighted_totals); and the (cell, query) pairs left out (a param with no costed
    step, or one `left_out` names)"""
    per = query_sums(steps, chain, extra)
    weights = {q: often for (f, q), often in sorted(LC.frequencies(w).items()) if f == family}
    means, missing = item_means(per, {q: p for f, q, _, p in w["items"] if f == family}, weights)
    missing |= set(left_out)
    complete = [c for c in layout_order({c for c, _ in per}) if not any(m[0] == c for m in missing)]
    return weighted(means, complete, chain, weights, family), missing


def weighted(means, complete, chain, weights, family):
    """{(cell, level, group): the items' means summed at their frequencies (compare.weighted_totals)}"""
    T = {}
    for lv in ("measured", *chain):
        for g in GROUPS:
            if any((c, q, (lv, g)) not in means for c in complete for q in weights):
                raise SystemExit(f"predict_formulas: level {lv} has no cost for every item of {family} on the layouts "
                                 "that ran them")
            pq = {(c, q): means[(c, q, (lv, g))] for c in complete for q in weights}
            for c, v in CP.weighted_totals(pq, complete, list(weights), weights, naming=False).items():
                T[(c, lv, g)] = v
    return T


def gather(srcs, w, capture_of):
    """every family's pass read once: the levels, the chain, the steps, the sampled levels from the capture
    `capture_of(family)` names, the totals per scope (SCOPES: each the sum of its families') and the check line, summed"""
    g = {"levels": None, "chain": None, "steps": {}, "uncosted": defaultdict(int),
         "T": {sc: defaultdict(float) for sc in SCOPES}, "missing": {sc: set() for sc in SCOPES}, "check": (0, 0),
         "captures": []}
    for family, src in srcs.items():
        lv, ch = read_levels(src)
        if g["chain"] is not None and ch != g["chain"]:
            raise SystemExit(f"predict_formulas: {src}/levels.json's chain {ch} is not {g['chain']}")
        g["levels"], g["chain"] = lv, ch
        st, unc, holes, bodies = read_steps(os.path.join(src, "steps.csv"), pass_levels(lv))
        if not st:
            raise SystemExit(f"predict_formulas: no KV step costed at every level in {src}/steps.csv")
        g["steps"].update(st)
        for op, n in unc.items():
            g["uncosted"][op] += n
        sampled = [x for x in lv if x["chain"] and x["sampled"]]
        cap = capture_of(family)
        extra, cap_holes = capture_sums(cap, family, bodies, sampled) if sampled else ({}, set())
        if sampled and not extra:
            raise SystemExit(f"predict_formulas: no run of {', '.join(x['name'] for x in sampled)} in {cap}: make it "
                             "with `just results predict-formulas` (kvcost predict from the sampled tallies)")
        g["captures"].append(cap)
        add_to_scopes(g, family, *workload_totals(st, ch, w, family, extra, cap_holes), holes)
        d, c = check_line(src)
        g["check"] = (g["check"][0] + d, g["check"][1] + c)
        g["statistics"] = one_statistics(g.get("statistics"), read_statistics(src), src)
    return g


def one_statistics(before, st, src):
    """the statistics every family's pass read: one file, or refused"""
    if before and before["sha256"] != st["sha256"]:
        raise SystemExit(f"predict_formulas: {src} read the statistics {st['path']} (sha256 {st['sha256'][:12]}), "
                         f"another family {before['path']} (sha256 {before['sha256'][:12]}): one workload, one statistics")
    return st


def add_to_scopes(g, family, totals, miss, holes):
    """one family's workload totals, and the (cell, query) pairs it left out, into each scope it counts in"""
    for sc in SCOPES:
        if in_scope(family, sc):
            for k, v in totals.items():
                g["T"][sc][k] += v
            g["missing"][sc] |= miss | holes


def read_statistics(src):
    """the statistics the pass's predicted levels read (statistics.json, run/predict-accuracy.sh writes it)"""
    path = os.path.join(src, "statistics.json")
    if not os.path.isfile(path):
        raise SystemExit(f"predict_formulas: no {path} (run/predict-accuracy.sh writes it with the pass)")
    with open(path) as f:
        return json.load(f)


def total_rows(T, cells, chain, measures, wid, scope):
    """one scope's workload_totals.csv rows, and the sum check: the largest |sum of the parts - (last - measured)| /
    measured"""
    rows, worst = [], 0.0
    for cell in cells:
        for g in GROUPS:
            m = prev = T[(cell, "measured", g)]
            parts = 0.0
            for lv in chain:
                x = T[(cell, lv, g)]
                part = (x - prev) / m if m else None
                parts += part or 0.0
                rows.append([wid, scope, layouts.name_of(cell), lv, g, f"{x:.6g}", f"{m:.6g}",
                             fmt(abs(x - m) / m if m else None), fmt(part), measures[lv]])
                prev = x
            if m:
                worst = max(worst, abs(parts - (T[(cell, chain[-1], g)] - m) / m))
    return rows, worst


def complete_cells(T, missing, wid):
    """the layouts that costed every item of a scope at every level, in the seven's order; refused when there is none"""
    cells = [c for c in layout_order({k[0] for k in T}) if not any(c == m[0] for m in missing)]
    if not cells:
        raise SystemExit(f"predict_formulas: no layout costed every item of {wid} at every level (left out: "
                         f"{', '.join(sorted({layouts.name_of(c) + ' ' + q for c, q in missing})[:8])} ...)")
    return cells


def refuse_unless_checked(differ, worst, chain):
    """the exact gate's check line and the sum check, before any table is written"""
    if differ:
        raise SystemExit(f"predict_formulas: the truth card differs from the run on {differ} lanes: the exact gate fails")
    if worst > SUM_TOL:
        raise SystemExit(f"predict_formulas: the parts do not add up to {chain[-1]} - measured (off by {worst:.3g})")


def tables(srcs, out, w, wid, capture_of):
    """srcs: {family: its pass's dir}; capture_of(family): the sampled levels' predicted capture dir"""
    os.makedirs(out, exist_ok=True)
    g = gather(srcs, w, capture_of)
    chain, steps = g["chain"], g["steps"]
    per_step = pass_levels(g["levels"])   # the levels each KV step carries (the per-operator view)
    measures = {lv["name"]: lv["measures"] for lv in g["levels"]}
    scopes = [sc for sc in SCOPES if any(in_scope(f, sc) for f in srcs)]   # a read-only workload has no writes scope
    Ts = {sc: g["T"][sc] for sc in scopes}
    cells_of = {sc: complete_cells(Ts[sc], g["missing"][sc], wid) for sc in scopes}
    wt, worst = [], 0.0
    for sc in scopes:
        rows, w_sc = total_rows(Ts[sc], cells_of[sc], chain, measures, wid, sc)
        wt += rows
        worst = max(worst, w_sc)
    refuse_unless_checked(g["check"][0], worst, chain)
    T, cells = Ts["workload"], cells_of["workload"]
    write_csv(os.path.join(out, "workload_totals.csv"),
              ["workload", "scope", "layout", "level", "lane_group", "total", "measured", "error", "part",
               "part_measures"], wt)
    by_op = by_operator({k: v for k, v in steps.items() if k[0] in cells}, per_step)
    write_csv(os.path.join(out, "by_operator.csv"),
              ["op", "layout", "lane_group", "level", "steps", "steps_measured_above_0", "measured", "total", "error",
               "step_error", "ratio_of_sums", "steps_within_1pct"], by_op)
    write_csv(os.path.join(out, "points.csv"), ["layout", "op", "query", "param", "step", "level", *GROUPS],
              [[layouts.name_of(k[0]), d["op"], k[1], k[2], k[3], lv, *(round(d[lv][g], 6) for g in GROUPS)]
               for k, d in sorted(steps.items()) if k[0] in cells for lv in ("measured", *per_step)])
    ctx = {"srcs": srcs, "w": w, "wid": wid, "chain": chain, "per_step": per_step, "captures": g["captures"],
           "measures": measures, "cells": cells, "T": T, "Ts": Ts, "cells_of": cells_of,
           "by_op": by_op, "uncosted": g["uncosted"], "missing": g["missing"]["workload"], "worst": worst,
           "check": g["check"], "statistics": g["statistics"]}
    write_summary(out, ctx)
    print("predict-formulas: DONE")


def error_of(T, cell, lv, g):
    m = T[(cell, "measured", g)]
    return abs(T[(cell, lv, g)] - m) / m if m else None


def med_max(T, cells, lv, g):
    xs = [e for e in (error_of(T, c, lv, g) for c in cells) if e is not None]
    return (statistics.median(xs), max(xs)) if xs else (None, None)


def scope_groups(T, cells):
    """the lane groups a scope measures (a read-only scope writes no byte)"""
    return [g for g in GROUPS if any(T[(c, "measured", g)] for c in cells)]


def level_errors(T, cells, chain):
    """per level, each lane group's median / max error over the layouts, as text"""
    return "; ".join(f"{lv}: " + ", ".join(f"{g} {pct(med_max(T, cells, lv, g)[0])} / {pct(med_max(T, cells, lv, g)[1])}"
                                           for g in scope_groups(T, cells)) for lv in chain)


def summary_head(c):
    """summary.md's first line, its provenance line and the error table per scope"""
    chain, Ts, cells_of = c["chain"], c["Ts"], c["cells_of"]
    alone = [sc for sc in Ts if sc != "workload"]
    first = (f"{c['wid']}, the workload total's error (median / max over {len(cells_of['workload'])} layouts): "
             f"{level_errors(Ts['workload'], cells_of['workload'], chain)}"
             + "".join(f"; its {sc} items alone: {level_errors(Ts[sc], cells_of[sc], chain)}" for sc in alone
                       if sc == "read")
             + f"; sum check: the parts add to {chain[-1]} - measured within {c['worst']:.1g}; exact gate on these steps: "
             f"{c['check'][0]} of {c['check'][1]:,} lanes differ")
    print(first)
    freq = LC.frequencies(c["w"])
    share = {sc: sum(v for (f, _), v in freq.items() if in_scope(f, sc)) / sum(freq.values()) for sc in Ts}
    md = [first, "",
          f"> Made by "
          f"kvcost.predict_accuracy.predict_formulas from {', '.join(f'{s}/steps.csv' for s in c['srcs'].values())}.", "",
          f"The workload total T = the sum over {c['wid']}'s items ({', '.join(c['srcs'])}) of frequency x the mean over "
          f"its params of the query's KV steps; error = |T_level - T_measured| / T_measured; the scopes "
          + ", ".join(f"{sc} ({share[sc]:.1%} of the frequency)" for sc in Ts)
          + f" from the same sums. The statistics: tier {c['statistics']['tier']} ({c['statistics']['path']}, sha256 "
          f"{c['statistics']['sha256'][:12]}); the sampled levels: {', '.join(c['captures'])}. Unweighted KV operations "
          f"(S + G + N + P + D), bytes read (the 7 read lanes), bytes written (write_key + write_value), and the op-byte "
          f"cost ({MODEL}); a write's lanes under the {VIEW} chunk view (a last-chunk touch charged the chunk target x "
          f"{compose.LAST_CHUNK_FILL:g}, measured and predicted alike).", ""]
    for sc in Ts:
        md += scope_table(sc, Ts[sc], cells_of[sc], chain) + [""]
    return md[:-1]


def scope_table(sc, T, cells, chain):
    """one scope's error table: per level, each lane group's median and max over the layouts"""
    gs = scope_groups(T, cells)
    what = "workload total" if sc == "workload" else f"{sc} items' total"
    md = [f"**The {what}'s error, per level** (median and max over the {len(cells)} layouts):", "",
          "| level | " + " | ".join(f"{g}: median | max" for g in gs) + " |", "|---|" + "---|---|" * len(gs)]
    for lv in chain:
        md.append(f"| {lv} | " + " | ".join(f"{pct(a)} | {pct(b)}" for a, b in (med_max(T, cells, lv, g) for g in gs))
                  + " |")
    return md


def summary_spread_and_parts(c):
    """the spread over the layouts, and the parts per layout"""
    chain, T, cells, last = c["chain"], c["T"], c["cells"], c["chain"][-1]
    md = ["", f"**The spread over the layouts** (the workload total's {last} / measured, per lane group):", "",
          "| lane group | min | max | max / min |", "|---|---|---|---|"]
    for g in GROUPS:
        r = [T[(x, last, g)] / T[(x, "measured", g)] for x in cells if T[(x, "measured", g)]]
        if r:
            md.append(f"| {g} | {min(r):.3f} | {max(r):.3f} | {max(r) / min(r):.3f} |")
    md += ["", f"**Where the workload total's error comes from, per layout** (each level minus the level before it, "
           f"signed, over measured; the parts add to {last} - measured):", "",
           "| layout | lane group | " + " | ".join(f"{lv}: {c['measures'][lv]}" for lv in chain) + f" | sum | {last} - "
           "measured |", "|---|---|" + "---|" * (len(chain) + 2)]
    for x in cells:
        for g in GROUPS:
            m = T[(x, "measured", g)]
            if m:
                seq = [m, *(T[(x, lv, g)] for lv in chain)]
                parts = [(b - a) / m for a, b in zip(seq, seq[1:])]
                md.append(f"| {layouts.name_of(x)} | {g} | " + " | ".join(f"{100 * p:+.1f}%" for p in parts)
                          + f" | {100 * sum(parts):+.1f}% | {100 * (seq[-1] - m) / m:+.1f}% |")
    return md


def summary_operators(c):
    """the per-operator table (the levels each KV step carries), and what was left out"""
    chain = c["per_step"]
    md = ["", "**Per operator and layout** (over the steps: error = sum |level - measured| / sum measured; a row whose "
          "steps all measure 0 is not tested):", "",
          "| operator | layout | lane group | steps | measured above 0 | " + " | ".join(chain) + " |",
          "|---|---|---|---|---|" + "---|" * len(chain)]
    rows = defaultdict(dict)
    for r in c["by_op"]:
        rows[(r[0], r[1], r[2])][r[3]] = r
    for (op, lay, g), per in rows.items():
        r0 = per[chain[0]]
        body = " | ".join(pct(per[lv][8]) for lv in chain) if r0[5] else " | ".join(["not tested"] * len(chain))
        md.append(f"| {op} | {lay} | {g} | {r0[4]:,} | {r0[5]:,} | {body} |")
    md += ["", "Read steps not costed at every level (not in the tables): "
           + (", ".join(f"{op} {n}" for op, n in sorted(c["uncosted"].items())) or "none") + ". Layouts left out (a query "
           "of the workload without its steps, or with a step not costed at every level): " + (", ".join(sorted({layouts.name_of(x) for x, _ in c["missing"]}))
                                                     or "none") + "."]
    return md


def write_summary(out, c):
    """summary.md (module docstring)"""
    md = summary_head(c) + summary_spread_and_parts(c) + summary_operators(c)
    with open(os.path.join(out, "summary.md"), "w") as f:
        f.write("\n".join(md) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dataset", required=True, help="the run's scales, e.g. ldbc=3 (the workload's dataset is read)")
    ap.add_argument("--workload", required=True, help="the workload file (an id, as predict-49: i01, b01, j01)")
    ap.add_argument("--from", dest="source", choices=("output", "paper-results"), default="output")
    ap.add_argument("--tables", required=True, help="where the tables go")
    a = ap.parse_args(argv)
    run = sizes.resolve(a.dataset)
    ds = benchmarks.dataset_in_run(a.workload, run)
    if not ds:
        raise SystemExit(f"predict_formulas: {a.workload}'s dataset is not in --dataset {a.dataset}")
    sc = sizes.scale(run, ds)
    w = PL.one_workload(ds, sc, a.workload)
    srcs = {f: str(item_dir(a.source, ds, sc, a.workload, f)) for f in families(w)}
    for f, src in srcs.items():
        if not os.path.isfile(os.path.join(src, "steps.csv")):
            raise SystemExit(f"predict_formulas: no {f} run in {src}: run `just results predict-formulas --workload "
                             f"{a.workload} --dataset {ds}={sc}`")
    tree = Tree() if a.source == "output" else Tree(paper_results_dir())
    tables(srcs, a.tables, w, a.workload, lambda f: str(tree.predicted(f, sc, sampled_set()).path))


if __name__ == "__main__":
    main()
