#!/usr/bin/env python3
"""The equal-bytes front when the chunk size is part of the choice: a mixed layout gives each part Split, Condensed, or
Chunked at ONE OF SEVERAL chunk sizes (compose.Composer.sizes), against the fronts with one chunk size for every part.
Analysis only: opens no store. Every line is an exact solve (exact_search.solve) on the same cost cache:

    line "chunked_c64k" (.. one per size)   each part: Split | Condensed | Chunked at that size (today's front at the grid's size)
    line "any size"                         each part: Split | Condensed | Chunked at any of the sizes

A part on another size than the grid's is priced from that size's measured Chunked/Chunked capture (its part rows); the
plan per query is compose.query_pick's (decided on the held-out params, costs on the reported ones). The limits: budget_sweep.caps_of's for the grid's size (the equal-bytes front's own), plus every size's uniform
layouts' bytes and each line's two ends. Costs as equal_bytes_front.py:
    total ops   = read weighted ops (G + 1.1 S + 0.01 N) + the writes' own reads (same weights) + PUTs + DELs
    total bytes = bytes read + the bytes the writes read first + bytes written

    python3 experiments/layout_choice/chunk_size_front.py --sf 3 --grid output/captures/ldbc/sf3 \\
        --size 4k --size 1k --workload i01,i02
        -> <root>/<workload dir>/chunk_size_front.csv (a row per unit set, line and limit), equal_bytes_front.csv (the "any
           size" line in equal_bytes_front.py's shape), chosen_settings.csv (the "any
           size" line: limit, part, setting), uniform_layouts.csv, settings_by_part.md, README.md
    <root> default: output/summary/ldbc/sf<SF>/mix-benefit/equal-bytes-front_chunk-sizes-<the sizes>  (the size set is part
        of the identity)
    --must-equal <equal_bytes_front.csv>: the grid-size line must equal that file's ungrouped rows (exit 1 otherwise)
    --recheck: only the --must-equal check, on the chunk_size_front.csv already at --root (no cost cache, no solves)
    --fresh: build each query's costs again; else they are kept and reused across runs (cost_cache.set_cache, in
        <stats>/<dataset>/<scale>/mix-cost-cache): a re-run, and I02 after I01 (its reads are I01's), load instead of
        pricing again
    --caps N, --reference CELL: caps_of's count of evenly spaced limits (default budget_sweep.CAPS) and the cell the named
        budgets B and cap_x_reference are taken against (default settings.B_UNIT, Sp-Co)
    --resolve-unproven PRIOR_ROOT --root NEW [--smoke-limit N]: only the prior run's unproven "any size" limits again at
        --max-nodes, each from its prior best mix; the rest copied; + resolved.csv (prior and new ops per limit)
Checks (exit 1): at every limit the "any size" line is not above the lowest one-size line; --must-equal.
"""
import argparse
import csv
import json
import multiprocessing
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.layout_cost import compose
from kvcost.layout_cost import benchmarks
from kvcost.layout_search import search as cost_cache
from budget_sweep import B_UNIT, CAPS, caps_of
from kvcost.layout_search.front import x_of
from kvcost.layout_search.search import lagrange_bounds, solve
from kvcost.layout_cost.compose import CELLS, KNOWN, LAYOUTS, add_pick_by_arg
from kvcost.layout_cost import settings
from kvcost.layout_cost.settings import grid_size, parse_sizes, size_name  # noqa: F401
from kvcost.layout_cost.mixes import assignment_label, unit_name
from kvevaluator.measure import grid_capture
from kvevaluator.output_tree import Tree

RAW = list(compose.MEASURES) + list(compose.CHANNELS)
ANY = "any size"
FIELDS = (["workload", "unit_set", "line", "cap_bytes", "cap_kind", "cap_x_reference", "total_ops", "total_bytes"] + RAW +
          ["best_uniform_within_cap", "best_uniform_ops", "ops_uniform_over_mix", "lowest_one_size_line", "lowest_one_size_ops",
           "ops_one_size_over_any", "parts_per_setting", "nodes", "seconds", "proven_optimum", "assignment"])
UNIT_SETS = {False: "per type", True: "per type and direction"}
_W = {}      # what a forked worker reads (set before the pool starts)


def setting_name(layout, gsize):
    """a setting as the outputs spell it: the grid's Chunked carries its size too"""
    return f"chunked_c{gsize}" if layout == "chunked" else layout


def named(assign, gsize):
    return {u: setting_name(l, gsize) for u, l in assign.items()}


def _solve_one(task):
    by_dir, line, i, cap, start = task
    data, pb, units, masks = _W["sets"][by_dir]
    t0 = time.time()
    info = {}
    hard = line == ANY                                    # the one-size lines are the equal-bytes front's own solves, unchanged
    assign, ops, byt, nodes, _ = solve(pb, "ops", cap, start, allowed=masks[line], lagrange=_W["lagrange"][by_dir] if hard else None,
                                       max_nodes=_W["max_nodes"] if hard else None, info=info)
    if assign is None:
        return by_dir, line, i, None
    t = compose.cost_mix(_W["w"], data, assign)          # the raw lanes, and the search's totals re-priced
    cur = compose.currencies(t)
    assert abs(cur["ops"] - ops) <= 1e-6 * max(1.0, ops) and abs(cur["bytes"] - byt) <= 1e-6 * max(1.0, byt), (by_dir, line, cap)
    return by_dir, line, i, (ops, byt, nodes, {k: round(t[k], 3) for k in RAW}, assign, time.time() - t0, info["proven"])


