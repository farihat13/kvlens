#!/usr/bin/env python3
"""I01 (LDBC Interactive's reads and updates) per layout, predicted / measured: writes, reads and the whole workload,
each item at its I01 frequency, at level predicted (tier 2; the anchor level is not run). Reads kvcost
cost-layouts' uniform.csv of each source. Owner: experiments/predict_statistics (run-predict-statistics-results.sh,
stage writes).

    w16_over_measured.py <measured uniform.csv> <predicted uniform.csv> <the write family in their family column>
"""
import collections
import csv
import sys


def totals(path, write_family):
    """cell -> {read_wb4k, write_wb4k, read_wops, write_wops}; and cell -> short layout name (the cost table: layout
    is the cell, name its short name, every source alike)"""
    t, short = collections.defaultdict(lambda: collections.defaultdict(float)), {}
    for r in csv.DictReader(open(path)):
        f = float(r["frequency"])
        fam = "write" if r["family"] == write_family else "read"
        cell = r["layout"]
        t[cell][fam + "_wb4k"] += f * float(r["wops_bytes4k"])
        t[cell][fam + "_wops"] += f * float(r["wops"])
        short[cell] = r["name"]
    return t, short


def main():
    M, _ = totals(sys.argv[1], sys.argv[3])
    P = {}
    P["predicted"], short = totals(sys.argv[2], sys.argv[3])
    if sorted(M) != sorted(short) or not M:
        raise SystemExit(f"w16_over_measured: layouts differ: measured {sorted(M)}, predicted {sorted(short)}")
    print("| layout | writes, wops_bytes4k: predicted | writes, wops: predicted | reads, wops_bytes4k: predicted | I01, wops_bytes4k: predicted "
          "| write share (measured) |")
    print("|---|---|---|---|---|---|")
    for L in sorted(M, key=lambda c: M[c]["read_wb4k"] + M[c]["write_wb4k"]):
        m, row = M[L], []
        for k in ("write_wb4k", "write_wops", "read_wb4k"):
            row.append(f"{P['predicted'][L][k] / m[k]:.3f}")
        tot = m["read_wb4k"] + m["write_wb4k"]
        row.append(f"{(P['predicted'][L]['read_wb4k'] + P['predicted'][L]['write_wb4k']) / tot:.3f}")
        row.append(f"{m['write_wb4k'] / tot:.3f}")
        print(f"| {short[L]} | " + " | ".join(row) + " |")
    print(f"\nw16_over_measured: {len(M)} layouts")


if __name__ == "__main__":
    main()
