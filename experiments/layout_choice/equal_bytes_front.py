#!/usr/bin/env python3
"""The equal-bytes front: at each total-bytes limit, the fewest total ops of the nine uniform layouts and of a mix
(per type; per type and direction), every mix an exact solve.
Analysis only. The lines on the grid's three settings are `kvcost search-mix --byte-limit file:<the limits>` rows
(one staircase row per option set, written under <root>/rows; the limits in <workload dir>/limits.json). One case still
solves here (search.solve), until its driver retires: a grid without the microbench (--no-microbench: the chunk-size
driver's composed grids), which kvcost does not load. The limits are budget_sweep.py's (the rule, caps_of), taken for both
unit sets; every limit is printed, the 1.00x ones too. Each BUILT mix is a point beside the curve: its measured
totals, the totals composed for its assignment, and the front solved at its own bytes.

    total ops   = read weighted ops (G + 1.1 S + 0.01 N) + the writes' own reads (same weights) + PUTs + DELs
    total bytes = bytes read + the bytes the writes read first + bytes written
    python3 experiments/layout_choice/equal_bytes_front.py --sf 3 --grid output/captures/ldbc/sf3 \\
        --root output/summary/ldbc/sf3/mix-benefit/equal-bytes-front --workload i01 --built inputs/specs/mixes_w16
        -> <root>/<workload dir>/equal_bytes_front.csv, uniform_layouts.csv (the nine points), built_points.csv, README.md
    --no-mix: the uniform layouts only (no search); --dataset linkbench --in-sample; figure: plot_equal_bytes_front.py
"""
import argparse
import csv
import glob
import json
import multiprocessing
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
from budget_sweep import B_UNIT, NAMED_BUDGETS, caps_of
from kvcost.layout_search.questions import FRONT_WOPS_BYTES
from kvcost.layout_search import results as R
from kvcost.layout_search.results import ResultFile  # (one reader: refuses a file mixing frequencies)
from kvcost.layout_cost.compose import (CHANNELS, MEASURES, Composer, currencies, load, cost_mix,
                                      workload_units)
from kvcost.layout_search.search import solve, uniform
from kvcost.layout_cost.compose import CELLS, add_pick_by_arg, load_reads, load_writes, workload_cost
from kvcost.layout_cost.mixes import assignment_label
from kvcost.layout_cost import mixes
from kvcost.layout_cost import benchmarks
from kvcost.layout_search import search as cost_cache
from kvcost.layout_cost.settings import name_of
from kvcost.layout_search.front import B_KIND, BUILT_FIELDS, built_points, spec_and_capture  # (a built mix's point)
from kvevaluator.output_tree import Tree, layout_capture, own_tree, same_place, tree_of
from kvevaluator.paths import recorded_path
from kvevaluator import workloads

RAW = list(MEASURES) + list(CHANNELS)
FIELDS = (["workload", "cap_bytes", "cap_kind", "cap_x_reference", "option_set", "option", "total_ops", "total_bytes"] + RAW +
          ["best_single_within_cap", "best_single_ops", "ops_single_over_mix", "nodes", "assignment"])
UNIFORM_FIELDS = ["workload", "cell", "total_ops", "total_bytes", "bytes_x_reference"] + RAW


def raw_of(t):
    return {k: round(t[k], 3) for k in RAW}


_W = {}      # what a forked worker reads: the workload, the grid, the unit sets (set before the pool starts)


def _solve_one(task):
    """one (limit, option set) solve and its re-pricing: exactly the serial loop's body"""
    i, cap, by_dir, plain, start = task
    t0 = time.time()
    data, pb, _, _ = _W["sets"][by_dir]
    assign, ops, byt, n, _ = solve(pb, "ops", cap, start, plain=plain)
    if assign is None:
        return i, by_dir, plain, None, time.time() - t0
    t = cost_mix(_W["w"], data, assign)
    cur = currencies(t)
    assert abs(cur["ops"] - ops) <= 1e-6 * max(1.0, ops) and abs(cur["bytes"] - byt) <= 1e-6 * max(1.0, byt), (by_dir, plain, cap)
    return i, by_dir, plain, (ops, byt, n, raw_of(t), assignment_label(assign)), time.time() - t0


