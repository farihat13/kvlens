#!/usr/bin/env python3
"""Why a Chunked list part's bytes grow against Split with the scale: per adjacency part I01 reads and list setting
(Split, Chunked at 64 KB, Chunked at 4 KB), the owners probed (rows into the steps that read the part), the reads
issued on it (S, G, N), its bytes, and the steps' chunk counters (chunk_probes, chunks_charged: a probe inside a chunk
the step already charged is free). Rows come from the MEASURED diagonal cell of that list setting; the plan is the one
the layout (properties Condensed, every list on that setting) picks. I01's frequencies, reported params. Opens no store.

    python3 experiments/mix_benefit/scale_check_owners_per_chunk.py --small <captures dir> --small-c4k <composed dir> \\
        --large <captures dir> --large-c64k <composed dir> --large-c4k <composed dir> --out DIR
        -> <out>/owners_per_chunk_read.csv   one row per (scale, part, list setting, chunk bytes)
           <out>/owners_per_chunk_read.md    owners per chunk read and bytes per owner, per part and scale
A measured grid (a captures/ldbc/<scale> dir) holds the diagonal cells' parts and steps, Chunked/Chunked at 4 KB among
them; the composed grids (compose_cells.py, composed/ldbc/<scale> dirs) give the picks. The small scale's 64 KB grid is its measured grid (all nine cells measured)."""
import argparse
import csv
import os
import sys
from collections import defaultdict

import scale_check_common as C
from scale_check_common import compose
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import capture_in

# (list setting, chunk bytes) -> the diagonal cell
CELLS = {("split", ""): "props_split_adj_split", ("chunked", "65536"): "props_chunked_adj_chunked",
         ("chunked", "4096"): "props_chunked_c4k_adj_chunked_c4k"}
SUMS = ("owners_probed", "S", "G", "N", "total_bytes", "chunk_probes", "chunks_charged", "owners_probed_by_steps_on_this_part_alone")


def rows_of(path):
    out = defaultdict(list)
    with open(path) as f:
        for r in capture_layout.reader(f):
            out[(r["query"], r["body"], r["param_idx"])].append(r)
    return out


def one(measured, picks_grid, setting, chunk):
    """(sf, {adjacency part: sums}, plans missing from the measured cell)"""
    data, w, units = C.load_w16(picks_grid)
    assign = C.lists_on(units, setting)
    cell = CELLS[(setting, chunk)]
    cap = capture_in(measured, "interactive")
    parts = rows_of(cap.path_of("parts", cell))
    steps = rows_of(cap.path_of("steps", cell))
    tot, missing = defaultdict(lambda: defaultdict(float)), 0
    for fam, q, often, params in w["items"]:
        if fam != "interactive":
            continue
        d = data[(fam, q)]
        qunits = {u for per in d["cost"].values() for u in per if u is not None}
        ps = compose.params_of(d, params)
        best = compose.query_pick(d, ps, assign, qunits)[0]
        for i in ps:
            pr = parts.get((q, best, str(i)))
            if pr is None:
                missing += 1
                continue
            step = {int(r["step_idx"]): r for r in steps[(q, best, str(i))]}
            parts_of_step = defaultdict(set)
            for r in pr:
                if r["part"].startswith("adj:"):
                    parts_of_step[int(r["step_idx"])].add(tuple(r["part"].split(":")[2:4]))
            seen, wgt = set(), often / len(ps)
            for r in pr:
                f = r["part"].split(":")
                if f[0] != "adj":
                    continue
                part, k = (f[2], f[3]), int(r["step_idx"])
                t = tot[part]
                for c in ("S", "G", "N"):
                    t[c] += wgt * float(r[c])
                t["total_bytes"] += wgt * sum(float(r[c] or 0) for c in C.BYTE_LANES)
                if (k, part) not in seen:                           # the step's own counters, once per (step, part)
                    seen.add((k, part))
                    alone = len(parts_of_step[k]) == 1
                    t["owners_probed"] += wgt * float(step[k]["rows_in"])
                    if alone:                                       # a step's chunk counters are the part's only then
                        t["owners_probed_by_steps_on_this_part_alone"] += wgt * float(step[k]["rows_in"])
                        t["chunk_probes"] += wgt * float(step[k]["chunk_probes"] or 0)
                        t["chunks_charged"] += wgt * float(step[k]["chunks_charged"] or 0)
    return data.sf, tot, missing


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--small", required=True, help="the small scale's measured grid (nine cells and Ch4k-Ch4k)")
    ap.add_argument("--small-c4k", required=True, help="the small scale's nine cells composed with Chunked at 4 KB")
    ap.add_argument("--large", required=True, help="the large scale's measured grid (the diagonal cells)")
    ap.add_argument("--large-c64k", required=True)
    ap.add_argument("--large-c4k", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows, missing = [], 0
    for measured, picks in ((a.small, {"": a.small, "65536": a.small, "4096": a.small_c4k}),
                            (a.large, {"": a.large_c64k, "65536": a.large_c64k, "4096": a.large_c4k})):
        for setting, chunk in CELLS:
            sf, tot, m = one(measured, picks[chunk], setting, chunk)
            missing += m
            for part in sorted(tot):
                t = tot[part]
                rows.append({"workload_id": "i01", "scale": f"sf{sf}", "part": " ".join(part), "lists": setting, "chunk_bytes": chunk,
                             **{c: round(t[c], 3) for c in SUMS}})
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "owners_per_chunk_read.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        wr.writeheader()
        wr.writerows(rows)
    by = {(r["part"], r["scale"], r["lists"], r["chunk_bytes"]): r for r in rows}
    scales = list(dict.fromkeys(r["scale"] for r in rows))
    L = ["# Owners per chunk read: I01's adjacency parts on Split and Chunked lists, by scale", "",
         "> Generated by "
         "`experiments/mix_benefit/scale_check_owners_per_chunk.py`.", "",
         "Measured diagonal cells; I01's frequencies, reported params. Owners probed = rows into the steps that read the part. "
         "On Chunked lists one SEEK reads one chunk, so owners per chunk read = owners probed / S.", "",
         "| part | scale | owners probed | Split: bytes per owner | 64 KB: chunks read | 64 KB: owners per chunk read | 64 KB: bytes per chunk read "
         "| 64 KB: bytes per owner | 4 KB: chunks read | 4 KB: owners per chunk read | 4 KB: bytes per chunk read | 4 KB: bytes per owner |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for part in sorted({k[0] for k in by}):
        for sf in scales:
            s, big, small = (by.get((part, sf, *k)) for k in CELLS)
            if not (s and big and small and s["owners_probed"]):
                continue
            cell = lambda r: (f"{r['S'] / 1e6:,.2f} M | {r['owners_probed'] / max(r['S'], 1):,.2f} | {r['total_bytes'] / max(r['S'], 1):,.0f} | "
                              f"{r['total_bytes'] / r['owners_probed']:,.0f}")
            L.append(f"| {part} | {sf.upper()} | {s['owners_probed'] / 1e6:,.1f} M | {s['total_bytes'] / s['owners_probed']:,.0f} | {cell(big)} | {cell(small)} |")
    with open(os.path.join(a.out, "owners_per_chunk_read.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L[6:]))
    print(f"[owners_per_chunk] {len(rows)} rows, {len({r['part'] for r in rows})} parts; {missing} (query, param) whose plan the measured cell did not run")
    return 1 if missing or not rows else 0


if __name__ == "__main__":
    sys.exit(main())
