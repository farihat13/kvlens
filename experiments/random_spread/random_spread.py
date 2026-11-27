#!/usr/bin/env python3
"""Random LDBC Interactive workloads on a triangle: which uniform layout is the cheapest as the split between complex
reads (IC), short reads (IS) and updates (IU) moves, with the mix inside each group drawn at random. Analysis only:
reads I01's per-query costs on the 49 (kvcost cost-layouts --workload i01 Sp-Sp ... Ch256k-Ch256k: layout-costs/i01/measured/query_costs.csv),
opens no store.

  layouts     every (properties, adjacency) pair over a set of settings (--sets; default two sets: Sp, Co, Ch1k, Ch4k,
              Ch16k = 25 layouts, and the same plus Ch64k, Ch256k = 49), cost = wops_bytes4k per run (cost-layouts)
  a workload  a split s = (complex, short, update) shares, and inside each group random weights w (Dirichlet(1) over its
              queries); the same N draws (seed SEED) at every point and in both columns:
                cost_sp_co  freq_q = s_g * w_q / cost_q(Sp-Co)   (s = each group's share of the workload's cost on Sp-Co)
                executions  freq_q = s_g * w_q                   (s = each group's share of the runs)
              its cost on a layout is sum_q freq_q * cost_q(layout); the winner is the cheapest layout of the set (a tie
              goes to the first in the set's order: properties outer, adjacency inner, settings in the order given)
  points      the triangle cut into STEPS^2 small triangles (5% steps), each priced at its centre
  marks       I01 (LDBC's own frequencies): its split in each column

Gates: the group-cost path equals pricing each workload's frequency vector directly (every draw at five points per
column and set); each group's share of the column's unit equals its split; every tile's wins add up to N.

    python3 experiments/random_spread/random_spread.py --dataset ldbc=3 [--from TREE] [--sets NAME=S,S,...;...]
        reads  <TREE>/summary/ldbc/<scale>/layout-costs/i01/measured/query_costs.csv (default the output dir; --from paper-results)
        -> the output dir's summary/ldbc/<scale>/random-spread/: tiles.csv (layout_set x column x tile x layout: wins out of N; only layouts that win a draw),
           marks.csv (I01's split per column), groups.csv (query -> group), winners.csv (layout_set x column x tile: the top layout, its wins, clear or not),
           random_spread_run.json (the constants, the sets, the gates' counts, a summary per set)
"""
import argparse
import csv
import json
import os

import numpy as np

from kvcost.layout_cost import layout_costs as LC
from kvcost.layout_cost import benchmarks
from kvevaluator import sizes
from kvevaluator.layouts import cell_id
from kvevaluator.output_tree import Tree

MODEL = "wops_bytes4k"
REF = "props_split_adj_condensed"                      # Sp-Co: the split in cost_sp_co is a share of the cost here
SEED, N, STEPS = 20261026, 100, 20
GROUPS = ("complex", "short", "update")                # corner order: bottom left, bottom right, top
GROUP_OF = {"ic": "complex", "is": "short", "iu": "update"}   # LDBC Interactive's three kinds, by the query's id
COLUMNS = ("cost_sp_co", "executions")
SHORT = {"split": "Sp", "condensed": "Co", "chunked": "Ch64k"}
SETS = {"sp-co-ch1k-ch4k-ch16k": ("split", "condensed", "chunked_c1k", "chunked_c4k", "chunked_c16k"),
        "sp-co-ch1k-ch4k-ch16k-ch64k-ch256k": ("split", "condensed", "chunked_c1k", "chunked_c4k", "chunked_c16k",
                                               "chunked", "chunked_c256k")}


def short(option):
    return SHORT.get(option) or "Ch" + option.split("_c")[-1]


def query_order(q):
    return q[:2], int(q[2:])


def tiles(n):
    """the triangle cut into n^2 small triangles, each as its 3 corners in (right share, top share)"""
    out = []
    for i in range(n):
        for j in range(n - i):
            out.append([(i, j), (i + 1, j), (i, j + 1)])
            if i + j <= n - 2:
                out.append([(i + 1, j), (i, j + 1), (i + 1, j + 1)])
    return [[(a / n, b / n) for a, b in t] for t in out]


def load(place):
    """([(family, query)] in order, {(family, query): group}, {(family, query): {layout: cost}}) of a cost-layouts
    place's query_costs.csv, its half view"""
    cost, group = {}, {}
    _, rows = LC.read_query_costs(place)
    for fq, per in rows.items():
        group[fq] = GROUP_OF[fq[1][:2]]
        for lay, r in per.items():
            cost.setdefault(fq, {})[lay] = float(r[MODEL])
    qs = sorted(cost, key=lambda fq: query_order(fq[1]))
    return qs, group, cost


