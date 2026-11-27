#!/usr/bin/env python3
"""The nine uniform layouts across BENCHMARKS: each layout's cost over that benchmark's best uniform layout. Analysis
only, and no solve: it reads the per-query costs way 6 wrote (experiments/mix_benefit/per_query_disagreement.py ->
costs.csv: family, query, weight, cell, wops, bytes, wops_bytes4k; the body each capture picked) and sums them per
benchmark (python/kvcost/layout_cost/query_weighting.py). Columns: I01 (LDBC's own mix), I02 (its reads), BI and IMDb / JOB each three times
(equal contribution = the geomean over queries, the headline; each query once; additive = each query's cost over Sp-Co's,
averaged: the BI form is a reporting choice), LinkBench's four request mixes (L01 the
log's, L02 reads only, L03 writes only, L04 three times the writes; inputs/workloads/linkbench/<scale>/).

    python3 experiments/layout_choice/cross_benchmark_regret.py --costs <costs.csv> --root <out>/summary/ldbc/sf3/mix-benefit/joint-mix
        -> <root>/cross_benchmark_regret.csv, cross_benchmark_regret.md
"""
import argparse
import csv
import math
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.layout_cost.compose import CELLS
from kvcost.layout_cost.workloads.linkbench import READS, WRITES
from kvcost.layout_cost import query_weighting as QW  # (sum / geomean: the one definition)