def front(name, w, grid, sizes, gsize, jobs, unit_sets, max_nodes=None, redo=None, smoke_limit=None, n_caps=CAPS, reference=B_UNIT, *,
          composer):
    """(rows, chosen rows, uniform rows, the reference bytes, the summary per unit set); with `redo` (--resolve-unproven)
    only those "any size" solves: {(unit set, cap_bytes):  result}"""
    ctx = multiprocessing.get_context("fork")
    t0 = time.time()
    loaded = []
    for by_dir in unit_sets:
        data = compose.load(grid, by_dir, composer=composer)
        loaded.append((w, data, compose.workload_units(w, data)))
    t1 = time.time()
    pbs = cost_cache.problems(loaded, jobs)
    t2 = time.time()
    lines = [f"chunked_c{gsize}"] + [s[0] for s in sizes]
    sets, caps, uniforms, ref = {}, {}, {}, None
    for by_dir, (_, data, units), pb in zip(unit_sets, loaded, pbs):
        print(f"   {name}: {UNIT_SETS[by_dir]}: {pb.cost_cache_entries:,} cost cache entries, {len(pb.queries)} queries, options per part "
              f"{sorted(set(pb.size))}", flush=True)
        at = lambda p, l: pb.options[p].index(l)
        masks = {line: [[at(p, "split"), at(p, "condensed"), at(p, "chunked" if line == lines[0] else line)] for p in range(len(pb.order))]
                 for line in lines}
        masks[ANY] = [list(range(k)) for k in pb.size]
        sets[by_dir] = (data, pb, units, masks)
        singles, c, _ = caps_of(pb, units, True, n_caps, reference)   # the grid's size: the equal-bytes front's own limits
        ref = ref or singles[reference][2]
        for cap, kind in sorted(c.items()):
            if not any(abs(cap - have) <= 1e-9 * have for have in caps):
                caps[cap] = kind
        for line in lines:                                   # each size's nine uniform layouts, and each line's two ends
            chunked = "chunked" if line == lines[0] else line
            for cell in CELLS:
                p, a = cell[len("props_"):].split("_adj_")
                sub = lambda l: chunked if l == "chunked" else l
                combo = pb.from_assign({u: sub(p if u[0] == "props" else a) for u in units})
                uniforms[(by_dir, line, cell)] = (combo, *pb.total(combo))
        for line in lines + [ANY]:
            for primary in ("bytes", "ops"):
                b = solve(pb, primary, None, None, allowed=masks[line])[2]
                if not any(abs(b - have) <= 1e-9 * have for have in caps):
                    caps[b] = "line end"
        for (bd, line, cell), (_, _, b) in sorted(uniforms.items()):
            if bd == by_dir and "chunked" in cell and not any(abs(b - have) <= 1e-9 * have for have in caps):
                caps[b] = "single"
    print(f"   {name}: load {t1 - t0:.0f} s, cost cache {t2 - t1:.0f} s ({jobs} slices at a time), limits {time.time() - t2:.0f} s; "
          f"{len(caps)} limits", flush=True)
    # the lagrange bounds of the "any size" solves: multipliers around the front's mean slope (ops per byte between its ends)
    lag = {}
    for by_dir in unit_sets:
        pb = sets[by_dir][1]
        _, o_lo, b_lo, _, _ = solve(pb, "bytes")
        _, o_hi, b_hi, _, _ = solve(pb, "ops")
        m0 = (o_lo - o_hi) / (b_hi - b_lo) if b_hi > b_lo and o_lo > o_hi else 0.0
        lag[by_dir] = lagrange_bounds(pb, [m0 * 4.0 ** k for k in range(-4, 5)]) if m0 > 0 else None
    _W.update(w=w, sets=sets, lagrange=lag, max_nodes=max_nodes)
    order, best_uniform, solved = sorted(caps), {}, {}

    def run(tasks, what):
        """the solves of one stage, in a fork pool (each one unchanged); progress every 50"""
        t3 = time.time()
        if jobs > 1:
            with ctx.Pool(jobs) as pool:
                for k, (bd, line, i, res) in enumerate(pool.imap_unordered(_solve_one, tasks, chunksize=1), 1):
                    solved[(bd, line, i)] = res
                    if k % 50 == 0:
                        print(f"   {name}: {what}: {k} of {len(tasks)} solves, {time.time() - t3:.0f} s", flush=True)
        else:
            for t in tasks:
                bd, line, i, res = _solve_one(t)
                solved[(bd, line, i)] = res
        print(f"   {name}: {what}: {len(tasks)} solves in {time.time() - t3:.0f} s wall", flush=True)

    if redo is not None:
        return resolve(name, sets, order, unit_sets, redo, gsize, run, solved, smoke_limit)

    def uniform_start(by_dir, line, i, cap):
        own = [(l, c) for (bd, l, c) in uniforms if bd == by_dir and (line == ANY or l == line)]
        fits = [(l, c) for l, c in own if uniforms[(by_dir, l, c)][2] <= cap * (1 + 1e-9)]
        best = min(fits, key=lambda lc: uniforms[(by_dir, *lc)][1:]) if fits else None
        best_uniform[(by_dir, line, i)] = best
        return uniforms[(by_dir, *best)][0] if best else None

    # the one-size lines first, each from the best uniform layout that fits (as the equal-bytes front starts); then the
    # "any size" line from the best one-size mix at the same limit: an incumbent only, the solve stays exact
    run([(by_dir, line, i, cap, uniform_start(by_dir, line, i, cap)) for i, cap in enumerate(order) for by_dir in unit_sets for line in lines],
        "one size per line")
    # ... and per type and direction also from the per type "any size" mix at the same limit (the same store: both
    # directions of an edge type on one setting), so its line is never above the per type one, proven or not
    for by_dir in unit_sets:                                 # per type first
        tasks = []
        for i, cap in enumerate(order):
            start = uniform_start(by_dir, ANY, i, cap)
            found = [solved[(by_dir, line, i)][4] for line in lines if solved[(by_dir, line, i)]]
            if by_dir and solved.get((False, ANY, i)):
                per_type = solved[(False, ANY, i)][4]
                found.append({u: per_type[u[:2]] for u in sets[by_dir][2]})
            if found:
                pb = sets[by_dir][1]
                combos = [pb.from_assign(x) for x in found]
                fit = [c for c in combos if pb.total(c)[1] <= cap * (1 + 1e-9)]
                if fit:
                    start = min(fit, key=lambda c: pb.total(c))
            tasks.append((by_dir, ANY, i, cap, start))
        run(tasks, f"{ANY}, {UNIT_SETS[by_dir]}")
    rows, chosen, summary = [], [], {}
    for by_dir in unit_sets:
        below, worst, n_any, bad = 0, (1.0, None), 0, []
        for i, cap in enumerate(order):
            one = {line: solved[(by_dir, line, i)] for line in lines if solved[(by_dir, line, i)]}
            low = min(one, key=lambda l: one[l][0]) if one else None
            for line in lines + [ANY]:
                res = solved[(by_dir, line, i)]
                if res is None:
                    continue
                ops, byt, nodes, raw, assign, secs, proven = res
                bu = best_uniform[(by_dir, line, i)]
                bu_ops = uniforms[(by_dir, *bu)][1] if bu else None
                row = {"workload": name, "unit_set": UNIT_SETS[by_dir], "line": line, "cap_bytes": round(cap, 3), "cap_kind": caps[cap],
                       "cap_x_reference": round(cap / ref, 4), "total_ops": round(ops, 3), "total_bytes": round(byt, 3), **raw,
                       "best_uniform_within_cap": f"{bu[1]} at {bu[0]}" if bu else "", "best_uniform_ops": round(bu_ops, 3) if bu else "",
                       "ops_uniform_over_mix": round(bu_ops / ops, 4) if bu else "", "nodes": nodes, "seconds": round(secs, 2), "proven_optimum": "yes" if proven else "no",
                       "assignment": assignment_label(named(assign, gsize))}
                if line == ANY:
                    n_any += 1
                    names = named(assign, gsize)
                    count = {}
                    for u, l in names.items():
                        count[l] = count.get(l, 0) + 1
                        chosen.append({"workload": name, "unit_set": UNIT_SETS[by_dir], "cap_bytes": round(cap, 3),
                                       "cap_x_reference": round(cap / ref, 4), "part": unit_name(u), "setting": l})
                    row["parts_per_setting"] = "; ".join(f"{k}={v}" for k, v in sorted(count.items()))
                    if low:
                        lo_ops = one[low][0]
                        row.update(lowest_one_size_line=low, lowest_one_size_ops=round(lo_ops, 3), ops_one_size_over_any=round(lo_ops / ops, 4))
                        if ops > lo_ops * (1 + 1e-9):
                            bad.append((cap, ops, low, lo_ops))
                        if ops < lo_ops * (1 - 1e-9):
                            below += 1
                            if lo_ops / ops > worst[0]:
                                worst = (lo_ops / ops, cap / ref)
                rows.append(row)
        unproven = sum(1 for i in range(len(order)) if solved[(by_dir, ANY, i)] and not solved[(by_dir, ANY, i)][6])
        alone = sum(1 for i in range(len(order)) if solved[(by_dir, ANY, i)] and not any(solved[(by_dir, l, i)] for l in lines))
        unreached = [(caps[cap], cap / ref) for i, cap in enumerate(order) if not solved[(by_dir, ANY, i)]]
        summary[by_dir] = {"limits": n_any, "below": below, "largest": worst, "above": bad, "unproven": unproven, "alone": alone,
                           "unreached": unreached}
    urows = [{"workload": name, "unit_set": UNIT_SETS[bd], "chunk_size": line, "cell": cell, "total_ops": round(o, 3), "total_bytes": round(b, 3),
              "bytes_x_reference": round(b / ref, 4)} for (bd, line, cell), (_, o, b) in sorted(uniforms.items())]
    return rows, chosen, urows, ref, summary