def staircase_rows(w, grid, caps, out, rows_dir, dataset, sf, criterion):
    """{(cap index, by_dir): (ops, bytes, nodes, assignment)} of the plain lines: `kvcost search-mix --byte-limit
    file:<out>/limits.json` per option set on the grid's tree (one proven staircase row each, in the output tree
    rows_dir), read back by cap"""
    order = sorted(caps)
    os.makedirs(out, exist_ok=True)
    path = os.path.abspath(os.path.join(out, "limits.json"))
    with open(path, "w") as f:
        json.dump({"limits": [{"cap_bytes": c, "cap_kind": caps[c]} for c in order]}, f, indent=1)
    found = {}
    for by_dir, oset in ((False, "per-type"), (True, "per-type-and-direction")):
        cmd = [sys.executable, "-m", "kvcost.cli.cli", "search-mix", "--dataset", f"{dataset}={sf}", "--from",
               str(tree_of(grid)), "--workload", w["id"], "--settings", "Sp,Co,Ch64k", *(["--by-direction"] if by_dir else []),
               "--byte-limit", f"file:{path}"]
        cmd += ["--pick-by", criterion] if criterion else []
        print("   " + " ".join(cmd), flush=True)
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, env=own_tree(rows_dir))
        res = R.home(str(Tree(rows_dir).summary(dataset, sf)), w["id"], oset)
        rows = [r for r in ResultFile(res).read() if r["question"] == "staircase" and r["model"] == FRONT_WOPS_BYTES and r["limit"] == f"file:{path}"
                and same_place(r["capture"], grid) and r["weighting"] == "sum"]
        if len(rows) != 1 or not rows[0]["proven"]:
            raise SystemExit(f"{res}: {len(rows)} proven staircase rows at file:{path} on {grid} (want 1)")
        steps = {x["cap_bytes"]: x for x in rows[0]["staircase"]}
        for i, cap in enumerate(order):
            x = steps[cap]
            if x["wops"] is not None:
                found[(i, by_dir)] = (x["wops"], x["bytes"], x["nodes"],
                                      R.assignment_of(x["assignment"]))
    return found


