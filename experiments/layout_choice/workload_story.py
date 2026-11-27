#!/usr/bin/env python3
"""The per-workload story: for each workload (python/kvcost/layout_cost/workloads/), price every option we compare and split each
query's cost by store part, so plot_workload_story.py can show what the queries touch, what each option
costs, and why. Analysis only (python/kvcost/layout_cost/compose.py's pricing over the grid captures); nothing is re-measured.

Options per workload (python/kvcost/layout_search/search.py solves each mixed one exactly):
  the 9 single layouts             (4 of them are existing formats' layouts: Dgraph, NebulaGraph, ...)
  per type, 6 questions            props per vertex type (vid group) + adjacency per edge type: fewest ops, fewest
                                   bytes, fewest ops within B x Sp-Co's bytes (settings.B_UNIT; B = 1, 2, 3, 5)
  per type and direction, the same 6 questions   adjacency also per direction (priced, not built)
Currencies (compose.currencies): ops = read ops + the writes' own reads + PUTs + DELs; bytes = bytes read + the
writes' own reads' bytes + bytes written.

Writes, per workload, into <out root>/<workload>/ (benchmarks.out_root: output/summary/<dataset>/<scale>/layout-choice/
<params>/<criterion>):
  options.csv      one row per option: its question, assignment and every measure (workload totals)
  breakdown.csv    per option x query x store part: the cost of ONE run (mean over the query's params)
  touch.csv        per query x store part WITH the list direction: reads / writes it, in at least one single layout
                   (--touch-only writes this file alone: seconds, no solve)
  README.md        the story: the queries, the prediction, what happened, the figures
plus <out root>/README.md (an index of the workloads) and runs/workload_story.json (the solve times;
kept apart so the CSVs stay byte-identical for the same inputs).

    python3 experiments/layout_choice/workload_story.py --sf 3 [--grid DIR] [--root TREE]
"""
import argparse
import csv
import glob
import time
import json
import multiprocessing
import os
import sys
from kvevaluator.paths import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.layout_cost.mixes import assignment, assignment_label, grid_settings, retired_grouped, unit_name  # (the assignment's text: one writer)
from kvcost.layout_cost.compose import (CHANNELS, MEASURES, add, load, params_of, cost_mix, query_cost,
                                      workload_units, zero)
from kvcost.layout_search.search import QUESTIONS, b_unit_layout, named_answers, named_questions, solve, uniform
from kvcost.layout_search import search as cost_cache
from kvcost.layout_cost.compose import CELLS, KNOWN, LAYOUTS, add_pick_by_arg
from kvcost.layout_cost.compose import Composer, currencies
from kvcost.layout_cost import compose
from kvcost.layout_cost import benchmarks
from kvevaluator.datasets.linkbench.params import check_param_set
from kvevaluator.output_tree import Tree
from kvcost.layout_cost.labels import axis_label, rule_label
import why_numbers
from kvcost.layout_cost.labels import display_title

BEAT = 0.99          # per type and direction "beats" per type when it is at least 1% cheaper (the same question)
SETS = (("per type", "per_type", False), ("per type and direction", "per_type_direction", True))


def layout_of(assign, u):
    """the layout of a (folded) unit: 'fixed' for the index; 'in=X, out=Y' when its directions differ"""
    if u is None:
        return "fixed"
    got = {k: l for k, l in assign.items() if k[:2] == u}
    if len(set(got.values())) == 1:
        return next(iter(got.values()))
    return ", ".join(f"{k[2]}={l}" for k, l in sorted(got.items()))


def uniform_of(cell, units):
    p, a = cell[len("props_"):].split("_adj_")
    return {u: (p if u[0] == "props" else a) for u in units}


def cell_label(cell):
    p, a = cell[len("props_"):].split("_adj_")
    if cell in KNOWN:
        return KNOWN[cell] if KNOWN[cell].startswith("All-") else f"{KNOWN[cell]} layout"
    return f"{p.capitalize()} props, {a.capitalize()} adjacency"


