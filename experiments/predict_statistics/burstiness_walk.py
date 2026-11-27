#!/usr/bin/env python3
"""IC2's ranked walk (read_top_k_by_index) at SF3, Chunked/Chunked, params 1-5: predicted / measured NEXTs (the crossed
entries) at B (tier 2; at level true_terms the crossed entries are the walk's own count, 1.00x by construction), with burstiness as built and with b = 1. Owner: experiments/predict_statistics
(run-predict-statistics-results.sh, stage burstiness).   burstiness_walk.py <ratios.csv, b as built> <ratios.csv, b = 1>"""
import csv
import math
import sys


def walk(p):
    return {r["param"]: r for r in csv.DictReader(open(p)) if r["query"] == "ic2" and r["op"] == "read_top_k_by_index"}


def main():
    built, flat = walk(sys.argv[1]), walk(sys.argv[2])
    assert sorted(built) == sorted(flat) == ["1", "2", "3", "4", "5"], (sorted(built), sorted(flat))
    print("| level | b = 1: range | geomean | b as built (1.565): range | geomean |")
    print("|---|---|---|---|---|")
    for lv in ("predicted",):                     # the anchor level is not run
        cells = []
        for d in (flat, built):
            x = [float(d[p][f"N_{lv}"]) / float(d[p]["N_measured"]) for p in sorted(d)]
            cells += [f"{min(x):.2f}-{max(x):.2f}x", f"{math.exp(sum(map(math.log, x)) / len(x)):.2f}x"]
        print(f"| {lv} | " + " | ".join(cells) + " |")
    print("\nmeasured crossed entries per param: " + ", ".join(f"p{p} {int(float(built[p]['N_measured'])):,}" for p in sorted(built)))


if __name__ == "__main__":
    main()
