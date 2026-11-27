#!/usr/bin/env python3
"""The nine uniform layouts on each benchmark, under each way of counting its queries: once each, the benchmark's
frequencies, and (BI, JOB: they define no frequencies) equal contribution, the geomean over queries
(python/kvcost/layout_cost/query_weighting.py). Analysis only: it reads way 6's per-query costs (per_query_disagreement.py -> costs.csv).
LinkBench has both: its log's frequencies, and once each = one request of each of its request types, at the type's mean
cost per request in the log (costs.csv's LinkBench rows are that mean; its weight column is the log's count).

Per (benchmark, frequencies, layout): the summed wops, bytes and wops_bytes4k, each one's ratio to the benchmark's best
uniform layout (under that column's way of adding up), and the equal-share number (each query's cost over its own cost on that best layout, geomean across
queries; below 1.00 = cheaper than the best-by-sum layout on the typical query).

    python3 experiments/mix_benefit/cross_benchmark_frequencies.py [--root <out>/summary/ldbc/sf3/mix-benefit] [--regret <csv>]
        -> <working root>/cross-benchmark-frequencies/cross_benchmark_frequencies.csv
"""
import argparse
import csv
import math
import os
from collections import defaultdict

import figure_common as F
from kvcost.layout_cost import query_weighting as QW  # (sum / geomean: the one definition)