def breakdown(w, data, assign):
    """[(family, query, how often, unit, per-run vector)] of `assign` -- a direction unit is folded into its edge type,
    so every option's parts share one set of names"""
    rows = []
    for family, q, often, params in w["items"]:
        d = data[(family, q)]
        units = {u for per in d["cost"].values() for u in per if u is not None}
        _, per_unit = query_cost(d, params_of(d, params), assign, units, by_unit=True)
        folded = {}
        for u, vec in per_unit.items():
            key = u[:2] if u is not None else None
            folded.setdefault(key, zero(data.composer))
            add(folded[key], vec)
        rows += [(family, q, often, u, vec) for u, vec in folded.items()]
    return rows


def built_mixes(units):
    """{assignment label: [mix names]} for the built stores (mixes/*.json), as they apply to this workload's
    units (two built mixes that differ only in parts this workload never touches are the same option here)"""
    settings = grid_settings()
    out = {}
    for path in sorted(glob.glob(os.path.join(EXP, "inputs", "specs", "mixes", "*.json"))):
        name = os.path.basename(path)[:-5]
        spec = json.load(open(path))
        if retired_grouped(spec, name):
            print(f"[story] built mix {name}: grouped props, an option the search does not offer -- skipped")
            continue
        out.setdefault(assignment_label(assignment(spec, units, settings, name)), []).append(name)
    return out


def question_key(q):
    return q.replace("fewest ", "fewest_").replace("=", "")


def options_for(w, data, data_dir):
    """(units, [option dicts]): the 9 single layouts, then per option set its 6 exact solves (QUESTIONS).
    Also {option set: (cost cache seconds, search seconds)} for the run record."""
    units = workload_units(w, data)
    opts = []
    for cell in CELLS:
        a = uniform_of(cell, units)
        opts.append({"option": cell, "option_set": "single", "question": "", "label": cell_label(cell), "kind": "single",
                     "fits": "", "beats": "", "assign": a, "total": cost_mix(w, data, a), "nodes": "", "dir": False})
    ref_bytes = currencies(cost_mix(w, data, b_unit_layout(units)))["bytes"]   # B = 1: B_UNIT's bytes
    timing, per_type = {}, {}
    # the cost cache of both option sets first (the driver run's: cost_cache), then every named solve of the workload side
    # by side in a fork pool (each one unchanged: the same answer and nodes as one after another), then the options in
    # the serial order
    _SOLVE.clear()
    for k, (set_name, key, by_dir) in enumerate(SETS):
        dd = data_dir if by_dir else data
        t0 = time.time()
        pb = cost_cache.problem(w, dd, workload_units(w, dd))
        timing[set_name] = {"cost_cache_s": round(time.time() - t0, 2), "parts": len(pb.parts), "cost_cache_entries": pb.cost_cache_entries}
        _SOLVE[(k, False)] = (pb, named_questions(pb, ref_bytes))
    tasks = [(k, plain, i) for (k, plain), (_, qs) in _SOLVE.items() for i in range(len(qs))]
    jobs = min(cost_cache.jobs(), len(tasks))
    if jobs > 1:
        with multiprocessing.get_context("fork").Pool(jobs) as pool:    # forked after the cost cache exist: the workers share them
            done = pool.map(_solve_task, tasks, chunksize=1)
    else:
        done = [_solve_task(t) for t in tasks]
    results = dict(zip(tasks, done))
    answers = {kp: named_answers(qs, [results[(*kp, i)] for i in range(len(qs))]) for kp, (_, qs) in _SOLVE.items()}
    for k, (set_name, key, by_dir) in enumerate(SETS):
        dd = data_dir if by_dir else data
        timing[set_name]["solves_s"] = round(sum(results[t][4] for t in tasks if t[0] == k), 2)   # summed over the workers
        for question, assign, fits, nodes in answers[(k, False)]:
            total = cost_mix(w, dd, assign)
            c = currencies(total)
            primary = "bytes" if question == "fewest bytes" or not fits else "ops"   # nothing fits: the fewest-bytes mix
            beats = ""
            if by_dir:
                pc, pfits = per_type[question]
                if fits and not pfits:                # it fits a budget per type cannot
                    beats = "yes"
                else:
                    beats = "yes" if c[primary] < BEAT * pc[primary] else "no"
            else:
                per_type[question] = (c, fits)
            label = f"{set_name}: {question if question.startswith('fewest') else 'fewest ops within ' + question}"
            if not fits:
                label += " (nothing fits: the fewest bytes)"
            opts.append({"option": f"{key}_{question_key(question)}", "option_set": set_name, "question": question,
                         "label": label, "kind": set_name, "fits": ("yes" if fits else "no") if question.startswith("B") else "",
                         "beats": beats, "assign": assign, "total": total, "nodes": nodes, "dir": by_dir})
    return units, opts, timing