def key_of(unit_set, cap):
    return unit_set, f"{float(cap):.3f}"


def resolve(name, sets, order, unit_sets, redo, gsize, run, solved, smoke_limit):
    """the prior run's unproven "any size" limits again, each from the prior run's best mix there (so the new value is
    never above it); refuses when a limit is not in this run's limit set or the prior mix does not price to the prior row"""
    tasks = []
    for by_dir in unit_sets:
        pb = sets[by_dir][1]
        part_of = {unit_name(u): u for u in pb.order}
        assert len(part_of) == len(pb.order), "two parts share a name"
        for i, cap in enumerate(order):
            prior = redo.get(key_of(UNIT_SETS[by_dir], cap))
            if prior is None:
                continue
            start = pb.from_assign({part_of[p]: "chunked" if s == f"chunked_c{gsize}" else s for p, s in prior["settings"].items()})
            ops, byt = pb.total(start)
            if abs(ops - prior["ops"]) > 1e-3 + 1e-9 * ops or abs(byt - prior["bytes"]) > 1e-3 + 1e-9 * byt:
                sys.exit(f"{name} {UNIT_SETS[by_dir]} cap {cap:.3f}: the prior mix prices to {ops:.3f} ops, {byt:.3f} bytes, the prior "
                         f"row says {prior['ops']:.3f}, {prior['bytes']:.3f}: not the same cost cache")
            tasks.append((by_dir, ANY, i, cap, start))
    if len(tasks) != len(redo):
        sys.exit(f"{name}: {len(redo)} unproven limits in the prior run, {len(tasks)} found in this run's limit set")
    print(f"   {name}: {len(tasks)} prior mixes price to their prior rows exactly", flush=True)
    if smoke_limit:                                          # one solve at a short limit before the long ones
        limit = _W["max_nodes"]
        _W["max_nodes"] = smoke_limit
        run(tasks[:1], f"smoke, {smoke_limit} node limit")
        _W["max_nodes"] = limit
        res = solved[tasks[0][:3]]
        if res is None or res[0] > redo[key_of(UNIT_SETS[tasks[0][0]], tasks[0][3])]["ops"] * (1 + 1e-9):
            sys.exit(f"{name}: SMOKE FAILED: {res and res[0]} ops against the prior row's")
        print(f"   {name}: SMOKE PASS: {res[0]:,.3f} ops, proven {res[6]}, {res[5]:.0f} s", flush=True)
    run(tasks, f"{ANY}, re-solve of the unproven limits")
    return {key_of(UNIT_SETS[bd], order[i]): solved[(bd, line, i)] for bd, line, i, _, _ in tasks}


