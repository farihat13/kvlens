#!/usr/bin/env python3
"""Check the BI / JOB headline weighting: recompute once each, additive (ref Sp-Co) and geomean from costs.csv here, with
no shared code, and compare with cross_benchmark_regret.csv's columns. Prints the before/after table (winner and worst
over best, per benchmark x model x rule). --perturb FAMILY,QUERY,CELL,FACTOR multiplies one cost first (the negative
control: the comparison must then fail).

    python3 experiments/mix_benefit/check_headline_weighting.py --costs <costs.csv> --regret <cross_benchmark_regret.csv>
"""
import argparse
import csv
import math
import sys
from collections import defaultdict

from kvevaluator.layouts import name_of

MODELS = ("wops", "bytes", "wops_bytes4k")
REF = "props_split_adj_condensed"
COLUMNS = {f: (f, f + ", additive (Sp-Co reference)", f + ", equal contribution") for f in ("BI", "IMDb / JOB")}   # once, additive, geomean


def lab(c):
    return name_of(c)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--costs", required=True)
    ap.add_argument("--regret", required=True)
    ap.add_argument("--perturb", default="")
    ap.add_argument("--pending", action="append", default=[], metavar="FAMILY",
                    help="a family not captured yet (e.g. 'IMDb / JOB'): skipped; it must then have no rows")
    a = ap.parse_args()
    pq = defaultdict(lambda: defaultdict(dict))          # family -> query -> cell -> {model: cost}
    for r in csv.DictReader(open(a.costs)):
        if r["family"] in COLUMNS:
            pq[r["family"]][r["query"]][r["cell"]] = {m: float(r[m]) for m in MODELS}
    if a.perturb:
        fam, q, cell, k = a.perturb.split(",")
        for m in MODELS:
            pq[fam][q][cell][m] *= float(k)
        print(f"PERTURBED: {fam} {q} {cell} x{k} (every model)")
    regret = {(r["benchmark"], r["model"], r["cell"]): float(r["ratio"]) for r in csv.DictReader(open(a.regret))}
    missing = sorted({c for fam, cols in COLUMNS.items() if fam not in a.pending for c in cols} - {b for b, _, _ in regret})
    if missing:
        sys.exit(f"{a.regret}: no column {missing} (a regret table older than the additive columns?)")
    checked = differ = 0
    print("| benchmark | model | once each: best, worst / best | additive, ref Sp-Co (A) | geomean (B, the headline) |")
    print("|---|---|---|---|---|")
    for fam, (once_col, add_col, geo_col) in COLUMNS.items():
        Q = pq[fam]
        if fam in a.pending:
            if Q:
                sys.exit(f"{fam}: --pending names it, but {a.costs} has {len(Q)} of its queries")
            print(f"| {fam} | pending (no capture yet) | | | |")
            continue
        cells = sorted(next(iter(Q.values())))
        for m in MODELS:
            once = {c: sum(Q[q][c][m] for q in Q) for c in cells}
            add = {c: sum(Q[q][c][m] / Q[q][REF][m] for q in Q) / len(Q) for c in cells}
            geo = {c: math.exp(sum(math.log(Q[q][c][m]) for q in Q) / len(Q)) for c in cells}
            for col, v in ((once_col, once), (add_col, add), (geo_col, geo)):
                b = min(v.values())
                for c in cells:
                    checked += 1
                    differ += abs(round(v[c] / b, 4) - regret[(col, m, c)]) > 2e-4
            f = lambda v: (lambda b, w: f"{lab(b)}, {lab(w)} {v[w] / v[b]:.2f}x")(min(v, key=v.get), max(v, key=v.get))
            print(f"| {fam} | {m} | {f(once)} | {f(add)} | {f(geo)} |")
    print(f"against {a.regret}: {checked} ratios compared, {differ} differ")
    return 1 if differ or not checked else 0


if __name__ == "__main__":
    sys.exit(main())