_SOLVE = {}      # what a forked worker reads: per (option set index, plain) its Problem and named_questions' list


def _solve_task(task):
    """one named solve: exact_search.solve on the option set's cost cache, as named_solves runs it"""
    k, plain, i = task
    pb, questions = _SOLVE[(k, plain)]
    _, primary, cap, start = questions[i]
    return solve(pb, primary, cap, start, plain=plain)


def param_spread(w, data):
    """README lines for the queries w["param_spread"] names (I01: the short reads): their share of the best uniform
    layout's total in each currency, the range of that share when each is priced at its cheapest / costliest param,
    and per query on how many params the best uniform layout on that param alone is the best on the mean too"""
    names = set(w.get("param_spread", ()))
    if not names:
        return []
    items = [it for it in w["items"] if it[1] in names]
    per = {}   # (query, cell) -> [(param, currencies)]
    for family, q, often, params in items:
        d = data[(family, q)]
        units = {u for c in d["cost"].values() for u in c if u is not None}
        for cell in CELLS:
            per[(q, cell)] = [(p, currencies(query_cost(d, [p], uniform_of(cell, units), units)))
                              for p in params_of(d, params)]
    totals = {cell: currencies(cost_mix(w, data, uniform_of(cell, workload_units(w, data)))) for cell in CELLS}
    L = []
    for cur in ("ops", "bytes"):
        best = min(CELLS, key=lambda k: totals[k][cur])
        mean = {it: sum(c[cur] for _, c in per[(it[1], best)]) / len(per[(it[1], best)]) for it in items}
        lo = sum(it[2] * min(c[cur] for _, c in per[(it[1], best)]) for it in items)
        hi = sum(it[2] * max(c[cur] for _, c in per[(it[1], best)]) for it in items)
        mid = sum(it[2] * mean[it] for it in items)
        rest = totals[best][cur] - mid
        agree = []
        for family, q, often, params in items:
            n = len(per[(q, best)])
            m = {k: sum(c[cur] for _, c in per[(q, k)]) / n for k in CELLS}
            top = min(m.values())
            mean_best = {k for k in CELLS if m[k] <= top * (1 + 1e-9)}
            hits = 0
            for i in range(n):
                v = {k: per[(q, k)][i][1][cur] for k in CELLS}
                low = min(v.values())
                hits += bool(mean_best & {k for k in CELLS if v[k] <= low * (1 + 1e-9)})
            agree.append(f"{q} {hits}/{n}")
        L.append(f"- in total {cur}, under the best uniform layout ({cell_label(best)}): {mid / totals[best][cur]:.1%} "
                 f"of the total; {lo / (rest + lo):.1%} to {hi / (rest + hi):.1%} if each is priced at its cheapest / "
                 f"costliest param. The best uniform layout on one param alone is also the best on the mean for: "
                 + ", ".join(agree) + ".")
    return ["**How much the params matter** (" + ", ".join(sorted(names, key=lambda q: (len(q), q))) + "; each "
            "priced as the mean over its params):"] + L + [""]


def fmt_num(v):
    return f"{v:,.0f}" if v >= 10 else f"{v:,.1f}"


def fmt_bytes(v):
    for unit, d in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if v >= d:
            return f"{v / d:,.1f} {unit}"
    return f"{v:,.0f} B"