def front(name, w, grid, composer, jobs=1, dataset="ldbc", rows_dir=None, out=None,
          sf=None, criterion=None):
    """(rows, per unit set: (data, pb, units, singles), the reference bytes). The plain lines come from kvcost search-mix --front's
    rows when rows_dir is given (staircase_rows); else they are solved here, independent: with jobs > 1 in a fork pool,
    each one unchanged, and the rows are written in the serial order (same rows, same nodes)."""
    ctx = multiprocessing.get_context("fork")
    _W.update(w=w)
    sets, caps, t0 = {}, {}, time.time()
    loaded = []
    for by_dir in (False, True):
        data = load(grid, by_dir, dataset, composer=composer)
        loaded.append((w, data, workload_units(w, data)))
    t1 = time.time()
    pbs = cost_cache.problems(loaded, jobs)       # both unit sets in one pool
    t2 = time.time()
    found = []
    for by_dir, (_, data, units), pb in zip((False, True), loaded, pbs):
        singles, c, _ = caps_of(pb, units, True)
        sets[by_dir] = (data, pb, units, singles)
        found.append(c)
        print(f"   {name}: setup, {'per type and direction' if by_dir else 'per type'}: {pb.cost_cache_entries:,} cost cache entries, "
              f"{len(pb.queries)} queries", flush=True)
    print(f"   {name}: setup: load {t1 - t0:.0f} s, cost cache {t2 - t1:.0f} s ({jobs} slices at a time), limits {time.time() - t2:.0f} s", flush=True)
    for c in found:
        for cap, kind in sorted(c.items()):
            if not any(abs(cap - have) <= 1e-9 * have for have in caps):   # per type's first; a limit both give is one row
                caps[cap] = kind
    print(f"   {name}: setup {time.time() - t0:.0f} s wall", flush=True)
    ref = sets[False][3][B_UNIT][2]
    _W["sets"] = sets
    order, bests, tasks = sorted(caps), [], []
    for i, cap in enumerate(order):
        singles = sets[False][3]
        fits = [c for c in CELLS if singles[c][2] <= cap * (1 + 1e-9)]
        best = min(fits, key=lambda c: (singles[c][1], singles[c][2])) if fits else None
        bests.append(best)
        for by_dir in (False, True):
            if rows_dir:                           # a kvcost search-mix --front row (staircase_rows), not solved here
                continue
            singles_d = sets[by_dir][3]
            tasks.append((i, cap, by_dir, True, singles_d[best][0] if best else None))
    t1 = time.time()
    if jobs > 1:
        with ctx.Pool(jobs) as pool:               # forked after the cost cache exist: the workers share them
            done = pool.map(_solve_one, tasks, chunksize=1)
    else:
        done = [_solve_one(t) for t in tasks]
    solved = {(i, by_dir, plain): res for i, by_dir, plain, res, _ in done}
    if rows_dir:                                   # the plain lines from the rows, re-priced here as a solve's are
        t2 = time.time()
        for (i, by_dir), (ops, byt, n, assign) in staircase_rows(w, grid, caps, out, rows_dir, dataset, sf,
                                                                 criterion).items():
            data = sets[by_dir][0]
            t = cost_mix(w, data, assign)
            cur = currencies(t)
            assert abs(cur["ops"] - ops) <= 1e-6 * max(1.0, ops) and abs(cur["bytes"] - byt) <= 1e-6 * max(1.0, byt), (by_dir, i)
            solved[(i, by_dir, True)] = (ops, byt, n, raw_of(t), assignment_label(assign))
        print(f"   {name}: the plain lines from kvcost search-mix --front rows under {rows_dir} in {time.time() - t2:.0f} s", flush=True)
    per_set = {}
    for i, by_dir, plain, res, secs in done:
        k = "per type and direction" if by_dir else "per type"
        a = per_set.setdefault(k, [0, 0.0, 0.0, 0])
        a[0] += 1; a[1] += secs; a[2] = max(a[2], secs); a[3] += res[2] if res else 0
    for k, (cnt, tot, mx, nodes) in per_set.items():
        print(f"   {name}: {k}: {cnt} solves, {nodes:,} nodes, {tot:.0f} s summed over the workers, longest {mx:.0f} s", flush=True)
    print(f"   {name}: {len(tasks)} solves over {len(order)} limits in {time.time() - t1:.0f} s wall ({jobs} at a time)", flush=True)
    rows = []
    for i, cap in enumerate(order):
        best = bests[i]
        base = {"workload": name, "cap_bytes": cap, "cap_kind": caps[cap], "cap_x_reference": round(cap / ref, 4)}
        data, pb, units, singles = sets[False]
        if best:
            assign = pb.to_parts(singles[best][0])
            rows.append({**base, "option_set": "single", "option": best, "total_ops": singles[best][1], "total_bytes": singles[best][2],
                         **raw_of(cost_mix(w, data, assign)), "best_single_within_cap": best, "best_single_ops": singles[best][1],
                         "ops_single_over_mix": 1.0, "nodes": "", "assignment": assignment_label(assign)})
        for by_dir in (False, True):
            singles = sets[by_dir][3]              # this unit set's own totals of the nine (its float sums), as before
            res = solved.get((i, by_dir, True))
            if res is None:                        # no mix fits
                continue
            ops, byt, n, raw, label = res
            oset = "per type and direction" if by_dir else "per type"
            rows.append({**base, "option_set": oset, "option": "fewest_ops_within_cap", "total_ops": ops, "total_bytes": byt,
                         **raw, "best_single_within_cap": best or "", "best_single_ops": singles[best][1] if best else "",
                         "ops_single_over_mix": round(singles[best][1] / ops, 4) if best else "", "nodes": n,
                         "assignment": label})
    return rows, sets, ref


