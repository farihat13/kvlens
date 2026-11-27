#!/usr/bin/env python3
"""Derive why plans are picked by operations plus bytes, not by operations alone. Per (store, LDBC read query,
reported param), the body with the fewest weighted operations (ops-only) against the body the rule picks (wops + bytes /
4 KB): the bytes each reads. The number keys give the largest gap on the measured uniform layouts.

Reads the tree's Interactive counts at the run's LDBC scale (captures/ldbc/<scale>/interactive/reported/counts: every
candidate body's counts, read_bytes; every store measured there); writes <tables>/plan-bytes.csv (one row per store,
query and param where the two bodies differ: both bodies, their wops and bytes) and <tables>/plan_bytes.tex:
  plans.sf3.grid.<query|layout|ratio>   the largest bytes ratio, ops-only / picked, on the measured uniform layouts
                                        (the ratio to one decimal; sf3 is the key's name at any scale)
  plans.sf3.grid.<opsonly|picked>.bytes that row's bytes read by each body
Weighted ops = G + 1.1 S + 0.01 N.

    uv run python paper/derive/plan_bytes.py [--from paper-results|output|<tree>] [--dataset ldbc=3] [--tables DIR]
"""
import argparse
import csv
import glob
import os

from kvevaluator import layouts as L
from snapshots import add_tree_args, scale_in, size, tree_of, write_numbers, write_table

OK = ("1", "True", "true")


def wops(r):
    return float(r["G"]) + 1.1 * float(r["S"]) + 0.01 * float(r["N"])


def differing(path):
    """rows (store, query, param, ops-only body, picked body, their wops and bytes) where the two bodies differ"""
    store = os.path.basename(path)[:-len(".csv")]
    bodies = {}
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            if r["ok"] in OK:
                bodies.setdefault((r["query"], r["param_idx"]), {})[r["body"]] = (wops(r), float(r["read_bytes"]))
    out = []
    for (query, param), b in sorted(bodies.items()):
        ops = min(b, key=lambda k: (b[k][0], k))
        picked = min(b, key=lambda k: (b[k][0] + b[k][1] / 4096, k))
        if ops != picked:
            out.append({"store": store, "query": query, "param_idx": param, "opsonly_body": ops, "picked_body": picked,
                        "opsonly_wops": round(b[ops][0], 2), "picked_wops": round(b[picked][0], 2),
                        "opsonly_bytes": b[ops][1], "picked_bytes": b[picked][1]})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_tree_args(ap)
    a = ap.parse_args()
    scale = scale_in(a, "ldbc")
    if scale is None:
        raise SystemExit("plan-bytes reads LDBC Interactive's counts: the run (--dataset) leaves ldbc out")
    captures = tree_of(a.tree).place_dir("captures", "ldbc", scale)
    files = sorted(glob.glob(str(captures / "interactive" / "reported" / "counts" / "*.csv")))
    rows = [r for f in files for r in differing(f)]
    if not rows:
        raise SystemExit(f"no store with differing picks under {captures}: {len(files)} counts files")
    ratio = lambda r: r["opsonly_bytes"] / max(r["picked_bytes"], 1)
    grid = [r for r in rows if L.is_cell(r["store"])]       # the measured uniform layouts (the grid, each chunk size)
    top = max(grid, key=ratio)
    out = {"plans.sf3.grid.query": top["query"].upper(), "plans.sf3.grid.layout": L.name_of(top["store"]),
           "plans.sf3.grid.ratio": f"{ratio(top):.1f}",
           "plans.sf3.grid.opsonly.bytes": size(top["opsonly_bytes"]), "plans.sf3.grid.picked.bytes": size(top["picked_bytes"])}
    for k, v in out.items():
        print(f"  {k} = {v}")
    write_table("plan-bytes", ["store", "query", "param_idx", "opsonly_body", "picked_body", "opsonly_wops", "picked_wops",
                               "opsonly_bytes", "picked_bytes"], rows, key=("store", "query", "param_idx"), out_dir=a.tables)
    write_numbers("plan_bytes", out, f"captures/ldbc/{captures.name}, interactive/reported/counts ({len(files)} stores); "
                  "weighted operations = G + 1.1 S + 0.01 N", out_dir=a.tables)


if __name__ == "__main__":
    main()