DATASET = "ldbc"   # the study's own dataset: I01's 49 layouts


def w16_freqs(sf):
    (w,) = benchmarks.select(DATASET, sf, "i01").values()
    return {(f, q): float(n) for f, q, n, _ in w["items"]}


def run_set(name, settings, qs, group, cost, freqs):
    """(tile rows, winner rows, mark rows, gate counts, summary) for one layout set"""
    layouts = [cell_id(p, a) for p in settings for a in settings]
    missing = [L for L in layouts if any(L not in cost[fq] for fq in qs)]
    if missing:
        raise SystemExit(f"random_spread: {name}: no cost for {missing[:3]}")
    C = np.array([[cost[fq][L] for L in layouts] for fq in qs])
    ref = C[:, layouts.index(REF)]
    idx = {g: [i for i, fq in enumerate(qs) if group[fq] == g] for g in GROUPS}
    if any(not v for v in idx.values()):
        raise SystemExit(f"random_spread: an empty group {[g for g in GROUPS if not idx[g]]}")
    rng = np.random.default_rng(SEED)                  # the same N draws for every point, column and set
    draws = [{g: rng.dirichlet(np.ones(len(idx[g]))) for g in GROUPS} for _ in range(N)]
    points = []
    for t, corners in enumerate(tiles(STEPS)):
        s_r = float(np.mean([p[0] for p in corners]))
        s_t = float(np.mean([p[1] for p in corners]))
        points.append((t, dict(zip(GROUPS, (1 - s_r - s_t, s_r, s_t)))))
    tile_rows, win_rows, mark_rows = [], [], []
    n_direct = n_meaning = 0
    for col in COLUMNS:
        div = ref if col == "cost_sp_co" else np.ones(len(qs))
        A = {g: np.array([d[g] @ (C[idx[g]] / div[idx[g], None]) for d in draws]) for g in GROUPS}   # draws x layouts
        part = {g: sum(freqs[qs[i]] * (ref[i] if col == "cost_sp_co" else 1.0) for i in idx[g]) for g in GROUPS}
        for g in GROUPS:
            mark_rows.append({"layout_set": name, "column": col, "mark": "i01", "group": g,
                              "share": repr(float(part[g] / sum(part.values())))})
        for t, split in points:
            tot = sum(split[g] * A[g] for g in GROUPS)
            best = tot.argmin(axis=1)
            wins = np.bincount(best, minlength=len(layouts))
            for j, L in enumerate(layouts):
                tile_rows.append({"layout_set": name, "column": col, "tile": t, "share_left": repr(split[GROUPS[0]]),
                                  "share_right": repr(split[GROUPS[1]]), "share_top": repr(split[GROUPS[2]]),
                                  "layout": L, "wins": int(wins[j])})
            top = int(wins.argmax())                    # a tie goes to the first in the set's order
            tied = int((wins == wins[top]).sum()) > 1
            win_rows.append({"layout_set": name, "column": col, "tile": t, "share_left": repr(split[GROUPS[0]]),
                             "share_right": repr(split[GROUPS[1]]), "share_top": repr(split[GROUPS[2]]),
                             "winner": layouts[top], "wins": int(wins[top]), "clear": int(wins[top] >= N / 2 and not tied)})
        # gate: the group-cost path equals pricing each workload's frequency vector directly; the split's meaning
        for _, split in points[:: len(points) // 5][:5]:
            via = sum(split[g] * A[g] for g in GROUPS)
            for k, d in enumerate(draws):
                f = np.zeros(len(qs))
                for g in GROUPS:
                    f[idx[g]] = split[g] * d[g] / div[idx[g]]
                if not np.allclose(f @ C, via[k], rtol=1e-9, atol=0):
                    raise SystemExit(f"random_spread: {name} {col} draw {k}: the two pricings differ")
                n_direct += len(layouts)
                unit = ref if col == "cost_sp_co" else np.ones(len(qs))
                for g in GROUPS:
                    got = float(f[idx[g]] @ unit[idx[g]]) / float(f @ unit)
                    if abs(got - split[g]) > 1e-9:
                        raise SystemExit(f"random_spread: {name} {col} draw {k}: {g} is {got!r} of the {col}, "
                                         f"the split says {split[g]!r}")
                    n_meaning += 1
    per = {}
    for r in tile_rows:
        per[(r["column"], r["tile"])] = per.get((r["column"], r["tile"]), 0) + r["wins"]
    if set(per.values()) != {N} or len(per) != len(COLUMNS) * STEPS ** 2:
        raise SystemExit(f"random_spread: {name}: a tile's wins do not add up to {N}, or tiles are missing")
    summary = {}
    for col in COLUMNS:
        rows = [r for r in win_rows if r["column"] == col]
        count = {}
        for r in rows:
            count[r["winner"]] = count.get(r["winner"], 0) + 1
        summary[col] = {"tiles_won_by": dict(sorted(count.items(), key=lambda kv: (-kv[1], kv[0]))),
                        "tiles_not_clear": sum(1 - r["clear"] for r in rows)}
    print(f"[gate] {name}: direct pricing == group costs on {n_direct} (point, draw, layout) cells; each group's share "
          f"== its split on {n_meaning} (point, draw, group); {len(per)} (column, tile) x {N} draws add up")
    for col in COLUMNS:
        top = ", ".join(f"{L} {n}" for L, n in list(summary[col]["tiles_won_by"].items())[:6])
        print(f"   {name} {col}: tiles won by {top}; {summary[col]['tiles_not_clear']} tiles without a clear winner")
    return tile_rows, win_rows, mark_rows, {"direct": n_direct, "meaning": n_meaning, "tiles": len(per)}, summary


def write(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    print(f"-> {path} ({len(rows)} rows)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", required=True, metavar="ldbc=SCALE", help="LDBC at its scale (ldbc=3: the paper's)")
    ap.add_argument("--from", dest="tree", default=None, help="the output tree to read (default the output dir; "
                                                               "paper-results); the outputs go to the output dir")
    ap.add_argument("--sets", default=None, help="NAME=S,S,...;NAME=... (settings as the layout names spell them); "
                    "default: " + "; ".join(f"{k}={','.join(v)}" for k, v in SETS.items()))
    a = ap.parse_args()
    sf = sizes.scale(sizes.resolve(a.dataset), DATASET)
    if not sf:
        raise SystemExit(f"--dataset {a.dataset}: no LDBC scale")
    costs = LC.place(Tree(a.tree), DATASET, sf, "i01")
    out = str(Tree().summary(DATASET, sf, "random-spread"))
    os.makedirs(out, exist_ok=True)
    sets = SETS if not a.sets else {k: tuple(v.split(",")) for k, v in (x.split("=", 1) for x in a.sets.split(";"))}
    qs, group, cost = load(costs)
    n_of = {g: sum(1 for fq in qs if group[fq] == g) for g in GROUPS}
    print(f"[load] {len(qs)} queries from {costs}: " + ", ".join(f"{g} {n}" for g, n in n_of.items()))
    freqs = w16_freqs(sf)
    if set(freqs) != set(qs):
        raise SystemExit(f"random_spread: I01's queries {sorted(set(freqs) ^ set(qs))} differ from the cost table's")
    tile_rows, win_rows, mark_rows, gates, summaries = [], [], [], {}, {}
    for name, settings in sets.items():
        t, w, m, g, s = run_set(name, settings, qs, group, cost, freqs)
        tile_rows += t
        win_rows += w
        mark_rows += m
        gates[name], summaries[name] = g, s
    write(os.path.join(out, "tiles.csv"), [r for r in tile_rows if r["wins"]])   # a layout with no win is left out
    write(os.path.join(out, "winners.csv"), win_rows)
    write(os.path.join(out, "marks.csv"), mark_rows)
    write(os.path.join(out, "groups.csv"), [{"family": f, "query": q, "group": group[(f, q)]} for f, q in qs])
    run = {"model": MODEL, "reference": REF, "seed": SEED, "draws": N, "tile_steps": STEPS, "groups": list(GROUPS),
           "queries_per_group": n_of, "columns": {
               "cost_sp_co": "the split is each group's share of the workload's cost on Sp-Co",
               "executions": "the split is each group's share of the runs"},
           "sets": {k: {"settings": list(v), "short": [short(o) for o in v], "layouts": len(v) ** 2} for k, v in sets.items()},
           "gates": gates, "summary": summaries}
    with open(os.path.join(out, "random_spread_run.json"), "w") as f:
        json.dump(run, f, indent=1, sort_keys=True)
        f.write("\n")
    print(f"-> {out}/random_spread_run.json")


if __name__ == "__main__":
    main()