def readme(sf, name, w, units, opts, built, spread=(), why=None, *, criterion):
    """the story in words; the numbers are the options.csv rows"""
    cur = {o["option"]: currencies(o["total"]) for o in opts}
    by = {o["option"]: o for o in opts}
    singles = [o["option"] for o in opts if o["kind"] == "single"]
    best1 = {c: min(singles, key=lambda k: (cur[k][c], cur[k]["bytes" if c == "ops" else "ops"])) for c in ("ops", "bytes")}
    L = [f"# {display_title(w)} ({axis_label(DATASET, sf, PARAM_SET)}; {rule_label(criterion)})", "",
         "> Generated by "
         "`experiments/layout_choice/workload_story.py`; the method is in `experiments/layout_choice/README.md`.", "",
         "## The workload", "",
         "| query | what it does | reads or writes | how often |", "|---|---|---|---|"]
    for family, q, often, params in w["items"]:
        sel = "" if not params else f" (only anchors {', '.join(str(p) for p in params)} of 5)"
        L.append(f"| {q} | {benchmarks.queries(DATASET).get(q, '?')}{sel} | {benchmarks.kind_of(family)}s | {often:,} |")
    fams = sorted({family for family, *_ in w["items"]})
    L += ["", "Each query's cost is the mean over its params: " + "; ".join(f"{f} queries, {benchmarks.params_text(DATASET)[f]}" for f in fams) + ".",
          ""]
    if w.get("method"):
        L += [f"**How the counts are built:** {w['method']}", ""]
    L += list(spread)
    L += ["## What we predicted (before measuring)", "", w["expect"], "", "## What happened", ""]
    for c, what in (("ops", "total ops"), ("bytes", "total bytes")):
        k = best1[c]
        ties = [by[u]["label"] for u in singles if u != k and cur[u][c] <= cur[k][c] * 1.01]
        also = f" -- within 1%: {'; '.join(ties)}" if ties else ""
        L.append(f"- Best single layout in {what}: **{by[k]['label']}** ({fmt_num(cur[k]['ops'])} ops, "
                 f"{fmt_bytes(cur[k]['bytes'])}){also}.")
    for c in ("ops", "bytes"):                       # what the per type mix buys
        L.append(f"- In total {c}: the per type mix is {cur[best1[c]][c] / cur[f'per_type_fewest_{c}'][c]:.2f}x cheaper "
                 f"than the best single layout.")
    seen = {}
    for o in opts:
        if o["kind"] != "per type":
            continue
        lab = assignment_label(o["assign"])
        if lab in seen:                                  # the same mix answers several questions: say so once
            seen[lab]["also"].append(o["question"])
            continue
        seen[lab] = {"o": o, "also": []}
    for lab, v in seen.items():
        o = v["o"]
        k = o["option"]
        c0 = "bytes" if o["question"] == "fewest bytes" else "ops"
        qs = ", ".join([o["question"]] + v["also"])
        same = next((u for u in singles if assignment_label(by[u]["assign"]) == lab), None)
        gains = [f"{cur[best1[c]][c] / cur[k][c]:.2f}x cheaper in {c} than the best single layout" if cur[k][c] < cur[best1[c]][c] / 1.01
                 else f"no real gain in {c} (< 1%)" for c in ("ops", "bytes")]
        fit = "" if o["fits"] != "no" else " Nothing fits the byte limit: this is the fewest-bytes mix."
        if same:
            L.append(f"- **per type, {qs}** is the single {by[same]['label']}.{fit}")
            continue
        names = built.get(lab, [])
        tag = (" Built and measured (" + ", ".join(f"`mixes/{n}.json`" for n in names) + "): measured == predicted.") if names else ""
        L.append(f"- **per type, {qs}** ({fmt_num(cur[k]['ops'])} ops, {fmt_bytes(cur[k]['bytes'])}; {', '.join(gains)}): {lab}.{fit}{tag}")
        ref = best1[c0]
        diffs = []
        for m, what, f in (("read_ops", "read ops", fmt_num), ("read_bytes", "bytes read", fmt_bytes),
                           ("write_rmw_bytes", "bytes the writes read", fmt_bytes),
                           ("write_bytes", "bytes written", fmt_bytes), ("write_all", "write ops", fmt_num)):
            val = (lambda t: t["write_ops"] + t["write_rmw_ops"]) if m == "write_all" else (lambda t, m=m: t[m])
            mine, theirs = val(o["total"]), val(by[ref]["total"])
            if max(mine, theirs) > 0 and abs(mine - theirs) > 0.01 * max(mine, theirs):
                diffs.append(f"{what} {f(mine)} against {f(theirs)}")
        L.append(f"  - against {by[ref]['label']}, measure by measure: " + ("; ".join(diffs) if diffs else "every measure within 1%") + ".")
    L += ["", "**Per type and direction** (adjacency also per direction; priced, not built), against per type, question by "
          "question (the question's currency; 1.00x = no gain):", "",
          "| question | per type | per type and direction | gain |", "|---|---|---|---|"]
    for q in QUESTIONS:
        pt, pd = by[f"per_type_{question_key(q)}"], by[f"per_type_direction_{question_key(q)}"]
        c = "bytes" if q == "fewest bytes" or pt["fits"] == "no" else "ops"
        show = lambda o: f"{fmt_num(cur[o['option']]['ops'])} ops, {fmt_bytes(cur[o['option']]['bytes'])}" + ("" if o["fits"] != "no" else " (nothing fits)")
        g = cur[pt["option"]][c] / cur[pd["option"]][c] if cur[pd["option"]][c] > 0 else 1.0
        gain = "fits where per type cannot" if pt["fits"] == "no" and pd["fits"] == "yes" else \
            f"{g:.2f}x{' (> 1%)' if pd['beats'] == 'yes' else ''}"
        L.append(f"| {q} | {show(pt)} | {show(pd)} | {gain} |")
    if why:
        L += ["", f"**Why:** {why}"]
    L += ["## Numbers (workload totals)", "",
          "| option | read ops | bytes read | write ops | bytes the writes read | bytes written | total ops | total bytes |",
          "|---|---|---|---|---|---|---|---|"]
    for o in opts:
        t, c = o["total"], cur[o["option"]]
        L.append(f"| {o['label']} | {fmt_num(t['read_ops'])} | {fmt_bytes(t['read_bytes'])} | "
                 f"{fmt_num(t['write_ops'] + t['write_rmw_ops'])} | {fmt_bytes(t['write_rmw_bytes'])} | "
                 f"{fmt_bytes(t['write_bytes'])} | {fmt_num(c['ops'])} | {fmt_bytes(c['bytes'])} |")
    L += ["", "All ops are weighted GET : SEEK : NEXT : PUT : DELETE = 1 : 1.1 : 0.01 : 1 : 1. Write ops = PUTs + DELs + "
          "the reads a write does first (read-modify-write). Total ops = read ops + write ops; total bytes = bytes read "
          "+ the bytes the writes read + bytes written. Counts are deterministic. Every mixed option is an exact optimum "
          "(branch and bound over the parts), not a search heuristic.", "",
          "## Figures (in reading order)", "",
          "1. `plots/options.png`: every option on total ops against total bytes (the options map); under it, which "
          "layout each option gives each part (the choice matrix) and which parts each query reads or writes.",
          "2. `plots/breakdown-parts.png`: why: each query's cost (one run) and the workload total under each "
          "option, split by store part.",
          "3. `plots/breakdown-ops-bytes.png`: the same split by KV op (GET / SEEK / NEXT / PUT / DELETE) and by "
          "byte lane (GET, SCAN, absence probes, read by the writes, written)."]
    if "sweep" in w:
        L.append("4. `plots/sweep.png`: how the cheapest layout changes as the writes grow.")
    hint = (f"experiments/layout_choice/run-layout-choice.sh --dataset linkbench --scale {sf} --params {check_param_set(PARAM_SET)}"
            if DATASET == "linkbench" else f"experiments/layout_choice/run-layout-choice.sh --sf {sf}")
    L += ["", "## Reproduce", "", "```", hint, "```", ""]
    return "\n".join(L)