MODELS = ("wops", "bytes", "wops_bytes4k")
# (benchmark, frequencies, family of costs.csv, use the file's weights?, the column of cross_benchmark_regret.csv it must
# equal, how the queries add up). BI and JOB define no frequencies: "equal contribution" is their headline, "once each"
# the robustness check.
COLUMNS = [
    ("LDBC Interactive", "LDBC frequencies", "LDBC, all of I01", True, "I01 LDBC mix", "sum"),
    ("LDBC Interactive", "once each", "LDBC, all of I01", False, None, "sum"),
    ("LDBC BI", "equal contribution", "BI", False, "BI, equal contribution", "geomean"),
    ("LDBC BI", "once each", "BI", True, "BI", "sum"),
    ("IMDb / JOB", "equal contribution", "IMDb / JOB", False, "IMDb / JOB, equal contribution", "geomean"),
    ("IMDb / JOB", "once each", "IMDb / JOB", True, "IMDb / JOB", "sum"),
    ("LinkBench", "its log's frequencies", "LinkBench", True, "LinkBench L01 (the log's mix)", "sum"),
    # one request of each request type (a link operation counts per link type), at the type's mean cost per request in the log
    ("LinkBench", "once each", "LinkBench", False, None, "sum"),
]
OUT_FIELDS = (["benchmark", "frequencies", "queries", "cell"] + list(MODELS)
              + [f"{m}_{k}" for m in MODELS for k in ("best_cell", "ratio", "equal_share")])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=F.ROOT, help="the dir holding per-query-disagreement/ and joint-mix/")
    ap.add_argument("--out", default=os.path.join(F.ROOT, "cross-benchmark-frequencies"))
    ap.add_argument("--regret", default="", help="the cross_benchmark_regret.csv to check against (default <root>/joint-mix/)")
    ap.add_argument("--pending", action="append", default=[], metavar="FAMILY",
                    help="a costs.csv family not captured yet (e.g. 'IMDb / JOB'): its columns are left out, not a failure; "
                         "it must then have no rows")
    a = ap.parse_args()
    per_query = defaultdict(dict)                       # family -> query -> cell -> row
    with open(os.path.join(a.root, "per-query-disagreement", "costs.csv")) as f:
        for r in csv.DictReader(f):
            per_query[r["family"]].setdefault(r["query"], {})[r["cell"]] = r
    out, pending = [], []
    for bench, freq, family, file_weights, _, form in COLUMNS:
        queries = per_query[family]
        if family in a.pending:
            if queries:
                raise SystemExit(f"{family}: --pending names it, but costs.csv has {len(queries)} of its queries")
            pending.append((bench, freq))
            continue
        if not queries or any(set(q) != set(F.CELLS) for q in queries.values()):
            raise SystemExit(f"{family}: {len(queries)} queries, not all on the nine layouts")
        if (file_weights and freq == "once each" or form != "sum") and any(float(q[c]["weight"]) != 1.0 for q in queries.values() for c in q):
            raise SystemExit(f"{family}: called once each, but costs.csv has weights other than 1")
        rows = {c: {"benchmark": bench, "frequencies": freq, "queries": len(queries), "cell": c} for c in F.CELLS}
        for m in MODELS:
            costs = {q: {c: float(queries[q][c][m]) for c in F.CELLS} for q in queries}
            weights = {q: float(queries[q][F.CELLS[0]]["weight"]) for q in queries} if file_weights else None
            total = QW.aggregate(costs, form, weight=weights)
            best = QW.best(total)
            for c in F.CELLS:
                logs = sorted(math.log(float(queries[q][c][m]) / float(queries[q][best][m])) for q in queries)
                rows[c].update({m: round(total[c], 3), f"{m}_best_cell": best, f"{m}_ratio": round(total[c] / total[best], 4),
                                f"{m}_equal_share": round(math.exp(sum(logs) / len(logs)), 4)})
        out += [rows[c] for c in F.CELLS]
    # the columns the search side already tabled must come out equal (cross_benchmark_regret.csv, 4 decimals)
    regret = a.regret or os.path.join(a.root, "joint-mix", "cross_benchmark_regret.csv")
    checked = differ = missing = 0
    if os.path.exists(regret):
        with open(regret) as f:
            want = {(r["benchmark"], r["model"], r["cell"]): float(r["ratio"]) for r in csv.DictReader(f)}
        mine = {(r["benchmark"], r["frequencies"], r["cell"]): r for r in out}
        for bench, freq, _, _, theirs, _ in COLUMNS:
            if (bench, freq) in pending:
                continue
            for m in MODELS:
                for c in F.CELLS if theirs else ():
                    if (theirs, m, c) not in want:
                        missing += 1
                        continue
                    checked += 1
                    differ += abs(mine[(bench, freq, c)][f"{m}_ratio"] - want[(theirs, m, c)]) > 1e-4
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "cross_benchmark_frequencies.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        w.writeheader()
        w.writerows(out)
    print(f"[cross_benchmark_frequencies] {len(COLUMNS) - len(pending)} columns x {len(F.CELLS)} layouts = {len(out)} rows -> {path}"
          + (f"; pending (no capture yet): {', '.join(f'{b}, {f}' for b, f in pending)}" if pending else ""))
    print(f"   against {regret}: {checked} ratios compared, {differ} differ, {missing} not in that table")
    for bench, freq, *_ in COLUMNS:
        if (bench, freq) in pending:
            continue
        rs = [r for r in out if (r["benchmark"], r["frequencies"]) == (bench, freq)]
        for m in MODELS:
            eq_best = min(rs, key=lambda r: (r[f"{m}_equal_share"], r["cell"]))
            print(f"   {bench}, {freq}, {m}: best {F.cell_label(rs[0][f'{m}_best_cell'])}, worst "
                  f"{F.cell_label(max(rs, key=lambda r: r[f'{m}_ratio'])['cell'])} {max(r[f'{m}_ratio'] for r in rs):.2f}x; equal share: "
                  f"best {F.cell_label(eq_best['cell'])} {eq_best[f'{m}_equal_share']:.2f}, worst "
                  f"{F.cell_label(max(rs, key=lambda r: r[f'{m}_equal_share'])['cell'])} {max(r[f'{m}_equal_share'] for r in rs):.2f}")
    return 1 if differ or missing or (os.path.exists(regret) and not checked) else 0


if __name__ == "__main__":
    raise SystemExit(main())
