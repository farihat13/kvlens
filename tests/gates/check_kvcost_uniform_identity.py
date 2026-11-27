#!/usr/bin/env python3
"""kvcost's check 3: a mix that puts every unit on one cell's settings must price exactly as that cell's measured view.

For each of the nine cells of a capture: views.cell_rows (the measured cell: picks.choose_cell + the runs/ rows)
against views.mix_rows under the uniform assignment (compose.query_pick from the parts, in store mode: the measured cell
priced as itself, so both sides take the record's anchored, pinned row; option mode chooses a uniform query with no
anchor and no pin, which is another plan rule), per (family, query): the same
body, and every count, lane and measure equal within 1e-9 relative. Also each measured row against compose.py's own
item cost (compose.load_reads / load_writes, the published measures). --control flips one adjacency unit of the uniform
assignment: the check must then find differences (exit 0 only if it does).

    python3 tests/gates/check_kvcost_uniform_identity.py --dataset ldbc --sf 0.1 [--root DIR] [--control]
"""
import argparse
import os
import sys

from kvevaluator import layouts as L
from kvcost.cli import views as V
from kvcost.cli import workloads as W
from kvcost.layout_cost import benchmarks
from kvevaluator.output_tree import Tree, capture_in

TOL = 1e-9
NUM = ("S", "G", "N", "P", "D") + V.LANES + V.compose.MEASURES + V.CM.MODELS


def differ(x, y):
    x, y = float(x or 0), float(y or 0)
    return abs(x - y) > TOL * max(1.0, abs(x), abs(y))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--sf", required=True)
    ap.add_argument("--root", default=None, help="the measured <dataset>/<scale> dir (default: the output tree's)")
    ap.add_argument("--pick-by", dest="criterion", default=None)
    ap.add_argument("--control", action="store_true", help="flip one adjacency unit: the check must fail")
    ap.add_argument("--params", default=None, help="linkbench: the param set (default log)")
    a = ap.parse_args()
    from kvcost.layout_cost import compose
    ad = W.get(a.dataset)
    ad.prepare(a)
    root = os.path.abspath(a.root) if a.root else str(Tree().place_dir("captures", ad.DATASET, a.sf))
    rule = compose.PlanRule(in_sample=True if getattr(ad, "IN_SAMPLE", False) else None)   # the adapter's policy (cli.plan_rule)
    data = compose.load(root, False, ad.DATASET, composer=compose.Composer(criterion=a.criterion,
                                                                                    families=ad.FAMILIES, plan_rule=rule,
                                                                                    chunk_view="measured"))   # as cell_rows
    units = compose.grid_units(data)
    cells = rows_cmp = bad = compose_cmp = wl_cmp = 0
    first, ties = [], []
    for lay in L.grid_layouts():
        p, adj = lay.settings()
        assign = {u: (p if u[0] == "props" else adj) for u in units}
        if a.control:                                     # one adjacency unit on another setting
            u = next(u for u in units if u[0] == "adj")
            assign[u] = next(s for s in compose.LAYOUTS if s != adj)
        meas = {(r["family"], r["query"]): r for r in V.cell_rows(ad.DATASET, root, lay.cell, a.criterion, families=ad.FAMILIES, rule=rule)}
        mix = {(r["family"], r["query"]): r for r in V.mix_rows(ad.DATASET, data, assign, a.criterion, mode="store")}
        cells += 1
        for k in sorted(set(meas) | set(mix)):
            rows_cmp += 1
            x, y = meas.get(k), mix.get(k)
            why = ("in one view only" if not (x and y) else
                   next((f"{c}: measured {x[c]} vs priced {y[c]}" for c in NUM if differ(x[c], y[c])), None))
            if not why and x["body"] != y["body"] and x["kind"] == "read":    # a write has one plan: its label differs only
                # two bodies with every count and byte equal: the two pickers break an exact tie differently
                ties.append(f"{lay.name} {k[0]} {k[1]}: measured picks {x['body']}, priced {y['body']}, every number equal")
            if why:
                bad += 1
                if len(first) < 5:
                    first.append(f"{lay.name} {k[0]} {k[1]}: {why}")
        # the measured view against compose.py's own item cost (the published measures), every family
        fams = dict(ad.FAMILIES)
        celldata = {fam: (V.compose.load_writes(root, lay.cell, "measured") if kind == "write"
                          else V.compose.load_reads(capture_in(root, fam).path, lay.cell, a.criterion, rule=rule))
                    for fam, kind in fams.items()}
        # each workload of the suite at its query frequencies: compose.py on the measured cell vs compose on the parts
        for name, w in (benchmarks.workloads(ad.DATASET, a.sf).items() if ad.WEIGHTED else ()):
            m, c = V.compose.workload_cost(w, celldata), compose.cost_mix(w, data, assign, mode="store")
            wl_cmp += 1
            d = next((f"{k}: measured {m[k]} vs priced {c[k]}" for k in V.compose.MEASURES if differ(m[k], c[k])), None)
            if d:
                bad += 1
                if len(first) < 5:
                    first.append(f"{lay.name} {w['id']}: {d}")
        for fam, kind in fams.items():
            per = celldata[fam]
            for q in per:
                ref = V.compose.item_cost({fam: per}, fam, q, None)
                r = meas[(fam, q)]
                compose_cmp += 1
                d = next((f"{c}: view {r[c]} vs compose.py {ref[c]}" for c in ref if differ(r[c], ref[c])), None)
                if d:
                    bad += 1
                    if len(first) < 5:
                        first.append(f"{lay.name} {fam} {q} vs compose.py: {d}")
    print(f"[check_kvcost_uniform_identity] {ad.NAME} sf{a.sf} {os.path.relpath(root, V.EXP)}: {cells} cells, {rows_cmp} "
          f"(family, query) rows measured vs priced, {compose_cmp} rows vs compose.py, {wl_cmp} workload totals; {bad} differ"
          + (" (CONTROL: one adjacency unit flipped)" if a.control else ""))
    for f in first:
        print("  DIFF", f)
    print(f"  {len(ties)} exact ties picked by another body (not a difference)")
    for t in ties[:5]:
        print("  TIE", t)
    if cells != 9 or rows_cmp == 0 or compose_cmp == 0 or (ad.WEIGHTED and wl_cmp == 0):
        print("FAIL: fewer than 9 cells or 0 rows compared")
        return 1
    if a.control:
        print("control " + ("caught: PASS" if bad else "NOT caught: FAIL"))
        return 0 if bad else 1
    print("PASS" if bad == 0 else "FAIL")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