def merge_resolved(rows, chosen, new, gsize):
    """the prior rows with the re-solved "any size" limits replaced; (rows, chosen rows, one row per re-solved limit)"""
    changes, out, prior = [], [], rows_by_key(rows)
    for r in rows:
        k = key_of(r["unit_set"], r["cap_bytes"])
        if r["line"] != ANY or k not in new:
            out.append(r)
            continue
        ops, byt, nodes, raw, assign, secs, proven = new[k]
        if ops > float(r["total_ops"]) * (1 + 1e-9) + 1e-3:
            sys.exit(f"{k}: the re-solve is above the prior row ({ops} > {r['total_ops']})")
        names = named(assign, gsize)
        count = {}
        for l in names.values():
            count[l] = count.get(l, 0) + 1
        r = {**r, "total_ops": round(ops, 3), "total_bytes": round(byt, 3), **raw, "nodes": nodes, "seconds": round(secs, 2),
             "proven_optimum": "yes" if proven else "no", "assignment": assignment_label(names),
             "parts_per_setting": "; ".join(f"{a}={b}" for a, b in sorted(count.items()))}
        if r["best_uniform_ops"] != "":
            r["ops_uniform_over_mix"] = round(float(r["best_uniform_ops"]) / ops, 4)
        if r["lowest_one_size_ops"] != "":
            r["ops_one_size_over_any"] = round(float(r["lowest_one_size_ops"]) / ops, 4)
        prior_ops = float(prior[k]["total_ops"])
        changes.append({"workload": r["workload"], "unit_set": r["unit_set"], "cap_bytes": r["cap_bytes"], "cap_x_reference": r["cap_x_reference"],
                        "prior_total_ops": round(prior_ops, 3), "total_ops": round(ops, 3), "prior_over_new": round(prior_ops / ops, 6),
                        "assignment_changed": "yes" if r["assignment"] != prior[k]["assignment"] else "no",
                        "proven_optimum": r["proven_optimum"], "seconds": round(secs, 2), "nodes": nodes})
        out.append(r)
        chosen = [c for c in chosen if key_of(c["unit_set"], c["cap_bytes"]) != k] + [
            {"workload": r["workload"], "unit_set": r["unit_set"], "cap_bytes": r["cap_bytes"], "cap_x_reference": r["cap_x_reference"],
             "part": unit_name(u), "setting": l} for u, l in names.items()]
    return out, sorted(chosen, key=lambda c: (c["unit_set"], float(c["cap_bytes"]), c["part"])), changes


def rows_by_key(rows):
    return {key_of(r["unit_set"], r["cap_bytes"]): r for r in rows if r["line"] == ANY}


