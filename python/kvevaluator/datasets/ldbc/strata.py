"""Anchors picked by percentile from LDBC's CSVs: the edge-pair reader and the exact percentile pick the param
generators share (workloads microbench and writes). Deterministic: sorted (count, id), no randomness."""
import csv
import os

from kvevaluator.datasets.ldbc.loader import _root

STRATA5 = [("p10", 0.10), ("p50", 0.50), ("p90", 0.90), ("p99", 0.99), ("max", 1.00)]
STRATA3 = [("p50", 0.50), ("p90", 0.90), ("max", 1.00)]


def read_pairs(sf, fname, subdir="dynamic"):
    """(col0, col1) rows of an edge CSV (positional -- headers repeat names)."""
    with open(os.path.join(_root(sf), subdir, fname), newline="") as f:
        rd = csv.reader(f, delimiter="|")
        next(rd)
        return [(r[0], r[1]) for r in rd]


def pick_strata(counts, strata):
    """[(id, count, stratum)] at exact percentiles of the sorted (count, id)
    distribution. Sorted by (count, numeric id) so ties break deterministically;
    duplicates (tiny distributions) keep the first stratum that picked them."""
    if not counts:
        raise SystemExit("a fan-out distribution is EMPTY -- the LDBC CSVs for this "
                         "SF look truncated or mis-downloaded; re-download and retry")
    ranked = sorted(counts.items(), key=lambda kv: (kv[1], int(kv[0])))
    picked, seen = [], set()
    for name, q in strata:
        ident, cnt = ranked[round(q * (len(ranked) - 1))]
        if ident not in seen:
            seen.add(ident)
            picked.append((ident, cnt, name))
    return picked