def uniform_only(name, w, grid, composer, dataset="ldbc"):
    """--no-mix: (rows, the nine uniform layouts' (ops, bytes, totals), the reference bytes). No search and no cost cache:
    nine cost_mix calls. The limits are each layout's own bytes and the registered multiples of the reference's."""
    data = load(grid, False, dataset, composer=composer)
    units = workload_units(w, data)
    nine = {}
    for cell in CELLS:
        assign = uniform(units, *cell[len("props_"):].split("_adj_"))
        t = cost_mix(w, data, assign)
        cur = currencies(t)
        nine[cell] = (cur["ops"], cur["bytes"], t, assign)
    ref = nine[B_UNIT][1]
    caps = {nine[c][1]: "single" for c in CELLS}
    for b in NAMED_BUDGETS:
        if not any(abs(b * ref - have) <= 1e-9 * have for have in caps):
            caps[b * ref] = f"{B_KIND}{b:g}"
    rows = []
    for cap in sorted(caps):
        fits = [c for c in CELLS if nine[c][1] <= cap * (1 + 1e-9)]
        if not fits:
            continue
        best = min(fits, key=lambda c: (nine[c][0], nine[c][1]))
        rows.append({"workload": name, "cap_bytes": cap, "cap_kind": caps[cap], "cap_x_reference": round(cap / ref, 4), "option_set": "single",
                     "option": best, "total_ops": nine[best][0], "total_bytes": nine[best][1], **raw_of(nine[best][2]),
                     "best_single_within_cap": best, "best_single_ops": nine[best][0], "ops_single_over_mix": 1.0, "nodes": "",
                     "assignment": assignment_label(nine[best][3])})
    return rows, {c: (nine[c][0], nine[c][1], nine[c][2]) for c in CELLS}, ref


def uniform_layouts(name, w, sets, ref):
    """the nine uniform layouts of a solved front: their totals as the per type problem holds them"""
    data, pb, _, singles = sets[False]
    return {c: (singles[c][1], singles[c][2], cost_mix(w, data, pb.to_parts(singles[c][0]))) for c in CELLS}


def capture_binaries(grid, dataset):
    """the (binary sha, dirty) pairs of the nine cells' records: each cell's measured capture, else its composed one
    (output_tree.layout_capture; a composed record names the binary of the captures it was composed from)"""
    out = set()
    for cell in CELLS:
        b = json.load(open(layout_capture(grid, workloads.names(kind="read", dataset=dataset)[0], cell).record(cell)))
        out.add((b["binary"]["binary_git_sha"][:9], b["binary"]["binary_dirty"]))
    return sorted(out)