def summary_of(rows, unit_set):
    """the README's summary of one unit set, from the "any size" rows (as front() counts it)"""
    sub = [r for r in rows if r["unit_set"] == unit_set and r["line"] == ANY]
    below, worst, bad = 0, (1.0, None), []
    for r in sub:
        if r["lowest_one_size_ops"] == "":
            continue
        ops, lo = float(r["total_ops"]), float(r["lowest_one_size_ops"])
        if ops > lo * (1 + 1e-9):
            bad.append((r["cap_bytes"], ops, r["lowest_one_size_line"], lo))
        if ops < lo * (1 - 1e-9):
            below += 1
            if lo / ops > worst[0]:
                worst = (lo / ops, float(r["cap_x_reference"]))
    return {"limits": len(sub), "below": below, "largest": worst, "above": bad, "unproven": sum(r["proven_optimum"] != "yes" for r in sub),
            "alone": sum(r["lowest_one_size_line"] == "" for r in sub)}


def settings_by_part(chosen):
    """per unit set and part: over how many limits it takes each setting, and the B range per setting"""
    out = []
    for us in sorted({r["unit_set"] for r in chosen}):
        sub = [r for r in chosen if r["unit_set"] == us]
        settings = sorted({r["setting"] for r in sub})
        nlim = len({r["cap_bytes"] for r in sub})
        out += [f"## {us} ({nlim} limits)", "", "Each cell: the number of limits at which the part takes the setting, and the range of B "
                "(the limit over the reference layout's bytes) over which it does.", "",
                "| part | " + " | ".join(settings) + " |", "|---|" + "---|" * len(settings)]
        for part in sorted({r["part"] for r in sub}):
            cells = []
            for s in settings:
                bs = [float(r["cap_x_reference"]) for r in sub if r["part"] == part and r["setting"] == s]
                cells.append(f"{len(bs)} (B {min(bs):.2f}-{max(bs):.2f})" if bs else "")
            out.append(f"| {part} | " + " | ".join(cells) + " |")
        out.append("")
    return "\n".join(out)


def reference_bytes(rows):
    """the bytes B is taken against in a front file: the cap of its B = 1 rows (cap_x_reference, or cap_x_nebula in a
    file in Co-Sp units: front.x_of); None when the file has no B = 1 row"""
    caps = {float(r["cap_bytes"]) for r in rows if x_of(r, "cap") == 1.0}
    if len(caps) > 1:
        raise SystemExit(f"a front file with {len(caps)} different B = 1 limits: {sorted(caps)}")
    return caps.pop() if caps else None


def must_equal(rows, path, gsize):
    """the grid-size line against equal_bytes_front.csv's ungrouped rows: (compared, differing, grid limits not in this
    run). Each file spaces its own evenly spaced ("grid") limits over its own byte range, and this run's range covers every
    size, so a reference grid limit this run has no row for is counted apart, not as a difference; any other reference
    limit (the single layouts, the midpoints, the named B) must be here. Refused when the two files take B against
    different references: their named limits then cannot coincide."""
    with open(path) as f:
        ref_rows = list(csv.DictReader(f))
    mine_ref, their_ref = reference_bytes(rows), reference_bytes(ref_rows)
    if mine_ref is None or their_ref is None or abs(mine_ref - their_ref) > 1e-9 * max(mine_ref, their_ref):
        def say(x, col):
            return f"{x / 1e9:,.3f} GB ({col})" if x is not None else "none (no B = 1 row)"
        raise SystemExit(f"--must-equal: the two fronts take B against different references: this run's B = 1 is "
                         f"{say(mine_ref, 'cap_x_reference')}, {path}'s is {say(their_ref, 'cap_x_reference, or cap_x_nebula in Co-Sp units')}; "
                         f"give a front taken against the same reference (--reference)")
    mine = {(r["unit_set"], f"{float(r['cap_bytes']):.3f}"): r for r in rows if r["line"] == f"chunked_c{gsize}"}
    n = bad = other_grid = 0
    for r in ref_rows:
        if r["option_set"] not in {x["unit_set"] for x in rows} or r["option"] != "fewest_ops_within_cap":
            continue
        m = mine.get((r["option_set"], f"{float(r['cap_bytes']):.3f}"))
        if m is None and r["cap_kind"] == "grid":
            other_grid += 1
            continue
        n += 1
        if m is None:                      # a reference limit this run has no row for
            bad += 1
            continue
        same = all(f"{float(m[k]):.3f}" == f"{float(r[k]):.3f}" for k in ["total_ops", "total_bytes"] + RAW) and m["assignment"].replace(
            f"chunked_c{gsize}", "chunked") == r["assignment"]
        bad += not same
        if not same and bad == 1:
            print(f"   FIRST DIFFERENCE {r['option_set']} cap {r['cap_bytes']}: ops {m['total_ops']} against {r['total_ops']}", flush=True)
    return n, bad, other_grid


def must_equal_line(rows, path, gsize):
    """(the README's line for --must-equal, 1 when it fails: a difference or 0 rows compared)"""
    n, bad, other_grid = must_equal(rows, path, gsize)
    return (f"The {gsize} line against `{os.path.relpath(path, compose.EXP)}`: {n} rows compared, {bad} differ "
            f"({other_grid} of its evenly spaced limits are not in this run's limit set)."), int(bool(bad) or not n)