def touch_rows(w, data_dir):
    """[{family, query, how_often, part, kind}]: the parts a query reads or writes, a list part per DIRECTION (the
    grid records it; breakdown.csv folds it away). kind = 'reads' if any of its KV ops are reads there in at least
    one single layout, else 'writes' (plot_workload_story's rule)."""
    out = []
    for family, q, often, params in w["items"]:
        d = data_dir[(family, q)]
        units = {u for per in d["cost"].values() for u in per}
        pp = params_of(d, params)
        kind = {}
        for cell in CELLS:
            c = query_cost(d, pp, uniform(units - {None}, *cell[len("props_"):].split("_adj_")), units - {None}, by_unit=True)
            if c is None:
                continue
            for u, vec in c[1].items():
                if vec["read_ops"] + vec["read_bytes"] > 0:
                    kind[u] = "reads"
                elif vec["write_bytes"] + vec["write_ops"] > 0:
                    kind.setdefault(u, "writes")
        order = lambda u: (u is None, u is not None and u[0] != "props", unit_name(u))
        out += [{"family": family, "query": q, "how_often": often, "part": unit_name(u), "kind": kind[u]}
                for u in sorted(kind, key=order)]
    return out


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)


DATASET = "ldbc"     # the suite main() is writing (benchmarks.py); read by readme()
PARAM_SET = None   # its param set (--param-set; LinkBench's names its folder and its title)