def readme(name, w, grid, dataset, rows, built, ref, criterion, after=(), notes=(), left=()):
    sets = [s for s in ("per type", "per type and direction")
            if any(r["option_set"] == s for r in rows)]
    caps = sorted({r["cap_bytes"] for r in rows})
    at = {(r["cap_bytes"], r["option_set"]): r for r in rows}
    L = [f"# The equal-bytes front: {w['id'].upper()} ({name}), SF{{sf}}", "",
         "> Generated by `experiments/layout_choice/equal_bytes_front.py`; working output, not a snapshot.", "",
         f"Capture: `{recorded_path(grid, Tree().root)}` (the nine cells' binaries: {', '.join(f'{s} dirty={d}' for s, d in capture_binaries(grid, dataset))}). Plans: the pick record's ({criterion or 'wops_bytes4k'}).",
         "**total ops** = read weighted ops (G + 1.1 S + 0.01 N) + the writes' own reads (same weights) + PUTs + DELs. "
         "**total bytes** = bytes read + the bytes the writes read first + bytes written. A query's cost = its mean over the reported params x how often it runs.",
         f"**Limit:** total bytes <= the limit; B = the limit / {name_of(B_UNIT)}'s total bytes ({ref / 1e9:,.2f} GB; B's unit, settings.B_UNIT). The limits are `budget_sweep.caps_of`'s, every one printed.",
         "**x** = the ops of the best uniform layout that fits / the ops of the exact fewest-ops mix that fits (1.00x = no gain; '-' = no uniform layout fits).",
         f"Covered: {len(caps)} limits, {len(rows)} rows, option sets: {', '.join(sets)}; {len(built)} built mixes.", "",
         "| B | limit (GB) | limit from | best uniform that fits | its ops | " + " | ".join(f"{s}: ops (x)" for s in sets) + " |",
         "|---|---|---|---|---|" + "---|" * len(sets)]
    for cap in caps:
        s = at.get((cap, "single"))
        cells = []
        for o in sets:
            r = at.get((cap, o))
            cells.append("no mix fits" if r is None else f"{r['total_ops']:,.0f} ({r['ops_single_over_mix']:.2f}x)" if s else f"{r['total_ops']:,.0f} (-)")
        kind = next(r["cap_kind"] for r in rows if r["cap_bytes"] == cap)
        L.append(f"| {cap / ref:.3f} | {cap / 1e9:,.2f} | {kind} | " + (f"{s['option']} | {s['total_ops']:,.0f}" if s else "none fits | ")
                 + " | " + " | ".join(cells) + " |")
    if built:
        L += ["", "## The built mixes: measured beside the curve", "",
              "measured = the built store's own counts; composed = the same assignment priced from the nine cells' parts; front = the exact fewest-ops mix (same unit set, the grid's three settings) within the mix's own bytes.", "",
              "| mix | unit set | B (its bytes) | measured ops | composed ops | front ops at its bytes | measured / front | best uniform within its bytes | uniform / measured | binary (dirty) |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for b in built:
            L.append(f"| {b['mix']} | {b['option_set']} | {b['bytes_x_reference']:.3f} | {b['measured_ops']:,.0f} | {b['composed_ops']:,.0f} | {b['front_ops_at_its_bytes']:,.0f} | "
                     f"{b['measured_over_front_ops']:.3f} | {b['best_single_within_its_bytes'] or 'none fits'} | {b['ops_single_over_measured'] if b['ops_single_over_measured'] == '' else format(b['ops_single_over_measured'], '.2f') + 'x'} | {b['binary_git_sha'][:9]} ({b['binary_dirty']}) |")
    if after:
        L += ["", "## Built after the curve was seen, to check the stretch between the named limits", "",
              "| mix | unit set | B (its bytes) | measured ops | composed ops | front ops at its bytes | measured / front | best uniform within its bytes | uniform / measured | binary (dirty) |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for b in after:
            L.append(f"| {b['mix']} | {b['option_set']} | {b['bytes_x_reference']:.3f} | {b['measured_ops']:,.0f} | {b['composed_ops']:,.0f} | {b['front_ops_at_its_bytes']:,.0f} | "
                     f"{b['measured_over_front_ops']:.3f} | {b['best_single_within_its_bytes']} | {b['ops_single_over_measured']:.2f}x | {b['binary_git_sha'][:9]} ({b['binary_dirty']}) |")
        for text in notes:                                        # a spec dir's README.md: what no file here can compute (the store, its run)
            L += ["", text.strip()]
    if left:
        L += ["", "## Built mixes left out", "",
              "Each lacks a capture of a part this workload prices, so it has no point here (it is not priced from the cells instead).", "",
              "| mix | missing capture | capture dir |", "|---|---|---|"]
        L += [f"| {x['mix']} | {x['missing']} | `{x['capture']}` |" for x in left]
    return "\n".join(L) + "\n"


def write_csv(path, fields, rows):
    with open(path, "w", newline="") as fh:                         # no wall time: a re-run gives the same bytes
        wr = csv.DictWriter(fh, fieldnames=fields)
        wr.writeheader()
        wr.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True, help="the suite's axis value: LDBC's scale factor, LinkBench's scale tag")
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    ap.add_argument("--grid", default=None, help="the grid: a captures/<dataset>/<scale> dir (default: the output tree's)")
    ap.add_argument("--root", default=None, help="the dir to write into, one folder per workload (default: <the analysis root>/equal-bytes-front)")
    ap.add_argument("--built", action="append", default=[], help="a built mixes' spec dir, SPEC_DIR[=CAPTURE_DIR] (its capture by default: "
                    "<grid>/<its basename>); repeatable")
    ap.add_argument("--built-after", action="append", default=[], help="the same for mixes built AFTER the curve was seen: their own file "
                    "(built_points_after_curve.csv) and README section; the spec dir's README.md, if any, is printed under it")
    benchmarks.add_workload_arg(ap)
    add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    ap.add_argument("--no-mix", action="store_true", help="the uniform layouts only: the nine points and the best one that fits per "
                    "limit; no search (seconds)")
    ap.add_argument("--in-sample", action="store_true", help="a grid with no held-out set (LinkBench): the picks are the capture's own")
    ap.add_argument("--no-microbench", action="store_true", help="a grid with the LDBC reads and writes only (a composed grid of another "
                    "chunk size): for workloads that use no microbench query (I01, I02)")
    ap.add_argument("--jobs", type=int, default=min(32, os.cpu_count() or 1), help="cost cache slices and solves at a time (each solve is "
                    "unchanged; 1 = the serial loop). A worker peaks at about 0.4 GB at SF3")
    cost_cache.add_cache_args(ap)      # --fresh: each query's costs are kept and reused across runs
    a = ap.parse_args()
    for x in a.built + a.built_after:     # a moved or mistyped spec dir must fail here, not price as "0 built mixes"
        spec_dir = x.partition("=")[0]
        if not glob.glob(os.path.join(spec_dir, "*.json")):
            raise SystemExit(f"--built {spec_dir}: no spec files there (a missing or empty spec dir)")
    cost_cache.apply_cache_args(a)
    composer = Composer.from_args(a, families=benchmarks.families("ldbc", drop=("microbench",)) if a.no_microbench else None)
    grid = a.grid or str(Tree().place_dir("captures", a.dataset, a.sf))
    root = a.root or os.path.join(benchmarks.out_root(a.dataset, a.sf, a.criterion, a.param_set), "equal-bytes-front")
    for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items():
        t0 = time.time()
        built, after, notes, left = [], [], [], []
        if a.no_mix:
            rows, nine, ref = uniform_only(name, w, grid, composer, a.dataset)
            tb = tw = time.time()
        else:
            out_w = benchmarks.workload_dir(a.dataset, a.sf, name, w, root, criterion=a.criterion, param_set=a.param_set)
            rows, sets, ref = front(name, w, grid, composer, a.jobs, a.dataset,
                                    rows_dir=None if a.no_microbench else os.path.join(root, "rows"), out=out_w,
                                    sf=a.sf, criterion=a.criterion)
            nine = uniform_layouts(name, w, sets, ref)
            tb = time.time()
            built, left = built_points(name, w, grid, sets, ref, a.built, a.criterion)
            after, left_after = built_points(name, w, grid, sets, ref, a.built_after, a.criterion)
            left += left_after
            notes = [open(n).read() for n in (os.path.join(spec_and_capture(x, grid)[0], "README.md") for x in a.built_after) if os.path.exists(n)]
            tw = time.time()
        out = benchmarks.workload_dir(a.dataset, a.sf, name, w, root, criterion=a.criterion, param_set=a.param_set)
        os.makedirs(out, exist_ok=True)
        write_csv(os.path.join(out, "equal_bytes_front.csv"), FIELDS, rows)
        write_csv(os.path.join(out, "uniform_layouts.csv"), UNIFORM_FIELDS,
                  [{"workload": name, "cell": c, "total_ops": nine[c][0], "total_bytes": nine[c][1], "bytes_x_reference": round(nine[c][1] / ref, 4),
                    **raw_of(nine[c][2])} for c in CELLS])
        if not a.no_mix:
            write_csv(os.path.join(out, "built_points.csv"), BUILT_FIELDS, built)
            if a.built_after:
                write_csv(os.path.join(out, "built_points_after_curve.csv"), BUILT_FIELDS, after)
            with open(os.path.join(out, "README.md"), "w") as fh:
                fh.write(readme(name, w, a.grid or grid, a.dataset, rows, built, ref, a.criterion, after, notes, left).replace("{sf}", str(a.sf)))
        ncaps = len({r["cap_bytes"] for r in rows})
        nodes = sum(r["nodes"] or 0 for r in rows)
        print(f"{name}: {ncaps} limits, {len(rows)} rows, {len(built)} + {len(after)} built mixes{f' ({len(left)} left out: no capture of a part)' if left else ''} ({tw - tb:.0f} s), {nodes:,} nodes, {time.time() - t0:.0f} s -> {out}", flush=True)
        if not rows or not ncaps:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