EBF_FIELDS = (["workload", "cap_bytes", "cap_kind", "cap_x_reference", "option_set", "option", "total_ops", "total_bytes"] + RAW +
              ["best_single_within_cap", "best_single_ops", "ops_single_over_mix", "nodes", "assignment"])


def front_shape(rows):
    """the "any size" line as equal_bytes_front.csv's mix rows (the chunk-size figure reads that shape)"""
    return [{**r, "option_set": r["unit_set"], "option": "fewest_ops_within_cap",
             "best_single_within_cap": r["best_uniform_within_cap"], "best_single_ops": r["best_uniform_ops"],
             "ops_single_over_mix": r["ops_uniform_over_mix"]} for r in rows if r["line"] == ANY]


def write_csv(path, fields, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--grid", default=None, help="the nine cells: a captures/ldbc/<scale> dir (default: the output tree's)")
    ap.add_argument("--size", action="append", default=[], metavar="NAME", help="another chunk size (4k): its Chunked/Chunked "
                    "(props_chunked_c4k_adj_chunked_c4k) in the grid's place; repeatable")
    ap.add_argument("--root", default=None)
    ap.add_argument("--per-type-only", action="store_true", help="the per type unit set only (a pilot)")
    ap.add_argument("--must-equal", default=None, metavar="CSV", help="an equal_bytes_front.csv of the same grid and workload")
    ap.add_argument("--recheck", action="store_true", help="only the --must-equal check, on the chunk_size_front.csv already at "
                    "--root (its README's line rewritten); prices nothing")
    benchmarks.add_workload_arg(ap)
    add_pick_by_arg(ap)
    ap.add_argument("--jobs", type=int, default=min(32, os.cpu_count() or 1))
    cost_cache.add_cache_args(ap)
    ap.add_argument("--max-nodes", type=int, default=None, metavar="N", help="per \"any size\" solve: stop after N search nodes and keep the best mix found "
                    "(a mix that fits the limit, not proven the optimum; column proven_optimum says which)")
    ap.add_argument("--resolve-unproven", default=None, metavar="PRIOR_ROOT", help="only the \"any size\" limits PRIOR_ROOT's run did not "
                    "prove, each from its best mix there, at --max-nodes; every other row copied; writes a new --root")
    ap.add_argument("--caps", type=int, default=CAPS, help=f"evenly spaced (in log) limits, caps_of's count (default {CAPS})")
    ap.add_argument("--reference", default=B_UNIT, choices=[B_UNIT], help=f"the cell B is taken against: B_UNIT ({B_UNIT}) only; "
                    "a front's reference_x<B> limits are read as B x B_UNIT's bytes everywhere")
    ap.add_argument("--smoke-limit", type=int, default=None, metavar="N", help="with --resolve-unproven: one solve at N nodes first")
    a = ap.parse_args()
    cost_cache.apply_cache_args(a)
    grid = a.grid or str(Tree().place_dir("captures", "ldbc", a.sf))
    sizes, gsize = parse_sizes(a.size, grid), grid_size()     # each size's ChK-ChK in the grid's place
    composer = compose.Composer(criterion=a.criterion, sizes=sizes, plan_rule=compose.PlanRule())
    every = sorted([gsize] + [s[0][len("chunked_c"):] for s in sizes], key=lambda x: int(x[:-1]))
    root = a.root or str(Tree().summary("ldbc", a.sf, "mix-benefit", f"equal-bytes-front_chunk-sizes-{'-'.join(every)}"))
    unit_sets = (False,) if a.per_type_only else (False, True)
    rc = 0
    if a.recheck:                  # the --must-equal check on a finished run's chunk_size_front.csv: no cost cache, no solves
        if not a.must_equal:
            sys.exit("--recheck needs --must-equal")
        for name, w in benchmarks.select("ldbc", a.sf, a.workload).items():
            out = benchmarks.workload_dir("ldbc", a.sf, name, w, root, criterion=a.criterion)
            with open(os.path.join(out, "chunk_size_front.csv")) as f:
                rows = list(csv.DictReader(f))
            line, bad = must_equal_line(rows, a.must_equal, gsize)
            print(f"{name}: {line}", flush=True)
            rc = rc or bad
            path = os.path.join(out, "README.md")
            with open(path) as f:
                text = f.read().split("\n")
            text = [line if t.startswith(f"The {gsize} line against") else t for t in text]
            with open(path, "w") as f:
                f.write("\n".join(text))
        return rc
    if a.resolve_unproven:
        return resolve_main(a, grid, sizes, gsize, root, unit_sets, composer)
    for name, w in benchmarks.select("ldbc", a.sf, a.workload).items():
        t0 = time.time()
        rows, chosen, urows, ref, summary = front(name, w, grid, sizes, gsize, a.jobs, unit_sets, a.max_nodes, n_caps=a.caps,
                                                  reference=a.reference, composer=composer)
        out = benchmarks.workload_dir("ldbc", a.sf, name, w, root, criterion=a.criterion)
        os.makedirs(out, exist_ok=True)
        write_csv(os.path.join(out, "chunk_size_front.csv"), FIELDS, rows)
        write_csv(os.path.join(out, "equal_bytes_front.csv"), EBF_FIELDS, front_shape(rows))
        write_csv(os.path.join(out, "chosen_settings.csv"), ["workload", "unit_set", "cap_bytes", "cap_x_reference", "part", "setting"], chosen)
        write_csv(os.path.join(out, "uniform_layouts.csv"), ["workload", "unit_set", "chunk_size", "cell", "total_ops", "total_bytes", "bytes_x_reference"], urows)
        with open(os.path.join(out, "settings_by_part.md"), "w") as f:
            f.write(f"# {w['id'].upper()}: the setting each part takes on the \"any size\" line, SF{a.sf}\n\n> "
                    f"Generated by `experiments/layout_choice/chunk_size_front.py`; working output.\n\n" + settings_by_part(chosen))
        lines = [f"# The equal-bytes front with the chunk size as a per-part choice: {w['id'].upper()} ({name}), SF{a.sf}", "",
                 "> Generated by `experiments/layout_choice/chunk_size_front.py`; working "
                 "output, not a snapshot.", "",
                 f"Capture: `{os.path.relpath(grid, compose.EXP)}` (Chunked = {gsize.upper().replace('K', ' K')}B); other sizes: "
                 + ", ".join(f"{s[0]} from `{os.path.relpath(s[1], compose.EXP)}` ({s[2]})" for s in sizes) + ".",
                 "A part on another size is priced from that size's Chunked/Chunked part rows; plans: `compose.query_pick` (decided on "
                 "the held-out params, costs on the reported ones); no grouped props options.",
                 "**total ops** = read weighted ops (G + 1.1 S + 0.01 N) + the writes' own reads (same weights) + PUTs + DELs. "
                 f"**total bytes** = bytes read + the bytes the writes read first + bytes written. B = the limit / the reference layout's "
                 f"bytes ({KNOWN.get(a.reference, a.reference)}, {ref / 1e9:,.2f} GB); {a.caps} evenly spaced limits (budget_sweep.caps_of).", "",
                 "| unit set | limits | \"any size\" strictly below the lowest one-size line | largest gain over it (at B) | above it | "
                 "limits where only \"any size\" fits | not proven the optimum (time limit) |", "|---|---|---|---|---|---|---|"]
        for by_dir in unit_sets:
            s = summary[by_dir]
            lines.append(f"| {UNIT_SETS[by_dir]} | {s['limits']} | {s['below']} | {s['largest'][0]:.3f}x" + (f" (B = {s['largest'][1]:.3f})" if s["largest"][1] else "")
                         + f" | {len(s['above'])} | {s['alone']} | {s['unproven']} |")
            print(f"{name}: {UNIT_SETS[by_dir]}: {s['limits']} limits, any size strictly below the lowest one-size line at {s['below']}, largest gain "
                  f"{s['largest'][0]:.3f}x" + (f" at B = {s['largest'][1]:.3f}" if s["largest"][1] else "") + f", above it at {len(s['above'])}; only any size fits at {s['alone']}; not proven the optimum at {s['unproven']}", flush=True)
            if s["above"]:
                rc = 1
                print(f"   CHECK FAILED: any size above a one-size line, first: {s['above'][0]}", flush=True)
        for by_dir in unit_sets:                             # a limit below the fewest-bytes mix: said, not dropped silently
            if summary[by_dir]["unreached"]:
                lines += ["", f"{UNIT_SETS[by_dir]}: no mix reaches " + ", ".join(f"{x:.4g}x ({k})" for k, x in summary[by_dir]["unreached"])
                          + " (below the fewest-bytes mix): no row."]
        if a.must_equal:
            line, bad = must_equal_line(rows, a.must_equal, gsize)
            lines += ["", line]
            print(f"{name}: {line}", flush=True)
            rc = rc or bad
        lines += ["", "Files: `chunk_size_front.csv` (a row per unit set, line and limit, the raw lanes kept), `equal_bytes_front.csv` (the \"any size\" line in "
                  "`equal_bytes_front.py`'s shape, for the chunk-size figure), `chosen_settings.csv` (the "
                  "\"any size\" line: limit, part, setting), `settings_by_part.md`, `uniform_layouts.csv` (the nine uniform layouts per size).", ""]
        with open(os.path.join(out, "README.md"), "w") as f:
            f.write("\n".join(lines))
        print(f"{name}: {len(rows)} rows, {time.time() - t0:.0f} s -> {out}", flush=True)
    return rc


def read_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def resolve_main(a, grid, sizes, gsize, root, unit_sets, composer):
    """--resolve-unproven: the prior run's files, with its unproven "any size" limits re-solved, into a new --root"""
    if not a.root or os.path.abspath(a.root) == os.path.abspath(a.resolve_unproven):
        sys.exit("--resolve-unproven needs a new --root (the prior run is not overwritten)")
    rc = 0
    for name, w in benchmarks.select("ldbc", a.sf, a.workload).items():
        t0 = time.time()
        src, out = benchmarks.workload_dir("ldbc", a.sf, name, w, a.resolve_unproven, criterion=a.criterion), benchmarks.workload_dir("ldbc", a.sf, name, w, root, criterion=a.criterion)
        if os.path.exists(os.path.join(out, "chunk_size_front.csv")):
            sys.exit(f"{out}: exists")
        rows, chosen = read_csv(os.path.join(src, "chunk_size_front.csv")), read_csv(os.path.join(src, "chosen_settings.csv"))
        redo = {}
        for r in rows:
            if r["line"] == ANY and r["proven_optimum"] != "yes" and r["unit_set"] in [UNIT_SETS[b] for b in unit_sets]:
                k = key_of(r["unit_set"], r["cap_bytes"])
                redo[k] = {"ops": float(r["total_ops"]), "bytes": float(r["total_bytes"]),
                           "settings": {c["part"]: c["setting"] for c in chosen if key_of(c["unit_set"], c["cap_bytes"]) == k}}
        print(f"{name}: {len(redo)} unproven \"any size\" limits in {src}", flush=True)
        new = front(name, w, grid, sizes, gsize, a.jobs, unit_sets, a.max_nodes, redo=redo, smoke_limit=a.smoke_limit, n_caps=a.caps,
                    reference=a.reference, composer=composer) if redo else {}
        rows, chosen, changes = merge_resolved(rows, chosen, new, gsize)
        os.makedirs(out, exist_ok=True)
        write_csv(os.path.join(out, "chunk_size_front.csv"), FIELDS, rows)
        write_csv(os.path.join(out, "equal_bytes_front.csv"), EBF_FIELDS, front_shape(rows))
        write_csv(os.path.join(out, "chosen_settings.csv"), ["workload", "unit_set", "cap_bytes", "cap_x_reference", "part", "setting"], chosen)
        write_csv(os.path.join(out, "resolved.csv"), ["workload", "unit_set", "cap_bytes", "cap_x_reference", "prior_total_ops", "total_ops",
                                                      "prior_over_new", "assignment_changed", "proven_optimum", "seconds", "nodes"], changes)
        for f in ("uniform_layouts.csv",):
            with open(os.path.join(src, f)) as fi, open(os.path.join(out, f), "w") as fo:
                fo.write(fi.read())
        with open(os.path.join(out, "settings_by_part.md"), "w") as f:
            f.write(f"# {w['id'].upper()}: the setting each part takes on the \"any size\" line, SF{a.sf}\n\n> "
                    f"Generated by `experiments/layout_choice/chunk_size_front.py --resolve-unproven`; working output.\n\n" + settings_by_part(chosen))
        moved = [c for c in changes if c["prior_over_new"] > 1 + 1e-9]
        top = max(changes, key=lambda c: c["prior_over_new"]) if changes else None
        lines = [f"# The equal-bytes front with the chunk size as a per-part choice, unproven limits re-solved: {w['id'].upper()} ({name}), SF{a.sf}", "",
                 "> Generated by `experiments/layout_choice/chunk_size_front.py --resolve-unproven`; "
                 "working output, not a snapshot.", "",
                 f"The prior run `{os.path.relpath(src, compose.EXP)}` with its {len(changes)} \"any size\" limits that its time limit left unproven "
                 f"solved again at a {a.max_nodes} node limit, each from the prior best mix (so a value can only drop); every other row is "
                 "the prior run's. Costs and columns as the prior run's README.", "",
                 f"Re-solved {len(changes)}: now proven {sum(c['proven_optimum'] == 'yes' for c in changes)}; value dropped at {len(moved)}"
                 + (f", largest drop {top['prior_over_new']:.4f}x (prior / new) at B = {float(top['cap_x_reference']):.3f}" if moved else "") + ".", "",
                 "| unit set | limits | \"any size\" strictly below the lowest one-size line | largest gain over it (at B) | above it | "
                 "limits where only \"any size\" fits | not proven the optimum (time limit) |", "|---|---|---|---|---|---|---|"]
        for by_dir in unit_sets:
            s = summary_of(rows, UNIT_SETS[by_dir])
            lines.append(f"| {UNIT_SETS[by_dir]} | {s['limits']} | {s['below']} | {s['largest'][0]:.3f}x" + (f" (B = {s['largest'][1]:.3f})" if s["largest"][1] else "")
                         + f" | {len(s['above'])} | {s['alone']} | {s['unproven']} |")
            print(f"{name}: {UNIT_SETS[by_dir]}: {s['limits']} limits, any size strictly below the lowest one-size line at {s['below']}, largest gain "
                  f"{s['largest'][0]:.3f}x" + (f" at B = {s['largest'][1]:.3f}" if s["largest"][1] else "") + f", above it at {len(s['above'])}; "
                  f"not proven the optimum at {s['unproven']}", flush=True)
            if s["above"]:
                rc = 1
                print(f"   CHECK FAILED: any size above a one-size line, first: {s['above'][0]}", flush=True)
        lines += ["", "Files: as the prior run's, plus `resolved.csv` (one row per re-solved limit: the prior and new ops, proven or not, seconds).", ""]
        with open(os.path.join(out, "README.md"), "w") as f:
            f.write("\n".join(lines))
        print(f"{name}: re-solved {len(changes)}, now proven {sum(c['proven_optimum'] == 'yes' for c in changes)}, dropped {len(moved)}"
              + (f", largest {top['prior_over_new']:.4f}x at B = {float(top['cap_x_reference']):.3f}" if moved else "")
              + f"; {len(rows)} rows, {time.time() - t0:.0f} s -> {out}", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