def main():
    global DATASET, PARAM_SET
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True, help="the suite's axis value: LDBC's scale factor, LinkBench's scale tag")
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    ap.add_argument("--grid", default=None, help="the grid: a captures/<dataset>/<scale> dir (default: the output tree's)")
    ap.add_argument("--root", default=None, help="the output tree to write into (default: the output dir)")
    benchmarks.add_workload_arg(ap)
    add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    ap.add_argument("--touch-only", action="store_true", help="write each workload's touch.csv and nothing else (no solve)")
    cost_cache.add_cache_args(ap)      # --fresh: each query's costs are kept and reused across runs
    a = ap.parse_args()
    cost_cache.apply_cache_args(a)
    composer = Composer.from_args(a)
    DATASET, PARAM_SET = a.dataset, a.param_set
    root = a.grid or str(Tree().place_dir("captures", a.dataset, a.sf))
    out = benchmarks.out_root(a.dataset, a.sf, a.criterion, a.param_set, root=a.root)
    if a.touch_only:
        data_dir = load(root, True, a.dataset, composer=composer)
        for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items():
            d = benchmarks.workload_dir(a.dataset, a.sf, name, w, out, criterion=a.criterion, param_set=a.param_set)
            os.makedirs(d, exist_ok=True)
            write_csv(os.path.join(d, "touch.csv"), touch_rows(w, data_dir))
            print(f"wrote {os.path.join(d, 'touch.csv')}")
        return
    data, data_dir = load(root, False, a.dataset, composer=composer), load(root, True, a.dataset, composer=composer)
    index = [f"# Layout choice, {axis_label(a.dataset, a.sf, a.param_set)}, {rule_label(a.criterion)}: one folder per workload", "",
             "> Generated by "
             "`experiments/layout_choice/workload_story.py`.", "",
             "| workload | best single (ops) | best single (bytes) | per type vs best single (ops / bytes) | "
             "per type and direction vs per type (ops / bytes) | per type (fewest ops) "
             "against the best single in ops, measure by measure |",
             "|---|---|---|---|---|---|"]
    runs = {}
    for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items():
        units, opts, timing = options_for(w, data, data_dir)
        runs[w["id"]] = timing
        built = built_mixes(units) if a.dataset == "ldbc" else {}     # the built mixes are LDBC-store specs
        d = benchmarks.workload_dir(a.dataset, a.sf, name, w, out, criterion=a.criterion, param_set=a.param_set)
        os.makedirs(os.path.join(d, "plots"), exist_ok=True)
        for old in ("assignments.csv", "plots/tradeoff.png", "plots/tradeoff.pdf", "plots/grid.png", "plots/grid.pdf"):
            if os.path.exists(os.path.join(d, old)):             # retired outputs: the descent cloud, the old figures
                os.remove(os.path.join(d, old))
        write_csv(os.path.join(d, "options.csv"),
                  [{"option": o["option"], "option_set": o["option_set"], "question": o["question"], "label": o["label"],
                    "kind": o["kind"], "fits": o["fits"], "beats_per_type_by_1pct": o["beats"],
                    "assignment": assignment_label(o["assign"]),
                    "built": " ".join(built.get(assignment_label(o["assign"]), [])) if o["kind"] != "single" else "",
                    **{m: round(o["total"][m], 3) for m in MEASURES + CHANNELS},
                    **{f"total_{c}": round(v, 3) for c, v in currencies(o["total"]).items()},
                    "nodes": o["nodes"]} for o in opts])
        rows = []
        for o in opts:
            for family, q, often, u, vec in breakdown(w, data_dir if o["dir"] else data, o["assign"]):
                rows.append({"option": o["option"], "family": family, "query": q, "how_often": often, "part": unit_name(u),
                             "layout": layout_of(o["assign"], u),
                             **{m: round(vec[m], 4) for m in MEASURES + CHANNELS}})
        write_csv(os.path.join(d, "breakdown.csv"), rows)
        write_csv(os.path.join(d, "touch.csv"), touch_rows(w, data_dir))
        with open(os.path.join(d, "README.md"), "w") as f:
            # the why text's numbers, from the CSVs just written (the same path that corrects a snapshot's README)
            why = why_numbers.render(w, why_numbers.Data.from_dir(d)) if w.get("why") else None
            f.write(readme(a.sf, name, w, units, opts, built, spread=param_spread(w, data), why=why, criterion=a.criterion))
        cur = {o["option"]: currencies(o["total"]) for o in opts}
        tot = {o["option"]: o["total"] for o in opts}
        lab = {o["option"]: o["label"] for o in opts}
        uni = [o["option"] for o in opts if o["kind"] == "single"]
        bu = {c: min(uni, key=lambda k: cur[k][c]) for c in ("ops", "bytes")}
        pt = {"ops": "per_type_fewest_ops", "bytes": "per_type_fewest_bytes"}
        pd = {"ops": "per_type_direction_fewest_ops", "bytes": "per_type_direction_fewest_bytes"}
        gain = " / ".join(f"{cur[bu[c]][c] / cur[pt[c]][c]:.2f}x" for c in ("ops", "bytes"))
        dgain = " / ".join(f"{cur[pt[c]][c] / cur[pd[c]][c]:.2f}x" for c in ("ops", "bytes"))
        for c in ("ops", "bytes"):                    # name every single layout within 1% of the best
            ties = [u for u in uni if u != bu[c] and cur[u][c] <= cur[bu[c]][c] * 1.01]
            lab[f"{bu[c]}+{c}"] = lab[bu[c]] + (" (within 1%: " + "; ".join(lab[u] for u in ties) + ")" if ties else "")
        moves = []
        for m, what in (("read_ops", "read ops"), ("read_bytes", "bytes read"), ("write_rmw_bytes", "bytes the writes read"),
                        ("write_bytes", "bytes written"), ("write_all", "write ops")):
            val = (lambda t: t["write_ops"] + t["write_rmw_ops"]) if m == "write_all" else (lambda t, m=m: t[m])
            mine, theirs = val(tot[pt["ops"]]), val(tot[bu["ops"]])
            if mine > 0 and theirs > 0 and abs(theirs / mine - 1) > 0.01:
                r = max(theirs, mine) / min(theirs, mine)
                moves.append((f"{what} {r:,.0f}x " if r >= 100 else f"{what} {r:.2g}x ") + ("lower" if theirs > mine else "higher"))
        index.append(f"| [{display_title(w)}]({os.path.basename(d)}/README.md) | {lab[bu['ops'] + '+ops']} | {lab[bu['bytes'] + '+bytes']} | "
                     f"{gain} | {dgain} | {', '.join(moves) or 'the same'} |")
        print(f"{name}: {len(opts)} options, " + "; ".join(f"{k} {v['parts']} parts: cost cache {v['cost_cache_s']}s, solves {v['solves_s']}s"
                                                            for k, v in timing.items()) + f" -> {d}", flush=True)
    index += ["", "Per type vs best single: the best single layout's total divided by the per type mix's "
              "(fewest ops / fewest bytes), in each currency: what mixing buys. "
              "Per type and direction vs per type likewise (1.00x = no gain). Every mix is an exact optimum.", ""]
    if a.workload:
        print("--workload: the index README and the run record not written (they cover every workload)")
    else:
        with open(os.path.join(out, "README.md"), "w") as f:
            f.write("\n".join(index))
        os.makedirs(os.path.join(out, "runs"), exist_ok=True)       # timings: not deterministic, so not in the CSVs
        with open(os.path.join(out, "runs", "workload_story.json"), "w") as f:
            json.dump({"sf": a.sf, "benchmark": a.dataset, "per_workload": runs},
                      f, indent=1, sort_keys=True)
            f.write("\n")
        for old in ("mix_front-etype.csv", "mix_front-direction.csv"):   # retired (a front needs every point)
            if os.path.exists(os.path.join(out, old)):
                os.remove(os.path.join(out, old))


if __name__ == "__main__":
    main()