MODELS = ("wops", "bytes", "wops_bytes4k")
# benchmark -> (families of costs.csv, the weight of a row or None to drop it, how its queries add up: python/kvcost/layout_cost/query_weighting.py)
LB = "LinkBench"
BI_EQ, JOB_EQ = "BI, equal contribution", "IMDb / JOB, equal contribution"
BI_ADD, JOB_ADD = "BI, additive (Sp-Co reference)", "IMDb / JOB, additive (Sp-Co reference)"
REF = "props_split_adj_condensed"     # the additive form's reference layout (Sp-Co)
BENCHMARKS = {
    "I01 LDBC mix": (("LDBC, all of I01",), lambda q, w: w, "sum"),
    "I02 LDBC reads": (("LDBC IC", "LDBC IS"), lambda q, w: w, "sum"),
    "BI": (("BI",), lambda q, w: w, "sum"),                       # each query once: the robustness check
    BI_EQ: (("BI",), lambda q, w: w, "geomean"),
    BI_ADD: (("BI",), lambda q, w: w, "additive"),
    "IMDb / JOB": (("IMDb / JOB",), lambda q, w: w, "sum"),       # each query once: the robustness check
    JOB_EQ: (("IMDb / JOB",), lambda q, w: w, "geomean"),
    JOB_ADD: (("IMDb / JOB",), lambda q, w: w, "additive"),
    "LinkBench L01 (the log's mix)": ((LB,), lambda q, w: w, "sum"),
    "LinkBench L02 (reads)": ((LB,), lambda q, w: w if q in READS else None, "sum"),
    "LinkBench L03 (writes)": ((LB,), lambda q, w: w if q in WRITES else None, "sum"),
    "LinkBench L04 (3x writes)": ((LB,), lambda q, w: 3 * w if q in WRITES else w, "sum"),
}
# one column per benchmark for the margins: BI and JOB define no frequencies, so they count by equal contribution
HEADLINE = ("I01 LDBC mix", BI_EQ, JOB_EQ, "LinkBench L01 (the log's mix)")
RULES = ("Plans: LDBC (I01, I02) by the pick record (wops_bytes4k on held-out params 11-15, costs on the reported params); BI and "
         "IMDb / JOB in-sample (each cell's cheapest body on its own params); LinkBench the planner's single body per request.")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--costs", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--pending", action="append", default=[], metavar="FAMILY",
                    help="a costs.csv family not captured yet (e.g. 'IMDb / JOB'): its columns print as pending, not a failure; "
                         "it must then have no rows")
    a = ap.parse_args()
    rows = list(csv.DictReader(open(a.costs)))
    per_query = defaultdict(lambda: defaultdict(dict))   # (benchmark, model) -> query -> cell -> cost
    weight = defaultdict(dict)                           # benchmark -> query -> weight
    for r in rows:
        for b, (fams, wfn, form) in BENCHMARKS.items():
            if r["family"] not in fams:
                continue
            w = wfn(r["query"], float(r["weight"]))
            if w is None:
                continue
            if form != "sum" and w != 1.0:
                raise SystemExit(f"{b}: equal contribution, but {r['query']} has weight {w} in costs.csv")
            weight[b][r["query"]] = w
            for m in MODELS:
                per_query[(b, m)][r["query"]][r["cell"]] = float(r[m])
    queries = {b: sorted(weight[b]) for b in BENCHMARKS}
    out, bad, pending = [], 0, []
    for b, (fams, _, form) in BENCHMARKS.items():
        cells = set(next(iter(per_query[(b, MODELS[0])].values()), {}))
        if set(fams) & set(a.pending) and not set(fams) <= set(a.pending):
            bad += 1
            print(f"   PART PENDING {b}: --pending names {sorted(set(fams) & set(a.pending))} of its families {list(fams)}")
            continue
        if set(fams) <= set(a.pending):
            if queries[b]:
                bad += 1
                print(f"   NOT PENDING {b}: --pending names it, but costs.csv has {len(queries[b])} of its queries")
            else:
                pending.append(b)
                print(f"   PENDING {b}: no capture yet (--pending)")
            continue
        if cells != set(CELLS) or not queries[b] or any(set(v) != cells for v in per_query[(b, MODELS[0])].values()):
            bad += 1
            print(f"   MISSING {b}: {len(cells)} of {len(CELLS)} layouts, {len(queries[b])} queries")
            continue
        for m in MODELS:
            total = QW.aggregate(per_query[(b, m)], form, weight=weight[b] if form == "sum" else None,
                                 ref=REF if form == "additive" else None)
            best = QW.best(total)
            for c in CELLS:
                out.append({"model": m, "benchmark": b, "aggregation": form, "queries": len(queries[b]), "cell": c,
                            "cost": round(total[c], 3), "best_single": best, "best_single_cost": round(total[best], 3),
                            "ratio": round(total[c] / total[best], 4)})
    os.makedirs(a.root, exist_ok=True)
    with open(os.path.join(a.root, "cross_benchmark_regret.csv"), "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(out[0]))
        wr.writeheader()
        wr.writerows(out)
    at = {(r["model"], r["benchmark"], r["cell"]): r["ratio"] for r in out}
    bs = [b for b in BENCHMARKS if any(r["benchmark"] == b for r in out)]
    hs = [b for b in HEADLINE if b in bs]
    L = ["# The nine uniform layouts across benchmarks: cost over each benchmark's best uniform layout", "",
         "> Generated by `experiments/layout_choice/cross_benchmark_regret.py`; working output, not a snapshot.", "",
         f"Source: way 6's per-query costs (`{a.costs}`), added up per benchmark (`python/kvcost/layout_cost/query_weighting.py`). LDBC and LinkBench: a sum weighted by their frequencies (how often I01 runs the query; the request log's counts). BI and JOB define no frequencies: their headline columns (\"equal contribution\") take the geomean of the per-query costs, so a 2x win and a 2x loss on any query count the same; the columns \"BI\" and \"IMDb / JOB\" sum every query once, and the \"additive\" columns average each query's cost over Sp-Co's (props_split_adj_condensed): the two other ways to count them. A geomean column's cost is that geomean; an additive column's is that average. No solve here: uniform layouts only.",
         "**wops** = G + 1.1 S + 0.01 N (+ PUTs + DELs for writes). **bytes** = bytes read + written. **wops_bytes4k** = wops + bytes / 4,096. The bill is not in these captures' per-query costs for BI, JOB and LinkBench, so it has no table.",
         RULES,
         f"Covered: {len(bs)} benchmark columns (" + ", ".join(f"{b}: {len(queries[b])} queries" for b in bs) + f") x {len(CELLS)} layouts x {len(MODELS)} models = {len(out)} cells.",
         f"Margins: max and geomean over ONE column per benchmark ({', '.join(hs)}), and over all {len(bs)} columns.", ""]
    if pending:
        L += [f"Pending (no capture yet): {', '.join(pending)}. The margins below cover the {len(hs)} benchmarks present.", ""]
    for m in MODELS:
        L += [f"## {m}", "", "| layout | " + " | ".join(bs) + f" | max ({len(hs)} benchmarks) | geomean ({len(hs)}) | max (all columns) | geomean (all) |", "|---|" + "---|" * (len(bs) + 4)]
        for c in CELLS:
            r4, ra = [at[(m, b, c)] for b in hs], [at[(m, b, c)] for b in bs]
            geo = lambda v: math.exp(math.fsum(math.log(x) for x in v) / len(v))
            L.append(f"| {c} | " + " | ".join(f"{at[(m, b, c)]:.2f}" for b in bs) + f" | {max(r4):.2f} | {geo(r4):.2f} | {max(ra):.2f} | {geo(ra):.2f} |")
        best = min(CELLS, key=lambda c: max(at[(m, b, c)] for b in hs))
        L += ["", f"Smallest worst case over the {len(hs)} benchmarks: {best}, {max(at[(m, b, best)] for b in hs):.2f}x.", ""]
    with open(os.path.join(a.root, "cross_benchmark_regret.md"), "w") as fh:
        fh.write("\n".join(L) + "\n")
    print(f"{len(bs)} benchmark columns, {len(out)} cells, {bad} missing, {len(pending)} pending -> {a.root}")
    return 1 if bad or not out else 0


if __name__ == "__main__":
    sys.exit(main())
